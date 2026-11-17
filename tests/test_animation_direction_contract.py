import copy
import unittest
from unittest.mock import patch
from imagegen.animation import compile_story_aligned_veo_prompt, build_animation_plan
from imagegen.animation_direction import CONTRACT, compile_image_motion_prompt, refresh_queued_direction
from imagegen.domain import DomainError
from tests import test_veo_animation as fixtures


class MotionPromptContractTests(unittest.TestCase):
    def data(self, **values):
        return {'beat': 'A named character waits while remembering a distant event.',
                'animation': {'prompt_contract': CONTRACT, 'motion_scope': 'story_action'}, **values}

    def test_narration_is_preserved_in_document_but_not_sent_as_motion(self):
        data = self.data(beat=('Биографический контекст — ' + 'важное обстоятельство ' * 70).rstrip())
        before = copy.deepcopy(data)
        prompt = compile_story_aligned_veo_prompt(data, duration_seconds=6)
        self.assertNotIn(data['beat'], prompt)
        self.assertNotIn('Scene context', prompt)
        alternate=self.data(beat='A different biography and events from the next chapter.',
                            source_quote='An earlier quotation',continuity_before='Previous scene',continuity_after='Future scene')
        self.assertEqual(prompt,compile_story_aligned_veo_prompt(alternate,duration_seconds=6))
        self.assertNotIn('Subject starts', prompt)
        self.assertNotIn('0–2', prompt)
        self.assertNotIn('2–5', prompt)
        self.assertIn('physical action already visible', prompt)
        self.assertEqual(before, data)

    def test_authored_visual_motion_is_separate_from_cross_scene_continuity(self):
        data=self.data(continuity_before='The hand already rests on the cup.',continuity_after='The cup stays on the table.')
        data['animation']['visual_motion']='The hand releases the cup and moves back.'
        prompt=compile_story_aligned_veo_prompt(data)
        self.assertIn(data['animation']['visual_motion'],prompt)
        self.assertNotIn(data['continuity_before'],prompt)
        self.assertNotIn(data['continuity_after'],prompt)
        self.assertNotIn('stationary throughout',prompt)

    def test_medical_and_animal_story_facts_are_not_removed_by_word_blacklists(self):
        for beat in ('The patient coughs and has a visible rash on both arms.',
                     'The dragon lands near the burning castle.',
                     'Джон легко побеждает многих новых товарищей в учебных боях.'):
            data=self.data(beat=beat);before=copy.deepcopy(data)
            prompt=compile_story_aligned_veo_prompt(data)
            self.assertEqual(data,before)
            self.assertNotIn(beat,prompt)
            self.assertIn('visible physical conditions',prompt)
            self.assertIn('all animals',prompt)
            self.assertIn('species',prompt)
            data['animation']['visual_motion']=beat
            self.assertIn(beat,compile_story_aligned_veo_prompt(data))

    def test_camera_only_is_explicit_and_unchanged(self):
        data=self.data();data['animation']['motion_scope']='camera_only'
        prompt=compile_story_aligned_veo_prompt(data,duration_seconds=4)
        self.assertIn('keep all people and objects stationary',prompt)
        self.assertIn('0–4 s:',prompt)
        self.assertNotIn(data['beat'],prompt)

    def test_old_unversioned_prompt_remains_compatible(self):
        prompt=compile_story_aligned_veo_prompt({'beat':'A crowd crosses a courtyard.'},duration_seconds=4)
        self.assertIn('2–4 s:',prompt)
        self.assertIn('A crowd crosses a courtyard.',prompt)

    def test_new_plan_preserves_authored_motion_scope(self):
        scene={'id':'s','asset_id':'image','data':self.data()}
        scene['data']['animation'].update(mode='manual',selected_engine='omni_flash',motion_scope='camera_only',visual_motion='Custom direction')
        plan=build_animation_plan({},[scene])['scenes'][0]['animation']
        self.assertEqual(plan['prompt_contract'],CONTRACT)
        self.assertEqual(plan['motion_scope'],'camera_only')
        self.assertEqual(plan['visual_motion'],'Custom direction')


class MotionPromptQueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.TestVeoAnimationPlan();self.fixture.setUp()
        self.service=self.fixture.service
        self.pid=self.fixture._create_sample_project(2)
        self.fixture._upload_scene_images(self.pid)
        self.ids=[s['id'] for s in self.service.detail(self.pid)['scenes']]

    def tearDown(self):
        self.fixture.tearDown()

    def ready(self):
        return patch.object(self.service,'project_prompts',return_value={'scenes':[{'scene_id':sid,'ready_for_flow':True} for sid in self.ids]})

    def assert_bound(self,sid):
        scene=self.service.scene(sid);anim=scene['data']['animation']
        self.assertEqual(anim['prompt_contract'],CONTRACT)
        with self.ready():
            job=next(j for j in self.service.animation_jobs(self.pid)['jobs'] if j['scene_id']==sid)
        stored=self.service.db.one('SELECT * FROM veo_jobs WHERE scene_id=? AND idempotency_key=?',(sid,anim['idempotency_key']))
        self.assertEqual(stored['prompt'],job['prompt'])
        self.assertEqual(stored['source_image_asset_id'],job['source_image_asset_id'])
        self.assertNotIn(scene['data']['beat'],job['prompt'])
        self.assertNotIn('2–5 s:',job['prompt'])
        return job

    def test_both_cloud_engines_queue_the_same_new_contract(self):
        for sid,engine in zip(self.ids,('omni_flash','veo_lite')):
            self.service.select_scene_animation_engine(sid,engine)
        with self.ready():
            self.service.start_animation_generation(self.pid,scene_ids=self.ids)
        for sid in self.ids:self.assert_bound(sid)

    def test_single_scene_queue_paths_use_new_contract(self):
        self.service.select_scene_animation_engine(self.ids[0],'omni_flash')
        self.service.queue_single_scene_omni(self.ids[0])
        self.assert_bound(self.ids[0])
        self.service.select_scene_animation_engine(self.ids[1],'veo_lite')
        self.service.queue_single_scene_veo(self.ids[1])
        self.assert_bound(self.ids[1])

    def test_request_prompt_is_immutable_across_compiler_updates(self):
        sid=self.ids[0]
        self.service.select_scene_animation_engine(sid,'omni_flash')
        self.service.queue_single_scene_omni(sid)
        before=self.assert_bound(sid)
        scene_before=self.service.scene(sid)
        with patch('imagegen.animation.compile_image_motion_prompt',return_value='A future compiler output'):
            with self.ready():
                after=self.service.animation_jobs(self.pid)['jobs'][0]
        self.assertEqual(before['prompt'],after['prompt'])
        self.assertEqual(before['idempotency_key'],after['idempotency_key'])
        self.assertEqual(scene_before,self.service.scene(sid))

    def test_legacy_queued_request_is_not_rewritten_or_recreated(self):
        sid=self.ids[0]
        self.service.select_scene_animation_engine(sid,'omni_flash')
        self.service.queue_single_scene_omni(sid)
        scene=self.service.scene(sid);data=copy.deepcopy(scene['data']);data['animation'].pop('prompt_contract')
        key=data['animation']['idempotency_key'];legacy_prompt='Original legacy request bytes: keep this source and action.'
        with self.service.db.transaction() as conn:
            import json
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (json.dumps(data),sid,scene['current_revision']))
            conn.execute('UPDATE veo_jobs SET prompt=? WHERE scene_id=? AND idempotency_key=?',(legacy_prompt,sid,key))
        scene_before=self.service.scene(sid)
        rows=self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,))
        with self.ready():
            self.service.start_animation_generation(self.pid,scene_ids=[sid])
            after=self.service.animation_jobs(self.pid)['jobs'][0]
        self.assertEqual(after['prompt'],legacy_prompt)
        self.assertEqual(after['idempotency_key'],key)
        self.assertEqual(scene_before,self.service.scene(sid))
        self.assertEqual(rows,self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,)))

    def legacy(self, engine='omni_flash'):
        sid=self.ids[0]
        self.service.select_scene_animation_engine(sid,engine)
        (self.service.queue_single_scene_omni if engine=='omni_flash' else self.service.queue_single_scene_veo)(sid)
        scene=self.service.scene(sid);data=copy.deepcopy(scene['data']);data['animation'].pop('prompt_contract')
        from imagegen.db import dump
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data),sid,scene['current_revision']))
            conn.execute('UPDATE veo_jobs SET prompt=? WHERE scene_id=?',('Legacy request with repeated narration',sid))
        with self.ready():job=self.service.animation_jobs(self.pid)['jobs'][0]
        binding={k:job[k] for k in ('idempotency_key','source_image_asset_id','source_image_sha256')}
        binding.update(not_sent=True,expected_scene_revision=job['scene_revision'])
        return sid,binding

    def test_unsent_legacy_upgrade_is_bound_atomic_and_repeatable(self):
        for engine in ('omni_flash','veo_lite'):
            with self.subTest(engine=engine):
                # Use a fresh fixture per engine to avoid selecting a queued request.
                if engine=='veo_lite':
                    self.tearDown();self.setUp()
                sid,binding=self.legacy(engine)
                before=self.service.scene(sid)
                result=refresh_queued_direction(self.service,self.pid,sid,binding)
                self.assertTrue(result['upgraded'])
                self.assertEqual(result['scene_revision'],before['current_revision']+1)
                self.assertNotEqual(result['idempotency_key'],binding['idempotency_key'])
                after=self.assert_bound(sid)
                self.assertEqual(after['source_image_asset_id'],binding['source_image_asset_id'])
                self.assertEqual(after['prompt'],result['prompt'])
                self.assertEqual(self.service.db.one('SELECT status FROM veo_jobs WHERE idempotency_key=?',
                                                    (binding['idempotency_key'],))['status'],'cancelled')
                with self.assertRaises(DomainError) as err:refresh_queued_direction(self.service,self.pid,sid,binding)
                self.assertEqual(err.exception.status,409)
                binding.update(idempotency_key=result['idempotency_key'],expected_scene_revision=result['scene_revision'])
                again=refresh_queued_direction(self.service,self.pid,sid,binding)
                self.assertFalse(again['upgraded'])
                self.assertEqual(again['idempotency_key'],result['idempotency_key'])

    def test_unconfirmed_stale_or_foreign_bindings_do_not_write(self):
        sid,binding=self.legacy()
        before=self.service.scene(sid);rows=self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,))
        for change in ({'not_sent':False},{'expected_scene_revision':0},{'idempotency_key':'other'},
                       {'source_image_sha256':'other'},{'source_image_asset_id':'other'}):
            with self.subTest(change=change),self.assertRaises(DomainError) as err:
                refresh_queued_direction(self.service,self.pid,sid,{**binding,**change})
            self.assertEqual(err.exception.status,409)
            self.assertEqual(before,self.service.scene(sid))
            self.assertEqual(rows,self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,)))
        foreign=self.fixture._create_sample_project(1)
        with self.assertRaises(DomainError) as err:refresh_queued_direction(self.service,foreign,sid,binding)
        self.assertEqual(err.exception.status,404)

    def test_generating_request_is_not_upgraded(self):
        sid,binding=self.legacy()
        self.service.set_scene_animation_status(sid,'generating_omni')
        binding['expected_scene_revision']=self.service.scene(sid)['current_revision']
        before=self.service.scene(sid);rows=self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,))
        with self.assertRaises(DomainError) as err:refresh_queued_direction(self.service,self.pid,sid,binding)
        self.assertEqual(err.exception.status,409)
        self.assertEqual(before,self.service.scene(sid))
        self.assertEqual(rows,self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,)))

    def test_retry_creates_complete_source_and_prompt_binding(self):
        sid,_=self.legacy('veo_lite')
        self.service.retry_scene_animation(sid)
        job=self.assert_bound(sid)
        self.assertEqual(job['engine'],'veo_lite')

    def test_prepare_endpoint_uses_same_contract(self):
        sid,binding=self.legacy()
        result=self.fixture.app.dispatch('POST',f'/api/projects/{self.pid}/animation/prepare-direction',
                                         {'scene_id':sid,**binding})
        self.assertTrue(result['upgraded'])
        self.assertEqual(result['prompt'],self.assert_bound(sid)['prompt'])

    def test_v2_queued_prompt_upgrades_without_changing_narration_or_source(self):
        sid,binding=self.legacy()
        from imagegen.db import dump
        scene=self.service.scene(sid);data=copy.deepcopy(scene['data'])
        data['animation']['prompt_contract']='image-motion/2'
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data),sid,scene['current_revision']))
        result=refresh_queued_direction(self.service,self.pid,sid,binding)
        after=self.service.scene(sid)
        self.assertEqual({k:v for k,v in data.items() if k!='animation'},
                         {k:v for k,v in after['data'].items() if k!='animation'})
        self.assertEqual(scene['active_generation_id'],after['active_generation_id'])
        self.assertEqual(result['prompt_contract'],CONTRACT)
        self.assertNotIn(data['beat'],result['prompt'])

    def test_unknown_response_updates_exact_attempt_without_invalidating_late_video(self):
        sid,binding=self.legacy()
        source=self.service.db.one('SELECT * FROM veo_jobs WHERE scene_id=?',(sid,))
        from imagegen.db import uid
        other={**source,'id':uid(),'idempotency_key':'other-pending-attempt'}
        with self.service.db.transaction() as conn:
            columns=list(other)
            conn.execute('INSERT INTO veo_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                         tuple(other[c] for c in columns))
        revision=self.service.scene(sid)['current_revision']
        result=self.service.set_scene_animation_status(sid,'generating_omni',error='Unknown SDK response; do not repeat',
            **{k:v for k,v in binding.items() if k!='not_sent'})
        self.assertEqual(result['status'],'generating_omni')
        self.assertEqual(self.service.scene(sid)['current_revision'],revision)
        self.assertEqual(self.service.db.one('SELECT * FROM veo_jobs WHERE id=?',(other['id'],)),other)
        updated=self.service.db.one('SELECT * FROM veo_jobs WHERE id=?',(source['id'],))
        self.assertEqual(updated['status'],'generating_omni')
        self.assertIn('Unknown SDK response',updated['error'])


class DirectionCapabilityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_animation_resume import AnimationResumeTests
        self.fixture=AnimationResumeTests();self.fixture.setUp()

    def tearDown(self):self.fixture.tearDown()

    def test_old_bridge_cannot_send_legacy_prompts_but_new_bridge_can(self):
        f=self.fixture;sid=f.ids[0];f.queue(sid)
        from imagegen.db import dump
        scene=f.service.scene(sid);data=copy.deepcopy(scene['data']);data['animation'].pop('prompt_contract')
        with f.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data),sid,scene['current_revision']))
        f.fixture.heartbeat(animations_only=True)
        with self.assertRaises(DomainError) as err:f.command()
        self.assertEqual(err.exception.status,409)
        self.assertIsNone(f.app.flow_bridge_v1['command'])
        f.fixture.heartbeat(animations_only=True,animation_direction_refresh=True,animation_direction_contract=CONTRACT)
        self.assertTrue(f.command()['ok'])

    def test_legacy_requests_outside_selected_scope_do_not_block_new_contract(self):
        f=self.fixture;f.queue(f.ids[0]);f.queue(f.ids[1])
        from imagegen.db import dump
        scene=f.service.scene(f.ids[1]);data=copy.deepcopy(scene['data']);data['animation'].pop('prompt_contract')
        with f.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data),scene['id'],scene['current_revision']))
        f.fixture.heartbeat(animations_only=True)
        self.assertTrue(f.command()['ok'])


if __name__=='__main__':unittest.main()

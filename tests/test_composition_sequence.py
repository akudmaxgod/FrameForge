import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from tests.test_source_review_resume import scene,proposal,review
from imagegen import frame_composition as camera
from imagegen import frame_sequence_review as sequence
from imagegen import narrative_frame_focus as focus
from imagegen.story_visual_plan import compile_scenes,parent_document,validate_scenes,validate_plan
from imagegen.source_review_status import signature
from imagegen.domain import DomainError,compile_prompt,compile_flow_prompt


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.quotes=['Корзина стоит.','Корзина лежит.','Корзина висит.'];self.story=' '.join(self.quotes)
        self.scenes=[scene(self.story,q) for q in self.quotes];self.calls=[]
        def request(instructions,items,**kw):
            self.calls.append((items[0]['key'],kw.get('review',False),kw.get('repair')))
            return {'reviews':[review(i) for i in items]} if kw.get('review') else {'frames':[proposal(i) for i in items]}
        request.identity={'test':'composition-sequence'};self.request=request
    def tearDown(self):self.temp.cleanup()
    def compile(self,**kw):
        with patch('imagegen.story_visual_plan.request_adapter',return_value=self.request):
            return compile_scenes(self.scenes,self.story,None,self.folder,**kw)
    def clean(self,payload,repair=None):return {'checked_pairs':[p['id'] for p in payload['pairs']],'findings':[]}
    def conflict(self,payload,category='state',pair_index=0):
        result=self.clean(payload);pair=payload['pairs'][pair_index]
        ids=[s['id'] for s in payload['sources'] if any(s['start']==f['narration']['start'] and s['end']==f['narration']['end'] for f in payload['frames'] if f['key'] in (pair['previous'],pair['target']))]
        result['findings']=[{'pair_id':pair['id'],'category':category,'reason':'Переход состояния не соответствует текущей цитате.',
                             'correction':'Исправьте только состояние второго кадра по текущему источнику.','source_ids':ids}]
        return result


class CompositionTests(Fixture):
    def test_new_plan_has_separate_facts_and_preserved_camera_defaults(self):
        frames,plan=self.compile();self.assertEqual(len(self.calls),6)
        self.assertEqual(camera.validate(frames[0])['shot_size'],'preserve')
        self.assertEqual(frames[0]['frame_facts']['facts']['subject'],'Корзина')
        self.assertNotIn('shot_size',frames[0]['frame_facts']['facts']);self.assertEqual(camera.instruction(frames[0]),'')
        validate_scenes(self.story,plan,frames)
    def test_camera_change_preserves_every_fact_and_review(self):
        frames,plan=self.compile();data=frames[0];before=copy.deepcopy(data)
        data['frame_composition']=camera.choice(data['frame_facts'],{'shot_size':'close','camera_angle':'low','placement':'thirds'})
        self.assertEqual(data['frame_facts'],before['frame_facts']);self.assertEqual(data['visual_content'],before['visual_content'])
        self.assertIn('every selected participant',camera.instruction(data));validate_scenes(self.story,plan,frames)
    def test_arbitrary_camera_text_and_factual_fields_are_rejected(self):
        frames,plan=self.compile()
        for values in ({'shot_size':'add a soldier','camera_angle':'low','placement':'center'},
                       {'shot_size':'wide','camera_angle':'low','placement':'center','participants':[]}):
            with self.assertRaises(DomainError):camera.choice(frames[0]['frame_facts'],values)
    def test_old_camera_cannot_survive_changed_facts(self):
        frames,plan=self.compile();data=frames[0];data['visual_content']['state']='Подмена'
        data['visual_content_hash']=focus.fingerprint(data['visual_content'])
        with self.assertRaises(DomainError):camera.validate(data)
    def test_fact_packet_tampering_is_rejected(self):
        frames,plan=self.compile();frames[0]['frame_facts']['facts']['participants']=[{'name':'Другой'}]
        with self.assertRaises(DomainError):camera.instruction(frames[0])
    def test_camera_hash_and_extra_fields_are_rejected(self):
        for mode in ('hash','extra'):
            frames,plan=self.compile();packet=frames[0]['frame_composition']
            if mode=='hash':packet['facts_hash']='old'
            else:packet['action']='Другой актор'
            with self.assertRaises(DomainError):camera.validate(frames[0])
    def test_compiler_and_flow_receive_same_camera_instruction(self):
        frames,plan=self.compile();data=frames[0]
        data['frame_composition']=camera.choice(data['frame_facts'],{'shot_size':'wide','camera_angle':'high','placement':'center'})
        project={'story':self.story,'mode':'illustration','style':'realism','world':''}
        text,_=compile_prompt(project,data,[],allow_unlocked=True);flow=compile_flow_prompt(project,data,[])
        for prompt in (text,flow):self.assertIn('CAMERA CHOICE: wide shot, elevated oblique camera, centered framing',prompt)
    def test_legacy_scene_without_new_packets_keeps_prompt(self):
        data={'beat':'A basket.','visible_people':0}
        self.assertEqual(camera.instruction(data),'');self.assertIsNone(camera.validate(data))
    def test_changed_focus_resets_camera_to_preserve(self):
        frames,plan=self.compile();data=frames[0]
        data['frame_composition']=camera.choice(data['frame_facts'],{'shot_size':'wide','camera_angle':'low','placement':'center'})
        data['visual_content']['state']='Новая проверенная фаза';data['visual_content_hash']=focus.fingerprint(data['visual_content'])
        changed=camera.attach(data);self.assertEqual(changed['frame_composition']['shot_size'],'preserve')
    def test_unsupported_source_review_cannot_create_composition(self):
        frames,plan=self.compile();data=frames[0];data['visual_content']['source_review']['checks'][0]['verdict']='issue'
        data['visual_content_hash']=focus.fingerprint(data['visual_content'])
        with self.assertRaises(DomainError):camera.facts(data)


class SequenceTests(Fixture):
    def test_windows_overlap_and_cover_every_adjacent_pair_once(self):
        self.quotes=['Корзина номер '+str(n)+' стоит.' for n in range(10)];self.story=' '.join(self.quotes);self.scenes=[scene(self.story,q) for q in self.quotes]
        frames,plan=self.compile();groups=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])
        self.assertEqual([len(g['frames']) for g in groups],[4,4,4])
        self.assertEqual(groups[0]['frames'][-1]['key'],groups[1]['frames'][0]['key'])
        self.assertEqual([p['id'] for g in groups for p in g['pairs']],['pair_'+str(n) for n in range(1,10)])
    def test_clean_certificate_is_bound_to_frames_and_source(self):
        frames,plan=self.compile(sequence_request=self.clean);certificate=plan['focus']['sequence_review']
        self.assertEqual(certificate['status'],'complete');self.assertEqual(len(certificate['checked_pairs']),2);validate_plan(self.story,plan)
    def test_source_and_focus_changes_invalidate_sequence_certificate(self):
        frames,plan=self.compile(sequence_request=self.clean);changed=copy.deepcopy(plan)
        changed['focus']['frames'][0]['focus']['state']='Подмена';changed['focus']['content_hash']=focus.fingerprint({k:v for k,v in changed['focus'].items() if k!='content_hash'})
        with self.assertRaises(ValueError):validate_plan(self.story,changed)
        with self.assertRaises(ValueError):sequence.validate_certificate('Другой сценарий.',plan['focus'])
    def test_valid_era_changes_holds_questions_can_be_accepted(self):
        frames,plan=self.compile();payload=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])[0]
        for row in payload['frames']:row['approved_focus']['source_status']='question'
        self.assertEqual(sequence.validate_response(self.clean(payload),payload),[])
        self.assertIn('flashback',sequence.RULES);self.assertIn('static hold',sequence.RULES)
    def test_semantic_findings_reference_exact_pair_both_sources_and_minimal_correction(self):
        frames,plan=self.compile();payload=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])[0]
        for category in sequence.CATEGORIES[:-1]:
            problem=sequence.validate_response(self.conflict(payload,category),payload)[0]
            self.assertTrue(problem['required']);self.assertEqual(problem['related_frames'],[f['key'] for f in payload['frames'][:2]])
            self.assertEqual(problem['source_scope'],payload['frames'][1]['narration']);self.assertTrue(problem['minimal_correction'])
    def test_camera_repetition_is_advisory_and_triggers_no_repair(self):
        calls=[]
        def reviewer(payload,repair=None):calls.append(payload);return self.conflict(payload,'camera_repetition')
        frames,plan=self.compile(sequence_request=reviewer)
        self.assertEqual(len(self.calls),6);self.assertEqual(len(calls),1)
        self.assertEqual(plan['focus']['sequence_review']['status'],'complete');self.assertFalse(plan['focus']['sequence_review']['issues'][0]['required'])
    def test_missing_pair_and_cross_pair_future_citations_are_rejected(self):
        frames,plan=self.compile();payload=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])[0]
        value=self.clean(payload);value['checked_pairs'].pop()
        with self.assertRaises(ValueError):sequence.validate_response(value,payload)
        value=self.conflict(payload);value['findings'][0]['source_ids'].append(payload['sources'][-1]['id'])
        with self.assertRaisesRegex(ValueError,'будущее'):sequence.validate_response(value,payload)
    def test_finding_must_cite_both_current_excerpts(self):
        frames,plan=self.compile();payload=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])[0]
        value=self.conflict(payload);value['findings'][0]['source_ids']=value['findings'][0]['source_ids'][:1]
        with self.assertRaises(ValueError):sequence.validate_response(value,payload)
    def test_only_failed_target_is_repaired_then_only_changed_window_is_reviewed(self):
        old=self.request;reviews=[]
        def writer(instructions,items,**kw):
            result=old(instructions,items,**kw)
            if not kw.get('review') and items[0]['key'].startswith('2:'):
                result['frames'][0]['state']='Исправлено' if kw.get('repair') else 'Неверно'
            return result
        writer.identity=old.identity;self.request=writer
        def reviewer(payload,repair=None):
            reviews.append(payload)
            return self.conflict(payload) if payload['frames'][1]['approved_focus']['state']=='Неверно' else self.clean(payload)
        before=copy.deepcopy(self.scenes);frames,plan=self.compile(sequence_request=reviewer)
        self.assertEqual(len(self.calls),8);self.assertEqual(len(reviews),2)
        self.assertEqual(sum(key.startswith('1:') and not critic for key,critic,_ in self.calls),1)
        self.assertEqual(frames[1]['visual_state'],'Исправлено');self.assertEqual(self.scenes,before)
        self.assertEqual(frames[1]['source_quote'],self.quotes[1]);validate_scenes(self.story,plan,frames)
        self.calls.clear();reviews.clear();self.compile(sequence_request=reviewer)
        self.assertEqual(self.calls,[]);self.assertEqual(reviews,[])
    def test_unresolved_conflict_has_bounded_repairs_and_never_publishes_partial_plan(self):
        before=copy.deepcopy(self.scenes)
        with self.assertRaisesRegex(ValueError,'Связность требует'):
            self.compile(sequence_request=lambda payload,repair=None:self.conflict(payload))
        self.assertEqual(len(self.calls),8);self.assertEqual(self.scenes,before)
        report=json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'needs_attention');self.assertEqual(report['frames'][1]['status'],'failed')
        self.assertEqual(report['sequence_review']['issues'][0]['related_frames'],[r['key'] for r in report['frames'][:2]])
    def test_explicit_resume_can_make_fresh_local_attempt_after_unchanged_repair(self):
        old=self.request
        def writer(instructions,items,**kw):
            value=old(instructions,items,**kw)
            if not kw.get('review') and items[0]['key'].startswith('2:'):
                value['frames'][0]['state']='Исправлено' if (kw.get('repair') or {}).get('attempt',0)>=2 else 'Неверно'
            return value
        writer.identity=old.identity;self.request=writer
        def reviewer(payload,repair=None):return self.clean(payload) if payload['frames'][1]['approved_focus']['state']=='Исправлено' else self.conflict(payload)
        with self.assertRaises(ValueError):self.compile(sequence_request=reviewer)
        before=len(self.calls);frames,plan=self.compile(sequence_request=reviewer)
        self.assertEqual(len(self.calls)-before,2);self.assertEqual(frames[1]['visual_state'],'Исправлено')
        self.assertEqual(plan['focus']['sequence_review']['status'],'complete')
    def test_review_format_retry_does_not_rewrite_any_frame(self):
        calls=[]
        def reviewer(payload,repair=None):
            calls.append(repair);value=self.clean(payload)
            if len(calls)==1:value['checked_pairs']=[]
            return value
        self.compile(sequence_request=reviewer);self.assertEqual(len(calls),2);self.assertIsNotNone(calls[1]);self.assertEqual(len(self.calls),6)
    def test_provider_limit_stops_sequence_without_content_repair(self):
        from imagegen.nvidia_text import NvidiaAPIError
        calls=[]
        def reviewer(payload,repair=None):calls.append(payload);raise NvidiaAPIError('HTTP 429 quota')
        with self.assertRaises(NvidiaAPIError):self.compile(sequence_request=reviewer)
        self.assertEqual(len(calls),1);self.assertEqual(len(self.calls),6)
        report=json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(report['sequence_review']['issues'][0]['kind'],'provider')
    def test_cancel_during_sequence_leaves_resumable_progress(self):
        def reviewer(payload,repair=None):raise InterruptedError('pause')
        with self.assertRaises(InterruptedError):self.compile(sequence_request=reviewer)
        report=json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(report['sequence_review']['status'],'paused');self.assertEqual(report['status'],'needs_attention')
    def test_repaired_frames_do_not_mark_unfinished_sequence_complete(self):
        calls=[]
        old=self.request
        def writer(instructions,items,**kw):
            value=old(instructions,items,**kw)
            if not kw.get('review') and kw.get('repair'):value['frames'][0]['state']='Повторно принятое состояние'
            return value
        writer.identity=old.identity;self.request=writer
        def reviewer(payload,repair=None):
            calls.append(payload)
            if len(calls)==1:return self.conflict(payload)
            report=json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
            self.assertNotEqual(report['status'],'complete')
            raise InterruptedError('pause before final sequence verdict')
        with self.assertRaises(InterruptedError):self.compile(sequence_request=reviewer)
        report=json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
        self.assertTrue(all(f['status']=='accepted' for f in report['frames']));self.assertEqual(report['status'],'needs_attention')
    def test_single_frame_requires_no_sequence_model_call(self):
        self.scenes=self.scenes[:1]
        def no_call(*a,**kw):raise AssertionError('No adjacent pair')
        frames,plan=self.compile(sequence_request=no_call);self.assertEqual(plan['focus']['sequence_review']['checked_pairs'],[])
    def test_window_size_limit_fails_without_truncating_sources(self):
        frames,plan=self.compile()
        with patch.object(sequence,'MAX_CHARS',50):
            with self.assertRaisesRegex(ValueError,'не обрезаны'):sequence.windows(self.story,plan['parent'],plan['focus']['frames'])
    def test_transport_uses_closed_schema_without_full_story(self):
        from types import SimpleNamespace
        frames,plan=self.compile();payload=sequence.windows(self.story,plan['parent'],plan['focus']['frames'])[0];bodies=[]
        def post(body):bodies.append(body);return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.clean(payload))}}]}
        request=sequence.request_adapter(SimpleNamespace(model='nvidia/fixture',_post_json=post))
        self.assertEqual(request(payload),self.clean(payload));body=bodies[0]
        self.assertTrue(body['response_format']['json_schema']['strict']);self.assertNotIn('story',json.loads(body['messages'][1]['content']))
    def test_direct_focus_transport_also_runs_sequence_after_source_acceptance(self):
        from types import SimpleNamespace
        from imagegen.story_visual_plan import apply_focus
        bodies=[]
        def post(body):
            payload=json.loads(body['messages'][1]['content']);bodies.append(payload)
            if 'window' in payload:value=self.clean(payload['window'])
            elif body['response_format']['json_schema']['name']=='frame_focus_review':
                rows=[]
                for item in payload['frames']:
                    row=review(item)
                    for check in row['checks']:check['source_ids']=[0];del check['evidence']
                    row['editorial_notes']=[];rows.append(row)
                value={'reviews':rows}
            else:
                rows=[]
                for item in payload['frames']:
                    row=proposal(item);del row['subject'];del row['evidence']
                    row['source_ids']=[0];row['subject_candidate_id']=next(c['id'] for c in item['subject_candidates'] if c['label'].casefold()=='корзина')
                    rows.append(row)
                value={'frames':rows}
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
        request=focus.client_request(SimpleNamespace(model='fixture',_post_json=post))
        document=parent_document(self.story,self.scenes)
        frames=[{'data':{**d,'narrative_beat':{'ordinal':n+1},'frame_context':document['frame_bindings'][n]}} for n,d in enumerate(self.scenes)]
        metadata,outputs=focus.prepare(self.story,{'narrative_storyboard':document},frames,request,self.folder,
                                      grounder=lambda data,entry:apply_focus(data,entry))
        self.assertEqual(sum('window' in b for b in bodies),1);self.assertEqual(len(bodies),7)
        self.assertEqual(metadata['narrative_frame_focus']['sequence_review']['status'],'complete')
        self.assertEqual(outputs[1]['data']['source_quote'],self.quotes[1])
    def test_camera_only_change_does_not_invalidate_sequence(self):
        frames,plan=self.compile(sequence_request=self.clean);digest=plan['focus']['sequence_review']['content_hash']
        frames[0]['frame_composition']=camera.choice(frames[0]['frame_facts'],{'shot_size':'wide','camera_angle':'high','placement':'thirds'})
        validate_scenes(self.story,plan,frames);self.assertEqual(plan['focus']['sequence_review']['content_hash'],digest)
    def test_audio_retime_preserves_camera_facts_and_semantic_sequence(self):
        from imagegen.frame_event_contract import retime
        for n,data in enumerate(self.scenes):data['voice_timing']={'start':n*3,'end':n*3+3,'duration':3}
        frames,plan=self.compile(sequence_request=self.clean);facts=copy.deepcopy(frames[0]['frame_facts'])
        frames[0]['frame_composition']=camera.choice(facts,{'shot_size':'wide','camera_angle':'high','placement':'thirds'})
        digest=plan['focus']['sequence_review']['content_hash']
        frames[0]['voice_timing'].update(end=3.2,duration=3.2);frames[1]['voice_timing'].update(start=3.2,duration=2.8)
        retime(self.story,plan,frames);validate_scenes(self.story,plan,frames)
        self.assertEqual(camera.validate(frames[0])['shot_size'],'wide');self.assertEqual(frames[0]['frame_facts'],facts)
        self.assertEqual(plan['focus']['sequence_review']['content_hash'],digest)
    def test_real_client_route_automatically_runs_sequence_review(self):
        from types import SimpleNamespace
        bodies=[]
        def post(body):
            bodies.append(body);payload=json.loads(body['messages'][1]['content'])['window']
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.clean(payload))}}]}
        client=SimpleNamespace(model='nvidia/fixture',_post_json=post)
        with patch('imagegen.story_visual_plan.request_adapter',return_value=self.request):
            frames,plan=compile_scenes(self.scenes,self.story,client,self.folder)
        self.assertEqual(len(bodies),1);self.assertEqual(plan['focus']['sequence_review']['status'],'complete')
    def test_repair_hints_do_not_transfer_later_reviewer_prose_into_source_packet(self):
        def reviewer(payload,repair=None):
            value=self.conflict(payload);value['findings'][0]['reason']='Позже прилетит FUTURE_MARKER.'
            return value
        with self.assertRaises(ValueError):self.compile(sequence_request=reviewer)
        repairs=[repair for _,critic,repair in self.calls if repair and not critic]
        self.assertTrue(repairs);self.assertNotIn('FUTURE_MARKER',json.dumps(repairs,ensure_ascii=False))
    def test_adjacent_windows_only_are_invalidated_after_one_frame_changes(self):
        self.quotes=['Корзина номер '+str(n)+' стоит.' for n in range(8)];self.story=' '.join(self.quotes);self.scenes=[scene(self.story,q) for q in self.quotes]
        calls=[]
        def reviewer(payload,repair=None):calls.append(payload['pairs'][0]['id']);return self.clean(payload)
        frames,plan=self.compile(sequence_request=reviewer);self.assertEqual(calls,['pair_1','pair_4','pair_7']);calls.clear()
        packet=copy.deepcopy(plan['focus']);packet['frames'][0]['focus']['state']='Другой принятый вид'
        result=sequence.audit(self.story,plan['parent'],packet,reviewer,self.folder)
        self.assertEqual(calls,['pair_1']);self.assertEqual(result['status'],'complete')


class CompositionApiTests(Fixture):
    def setUp(self):
        super().setUp()
        from imagegen.server import Application
        self.app=Application(self.folder/'app');self.service=self.app.service
        self.project=self.service.create_project({'title':'Camera fixture','story':self.story,'style':'Cinematic','mode':'illustration'})
        frames,plan=self.compile();self.service.import_analysis(self.project['id'],{'world':'','characters':[],'locations':[],'props':[],'scenes':frames},self.project['version'])
        self.sid=self.service.detail(self.project['id'])['scenes'][0]['id'];self.path='/api/scenes/'+self.sid+'/composition'
    def get(self):return self.app.dispatch('GET',self.path,None)
    def payload(self):
        current=self.get();return {'revision':current['revision'],'facts_hash':current['facts']['content_hash'],
                                  'shot_size':'wide','camera_angle':'high','placement':'thirds'}
    def test_get_is_read_only_and_save_changes_only_camera(self):
        before=self.service.scene(self.sid);generations=self.service.db.all('SELECT * FROM generations');current=self.get()
        self.assertEqual(before,self.service.scene(self.sid));result=self.app.dispatch('POST',self.path,self.payload())
        after=self.service.scene(self.sid)
        for key in ('visual_content','frame_focus','source_quote','source_start','source_end','voice_timing','frame_facts'):
            self.assertEqual(before['data'].get(key),after['data'].get(key))
        self.assertEqual(after['current_revision'],before['current_revision']+1);self.assertFalse(result['media_regeneration_started'])
        self.assertEqual(self.service.db.all('SELECT * FROM generations'),generations)
    def test_save_rejects_stale_revision_hash_and_fact_injection(self):
        for mode in ('revision','hash','facts'):
            payload=self.payload()
            if mode=='revision':payload['revision']-=1
            elif mode=='hash':payload['facts_hash']='changed'
            else:payload['participants']=[]
            with self.assertRaises(DomainError):self.app.dispatch('POST',self.path,payload)
        self.assertEqual(self.service.scene(self.sid)['current_revision'],1)
    def test_same_choice_does_not_create_new_revision(self):
        payload=self.payload();self.app.dispatch('POST',self.path,payload);payload['revision']=2
        self.assertFalse(self.app.dispatch('POST',self.path,payload)['changed']);self.assertEqual(self.service.scene(self.sid)['current_revision'],2)
    def test_camera_change_keeps_media_but_marks_only_that_frame_stale(self):
        for n,row in enumerate(self.service.detail(self.project['id'])['scenes']):
            buffer=io.BytesIO();Image.new('RGB',(64,64),(20+n*70,40,80)).save(buffer,format='PNG');self.service.upload_scene_image(row['id'],buffer.getvalue())
        assets=self.service.db.all('SELECT * FROM assets');generations=self.service.db.all('SELECT * FROM generations')
        self.app.dispatch('POST',self.path,self.payload());detail=self.service.detail(self.project['id'])
        self.assertTrue(detail['scenes'][0]['stale']);self.assertFalse(detail['scenes'][1]['stale'])
        self.assertEqual(self.service.db.all('SELECT * FROM assets'),assets);self.assertEqual(self.service.db.all('SELECT * FROM generations'),generations)


if __name__=='__main__':unittest.main()

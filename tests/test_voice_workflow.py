import base64
import copy
import json
import unittest
from unittest.mock import patch
from imagegen.domain import DomainError
from imagegen.editing import normalize
from imagegen.voice_alignment import align_words
from imagegen.voice_timing import timed_frames, verify_plan
from imagegen.voice_workflow import VoiceWorkflow
from tests.test_veo_animation import TestVeoAnimationPlan
from tests.test_montage import wav


class VoiceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TestVeoAnimationPlan(); self.fixture.setUp()
        self.service, self.app = self.fixture.service, self.fixture.app
        self.pid = self.fixture._create_sample_project(2)
        self.audio = self.service.editing.audio(self.pid, {'filename':'voice.wav','data':base64.b64encode(wav()).decode()})
        self.workflow = VoiceWorkflow(self.service, self.app.edit_exports)
        story = self.service.project(self.pid)['story']
        tokens = story.replace('.', '').split()
        self.asr = {'model':'fixture','complete':True,'audio_duration':1,'segments':[{'words':[
            {'word':word,'start':i*.06,'end':i*.06+.045,'probability':.99} for i,word in enumerate(tokens)]}]}

    def tearDown(self):
        self.fixture.tearDown()

    def test_visual_edit_keeps_exact_voice_fragment_and_timing(self):
        from imagegen.db import dump
        scene = self.service.detail(self.pid)['scenes'][0]
        quote = scene['data']['source_quote']
        fragment = ' '.join(quote.split()[:2])
        self.assertLess(len(fragment), len(quote))
        start = self.service.project(self.pid)['story'].find(fragment)
        timing = {'start': 0, 'end': 1, 'duration': 1}
        data = {**scene['data'], 'source_quote': fragment, 'source_start': start,
                'source_end': start + len(fragment), 'voice_timing': timing}
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data), scene['id'], scene['current_revision']))
        updated = self.service.edit_scene(scene['id'], {
            'audit_source_quote': fragment, 'audit_visual_direction': 'Corrected visual direction.'
        }, scene['current_revision'])
        self.assertEqual(updated['data']['source_quote'], fragment)
        self.assertEqual(updated['data']['source_start'], start)
        self.assertEqual(updated['data']['source_end'], start + len(fragment))
        self.assertEqual(updated['data']['voice_timing'], timing)
        self.assertEqual(updated['data']['audit_source_quote'], updated['data']['source_quote'])

    def start(self, **data):
        with patch.object(self.workflow,'capability',return_value={'ready':True}):
            return self.workflow.start(self.pid,{'audio_asset_id':self.audio['asset_id'],**data})

    def prepare(self):
        self.start()
        with patch.object(self.workflow,'recognize',return_value=self.asr):
            self.workflow.prepare(self.pid)

    def test_audio_before_import_is_allowed_and_full_voice_coverage_saved(self):
        detail=self.service.detail(self.pid)
        analysis={'world':detail['world'],'characters':detail['characters'],'locations':detail['locations'],
                  'props':detail['props'],'scenes':[s['data'] for s in detail['scenes']]}
        self.service.import_analysis(self.pid,analysis,detail['version'])
        story=self.service.project(self.pid)['story']
        self.prepare()
        detail=self.service.detail(self.pid)
        self.assertEqual(story,detail['story'])
        frames=[s['data']['voice_timing'] for s in detail['scenes']]
        self.assertEqual(frames[0]['start'],0)
        self.assertAlmostEqual(sum(f['duration'] for f in frames),1)
        self.assertEqual(frames[0]['end'],frames[1]['start'])
        self.assertEqual(detail['voice_workflow']['state'],'ready')
        edit=self.service.editing.get(self.pid)['document']
        self.assertEqual(edit['audio_tracks'][0]['asset_id'],self.audio['asset_id'])
        self.assertEqual(edit['export_settings']['width'],1920)

    def test_mismatching_narration_does_not_replace_scenes(self):
        before=self.service.detail(self.pid)['scenes']
        self.start()
        wrong={'model':'fixture','audio_duration':1,'segments':[{'words':[{'word':'Другой','start':0,'end':.4,'probability':.99}]}]}
        with patch.object(self.workflow,'recognize',return_value=wrong):
            with self.assertRaisesRegex(DomainError,'недостаточно совпадает'):
                self.workflow.prepare(self.pid)
        self.assertEqual(before,self.service.detail(self.pid)['scenes'])

    def test_existing_montage_and_images_cannot_be_replaced(self):
        self.fixture._upload_scene_images(self.pid)
        with self.assertRaisesRegex(DomainError,'изображения или видео'):
            self.start()

    def test_cross_project_audio_rejected(self):
        other=self.fixture._create_sample_project(1)
        with patch.object(self.workflow,'capability',return_value={'ready':True}):
            with self.assertRaisesRegex(DomainError,'этого проекта'):
                self.workflow.start(other,{'audio_asset_id':self.audio['asset_id']})

    def test_resume_preserves_finished_plan_and_pause_is_durable(self):
        self.prepare()
        ids=[s['id'] for s in self.service.detail(self.pid)['scenes']]
        self.workflow.control(self.pid,'pause')
        with self.assertRaises(InterruptedError):self.workflow.check(self.pid)
        self.workflow.update(self.pid,state='paused')
        self.workflow.control(self.pid,'resume')
        self.workflow.prepare(self.pid)
        self.assertEqual(ids,[s['id'] for s in self.service.detail(self.pid)['scenes']])

    def test_short_cloud_clip_keeps_narration_timeline_length(self):
        doc={'clips':[{'trim_in_ms':0,'trim_out_ms':8000,'playback_rate':.8}], 'audio_tracks':[]}
        self.assertEqual(normalize(doc)['duration_ms'],10000)

    def test_saving_omni_selection_preserves_local_voice_duration(self):
        self.prepare()
        detail=self.service.detail(self.pid)
        self.service.apply_animation_selection(self.pid,{'selected_scene_ids':[detail['scenes'][0]['id']],
            'animation_engine':'omni_flash','expected_revisions':{s['id']:s['current_revision'] for s in detail['scenes']}})
        scene=self.service.detail(self.pid)['scenes'][1]
        self.assertAlmostEqual(scene['data']['animation']['duration_seconds'],__import__('math').ceil(scene['data']['voice_timing']['duration']*24)/24)

    def test_required_references_are_cast_selected_and_locked_automatically(self):
        self.prepare()
        from imagegen.db import dump,now,uid
        import hashlib,io
        from PIL import Image
        anchor={'anchor_id':'city','anchor_type':'city_master','reference_required':True,
                'canonical_name':'Город — главный вид','description':'Brick city reference'}
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE projects SET analysis_metadata=? WHERE id=?',
                (dump({'forgotten_cities':{'anchors':[anchor]}}),self.pid))
            # Imported analyses may contain many scene locations. They do not
            # consume the limit on required visual anchors.
            for index in range(10):
                conn.execute('INSERT INTO locations(id,project_id,name,description) VALUES(?,?,?,?)',
                    (uid(),self.pid,f'Локация сцены {index}','Scene setting'))
        original=self.service.cast_location
        def cast(target,count):
            result=original(target,count)
            for i,gid in enumerate(result['generation_ids']):
                blob=io.BytesIO();Image.new('RGB',(64,36),(50+i*40,80,100)).save(blob,format='PNG');raw=blob.getvalue()
                aid=uid();path='assets/'+aid+'.png';(self.service.root/path).parent.mkdir(exist_ok=True);(self.service.root/path).write_bytes(raw)
                with self.service.db.transaction() as conn:
                    conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,self.pid,gid,path,hashlib.sha256(raw).hexdigest(),64,36,len(raw)))
                    conn.execute("UPDATE generations SET state='succeeded',asset_id=? WHERE id=?",(aid,gid))
            return result
        with patch.object(self.service,'_project_profile_type',return_value='forgotten_cities'),patch.object(self.service,'cast_location',side_effect=cast),patch.object(self.workflow.editorial,'reference_verdict',side_effect=[{'status':'fail','score':10,'reason':'wrong subject'},{'status':'pass','score':90,'reason':'source matched'}]):
            self.workflow.ensure_references(self.pid)
        location=self.service.db.one('SELECT * FROM locations WHERE project_id=? AND name=?',(self.pid,anchor['canonical_name']))
        self.assertTrue(location['locked'])
        self.assertTrue(self.workflow.get(self.pid)['result']['automatic_references']['city'])
        session=self.service.reference_cast('location',location)
        second=self.service.db.one('SELECT asset_id FROM generations WHERE id=?',(session['generation_ids'][1],))['asset_id']
        self.assertEqual(json.loads(location['references_json'])['environment'],second)

    def test_local_production_finishes_with_audio_and_fullhd(self):
        self.prepare()
        self.fixture._upload_scene_images(self.pid)
        self.workflow.produce(self.pid)
        result=self.workflow.get(self.pid)
        self.assertEqual(result['state'],'completed')
        self.assertTrue(result['result']['video_asset_id'])
        edit=self.service.editing.get(self.pid)
        self.assertAlmostEqual(edit['timeline']['duration_ms'],1000)
        import av
        asset=self.service.asset(result['result']['video_asset_id'])
        with av.open(str(self.service.root/asset['path'])) as video:
            self.assertEqual((video.streams.video[0].width,video.streams.video[0].height),(1920,1080))
            self.assertEqual(len(video.streams.audio),1)

    def test_city_reference_does_not_turn_source_into_interior_or_adobe(self):
        project=self.service.detail(self.pid)
        project['channel_profile_id']='documentary'
        description='Город заложили на равнине. Стены выполнены из обожжённого кирпича.'
        for suffix in ('главный вид','современные руины'):
            with patch.object(self.service.db,'one',return_value={'profile_type':'forgotten_cities'}):
                snapshot=self.service.location_snapshot(project,{'name':'Город — '+suffix,'description':description})
            self.assertIn(description,snapshot['prompt'])
            self.assertIn('broad outdoor view',snapshot['prompt'])
            self.assertNotIn('adobe',snapshot['prompt'])
            self.assertNotIn('inanimate furnishings only',snapshot['prompt'])


if __name__=='__main__':unittest.main()

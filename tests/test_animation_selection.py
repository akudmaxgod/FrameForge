import copy
import io
import json
import unittest
from pathlib import Path
from PIL import Image
from unittest.mock import patch
from tests import test_veo_animation as fixture_module
from imagegen.animation import smart_ken_burns_motion
from imagegen.domain import DomainError
from imagegen.ken_burns import render_ken_burns_frames,render_ken_burns_video
import av

class AnimationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.TestVeoAnimationPlan();self.fixture.setUp();self.service=self.fixture.service
        self.pid=self.fixture._create_sample_project(4);self.fixture._upload_scene_images(self.pid)
    def tearDown(self):self.fixture.tearDown()
    def payload(self,selected):
        return {'omni_scene_ids':selected,'expected_revisions':{s['id']:s['current_revision'] for s in self.service.detail(self.pid)['scenes']}}
    def test_selected_omni_others_local_and_no_jobs_or_image_changes(self):
        scenes=self.service.detail(self.pid)['scenes'];ids=[scenes[0]['id'],scenes[2]['id']]
        before={s['id']:{k:v for k,v in self.service.scene(s['id'])['data'].items() if k!='animation'} for s in scenes}
        generation_count=len(self.service.db.all('SELECT id FROM generations'))
        result=self.service.apply_animation_selection(self.pid,self.payload(ids))
        self.assertEqual((result['selected_omni'],result['local_2_5d']),(2,2))
        after=self.service.detail(self.pid)
        for scene in after['scenes']:
            animation=scene['data']['animation'];self.assertEqual(animation['selected_engine'],'omni_flash' if scene['id'] in ids else 'ken_burns')
            self.assertEqual(before[scene['id']],{k:v for k,v in self.service.scene(scene['id'])['data'].items() if k!='animation'})
            self.assertEqual(animation['ken_burns_mode'],'auto')
        self.assertEqual(len(self.service.db.all('SELECT id FROM generations')),generation_count)
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])
        self.assertEqual(after['animation_settings']['plan_mode'],'selective')
        revisions=self.service.db.all('SELECT id,current_revision FROM scenes WHERE project_id=?',(self.pid,))
        self.service.apply_animation_selection(self.pid,self.payload(ids))
        self.assertEqual(revisions,self.service.db.all('SELECT id,current_revision FROM scenes WHERE project_id=?',(self.pid,)))
    def test_conflict_and_foreign_scene_are_atomic(self):
        payload=self.payload([]);payload['expected_revisions'][next(iter(payload['expected_revisions']))]-=1
        before=self.service.db.all('SELECT * FROM scenes')
        with self.assertRaises(DomainError) as error:self.service.apply_animation_selection(self.pid,payload)
        self.assertEqual(error.exception.status,409);self.assertEqual(before,self.service.db.all('SELECT * FROM scenes'))
        with self.assertRaises(DomainError):self.service.apply_animation_selection(self.pid,self.payload(['foreign']))
    def test_veo_selection_model_switch_and_scoped_queue(self):
        scenes=self.service.detail(self.pid)['scenes'];ids=[scenes[0]['id'],scenes[2]['id']]
        def select(engine):
            payload=self.payload(ids);payload.pop('omni_scene_ids');payload.update(selected_scene_ids=ids,animation_engine=engine)
            return self.service.apply_animation_selection(self.pid,payload)
        self.service.select_scene_animation_engine(ids[0],'omni_flash',omni_resolution='360p',duration_seconds=8)
        self.service.select_scene_animation_engine(ids[0],'veo_lite',camera_motion='reaction',duration_seconds=5)
        result=select('veo_lite');self.assertEqual((result['selected_veo'],result['local_2_5d']),(2,2))
        self.assertEqual(self.service.detail(self.pid)['animation_settings']['selected_animation_engine'],'veo_lite')
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])
        select('omni_flash');anim=self.service.scene(ids[0])['data']['animation']
        self.assertEqual((anim['duration_seconds'],anim['omni']['resolution']),(8,'360p'))
        self.assertEqual(anim['veo']['camera_motion'],'reaction')
        select('veo_lite');anim=self.service.scene(ids[0])['data']['animation']
        self.assertEqual((anim['duration_seconds'],anim['camera_motion']),(5,'reaction'))
        for scene in self.service.detail(self.pid)['scenes']:
            self.assertEqual(scene['data']['animation']['selected_engine'],'veo_lite' if scene['id'] in ids else 'ken_burns')
        with patch.object(self.service,'project_prompts',return_value={'scenes':[{'scene_id':sid,'ready_for_flow':True} for sid in ids]}):
            self.service.start_animation_generation(self.pid,scene_ids=ids)
        jobs=self.service.db.all('SELECT scene_id,model,engine,duration_seconds,status FROM veo_jobs')
        self.assertEqual({j['scene_id'] for j in jobs},set(ids))
        self.assertTrue(all(j['model']=='veo-3.1-lite' and j['engine']=='veo_lite' and j['duration_seconds']==5 and j['status']=='queued_veo' for j in jobs))
        self.assertTrue(all(not s['video_asset_id'] for s in self.service.detail(self.pid)['scenes']))
    def test_invalid_cloud_model_cannot_change_selection(self):
        before=self.service.db.all('SELECT id,current_revision FROM scenes')
        payload=self.payload([]);payload['animation_engine']='foreign-model'
        with self.assertRaises(DomainError):self.service.apply_animation_selection(self.pid,payload)
        self.assertEqual(before,self.service.db.all('SELECT id,current_revision FROM scenes'))
    def test_single_frame_api_keeps_duration_other_scenes_and_separates_story_from_motion(self):
        scene=self.service.detail(self.pid)['scenes'][1];sid=scene['id']
        app=self.fixture.app
        app.dispatch('POST',f'/api/scenes/{sid}/animation/select',{'engine':'omni_flash','duration_seconds':8})
        app.dispatch('POST',f'/api/scenes/{sid}/animation/select',{'engine':'omni_flash','omni_resolution':'360p'})
        self.assertEqual(self.service.scene(sid)['data']['animation']['omni']['duration_seconds'],8)
        before={s['id']:copy.deepcopy(s) for s in self.service.detail(self.pid)['scenes'] if s['id']!=sid}
        app.dispatch('POST',f'/api/projects/{self.pid}/animation/generate',{'scene_ids':[sid]})
        jobs=self.service.db.all('SELECT * FROM veo_jobs');self.assertEqual(len(jobs),1)
        job=jobs[0];self.assertEqual((job['scene_id'],job['engine'],job['duration_seconds'],job['resolution']),(sid,'omni_flash',8,'360p'))
        self.assertEqual(scene['data']['beat'],self.service.scene(sid)['data']['beat']);self.assertNotIn(scene['data']['beat'],job['prompt']);self.assertIn('Camera:',job['prompt']);self.assertIn('Preserve identity',job['prompt']);self.assertEqual(job['source_image_asset_id'],self.service.db.one('SELECT asset_id FROM generations WHERE id=?',(scene['active_generation_id'],))['asset_id'])
        after={s['id']:s for s in self.service.detail(self.pid)['scenes'] if s['id']!=sid};self.assertEqual(before,after)
    def test_unapproved_trial_cannot_queue_a_job(self):
        sid=self.service.detail(self.pid)['scenes'][0]['id']
        self.service.select_scene_animation_engine(sid,'omni_flash')
        with patch.object(self.service,'_requires_still_approval',return_value=True):
            self.service.review_generation(self.service.scene(sid)['active_generation_id'],'rejected')
            with self.assertRaises(DomainError):self.service.start_animation_generation(self.pid,scene_ids=[sid])
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])
    def test_completed_and_active_jobs_are_preserved(self):
        scenes=self.service.detail(self.pid)['scenes'];completed,queued=scenes[:2]
        self.service.import_scene_video(completed['id'],self.fixture._create_valid_mp4(width=160,height=90,duration=1),filename='ready.mp4')
        self.service.queue_single_scene_omni(queued['id'])
        before={s['id']:self.service.scene(s['id']) for s in (completed,queued)}
        result=self.service.apply_animation_selection(self.pid,self.payload([]))
        self.assertEqual(set(result['preserved_scenes']),set(before))
        for sid,old in before.items():self.assertEqual(self.service.scene(sid),old)
    def test_manual_camera_override_and_return_to_auto_survive_replan(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        self.service.select_scene_animation_engine(scene['id'],'ken_burns',ken_burns_preset='slow_zoom_in')
        self.service.apply_animation_selection(self.pid,self.payload([]))
        self.assertEqual(self.service.scene(scene['id'])['data']['animation']['ken_burns_preset'],'slow_zoom_in')
        self.service.select_scene_animation_engine(scene['id'],'ken_burns',ken_burns_preset='auto')
        self.service.build_animation_plan(self.pid,plan_mode='selective')
        anim=self.service.scene(scene['id'])['data']['animation']
        self.assertEqual(anim['ken_burns_mode'],'auto');self.assertEqual(anim['ken_burns_preset'],'gentle_pull_back')
    def test_new_default_is_local_and_missing_images_wait(self):
        pid=self.fixture._create_sample_project(2)
        result=self.service.build_animation_plan(pid)
        self.assertTrue(all(s['animation']['selected_engine']=='ken_burns' and s['animation']['status']=='waiting_source_image' for s in result['scenes']))
        with self.assertRaises(DomainError):self.service.start_animation_generation(pid)
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs WHERE project_id=?',(pid,)),[])
    def test_camera_rules_and_six_percent_boundaries(self):
        cases=[({'shot':'Крупный план лица'},'gentle_push_in'),({'shot':'Общий план города'},'gentle_pull_back'),({'beat':'Он замечает ключ'},'gentle_push_in'),({'visible_people':3},'gentle_pull_back')]
        for scene,expected in cases:self.assertEqual(smart_ken_burns_motion(scene,1)['preset'],expected)
        self.assertNotEqual(smart_ken_burns_motion({},1)['preset'],smart_ken_burns_motion({},2)['preset'])
        image=Image.new('RGB',(100,100),'blue')
        for i in range(100):image.putpixel((i,0),(255,0,0));image.putpixel((0,i),(255,0,0))
        frames,info=render_ken_burns_frames(image,preset_name='gentle_pull_back',duration=1,fps=8,target_size=(100,100))
        self.assertEqual(info['overscan'],0);self.assertEqual(frames[-1].getpixel((0,0)),(255,0,0));self.assertNotEqual(frames[0].getpixel((0,0)),(255,0,0))
        buf=io.BytesIO();image.save(buf,format='PNG');path=self.service.root/'camera.mp4'
        with patch('imagegen.ken_burns.render_ken_burns_frames',side_effect=AssertionError('must stream')):
            result=render_ken_burns_video(buf.getvalue(),path,preset_name='gentle_push_in',duration=1,fps=8,target_size=(100,100))
        self.assertEqual(result['frames'],8)
        with av.open(str(path)) as media:self.assertEqual(len(list(media.decode(video=0))),8)

    def test_selection_of_232_scenes_is_one_atomic_save(self):
        pid=self.fixture._create_sample_project(232)
        scenes=self.service.detail(pid)['scenes'];selected=[scenes[i]['id'] for i in (0,115,231)]
        result=self.service.apply_animation_selection(pid,{'omni_scene_ids':selected,'expected_revisions':{s['id']:s['current_revision'] for s in scenes}})
        self.assertEqual((result['selected_omni'],result['local_2_5d']),(3,229))
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs WHERE project_id=?',(pid,)),[])
        after=self.service.detail(pid)['scenes']
        self.assertEqual(len(after),232);self.assertEqual(sum(s['data']['animation']['selected_engine']=='omni_flash' for s in after),3)

if __name__=='__main__':unittest.main()

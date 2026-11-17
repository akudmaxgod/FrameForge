import io
import json
import threading
import unittest
import urllib.request
import numpy as np
from PIL import Image
from unittest.mock import patch
from tests.test_animation_selection import AnimationSelectionTests
from imagegen.ken_burns import render_ken_burns_frames
from imagegen.server import Application, make_server


class SmoothCameraTests(unittest.TestCase):
    def test_slow_zoom_moves_in_subpixel_steps(self):
        x=np.arange(512)
        line=(240*np.exp(-((x-355)/8)**2)).astype('uint8')
        source=Image.fromarray(np.tile(line,(288,1))).convert('RGB')
        frames,_=render_ken_burns_frames(source,'gentle_push_in',6,24,(512,288))
        positions=[]
        for frame in frames:
            weights=np.asarray(frame).mean(axis=(0,2))
            positions.append(float(np.dot(weights,x)/weights.sum()))
        steps=np.diff(positions)
        self.assertTrue(np.all(steps>0))
        self.assertLess(float(steps.max()),0.20)
        # Eight-bit resampling adds small centroid noise; integer crop jumps
        # move this feature by several tenths of a pixel in a single frame.
        self.assertLess(float(np.abs(np.diff(steps)).max()),0.08)

    def test_horizontal_paths_keep_all_source_edges_outside_frame(self):
        source=Image.new('RGB',(640,360),'white')
        for x in range(640):source.putpixel((x,0),(255,0,0));source.putpixel((x,359),(255,0,0))
        for y in range(360):source.putpixel((0,y),(255,0,0));source.putpixel((639,y),(255,0,0))
        for preset in ('gentle_pan_left','gentle_pan_right'):
            frames,info=render_ken_burns_frames(source,preset,2,24,(320,180))
            self.assertTrue(info['edge_safe']);self.assertEqual(info['actual_preset'],preset)
            for frame in frames:
                pixels=np.asarray(frame)
                self.assertTrue(np.all(pixels==255),preset)

    def test_streamed_video_preserves_resolution_duration_and_visible_motion(self):
        import tempfile
        from pathlib import Path
        import av
        from imagegen.ken_burns import render_ken_burns_video
        with tempfile.TemporaryDirectory() as root:
            x=np.arange(640)
            line=(240*np.exp(-((x-470)/14)**2)).astype('uint8')
            source=Image.fromarray(np.tile(line,(360,1))).convert('RGB')
            buf=io.BytesIO();source.save(buf,format='PNG')
            path=Path(root)/'motion.mp4'
            info=render_ken_burns_video(buf.getvalue(),path,'gentle_push_in',duration=2,fps=24,target_size=(640,360))
            self.assertEqual(info['frames'],48)
            with av.open(str(path)) as container:
                stream=container.streams.video[0]
                self.assertEqual((stream.width,stream.height),(640,360))
                self.assertEqual(float(stream.average_rate),24)
                frames=list(container.decode(video=0))
            self.assertEqual(len(frames),48)
            self.assertAlmostEqual(float(frames[-1].time)+1/24,2,places=3)
            def center(frame):
                weights=frame.to_ndarray(format='rgb24').mean(axis=(0,2))
                return float(np.dot(weights,x)/weights.sum())
            positions=[center(frame) for frame in frames]
            self.assertGreater(positions[-1]-positions[0],15)
            self.assertGreater(min(np.diff(positions)),0)

    def test_local_rebuild_preserves_remote_jobs_and_previous_assets(self):
        f=AnimationSelectionTests();f.setUp()
        try:
            s=f.service;pid=f.pid;scenes=s.detail(pid)['scenes']
            s.apply_animation_selection(pid,f.payload([scenes[0]['id']]))
            with patch('imagegen.service.render_ken_burns_video',side_effect=small_render):
                s.start_animation_generation(pid,scene_ids=[scenes[0]['id'],scenes[1]['id']])
                before=s.scene(scenes[1]['id']);old_asset=before['video_asset_id'];remote=s.scene(scenes[0]['id']);jobs=s.db.all('SELECT * FROM veo_jobs')
                s.start_animation_generation(pid,scene_ids=[scenes[0]['id'],scenes[1]['id']],regenerate_local=True)
                after=s.scene(scenes[1]['id'])
            self.assertNotEqual(after['video_asset_id'],old_asset)
            self.assertEqual(s.scene(scenes[0]['id']),remote);self.assertEqual(s.db.all('SELECT * FROM veo_jobs'),jobs)
            self.assertEqual(after['data']['animation']['ken_burns_renderer_version'],4)
            self.assertTrue((s.root/s.db.one('SELECT path FROM assets WHERE id=?',(old_asset,))['path']).is_file())
        finally:f.tearDown()

    def test_local_selection_never_audits_whole_project(self):
        f=AnimationSelectionTests();f.setUp()
        try:
            s=f.service;sid=s.detail(f.pid)['scenes'][0]['id']
            with patch.object(s,'detail',side_effect=AssertionError('whole project audit')):
                result=s.select_scene_animation_engine(sid,'ken_burns')
            self.assertEqual(result['scene']['data']['animation']['selected_engine'],'ken_burns')
            self.assertEqual(result['summary']['total_frames'],len(s.db.all('SELECT id FROM scenes WHERE project_id=?',(f.pid,))))
        finally:f.tearDown()

    def test_one_local_trial_does_not_audit_the_whole_project(self):
        f=AnimationSelectionTests();f.setUp()
        try:
            s=f.service;scenes=s.detail(f.pid)['scenes']
            s.apply_animation_selection(f.pid,f.payload([]))
            with patch.object(s,'detail',side_effect=AssertionError('unnecessary whole-project audit')), patch('imagegen.service.render_ken_burns_video',side_effect=small_render):
                result=s.start_animation_generation(f.pid,scene_ids=[scenes[0]['id']])
            self.assertEqual(result['scenes'][0]['status'],'completed')
            self.assertFalse(s.scene(scenes[1]['id'])['video_asset_id'])
        finally:f.tearDown()

    def test_explicit_cloud_video_deletion_starts_a_new_request(self):
        f=AnimationSelectionTests();f.setUp()
        try:
            s=f.service;scene=s.detail(f.pid)['scenes'][0];sid=scene['id']
            first=s.queue_single_scene_omni(sid);key=first['scene']['data']['animation']['idempotency_key']
            uploaded=s.upload_scene_video(sid,b'RIFF\x00\x00\x00\x00WEBM',idempotency_key=key)
            old_asset=uploaded['video_asset_id']
            s.delete_scene_video(sid);nonce=s.scene(sid)['data']['animation']['request_nonce']
            s.delete_scene_video(sid);self.assertEqual(s.scene(sid)['data']['animation']['request_nonce'],nonce)
            second=s.queue_single_scene_omni(sid);second_key=second['scene']['data']['animation']['idempotency_key']
            self.assertNotEqual(key,second_key)
            again=s.queue_single_scene_omni(sid);self.assertTrue(again['reused'])
            new=s.upload_scene_video(sid,b'RIFF\x00\x00\x00\x00WEBM-new',idempotency_key=second_key)
            self.assertNotEqual(new['video_asset_id'],old_asset)
            self.assertEqual(s.scene(sid)['video_asset_id'],new['video_asset_id'])
        finally:f.tearDown()

    def test_bridge_heartbeat_is_not_blocked_by_a_local_generation_request(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            app=Application(root);p=app.service.create_project({'title':'Lock check','story':'A scene.','style':'realism'});pid=p['id']
            server=make_server(app,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            started=threading.Event();release=threading.Event();errors=[]
            def slow(*args,**kwargs):started.set();release.wait(5);return {'scenes':[]}
            def request(path,data):
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Session-Token':app.token})
                with urllib.request.urlopen(req,timeout=2) as r:return json.load(r)
            def launch():
                try:request(f'/api/projects/{pid}/animation/generate',{})
                except Exception as e:errors.append(e)
            try:
                with patch.object(app.service,'start_animation_generation',side_effect=slow):
                    runner=threading.Thread(target=launch);runner.start();self.assertTrue(started.wait(2))
                    request('/api/bridge/v1/heartbeat',{'project_id':pid,'flow_project_id':'test-flow','adapter_connected':True,'adapter_instance_id':'test','claim_adapter':True,'script_version':'0.1.66'})
                    self.assertFalse(release.is_set());release.set();runner.join(2);self.assertEqual(errors,[])
            finally:release.set();server.shutdown();server.server_close()


def small_render(image,out,**kwargs):
    from imagegen.ken_burns import render_ken_burns_video
    return render_ken_burns_video(image,out,preset_name=kwargs.get('preset_name','gentle_push_in'),duration=.3,fps=10,target_size=(128,72))

if __name__=='__main__':unittest.main()

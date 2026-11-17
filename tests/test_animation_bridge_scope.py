import json
import threading
import unittest
import urllib.request
from unittest.mock import patch
from tests import test_veo_animation as fixtures
from imagegen.domain import DomainError
from imagegen.server import make_server, BRIDGE_ADAPTER_TTL


class AnimationBridgeScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan(); self.fixture.setUp()
        self.app = self.fixture.app; self.fixture.service = self.app.service
        self.service = self.app.service
        self.pid = self.fixture._create_sample_project(3)
        self.fixture._upload_scene_images(self.pid)
        self.ids = [s['id'] for s in self.service.detail(self.pid)['scenes']]
        self.app.dispatch('POST', '/api/projects/current', {'project_id':self.pid})
        self.heartbeat()
    def tearDown(self): self.fixture.tearDown()
    def heartbeat(self, running=False, **changes):
        return self.app.dispatch('POST', '/api/bridge/v1/heartbeat', {
            'project_id':self.pid,'flow_project_id':'flow','adapter_instance_id':'adapter',
            'adapter_connected':True,'scoped_scene_start':True,'running':running,**changes})
    def start(self, ids):
        return self.app.dispatch('POST','/api/bridge/v1/command',
            {'action':'start','project_id':self.pid,'scene_ids':ids})
    def jobs(self, client='google-flow', pid=None):
        with patch.object(self.service,'animation_jobs',return_value={'jobs':[{'scene_id':sid} for sid in self.ids]}):
            return self.app.dispatch('GET','/api/projects/'+(pid or self.pid)+'/animation/jobs',{},client=client)['jobs']
    def test_acknowledged_scoped_run_cannot_fetch_other_queued_videos(self):
        command = self.start([self.ids[1]])
        self.heartbeat(True, ack_command_id=command['command_id'])
        self.assertEqual(self.jobs(), [{'scene_id':self.ids[1]}])
        self.assertEqual(self.app.flow_bridge_v1['command'],None)
        self.assertEqual(len(self.jobs(client=None)),3)
    def test_scoped_run_is_released_when_it_finishes(self):
        self.start([self.ids[1]]); self.heartbeat(True); self.heartbeat(False)
        self.assertEqual(len(self.jobs()),3)
    def test_full_start_resets_previous_subset(self):
        self.start([self.ids[1]]); self.start(None)
        self.assertEqual(len(self.jobs()),3)
    def test_expired_or_foreign_scope_cannot_expand_to_all_jobs(self):
        self.start([self.ids[1]])
        with self.assertRaises(DomainError): self.jobs(pid='foreign')
        self.app.flow_bridge_v1['last_seen'] -= BRIDGE_ADAPTER_TTL+1
        with self.assertRaises(DomainError): self.jobs()
    def test_foreign_adapter_cannot_clear_the_active_scope(self):
        self.start([self.ids[1]]); self.heartbeat(True)
        self.heartbeat(False, adapter_instance_id='other')
        self.assertEqual(self.jobs(),[{'scene_id':self.ids[1]}])
    def test_http_adapter_header_reaches_the_scope_filter(self):
        self.start([self.ids[1]])
        server = make_server(self.app,0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch.object(self.service,'animation_jobs',return_value={'jobs':[{'scene_id':sid} for sid in self.ids]}):
                url=f'http://127.0.0.1:{server.server_port}/api/projects/{self.pid}/animation/jobs'
                with urllib.request.urlopen(urllib.request.Request(url,headers={'X-Client':'google-flow'})) as response:
                    self.assertEqual(json.load(response)['jobs'],[{'scene_id':self.ids[1]}])
                with urllib.request.urlopen(url) as response: self.assertEqual(len(json.load(response)['jobs']),3)
        finally:
            server.shutdown();server.server_close();thread.join()
    def request_binding(self, sid):
        self.service.select_scene_animation_engine(sid,'omni_flash')
        self.service.start_animation_generation(self.pid,scene_ids=[sid])
        scene=self.service.scene(sid);anim=scene['data']['animation']
        return {'expected_scene_revision':scene['current_revision'],'idempotency_key':anim['idempotency_key'],
            'source_image_asset_id':anim['source_image_asset_id'],'source_image_sha256':anim['source_image_sha256']}
    def test_confirmed_failure_records_only_the_matching_request(self):
        sid=self.ids[0];binding=self.request_binding(sid)
        image=self.service.scene(sid)['active_generation_id']
        self.app.dispatch('POST','/api/scenes/'+sid+'/animation/progress',{'status':'failed','error':'ERR_GEN_FAILED',**binding})
        self.assertEqual(self.service.scene(sid)['data']['animation']['status'],'failed')
        self.assertEqual(self.service.scene(sid)['active_generation_id'],image)
        self.assertEqual(self.service.db.one('SELECT status FROM veo_jobs WHERE scene_id=?',(sid,))['status'],'failed')
    def test_late_error_cannot_fail_a_new_request_or_a_saved_clip(self):
        sid=self.ids[0];binding=self.request_binding(sid)
        for key,value in [('idempotency_key','wrong'),('source_image_sha256','wrong'),('expected_scene_revision',0)]:
            with self.assertRaises(DomainError):self.service.set_scene_animation_status(sid,'failed',**{**binding,key:value})
        self.service.upload_scene_video(sid,self.fixture._create_valid_mp4(width=160,height=90,duration=1),filename='ready.mp4')
        saved=self.service.scene(sid)
        with self.assertRaises(DomainError):self.service.set_scene_animation_status(sid,'failed',**binding)
        self.assertEqual(saved,self.service.scene(sid))


if __name__ == '__main__': unittest.main()

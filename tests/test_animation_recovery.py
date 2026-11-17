import copy
import unittest
from unittest.mock import patch
from imagegen.animation_recovery import resolve_no_result, manifest
from imagegen.domain import DomainError
from tests import test_animation_direction_contract as motion_fixture


class AttemptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=motion_fixture.MotionPromptQueueTests();self.f.setUp();self.s=self.f.service
        self.sid=self.f.ids[0]
        self.s.select_scene_animation_engine(self.sid,'omni_flash')
        self.s.queue_single_scene_omni(self.sid)
        self.scene=self.s.scene(self.sid);self.anim=self.scene['data']['animation']
        self.payload={'confirmed_no_result':True,'expected_scene_revision':self.scene['current_revision'],
                      'idempotency_key':self.anim['idempotency_key']}

    def tearDown(self):self.f.tearDown()

    def test_confirmation_is_required_and_replay_is_idempotent(self):
        with self.assertRaises(DomainError):resolve_no_result(self.s,self.sid,{**self.payload,'confirmed_no_result':False})
        self.assertEqual(self.scene,self.s.scene(self.sid))
        first=resolve_no_result(self.s,self.sid,self.payload)
        before=self.s.scene(self.sid)
        second=resolve_no_result(self.s,self.sid,self.payload)
        self.assertTrue(first['resolved']);self.assertTrue(second['already_resolved'])
        self.assertEqual(before,self.s.scene(self.sid))

    def test_retry_keeps_omni_source_and_story_but_uses_new_request_identity(self):
        resolve_no_result(self.s,self.sid,self.payload)
        after=self.s.scene(self.sid)
        self.assertEqual(after['data']['animation']['selected_engine'],'omni_flash')
        self.assertEqual(after['active_generation_id'],self.scene['active_generation_id'])
        self.assertEqual({k:v for k,v in after['data'].items() if k!='animation'},
                         {k:v for k,v in self.scene['data'].items() if k!='animation'})
        self.assertEqual(after['data']['animation']['status'],'planned_omni')
        self.s.queue_single_scene_omni(self.sid)
        new=self.s.scene(self.sid)['data']['animation']
        self.assertNotEqual(new['idempotency_key'],self.anim['idempotency_key'])
        self.assertEqual(new['source_image_sha256'],self.anim['source_image_sha256'])
        old=self.s.db.one('SELECT status FROM veo_jobs WHERE idempotency_key=?',(self.anim['idempotency_key'],))
        self.assertEqual(old['status'],'failed')

    def test_stale_binding_and_foreign_attempt_are_rejected_without_changes(self):
        for values in ({'expected_scene_revision':0},{'idempotency_key':'foreign'}):
            with self.assertRaises(DomainError) as error:resolve_no_result(self.s,self.sid,{**self.payload,**values})
            self.assertEqual(error.exception.status,409)
            self.assertEqual(self.scene,self.s.scene(self.sid))

    def test_confirmed_provider_rejection_is_terminal_without_creating_retry(self):
        payload={'confirmed_provider_rejection':True,'provider_message':'Provider explicitly rejected this request.',
                 'expected_scene_revision':self.scene['current_revision'],'idempotency_key':self.anim['idempotency_key']}
        result=resolve_no_result(self.s,self.sid,payload);after=result['scene']['data']['animation']
        self.assertEqual(after['status'],'failed')
        self.assertEqual(after['idempotency_key'],self.anim['idempotency_key'])
        self.assertEqual(after.get('request_nonce'),self.anim.get('request_nonce'))
        self.assertIn('explicitly rejected',after['error'])
        self.assertEqual(after['resolved_attempts'][self.anim['idempotency_key']]['decision'],'provider_rejected')
        self.assertEqual(self.s.animation_jobs(self.f.pid)['jobs'],[])
        before=self.s.scene(self.sid)
        self.assertTrue(resolve_no_result(self.s,self.sid,payload)['already_resolved'])
        self.assertEqual(before,self.s.scene(self.sid))
        with self.assertRaises(DomainError):resolve_no_result(self.s,self.sid,self.payload)

    def test_provider_rejection_requires_text_and_preserves_stale_guard(self):
        for message in ('','x'*2001):
            with self.assertRaises(DomainError):resolve_no_result(self.s,self.sid,{
                **self.payload,'confirmed_provider_rejection':True,'provider_message':message})
        self.assertEqual(self.scene,self.s.scene(self.sid))
        with self.assertRaises(DomainError) as error:resolve_no_result(self.s,self.sid,{
            **self.payload,'confirmed_provider_rejection':True,'provider_message':'Actual provider refusal',
            'expected_scene_revision':0})
        self.assertEqual(error.exception.status,409)

    def test_manifest_and_http_resolution(self):
        row=next(r for r in manifest(self.s,self.f.pid)['scenes'] if r['scene_id']==self.sid)
        self.assertEqual(row['position'],self.scene['position'])
        app=self.f.fixture.app
        app.dispatch('POST',f'/api/scenes/{self.sid}/animation/resolve',self.payload)
        result=app.dispatch('GET',f'/api/projects/{self.f.pid}/animation/recovery',None)
        row=next(r for r in result['scenes'] if r['scene_id']==self.sid)
        self.assertIn(self.anim['idempotency_key'],row['resolved_attempt_keys'])
        self.assertIsNone(row['video_asset_id'])


if __name__=='__main__':unittest.main()

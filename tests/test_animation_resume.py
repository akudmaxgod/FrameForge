import unittest
from unittest.mock import patch
from tests import test_animation_bridge_scope as scope_fixture
from imagegen.domain import DomainError


class AnimationResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = scope_fixture.AnimationBridgeScopeTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.app = self.fixture.app
        self.pid = self.fixture.pid
        self.ids = self.fixture.ids

    def tearDown(self):
        self.fixture.tearDown()

    def queue(self, sid, engine='omni_flash'):
        self.service.select_scene_animation_engine(sid, engine)
        with patch.object(self.service, 'project_prompts', return_value={'scenes': [
                {'scene_id': sid, 'ready_for_flow': True}]}):
            self.service.start_animation_generation(self.pid, scene_ids=[sid])

    def test_repeated_bulk_start_preserves_request_and_scene_revision(self):
        for sid, engine in zip(self.ids, ('omni_flash', 'veo_lite', 'omni_flash')):
            self.queue(sid, engine)
        before = [self.service.scene(sid) for sid in self.ids]
        jobs = self.service.db.all('SELECT * FROM veo_jobs ORDER BY id')
        with patch.object(self.service, 'project_prompts', return_value={'scenes': []}):
            self.service.start_animation_generation(self.pid, scene_ids=self.ids)
        self.assertEqual(before, [self.service.scene(sid) for sid in self.ids])
        self.assertEqual(jobs, self.service.db.all('SELECT * FROM veo_jobs ORDER BY id'))

    def test_reselect_same_engine_is_noop_and_changed_engine_is_blocked(self):
        sid = self.ids[0]
        self.queue(sid)
        before = self.service.scene(sid)
        self.assertTrue(self.service.select_scene_animation_engine(sid, 'omni_flash')['reused'])
        self.assertEqual(before, self.service.scene(sid))
        self.service.set_scene_animation_status(sid, 'generating_omni')
        before = self.service.scene(sid)
        with self.assertRaises(DomainError) as error:
            self.service.select_scene_animation_engine(sid, 'veo_lite')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(before, self.service.scene(sid))

    def test_generating_request_is_not_requeued(self):
        sid = self.ids[0]
        self.queue(sid)
        self.service.set_scene_animation_status(sid, 'generating_omni')
        before = self.service.scene(sid)
        jobs = self.service.db.all('SELECT * FROM veo_jobs ORDER BY id')
        with patch.object(self.service, 'project_prompts', return_value={'scenes': []}):
            self.service.start_animation_generation(self.pid, scene_ids=[sid])
        self.assertEqual(before, self.service.scene(sid))
        self.assertEqual(jobs, self.service.db.all('SELECT * FROM veo_jobs ORDER BY id'))

    def command(self, **extra):
        return self.app.dispatch('POST', '/api/bridge/v1/command', {
            'action': 'start', 'project_id': self.pid, 'scene_ids': [self.ids[0]],
            'animations_only': True, **extra})

    def test_bound_progress_preserves_revision_and_original_video_upload_binding(self):
        sid = self.ids[0]
        self.queue(sid)
        scene = self.service.scene(sid)
        anim = scene['data']['animation']
        binding = {'expected_scene_revision': scene['current_revision'],
                   'idempotency_key': anim['idempotency_key'],
                   'source_image_asset_id': anim['source_image_asset_id'],
                   'source_image_sha256': anim['source_image_sha256']}
        for status in ('generating_omni', 'queued_omni', 'generating_omni'):
            self.service.set_scene_animation_status(sid, status, **binding)
            self.assertEqual(self.service.scene(sid)['current_revision'], scene['current_revision'])
        self.service.upload_scene_video(sid, self.fixture.fixture._create_valid_mp4(width=160, height=90, duration=1),
                                        filename='ready.mp4', **binding)
        self.assertTrue(self.service.scene(sid)['video_asset_id'])

    def test_animation_only_command_requires_capability_and_keeps_scope(self):
        with self.assertRaises(DomainError) as error:
            self.command()
        self.assertEqual(error.exception.status, 409)
        self.fixture.heartbeat(animations_only=True)
        result = self.command()
        self.assertTrue(result['ok'])
        command = self.app.flow_bridge_v1['command']
        self.assertTrue(command['animations_only'])
        self.assertFalse(command['images_only'])
        self.assertEqual(command['scene_ids'], [self.ids[0]])

    def test_animation_only_rejects_mixed_or_busy_commands(self):
        self.fixture.heartbeat(animations_only=True)
        for extra in ({'images_only': True}, {'scene_ids': None}, {'batch_stage': 'scenes'}, {'generation_ids': ['g']}):
            with self.assertRaises(DomainError) as error:
                self.command(**extra)
            self.assertEqual(error.exception.status, 400)
        self.fixture.heartbeat(True, animations_only=True)
        with self.assertRaises(DomainError) as error:
            self.command()
        self.assertEqual(error.exception.status, 409)


if __name__ == '__main__':
    unittest.main()

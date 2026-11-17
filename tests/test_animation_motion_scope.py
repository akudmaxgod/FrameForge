import copy
import unittest
from unittest.mock import patch

from imagegen.animation import compile_story_aligned_veo_prompt
from imagegen.domain import DomainError
from tests import test_veo_animation as fixtures


class AnimationMotionScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan(); self.fixture.setUp()
        self.service = self.fixture.service
        self.pid = self.fixture._create_sample_project(3)
        self.fixture._upload_scene_images(self.pid)
        self.sid = self.service.detail(self.pid)['scenes'][0]['id']

    def tearDown(self):
        self.fixture.tearDown()

    def select(self, **values):
        return self.service.select_scene_animation_engine(self.sid, 'omni_flash', **values)

    def jobs(self):
        with patch.object(self.service, 'project_prompts', return_value={'scenes': [{'scene_id': self.sid, 'ready_for_flow': True}]}):
            return self.service.animation_jobs(self.pid)['jobs']

    def test_camera_only_preserves_source_and_other_scenes(self):
        before = self.service.scene(self.sid)
        others = [copy.deepcopy(s) for s in self.service.detail(self.pid)['scenes'] if s['id'] != self.sid]
        self.select(motion_scope='camera_only')
        self.service.queue_single_scene_omni(self.sid)
        after = self.service.scene(self.sid)
        self.assertEqual(before['active_generation_id'], after['active_generation_id'])
        self.assertEqual({k: v for k, v in before['data'].items() if k != 'animation'},
                         {k: v for k, v in after['data'].items() if k != 'animation'})
        self.assertEqual(others, [s for s in self.service.detail(self.pid)['scenes'] if s['id'] != self.sid])
        passport = self.service.animation_passport(self.sid)
        job = self.jobs()[0]
        self.assertEqual(passport['source_quote'], before['data']['source_quote'])
        self.assertEqual(passport['motion_scope'], 'camera_only')
        self.assertNotIn(before['data']['beat'], job['prompt'])
        self.assertIn('keep all people and objects stationary', job['prompt'])
        self.assertEqual(job['prompt'], passport['prompt'])
        self.assertEqual(job['prompt'], self.service.db.one('SELECT prompt FROM veo_jobs WHERE idempotency_key=?', (job['idempotency_key'],))['prompt'])
        self.assertTrue(all('only the camera' in action for action in passport['micro_actions'].values()))

    def test_changed_direction_has_new_request_key_and_cancels_old_job(self):
        self.select(); self.service.queue_single_scene_omni(self.sid)
        first = self.service.scene(self.sid)['data']['animation']['idempotency_key']
        self.select(motion_scope='camera_only'); self.service.queue_single_scene_omni(self.sid)
        second = self.service.scene(self.sid)['data']['animation']['idempotency_key']
        self.assertNotEqual(first, second)
        self.assertEqual(self.service.db.one('SELECT status FROM veo_jobs WHERE idempotency_key=?', (first,))['status'], 'cancelled')
        self.assertTrue(self.service.queue_single_scene_omni(self.sid)['reused'])
        self.assertEqual(second, self.service.scene(self.sid)['data']['animation']['idempotency_key'])
        self.select(camera_motion='slow fixed-pose pull-back'); self.service.queue_single_scene_omni(self.sid)
        self.assertNotEqual(second, self.service.scene(self.sid)['data']['animation']['idempotency_key'])

    def test_bulk_and_single_queue_use_same_direction_key_and_prompt(self):
        self.select(motion_scope='camera_only', duration_seconds=8, omni_resolution='360p')
        self.service.queue_single_scene_omni(self.sid, duration_seconds=8, resolution='360p')
        single = self.jobs()[0]
        with patch.object(self.service, 'project_prompts', return_value={'scenes': [{'scene_id': self.sid, 'ready_for_flow': True}]}):
            self.service.start_animation_generation(self.pid, scene_ids=[self.sid])
        bulk = self.jobs()[0]
        for field in ('prompt', 'idempotency_key', 'source_image_asset_id', 'source_image_sha256', 'duration_seconds', 'resolution'):
            self.assertEqual(single[field], bulk[field], field)

    def test_scope_survives_cloud_parameters_and_engine_changes(self):
        self.select(motion_scope='camera_only')
        self.select(duration_seconds=10)
        self.service.select_scene_animation_engine(self.sid, 'ken_burns')
        self.select(omni_resolution='360p')
        self.assertEqual(self.service.scene(self.sid)['data']['animation']['motion_scope'], 'camera_only')

    def test_invalid_scope_does_not_write_revision(self):
        before = self.service.scene(self.sid)
        with self.assertRaises(DomainError): self.select(motion_scope='invalid')
        self.assertEqual(before, self.service.scene(self.sid))

    def test_api_forwards_scope(self):
        self.fixture.app.dispatch('POST', f'/api/scenes/{self.sid}/animation/select', {'engine': 'omni_flash', 'motion_scope': 'camera_only'})
        self.assertEqual(self.service.scene(self.sid)['data']['animation']['motion_scope'], 'camera_only')

    def test_veo_camera_only_has_new_key_and_same_stored_and_sent_prompt(self):
        self.service.select_scene_animation_engine(self.sid, 'veo_lite')
        self.service.queue_single_scene_veo(self.sid)
        first = self.service.scene(self.sid)['data']['animation']['idempotency_key']
        self.service.select_scene_animation_engine(self.sid, 'veo_lite', motion_scope='camera_only')
        self.service.queue_single_scene_veo(self.sid)
        job = self.jobs()[0]
        self.assertNotEqual(first, job['idempotency_key'])
        self.assertIn('keep all people and objects stationary', job['prompt'])
        stored = self.service.db.one('SELECT prompt FROM veo_jobs WHERE idempotency_key=?', (job['idempotency_key'],))
        self.assertEqual(stored['prompt'], job['prompt'])
        self.assertTrue(self.service.queue_single_scene_veo(self.sid)['reused'])

    def test_switch_to_local_cancels_remote_request_and_clears_error(self):
        self.select(); self.service.queue_single_scene_omni(self.sid)
        before = self.service.scene(self.sid)
        old_key = before['data']['animation']['idempotency_key']
        self.service.select_scene_animation_engine(self.sid, 'ken_burns', duration_seconds=6)
        after = self.service.scene(self.sid)
        self.assertEqual(after['active_generation_id'], before['active_generation_id'])
        self.assertNotIn('idempotency_key', after['data']['animation'])
        self.assertIsNone(after['data']['animation']['error'])
        self.assertEqual(self.service.db.one('SELECT status FROM veo_jobs WHERE idempotency_key=?', (old_key,))['status'], 'cancelled')

    def test_anonymous_people_are_preserved_without_named_cast(self):
        prompt = compile_story_aligned_veo_prompt({'beat': 'A crowd crosses a courtyard.', 'visible_people': 5})
        self.assertNotIn('No active human', prompt)
        self.assertIn('Preserve every person visible', prompt)
        self.assertIn('A crowd crosses a courtyard.', prompt)

    def test_four_second_timeline_does_not_run_past_duration(self):
        prompt = compile_story_aligned_veo_prompt({'beat': 'A man looks up.'}, duration_seconds=4)
        self.assertIn('2–4 s:', prompt)
        self.assertNotIn('2–5 s:', prompt)


if __name__ == '__main__': unittest.main()

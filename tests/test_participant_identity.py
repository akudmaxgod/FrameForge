import io
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from imagegen.domain import DomainError
from imagegen.server import Application


class ParticipantIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Application(tmp.name)
        self.service = self.app.service
        profile = {**self.service.settings(), 'unet': 'test', 'clip': 'test', 'vae': 'test'}
        self.addCleanup(patch.stopall)
        patch.object(self.service, 'profile', return_value=profile).start()
        channel = self.service.create_channel({'name': 'Identity test', 'profile_type': 'forgotten_cities'})
        quotes = ['В 1963 году житель ремонтировал стену своего дома.',
                  'Мужчина взял фонарь и вошёл в проход.', 'Вдали виден пустой подземный коридор.']
        story = ' '.join(quotes)
        self.project = self.service.create_project({'title': 'Identity', 'story': story,
            'style': 'documentary', 'mode': 'objects', 'channel_profile_id': channel['id']})
        scenes = []
        for i, quote in enumerate(quotes):
            start = story.index(quote)
            roles = ['житель' if i == 0 else 'мужчина'] if i < 2 else []
            scenes.append({'beat': quote, 'source_quote': quote, 'source_start': start,
                'source_end': start + len(quote), 'characters': [], 'wardrobe': {}, 'props': [],
                'location': 'Проход', 'time': '1963', 'shot': 'Wide',
                'continuity_before': '', 'continuity_after': '',
                'audit_source_quote': quote, 'audit_visual_direction': 'Show only this source action.',
                'audit_people': len(roles), 'audit_roles': roles,
                'audit_source_binding': 'narrated_event' if roles else 'architecture',
                'audit_time_layer': 'documented_event'})
        self.service.import_analysis(self.project['id'], {'world': '1963', 'characters': [],
            'locations': [], 'props': [], 'scenes': scenes}, self.project['version'])
        self.scenes = self.service.detail(self.project['id'])['scenes']
        image = io.BytesIO()
        Image.new('RGB', (1024, 576), '#526273').save(image, 'PNG')
        self.origin = self.service.upload_scene_image(self.scenes[0]['id'], image.getvalue())
        self.generation = self.service.scene(self.scenes[0]['id'])['active_generation_id']

    def pin(self, targets=None, **overrides):
        targets = targets or [self.scenes[0]['id'], self.scenes[1]['id']]
        body = {'generation_id': self.generation, 'scene_ids': targets, 'name': 'Хозяин дома',
            'appearance': 'Short grey hair, brown work jacket, light shirt, brown boots.',
            'revisions': {sid: self.service.scene(sid)['current_revision'] for sid in targets}, **overrides}
        return self.app.dispatch('POST', f"/api/projects/{self.project['id']}/pin-participant", body)

    def test_same_person_reference_reaches_snapshot_and_flow_without_extra_person(self):
        result = self.pin()
        target = self.service.scene(self.scenes[1]['id'])
        snapshot = self.service.scene_snapshot(target)
        self.assertEqual(snapshot['expected_people'], 1)
        self.assertEqual(snapshot['references'][0]['asset_id'], result['asset_id'])
        self.assertEqual(snapshot['references'][0]['scope'], 'participant_identity')
        self.assertNotIn('no fixed identity reference is available', snapshot['prompt'])
        payload = self.service.project_prompts(self.project['id'], include_all_scenes=True)
        flow = next(s for s in payload['scenes'] if s['scene_id'] == target['id'])
        prefix = flow['flow_prompt'][:2200]
        self.assertIn('SAME person', prefix)
        self.assertIn('brown work jacket', prefix)
        self.assertIn(target['data']['source_quote'], prefix)
        self.assertIn('Visible living people: 1', prefix)
        self.assertEqual(flow['references'][0]['type'], 'character')
        self.assertEqual(flow['references'][0]['name'], 'Хозяин дома')

    def test_origin_approval_and_unrelated_empty_scene_are_not_revised(self):
        original = self.service.scene(self.scenes[0]['id'])
        empty = self.service.scene(self.scenes[2]['id'])
        self.pin()
        self.assertEqual(self.service.scene(original['id']), original)
        self.assertEqual(self.service.scene(empty['id']), empty)
        self.assertEqual(self.service.scene_snapshot(empty)['references'], [])

    def test_empty_target_rejects_entire_operation(self):
        before = self.service.scene(self.scenes[1]['id'])
        with self.assertRaises(DomainError):
            self.pin([before['id'], self.scenes[2]['id']])
        self.assertEqual(self.service.scene(before['id']), before)

    def test_stale_revision_rejected_before_pin(self):
        target = self.scenes[1]['id']
        with self.assertRaises(DomainError):
            self.pin(revisions={self.scenes[0]['id']: 1, target: 999})
        self.assertNotIn('participant_identity', self.service.scene(target)['data'])

    def test_changed_source_does_not_silently_keep_identity(self):
        self.pin()
        scene = self.service.scene(self.scenes[1]['id'])
        scene['data']['participant_identity']['source']['quote'] = 'Другой мужчина.'
        with self.assertRaisesRegex(DomainError, 'Текст сцены изменился'):
            self.service.scene_snapshot(scene)

    def test_missing_or_modified_identity_file_blocks_generation(self):
        self.pin()
        scene = self.service.scene(self.scenes[1]['id'])
        asset = self.service.db.one('SELECT * FROM assets WHERE id=?', (scene['data']['participant_identity']['asset_id'],))
        (self.service.root / asset['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(DomainError, 'Файл эталона'):
            self.service.scene_snapshot(scene)

    def test_repeated_pin_does_not_create_another_revision(self):
        self.pin()
        target = self.service.scene(self.scenes[1]['id'])
        self.pin()
        self.assertEqual(self.service.scene(target['id']), target)

    def test_reference_from_another_project_is_rejected(self):
        other = self.service.create_project({'title': 'Other', 'story': 'Другое место.',
            'style': 'documentary', 'mode': 'objects'})
        with self.assertRaises(DomainError):
            self.app.dispatch('POST', f"/api/projects/{other['id']}/pin-participant",
                {'generation_id': self.generation, 'scene_ids': [self.scenes[1]['id']]})


if __name__ == '__main__':
    unittest.main()

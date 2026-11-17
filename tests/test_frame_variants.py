import io
import json
import tempfile
import unittest
from PIL import Image
from imagegen.server import Application
from imagegen.domain import DomainError
from imagegen.worker import Worker


def png(color='#526880', width=1024, height=576):
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buf, format='PNG')
    return buf.getvalue()


def sample_analysis(count=2):
    quotes = ['Tester works in the lab.', 'Tester discovers a breakthrough.']
    return {
        'world': 'Test world',
        'characters': [{'name': 'Tester', 'description': 'Adult tester with blue coat', 'wardrobe': {}}],
        'locations': [{'name': 'Lab', 'description': 'Clean laboratory setting'}],
        'props': [],
        'scenes': [
            {'beat': f'Event {i}', 'source_quote': quotes[i % len(quotes)], 'characters': ['Tester'],
             'wardrobe': {}, 'location': 'Lab', 'time': 'Day', 'shot': 'Wide',
             'continuity_before': '', 'continuity_after': ''}
            for i in range(count)
        ]
    }


class FrameVariantsAndPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Application(self.temp_dir.name)
        self.service = self.app.service
        self.service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v', 'image_backend': 'flow'})
        self.project = self.service.create_project({
            'title': 'Variants Project',
            'story': 'Tester works in the lab. Tester discovers a breakthrough.',
            'style': 'Cinematic photorealism',
            'mode': 'characters'
        })
        self.detail = self.service.import_analysis(self.project['id'], sample_analysis(2), self.project['version'])
        for char in self.detail['characters']:
            self.service.upload_character_portrait(char['id'], png('#aabbcc'), lock=True)
        worker = Worker(self.service)
        for loc in self.detail['locations']:
            gid = self.service.cast_location(loc['id'], 1)['generation_ids'][0]
            gen = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
            profile = json.loads(gen['snapshot'])['profile']
            worker.save_image(gen, png('#ddeeff', profile['width'], profile['height']))
            gen_updated = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
            self.service.lock_location(loc['id'], {'environment': gen_updated['asset_id']}, loc['revision'])
        self.scene1 = self.detail['scenes'][0]
        self.scene2 = self.detail['scenes'][1]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_successful_upload_becomes_active_generation(self):
        img1 = png('#112233')
        res = self.service.upload_scene_image(self.scene1['id'], img1)
        self.assertEqual(self.scene1['id'], res['scene_id'])

        detail = self.service.detail(self.project['id'])
        sc1 = next(s for s in detail['scenes'] if s['id'] == self.scene1['id'])
        self.assertEqual(res['generation_id'], sc1['active_generation_id'])
        self.assertEqual(1, len([g for g in detail['generations'] if g['scene_id'] == self.scene1['id'] and g['state'] == 'succeeded']))

    def test_subsequent_regeneration_creates_candidate_variant_without_changing_active(self):
        # First generation (becomes active)
        img1 = png('#112233')
        res1 = self.service.upload_scene_image(self.scene1['id'], img1)

        # User triggers regeneration -> creates queued proxy generation
        batch_res = self.service.batch(self.project['id'], 'test-idemp-1', [self.scene1['id']])
        detail = self.service.detail(self.project['id'])
        sc1 = next(s for s in detail['scenes'] if s['id'] == self.scene1['id'])
        self.assertEqual(res1['generation_id'], sc1['active_generation_id'])

        # Upload second image (regeneration result)
        img2 = png('#445566')
        res2 = self.service.upload_scene_image(self.scene1['id'], img2)
        self.assertNotEqual(res1['generation_id'], res2['generation_id'])
        self.assertNotEqual(res1['asset_id'], res2['asset_id'])

        # Verify: old generation remains active, new generation is saved as a succeeded candidate
        detail = self.service.detail(self.project['id'])
        sc1 = next(s for s in detail['scenes'] if s['id'] == self.scene1['id'])
        self.assertEqual(res1['generation_id'], sc1['active_generation_id'])

        variants = [g for g in detail['generations'] if g['scene_id'] == self.scene1['id'] and g['state'] == 'succeeded']
        self.assertEqual(2, len(variants))
        self.assertEqual({res1['generation_id'], res2['generation_id']}, {v['id'] for v in variants})

        # Select variant 2
        select_res = self.service.select_generation(res2['generation_id'])
        self.assertEqual(res2['generation_id'], select_res['selected'])

        detail_after_select = self.service.detail(self.project['id'])
        sc1_after = next(s for s in detail_after_select['scenes'] if s['id'] == self.scene1['id'])
        self.assertEqual(res2['generation_id'], sc1_after['active_generation_id'])

    def test_duplicate_upload_same_image_does_not_create_duplicate_variants(self):
        img1 = png('#112233')
        res1 = self.service.upload_scene_image(self.scene1['id'], img1)

        # Re-upload the same exact image (e.g. userscript retry)
        res_dup = self.service.upload_scene_image(self.scene1['id'], img1)
        self.assertEqual(res1['generation_id'], res_dup['generation_id'])
        self.assertEqual(res1['asset_id'], res_dup['asset_id'])

        detail = self.service.detail(self.project['id'])
        variants = [g for g in detail['generations'] if g['scene_id'] == self.scene1['id'] and g['state'] == 'succeeded']
        self.assertEqual(1, len(variants))

    def test_export_uses_selected_variant(self):
        img1 = png('#112233')
        res1 = self.service.upload_scene_image(self.scene1['id'], img1)
        img_scene2 = png('#778899')
        res_sc2 = self.service.upload_scene_image(self.scene2['id'], img_scene2)

        # Generate second variant for scene 1
        img2 = png('#445566')
        res2 = self.service.upload_scene_image(self.scene1['id'], img2)

        # Select variant 2
        self.service.select_generation(res2['generation_id'])

        # Export project archive
        export_zip = self.service.export(self.project['id'])
        import zipfile
        with zipfile.ZipFile(io.BytesIO(export_zip)) as zf:
            manifest_json = json.loads(zf.read('manifest.json'))
            sc1_manifest = next(s for s in manifest_json['scenes'] if s['scene']['id'] == self.scene1['id'])
            self.assertEqual(res2['generation_id'], sc1_manifest['generation']['id'])
            self.assertEqual(res2['asset_id'], sc1_manifest['asset']['id'])

    def test_video_staleness_detected_when_variant_changes(self):
        img1 = png('#112233')
        res1 = self.service.upload_scene_image(self.scene1['id'], img1)

        # Attach video to scene 1 pointing to res1 asset
        vid_bytes = b'fake-mp4-data'
        vid_res = self.service.upload_scene_video(
            self.scene1['id'], vid_bytes, filename='video.mp4',
            source_image_asset_id=res1['asset_id']
        )
        self.assertIsNotNone(vid_res.get('video_asset_id'))

        # Check detail: video is fresh
        detail = self.service.detail(self.project['id'])
        sc1 = next(s for s in detail['scenes'] if s['id'] == self.scene1['id'])
        self.assertFalse(sc1.get('video_stale', False))

        # Generate variant 2 and select it
        img2 = png('#445566')
        res2 = self.service.upload_scene_image(self.scene1['id'], img2)
        self.service.select_generation(res2['generation_id'])

        # Check detail: video is now marked stale because it belongs to variant 1
        detail2 = self.service.detail(self.project['id'])
        sc1_v2 = next(s for s in detail2['scenes'] if s['id'] == self.scene1['id'])
        self.assertTrue(sc1_v2.get('video_stale', True))


if __name__ == '__main__':
    unittest.main()

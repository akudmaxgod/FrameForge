"""Flow queue identity: repeated browser requests are not new variants."""
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from PIL import Image

from imagegen.service import Service
from tests.test_app import analysis, png


class FlowBatchDeduplicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.service = Service(temp.name)
        self.service.save_settings({'image_backend': 'flow', 'unet': 'u', 'clip': 'c', 'vae': 'v'})
        self.pid = self.service.create_project({'title': 'Flow', 'story': 'Event 0. Event 1.',
                                                'style': 'realism'})['id']
        self.service.import_analysis(self.pid, analysis(2, characters=False), 1)
        self.scenes = self.service.detail(self.pid)['scenes']

    def test_repeat_bulk_request_reuses_queue_after_first_frame_upload(self):
        batch = self.service.batch(self.pid, 'first-click')
        self.service.upload_scene_image(self.scenes[0]['id'], png())
        repeated = self.service.batch(self.pid, 'second-click')
        self.assertEqual(batch['id'], repeated['id'])
        jobs = self.service.db.all('SELECT * FROM generations WHERE purpose=\'scene\'')
        self.assertEqual(2, len(jobs))
        self.assertEqual(['queued', 'succeeded'], sorted(job['state'] for job in jobs))

    def test_concurrent_bulk_requests_with_different_keys_create_one_queue(self):
        barrier = threading.Barrier(2)
        with patch.object(self.service, 'review_source_plans', side_effect=lambda *args: barrier.wait(timeout=10)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                batches = list(pool.map(lambda key: self.service.batch(self.pid, key), ['tab-one', 'tab-two']))
        self.assertEqual(batches[0]['id'], batches[1]['id'])
        self.assertEqual(1, len(self.service.db.all('SELECT * FROM batches')))
        self.assertEqual(2, len(self.service.db.all('SELECT * FROM generations WHERE purpose=\'scene\'')))

    def test_completed_batch_allows_explicit_new_generation(self):
        first = self.service.batch(self.pid, 'first-click')
        for index, scene in enumerate(self.scenes):
            image = io.BytesIO()
            Image.new('RGB', (1024, 576), (32 + index * 40, 76, 123)).save(image, format='PNG')
            self.service.upload_scene_image(scene['id'], image.getvalue())
        self.assertEqual('completed', self.service.db.one('SELECT state FROM batches WHERE id=?', (first['id'],))['state'])
        second = self.service.batch(self.pid, 'new-variants')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(4, len(self.service.db.all('SELECT * FROM generations WHERE purpose=\'scene\'')))

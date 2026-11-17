"""Actual API/worker image persistence when optional checks fail; no cloud calls."""
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from imagegen.db import dump, now, uid
from imagegen.domain import DomainError
from imagegen.server import Application
from imagegen.forgotten_cities.retries import RetryStore
from imagegen.worker import ContentRejectedError


class AdvisoryImageReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Application(temp.name)
        self.service = self.app.service
        self.project = self.service.create_project({'title': 'Advisory', 'story': 'A buried woman.',
                                                    'style': 'documentary', 'mode': 'objects'})
        self.sid = uid()
        self.snapshot = {'project_id': self.project['id'], 'scene_id': self.sid, 'scene_revision': 1,
                         'references': [], 'profile': {'width': 1024, 'height': 576},
                         'scene': {'forgotten_cities_prompt_contract': {'source_binding': 'buried_remains'}}}
        with self.service.db.transaction() as conn:
            conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (self.sid, self.project['id'], 1))
            conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (self.sid, 1, dump({}), now()))
            self.gid = self.service.insert_generation(conn, self.project['id'], 'scene', self.snapshot,
                                                       scene_id=self.sid, scene_revision=1)
            self.next_id = self.service.insert_generation(conn, self.project['id'], 'prop', {})
        image = io.BytesIO()
        Image.new('RGB', (1024, 576), '#987654').save(image, 'PNG')
        self.pixels = image.getvalue()

    def assert_saved_and_queue_continues(self):
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (self.gid,))
        self.assertEqual(generation['state'], 'succeeded')
        asset = self.service.asset(generation['asset_id'])
        self.assertTrue((self.service.root / asset['path']).is_file())
        self.assertEqual(json.loads(generation['snapshot'])['source_fidelity_review']['status'], 'unverified')
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?', (self.next_id,))['state'], 'queued')
        self.assertEqual(RetryStore(self.service.db).get(self.snapshot), {})

    def test_flow_api_upload_saves_on_quota_or_uncertainty_and_survives_reload(self):
        for result in (DomainError('quota', 429), {'status': 'uncertain', 'reason': 'Subject is unclear'}):
            options = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with patch.object(self.service, 'scene_snapshot', return_value=dict(self.snapshot)), \
                 patch.object(self.app.worker, '_validate_external_scene_image_strict', side_effect=ModuleNotFoundError('ultralytics')), \
                 patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.review_image', **options):
                self.app.dispatch('POST', f'/api/scenes/{self.sid}/upload', {'image': self.pixels})
            self.assert_saved_and_queue_continues()
        reopened = Application(str(self.service.root))
        snapshot = json.loads(reopened.service.db.one('SELECT snapshot FROM generations WHERE id=?', (self.gid,))['snapshot'])
        self.assertEqual(snapshot['quality_review']['status'], 'unverified')

    def test_worker_saves_on_detector_and_reviewer_failure(self):
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (self.gid,))
        with patch.object(self.app.worker, '_validate_generated_composition', side_effect=ContentRejectedError('false positive')), \
             patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.review_image', side_effect=DomainError('offline', 503)):
            self.app.worker.save_image(generation, self.pixels)
        self.assert_saved_and_queue_continues()

    def test_reference_skips_content_detectors_and_remote_review(self):
        snapshot = {**self.snapshot, 'generation_role': 'reference'}
        with self.service.db.transaction() as conn:
            gid = self.service.insert_generation(conn, self.project['id'], 'prop', snapshot)
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        with patch.object(self.app.worker, '_validate_generated_composition', side_effect=AssertionError('must not check references')) as detector, \
             patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.review_image', side_effect=AssertionError('must not review references')) as reviewer:
            self.app.worker.save_image(generation, self.pixels)
        detector.assert_not_called()
        reviewer.assert_not_called()
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?', (gid,))['state'], 'succeeded')

    def test_forgotten_cities_does_not_run_rigid_people_detector(self):
        snapshot = {**self.snapshot, 'expected_people': 1}
        with patch.object(self.app.worker, '_has_panel_grid', return_value=False), \
             patch.object(self.app.worker, '_has_embedded_frame_or_matte', return_value=False), \
             patch.object(self.app.worker, '_has_embedded_text', return_value=False), \
             patch.object(self.app.worker, '_detected_face_count', side_effect=AssertionError('unnecessary detector')) as faces:
            self.app.worker.validate_external_scene_image(snapshot, self.pixels)
            image = Image.open(io.BytesIO(self.pixels))
            self.app.worker._validate_generated_composition({'purpose': 'scene'}, snapshot, image)
        faces.assert_not_called()

    def test_invalid_dimensions_still_block_saving(self):
        image = io.BytesIO()
        Image.new('RGB', (32, 32)).save(image, 'PNG')
        with self.assertRaises(DomainError):
            self.app.worker.save_image(self.service.db.one('SELECT * FROM generations WHERE id=?', (self.gid,)), image.getvalue())
        self.assertEqual(self.service.db.all('SELECT id FROM assets'), [])

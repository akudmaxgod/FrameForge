import json
import unittest
from unittest.mock import patch
from imagegen.worker import ContentRejectedError
from tests.test_population_evidence import PopulationPipelineTests


class SceneIdentityAdvisoryTests(unittest.TestCase):
    setUp = PopulationPipelineTests.setUp

    def test_low_similarity_passes_both_scene_paths_with_same_warning(self):
        self.worker._identity_similarity.return_value = .01
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        review = dict(self.snapshot['identity_review'])
        self.worker._validate_generated_composition({'purpose': 'scene'}, self.snapshot, self.image)
        self.assertEqual(self.snapshot['identity_review'], review)
        self.assertEqual(review['status'], 'needs_review')
        self.assertEqual(review['similarity'], .01)
        self.assertEqual(review['enforcement'], 'advisory')

    def test_two_human_references_are_also_advisory(self):
        self.snapshot['references'].append({'role': 'portrait', 'entity_type': 'human', 'asset_id': 'second'})
        with patch.object(self.worker, '_identity_assignment_similarity', return_value=.02) as compare:
            self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        compare.assert_called_once()
        self.assertEqual(self.snapshot['identity_review']['human_references'], 2)
        self.assertEqual(self.snapshot['identity_review']['status'], 'needs_review')

    def test_missing_small_face_and_invalid_measurements_pass_as_unverified(self):
        for small in (True, False):
            for value in (None, float('nan'), float('inf')):
                self.worker._identity_similarity.return_value = value
                self.worker._identity_face_too_small.return_value = small
                self.worker.validate_external_scene_image(self.snapshot, self.pixels)
                review = self.snapshot['identity_review']
                self.assertEqual(review['status'], 'unverified')
                self.assertIsNone(review['similarity'])
                json.dumps(review, allow_nan=False)

    def test_unavailable_identity_model_does_not_stop_saving(self):
        self.worker._identity_similarity.side_effect = RuntimeError('model unavailable')
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        self.assertEqual(self.snapshot['identity_review']['status'], 'unverified')
        self.worker._validate_generated_composition({'purpose': 'scene'}, self.snapshot, self.image)
        self.assertEqual(self.snapshot['identity_review']['status'], 'unverified')

    def test_high_similarity_is_not_flagged(self):
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        self.assertEqual(self.snapshot['identity_review']['status'], 'not_flagged')

    def test_flow_upload_persists_low_similarity_and_is_idempotent(self):
        self.worker._identity_similarity.return_value = .01
        with patch.object(self.service, 'scene_snapshot', side_effect=lambda *a, **k: dict(self.snapshot)):
            results = [self.service.upload_scene_image(self.scene['id'], self.pixels,
                         image_validator=self.worker.validate_external_scene_image) for _ in range(2)]
        self.assertEqual(results[0]['generation_id'], results[1]['generation_id'])
        row = self.service.db.one('SELECT * FROM generations WHERE id=?', (results[0]['generation_id'],))
        self.assertEqual(row['state'], 'succeeded')
        self.assertEqual(json.loads(row['snapshot'])['identity_review']['status'], 'needs_review')
        self.assertEqual(self.service.scene(self.scene['id'])['active_generation_id'], row['id'])

    def test_builtin_save_persists_low_similarity_without_retry(self):
        self.worker._identity_similarity.return_value = .01
        with self.service.db.transaction() as db:
            gid = self.service.insert_generation(db, self.pid, 'scene', self.snapshot,
                        scene_id=self.scene['id'], scene_revision=self.scene['current_revision'])
        self.worker.save_image(self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,)), self.pixels)
        row = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        self.assertEqual(row['state'], 'succeeded')
        self.assertEqual(json.loads(row['snapshot'])['identity_review']['status'], 'needs_review')
        self.assertFalse(self.service.db.one("SELECT id FROM generations WHERE purpose='scene' AND state='queued'"))


if __name__ == '__main__':
    unittest.main()

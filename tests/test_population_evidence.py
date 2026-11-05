import copy
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image
from imagegen.composition_review import record_population_review
from imagegen.domain import compile_prompt, compile_flow_prompt, people_count_unresolved
from imagegen.script_analysis.storyboard import plan, VERSION
from imagegen.worker import Worker, ContentRejectedError, DetectorUnavailableError
from tests import test_veo_animation as fixtures


class PopulationContractTests(unittest.TestCase):
    def draft(self, quote):
        return {'source_quote': quote, 'beat': quote, 'characters': ['Arin'],
                'visible_people': 1, 'population_policy': 'scene_participants',
                'storyboard_frame': {'version': 'text-storyboard/2',
                                     'interpretation_status': 'source_bound_draft'}}

    def test_named_registry_is_not_total_population_across_genres(self):
        for quote in ('Арин тренируется с новыми товарищами.',
                      'Arin teaches the students in the classroom.',
                      'Arin examines several patients.', 'Arin walks alone.'):
            # Even "alone" is not parsed by counting names. The full source is
            # retained for rendering; this partitioner makes no semantic claim.
            with self.subTest(quote=quote):
                data = self.draft(quote)
                before = copy.deepcopy(data)
                self.assertTrue(people_count_unresolved(data))
                hero = {'name': 'Arin', 'description': 'A brown-haired man.',
                        'locked': False, 'references': {}}
                local, _ = compile_prompt({'mode': 'dynamic', 'style': 'Cinematic'}, data, [hero], allow_unlocked=True)
                flow = compile_flow_prompt({'mode': 'dynamic', 'style': 'Cinematic'}, data, [hero])
                for prompt in (local, flow):
                    self.assertIn(quote, prompt)
                    self.assertNotIn('Strict person contract: exactly', prompt)
                    self.assertNotIn('Exactly 1 visible human subject', prompt)
                    self.assertNotIn('Solo shot, exactly 1', prompt)
                    self.assertIn('no verified exact census', prompt)
                self.assertEqual(data, before)

    def test_reviewed_composition_and_object_only_keep_explicit_contract(self):
        for changes in ({'population_policy': 'object_only'},
                        {'population_policy': 'source_reviewed_participants'},
                        {'visual_content': {'version': 'story-visual-plan/1'}},
                        {'participant_identity': {'asset_id': 'fixed-actor'}}):
            self.assertFalse(people_count_unresolved({**self.draft('Source.'), **changes}))

    def test_new_projection_records_count_provenance_and_covers_source(self):
        story = 'Arin teaches several new students. Arin checks their work.'
        result = plan({'characters': [{'id': 'a', 'name': 'Arin', 'description': 'Teacher.'}],
                       'locations': [], 'scenes': [{'beat': story, 'characters': ['Arin']}]}, story, 2)
        self.assertEqual(VERSION, 'text-storyboard/4')
        for data in result['scenes']:
            self.assertEqual(data['population_count_basis'], 'named_identity_registry')
            self.assertTrue(people_count_unresolved(data))
            self.assertEqual(data['source_quote'], story[data['source_start']:data['source_end']])


class PopulationEvidenceTests(unittest.TestCase):
    def test_agreeing_detectors_are_not_proof_of_living_extra_people(self):
        for counts in ({'faces': 4, 'people': 3, 'hands': 8},
                       {'faces': 2, 'people': 2, 'hands': 4}):
            snapshot = {'expected_people': 1}
            review = record_population_review(snapshot, counts, method='test')
            self.assertEqual(review['status'], 'needs_review')
            self.assertEqual(review['detected'], counts)
            self.assertFalse(review['confirmed_mismatch'])
            self.assertEqual(review['enforcement'], 'advisory')

    def test_missing_person_and_extra_hands_remain_visible_warnings(self):
        for counts, flag in (({'faces': 0, 'people': 0}, 'people_not_confirmed'),
                             ({'faces': 1, 'people': 1, 'hands': 6}, 'possible_extra_hands')):
            review = record_population_review({'expected_people': 1}, counts, method='test')
            self.assertEqual(review['status'], 'needs_review')
            self.assertIn(flag, review['flags'])

    def test_crop_and_back_view_are_not_false_missing_people(self):
        review = record_population_review({'expected_people': 2}, {'faces': 0, 'people': 2}, method='test')
        self.assertEqual(review['status'], 'not_flagged')

    def test_open_census_and_detector_failure_never_become_verified_pass(self):
        review = record_population_review({'expected_people': 1, 'people_count_unresolved': True},
                                          {'faces': 9, 'people': 9}, method='test')
        self.assertEqual(review['status'], 'not_applicable')
        review = record_population_review({'expected_people': 1}, {'faces': None, 'people': None},
                                          method='test', errors=['DetectorUnavailableError'])
        self.assertEqual(review['status'], 'unverified')
        self.assertIsNone(review['detected']['faces'])


class PopulationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.pid = self.fixture._create_sample_project(1)
        self.scene = self.service.detail(self.pid)['scenes'][0]
        self.worker = Worker(self.service, runtime=object())
        self.image = Image.new('RGB', (1024, 576), '#687279')
        buf = io.BytesIO()
        self.image.save(buf, format='PNG')
        self.pixels = buf.getvalue()
        self.snapshot = {'profile': {'width': 1024, 'height': 576}, 'expected_people': 1,
                         'style': 'Cinematic', 'project_id': self.pid,
                         'scene_id': self.scene['id'], 'scene_revision': self.scene['current_revision'],
                         'references': [{'role': 'portrait', 'entity_type': 'human', 'asset_id': 'hero'}]}
        for name, result in (('_has_panel_grid', False), ('_has_embedded_text', False),
                             ('_validate_matte', None), ('_detected_face_count', 4),
                             ('_detected_person_count', 3), ('_detected_hand_count', 8),
                             ('_identity_similarity', .9), ('_identity_face_too_small', False)):
            self.enterContext(patch.object(self.worker, name, return_value=result))

    def test_flow_and_local_generation_share_same_evidence_policy(self):
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        flow_review = copy.deepcopy(self.snapshot['composition_review'])
        self.worker._validate_generated_composition({'purpose': 'scene'}, self.snapshot, self.image)
        self.assertEqual(flow_review, self.snapshot['composition_review'])
        self.assertEqual(self.worker._identity_similarity.call_count, 2)

    def test_upload_saves_review_and_does_not_queue_regeneration(self):
        with patch.object(self.service, 'scene_snapshot', return_value=self.snapshot):
            result = self.service.upload_scene_image(self.scene['id'], self.pixels,
                                                      image_validator=self.worker.validate_external_scene_image)
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (result['generation_id'],))
        self.assertEqual(generation['state'], 'succeeded')
        saved = json.loads(generation['snapshot'])
        self.assertEqual(saved['composition_review']['status'], 'needs_review')
        self.assertEqual(saved['composition_review']['detected']['people'], 3)
        self.assertTrue((self.service.root / self.service.asset(result['asset_id'])['path']).is_file())
        self.assertFalse(self.service.db.one("SELECT id FROM generations WHERE purpose='scene' AND state='queued'"))
        self.assertEqual(self.service.scene(self.scene['id'])['current_revision'], self.scene['current_revision'])

    def test_identity_is_advisory_and_invalid_files_remain_blocking(self):
        self.worker._identity_similarity.return_value = .01
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        self.assertEqual(self.snapshot['identity_review']['status'], 'needs_review')
        self.worker._validate_generated_composition({'purpose': 'scene'}, self.snapshot, self.image)
        self.assertEqual(self.snapshot['identity_review']['status'], 'needs_review')
        with self.assertRaises(OSError):
            self.worker.validate_external_scene_image(self.snapshot, self.pixels[:30])

    def test_builtin_saving_keeps_warning_without_retry_or_source_mutation(self):
        with self.service.db.transaction() as db:
            gid = self.service.insert_generation(db, self.pid, 'scene', self.snapshot,
                                                  scene_id=self.scene['id'],
                                                  scene_revision=self.scene['current_revision'])
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        self.worker.save_image(generation, self.pixels)
        saved = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        self.assertEqual(saved['state'], 'succeeded')
        self.assertEqual(json.loads(saved['snapshot'])['composition_review']['status'], 'needs_review')
        self.assertFalse(self.service.db.one("SELECT id FROM generations WHERE purpose='scene' AND state='queued'"))
        self.assertEqual(self.service.scene(self.scene['id'])['current_revision'], self.scene['current_revision'])

    def test_isolated_portrait_still_requires_one_face(self):
        with self.assertRaisesRegex(ContentRejectedError, 'ровно одно лицо'):
            self.worker._validate_generated_composition({'purpose': 'casting'}, self.snapshot, self.image)

    def test_unavailable_counts_are_not_zero_and_identity_still_runs(self):
        self.worker._detected_face_count.side_effect = DetectorUnavailableError('offline')
        self.worker.validate_external_scene_image(self.snapshot, self.pixels)
        review = self.snapshot['composition_review']
        self.assertIsNone(review['detected']['faces'])
        self.worker._identity_similarity.assert_called_once()


if __name__ == '__main__':
    unittest.main()

import json
import tempfile
import unittest
from unittest.mock import patch
from imagegen.service import Service
from imagegen.db import dump, now, uid
from imagegen.domain import DomainError, SourceImageRejected
from imagegen.forgotten_cities.retries import RetryStore, retry_prompt
from imagegen.worker import Worker


class SemanticRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.pid = self.service.create_project({'title': 'Retry test', 'story': 'A woman was found buried.',
                                               'style': 'documentary', 'mode': 'objects'})['id']
        self.sid = uid()
        with self.service.db.transaction() as conn:
            conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (self.sid, self.pid, 1))
            conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (self.sid, 1, dump({}), now()))
        self.snapshot = {'project_id': self.pid, 'scene_id': self.sid, 'scene_revision': 1,
                         'story_hash': 'story', 'references': [], 'prompt': 'BASE SOURCE PROMPT',
                         'scene': {'forgotten_cities_prompt_contract': {'source_binding': 'buried_remains'}}}

    def verdict(self, image='a', status='fail'):
        return {'status': status, 'image_sha256': image, 'observed': 'living woman',
                'reason': 'Source requires a burial.', 'correction': 'Show deceased remains in the burial.'}

    def reject(self, image='a'):
        with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.review_image', return_value=self.verdict(image)):
            with self.assertRaises(SourceImageRejected) as caught:
                self.service.validate_scene_source_image(self.snapshot, b'pixels')
        return caught.exception

    def test_duplicate_upload_reload_and_scene_context_budget(self):
        first = self.reject().details['retry']
        self.assertEqual(first['attempts'], 1)
        self.assertEqual(self.reject().details['retry']['attempts'], 1)
        reopened = Service(self.temp.name)
        self.assertEqual(RetryStore(reopened.db).get(self.snapshot)['attempts'], 1)
        self.assertIn('Show deceased remains', retry_prompt('BASE', first))
        changed = {**self.snapshot, 'scene_revision': 2}
        self.assertEqual(RetryStore(reopened.db).get(changed), {})
        self.assertEqual(RetryStore(reopened.db).get({**self.snapshot, 'scene_id': 'other'}), {})

    def test_third_rejection_fails_only_selected_queued_scene(self):
        with self.service.db.transaction() as conn:
            target = self.service.insert_generation(conn, self.pid, 'scene', self.snapshot, scene_id=self.sid, scene_revision=1)
            other = self.service.insert_generation(conn, self.pid, 'prop', {}, prop_id=None)
        self.assertFalse(self.reject('a').details['retry']['exhausted'])
        self.assertFalse(self.reject('b').details['retry']['exhausted'])
        self.assertTrue(self.reject('c').details['retry']['exhausted'])
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?', (target,))['state'], 'failed')
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?', (other,))['state'], 'queued')
        self.assertEqual(self.reject('d').details['retry']['attempts'], 3)

    def test_new_request_with_identical_pixels_is_bounded_but_reupload_is_free(self):
        self.snapshot['fidelity_candidate_id'] = 'request-1'
        self.assertEqual(self.reject('same-pixels').details['retry']['attempts'], 1)
        self.assertEqual(self.reject('same-pixels').details['retry']['attempts'], 1)
        for n in (2, 3):
            self.snapshot['fidelity_candidate_id'] = f'request-{n}'
            self.assertEqual(self.reject('same-pixels').details['retry']['attempts'], n)
        self.assertTrue(RetryStore(self.service.db).get(self.snapshot)['exhausted'])

    def test_uncertainty_and_quota_do_not_consume_attempt_or_trigger_retry(self):
        for result in (self.verdict(status='uncertain'), DomainError('Лимит Gemini', 429)):
            options = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.review_image', **options):
                result = self.service.validate_scene_source_image(self.snapshot, b'pixels')
                self.assertEqual(result['status'], 'unverified')
                self.assertEqual(self.snapshot['source_fidelity_review'], result)
        self.assertEqual(RetryStore(self.service.db).get(self.snapshot), {})

    def test_rejected_images_and_reasons_are_archived_without_new_jobs(self):
        self.reject()
        files = list(self.service.root.glob('fidelity-rejected/*/*.png'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b'pixels')
        record = json.loads(files[0].with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(record['scene_id'], self.sid)
        self.assertEqual(record['verdict']['status'], 'fail')
        self.assertEqual(self.service.db.all('SELECT id FROM generations'), [])

    def test_worker_reuses_job_with_corrected_prompt_then_stops(self):
        with self.service.db.transaction() as conn:
            gid = self.service.insert_generation(conn, self.pid, 'scene', self.snapshot, scene_id=self.sid, scene_revision=1)
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        worker = Worker(self.service)
        error = self.reject()
        corrected = {**self.snapshot, 'prompt': retry_prompt('BASE', error.details['retry'])}
        with patch.object(self.service, 'scene_snapshot', return_value=corrected):
            worker._retry_source_image(generation, error)
        saved = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        self.assertEqual(saved['state'], 'queued')
        self.assertIn('Show deceased remains', json.loads(saved['snapshot'])['prompt'])
        self.assertEqual(len(self.service.db.all('SELECT id FROM generations')), 1)
        self.reject('b')
        worker._retry_source_image(saved, self.reject('c'))
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?', (gid,))['state'], 'failed')

    def test_batch_replay_keeps_budget_and_explicit_new_single_request_resets_it(self):
        def snapshot(*args, **kwargs):
            retry = RetryStore(self.service.db).get(self.snapshot)
            return {**self.snapshot, 'source_fidelity_retry': retry, 'prompt': retry_prompt('BASE', retry)}
        with patch.object(self.service, 'project_preflight', return_value={'ok': True}), \
             patch.object(self.service, 'review_source_plans', return_value={}), \
             patch.object(self.service, 'scene_snapshot', side_effect=snapshot):
            first = self.service.batch(self.pid, 'original-request', [self.sid])
            for image in ('a', 'b', 'c'):
                self.reject(image)
            self.assertEqual(self.service.batch(self.pid, 'original-request', [self.sid])['id'], first['id'])
            self.assertTrue(RetryStore(self.service.db).get(self.snapshot)['exhausted'])
            second = self.service.batch(self.pid, 'explicit-new-request', [self.sid])
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(RetryStore(self.service.db).get(self.snapshot), {})


if __name__ == '__main__':
    unittest.main()

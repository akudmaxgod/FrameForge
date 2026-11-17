import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from imagegen.domain import DomainError
from imagegen.forgotten_cities.fidelity import SourceFidelityReviewer, apply_review


class FidelityReviewerTests(unittest.TestCase):
    def test_prepared_analysis_survives_check_failure_and_resumes_without_text_analysis(self):
        from imagegen.worker import Worker
        worker = Worker.__new__(Worker)
        worker.service = MagicMock()
        worker.db = MagicMock()
        worker.db.one.return_value = {'profile_type': 'forgotten_cities'}
        worker.service.project.return_value = {'channel_profile_id': 'channel', 'story': 'A bead.'}
        worker.service.settings.return_value = {'google_api_key': 'test'}
        worker.service.root = tempfile.gettempdir()
        worker.update = MagicMock()
        analysis = {'scenes': [{'source_quote': 'A bead.'}], 'characters': []}
        snapshot = {'project': {'version': 1}, 'profile': {'width': 1024, 'height': 576}}
        generation = {'id': 'analysis', 'project_id': 'project', 'purpose': 'analysis', 'snapshot': json.dumps(snapshot)}
        with patch.object(SourceFidelityReviewer, 'review_plans', side_effect=DomainError('check failed', 503)):
            with self.assertRaises(DomainError):
                worker._save_analysis(generation, analysis, 1)
        saved = worker.update.call_args.kwargs['snapshot']
        self.assertEqual(saved['pending_analysis'], analysis)
        worker._save_analysis = MagicMock()
        worker.prepare({**generation, 'snapshot': json.dumps(saved)})
        worker._save_analysis.assert_called_once_with(
            {**generation, 'snapshot': json.dumps(saved)}, analysis, 1)

    def test_invalid_record_is_repaired_alone_and_other_records_are_cached(self):
        quotes = ['Found a bead.', 'Found a skull.', 'Found a vessel.']
        story = ' '.join(quotes)
        scenes = [{'source_quote': q} for q in quotes]
        batches = []
        def request(prompt):
            rows = json.loads(prompt.split('Data follows:\n')[1])['scenes']
            batches.append([r['quote'] for r in rows])
            entries = [dict(key=r['key'], status='pass', subject='artifact', subject_state='artifact',
                expected_people=0, roles=[], required_artifacts=[], forbidden=[], time_layer='excavation',
                visual_direction='Close-up of the stated find.', reason='The quote states the object.') for r in rows]
            if len(batches) == 1:
                entries[1]['time_layer'] = 'wrong-enum'
            return {'scenes': entries}
        with tempfile.TemporaryDirectory() as root:
            reviewer = SourceFidelityReviewer(root, {}, request)
            self.assertEqual(len(reviewer.review_plans(scenes, story, [])), 3)
            self.assertEqual(batches, [quotes, [quotes[1]]])
            reviewer.request = lambda *args: self.fail('Successful plans should be reused')
            self.assertEqual(len(reviewer.review_plans(scenes, story, [])), 3)

    def test_cosmetic_format_variants_are_normalized_without_losing_constraints(self):
        entry = dict(status=' PASS ', subject='bead', subject_state=' ARTIFACT ', expected_people='0',
            roles=[], required_artifacts=['  bead '], forbidden=[], time_layer='museum-display',
            visual_direction='Show the bead.', reason='Source mentions a bead.')
        result = SourceFidelityReviewer._normalize_plan(entry, {'Bead'})
        SourceFidelityReviewer._validate_plan(result, {'Bead'})
        self.assertEqual(result['required_artifacts'], ['Bead'])
        entry['required_artifacts'] = ['Invented artifact']
        with self.assertRaises(DomainError):
            SourceFidelityReviewer._validate_plan(SourceFidelityReviewer._normalize_plan(entry, {'Bead'}), {'Bead'})

    def test_real_request_declares_required_schema_fields(self):
        inputs = {'scenes': [{'key': 'one'}], 'anchors': [{'canonical_name': 'Eye'}]}
        captured = []
        def response(*args, **kwargs):
            captured.append(args[1])
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"scenes":[]}'}]}}]}
        with tempfile.TemporaryDirectory() as root, patch('imagegen.forgotten_cities.fidelity.GeminiKeyPool.json', side_effect=response):
            SourceFidelityReviewer(root, {'google_api_key': 'test-key'})._request('Data follows:\n' + json.dumps(inputs))
        schema = captured[0]['generationConfig']['responseJsonSchema']['properties']['scenes']['items']
        self.assertIn('expected_people', schema['required'])
        self.assertEqual(schema['properties']['required_artifacts']['items']['enum'], ['Eye'])
        self.assertEqual(schema['properties']['key']['enum'], ['one'])

    def test_every_scene_is_reviewed_and_cache_survives_reload(self):
        quotes = [f'Предмет номер {i} найден в этом месте.' for i in range(17)]
        story = ' '.join(quotes)
        scenes = [{'source_quote': q, 'source_start': story.index(q)} for q in quotes]
        seen = []
        def request(prompt):
            batch = json.loads(prompt.split('Data follows:\n')[1])['scenes']
            seen.extend(row['quote'] for row in batch)
            return {'scenes': [dict(key=row['key'], status='pass', subject='археологический предмет',
                subject_state='artifact', expected_people=0, roles=[], required_artifacts=[], forbidden=['живой портрет'],
                time_layer='museum_display', visual_direction='Крупный план находки.', reason='Предмет указан в цитате.')
                for row in batch]}
        with tempfile.TemporaryDirectory() as root:
            reviewer = SourceFidelityReviewer(root, {}, request)
            self.assertEqual(len(reviewer.review_plans(scenes, story, [])), 17)
            self.assertEqual(seen, quotes)
            seen.clear()
            reloaded = SourceFidelityReviewer(root, {}, request)
            self.assertEqual(len(reloaded.review_plans(scenes, story, [])), 17)
            self.assertEqual(seen, [])
            changed_story = story + ' Новые сведения меняют контекст.'
            reloaded.review_plans(scenes, changed_story, [])
            self.assertEqual(len(seen), 17)

    def test_partial_plan_coverage_and_uncertainty_do_not_pass(self):
        scene = {'source_quote': 'Найдена бусина.'}
        with tempfile.TemporaryDirectory() as root:
            reviewer = SourceFidelityReviewer(root, {}, lambda prompt: {'scenes': []})
            with self.assertRaisesRegex(DomainError, 'не покрыла все'):
                reviewer.review_plans([scene], scene['source_quote'], [])
            with self.assertRaisesRegex(DomainError, 'требует уточнения'):
                apply_review({}, {'status': 'uncertain', 'reason': 'Неясно, к чему относится местоимение.'})

    def test_pixel_review_is_bound_to_actual_image_and_source(self):
        scene = {'source_quote': 'В погребении нашли женщину.'}
        snapshot = {'scene': scene}
        calls = []
        def request(prompt, images):
            calls.append((prompt, images))
            return {'status': 'fail', 'observed': 'Живая женщина.', 'reason': 'В тексте погребение.',
                    'correction': 'Показать останки в погребении.'}
        with tempfile.TemporaryDirectory() as root:
            reviewer = SourceFidelityReviewer(root, {}, request)
            for _ in range(2):
                verdict = reviewer.review_image(snapshot, b'image1', scene['source_quote'])
                self.assertEqual(verdict['status'], 'fail')
            self.assertEqual(len(calls), 1)
            reviewer.review_image(snapshot, b'image2', scene['source_quote'])
            self.assertEqual(len(calls), 2)
            self.assertIn('actual visible pixels', calls[0][0])
            self.assertEqual(calls[0][1][0], b'image1')
            reviewer.request = lambda *args: {'status': 'pass'}
            with self.assertRaisesRegex(DomainError, 'некорректный ответ'):
                reviewer.review_image(snapshot, b'image3', scene['source_quote'])
            unavailable = SourceFidelityReviewer(root, {})
            with self.assertRaisesRegex(DomainError, 'API-ключ'):
                unavailable.review_image(snapshot, b'image4', scene['source_quote'])


if __name__ == '__main__':
    unittest.main()

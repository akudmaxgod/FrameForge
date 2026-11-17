import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from imagegen.script_analysis.cache import DiskCache
from imagegen.script_analysis.compact_staging import Staging, stage
from imagegen.script_analysis.errors import AnalysisError
from imagegen.domain import extract_scene_episodic_roles, compile_flow_prompt


class CompactStagingTests(unittest.TestCase):
    def fixture(self):
        source = 'Король узнал, что Дейенерис ждёт ребёнка.'
        return {'analyzer_version': 'deepseek', 'characters': [
            {'id': 'ch_1', 'name': 'Роберт'}, {'id': 'ch_2', 'name': 'Нед'},
            {'id': 'ch_3', 'name': 'Дейенерис'}],
            'scenes': [{'source_quote': source, 'source_start': 0, 'source_end': len(source),
                        'beat': source, 'characters': ['Дейенерис'], 'character_ids': ['ch_3'],
                        'episodic_people': ['ребёнок']}],
            'diagnostics': {}}, source

    def response(self):
        return Staging.model_validate({'episodes': [{'place': 'Малый совет', 'time': 'До смерти Роберта',
            'state': 'Роберт и Нед — взрослые', 'kind': 'report',
            'shots': [{'n': 1, 'cast': ['ch_1', 'ch_2'], 'roles': [],
                       'action': 'Роберт сообщает новость Неду, Нед внимательно слушает.'}]}]})

    def test_staging_preserves_source_and_removes_reported_people(self):
        analysis, source = self.fixture()
        before = copy.deepcopy(analysis)
        with TemporaryDirectory() as temp:
            client = Mock()
            client.ask_json.return_value = self.response()
            result = stage(analysis, source, client=client, cache=DiskCache(Path(temp)))
            shot = result['scenes'][0]
            self.assertEqual(analysis, before)
            self.assertEqual(shot['source_quote'], source)
            self.assertEqual(shot['characters'], ['Роберт', 'Нед'])
            self.assertEqual(extract_scene_episodic_roles(shot), [])
            prompt = compile_flow_prompt({}, shot, result['characters'][:2])
            self.assertNotIn('Дейенерис', prompt)
            self.assertNotIn('ребён', prompt)
            client.ask_json.reset_mock()
            again = stage(analysis, source, client=client, cache=DiskCache(Path(temp)))
            self.assertEqual(again['scenes'], result['scenes'])
            client.ask_json.assert_not_called()

    def test_bad_mapping_is_not_cached_or_applied(self):
        analysis, source = self.fixture()
        response = self.response()
        response.episodes[0].shots[0].n = 2
        client = Mock()
        client.ask_json.return_value = response
        with TemporaryDirectory() as temp:
            with self.assertRaises(AnalysisError):
                stage(analysis, source, client=client, cache=DiskCache(Path(temp)))
            self.assertEqual(list(Path(temp).glob('*.json')), [])
            self.assertNotIn('compact_staging', analysis['scenes'][0])

    def test_changed_source_invalidates_completed_plan(self):
        analysis, source = self.fixture()
        client = Mock()
        client.ask_json.return_value = self.response()
        with TemporaryDirectory() as temp:
            cache = DiskCache(Path(temp))
            result = stage(analysis, source, client=client, cache=cache)
            stage(result, source, client=client, cache=cache)
            self.assertEqual(client.ask_json.call_count, 1)
            result['scenes'][0]['source_quote'] += ' Нед возразил.'
            stage(result, source + ' Нед возразил.', client=client, cache=cache)
            self.assertEqual(client.ask_json.call_count, 2)

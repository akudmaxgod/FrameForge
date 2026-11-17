import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imagegen import source_evidence as refs
from imagegen.catalog_alias_repair import repair, AliasRepairError, AUDIT_KEY
from imagegen.story_catalog import build, match
from imagegen.story_overview_checkpoint import save, load
from imagegen.nvidia_text import NemotronSemanticPass, MODEL
from imagegen.analyzer_v3.models import DocumentIR


def response(value):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}


class AliasRepairTests(unittest.TestCase):
    def fixture(self):
        story = 'Хеттская держава исчезла. Культурные формы связаны с хеттским миром. Анна, она же Аня, ждёт.'
        menu = refs.options(story, [{'start': 0, 'end': len(story), 'text': story}])
        raw = {'timeline': 'По источнику', 'causal_links': '', 'uncertainties': '', 'setting_facts': [],
            'catalog': {'entities': [
                {'key': 'state', 'name': 'Хеттская держава', 'kind': 'organization', 'quote_ids': ['q1'],
                 'aliases': [{'name': 'хеттский мир', 'quote_ids': ['q2']}], 'species': None},
                {'key': 'anna', 'name': 'Анна', 'kind': 'person', 'quote_ids': ['q3'],
                 'aliases': [{'name': 'Аня', 'quote_ids': ['q3']}], 'species': None}],
                'relations': [], 'states': []}}
        return story, menu, raw, refs.overview(raw, menu, story)

    def decision(self, decision='retract', ids=None, issue='a1'):
        return {'issue_id': issue, 'decision': decision, 'quote_ids': ids or [],
                'reason': 'В тексте связь культур, а тождество не установлено.'}

    def client(self, rows):
        return SimpleNamespace(_post_json=Mock(return_value=response({'repairs': rows})),
            discard_last_response=Mock())

    def test_screenshot_retraction_preserves_entities_facts_and_proven_aliases(self):
        story, menu, raw, original = self.fixture()
        before = copy.deepcopy(original)
        client = self.client([self.decision()])
        fixed = repair(story, original, menu, client)
        packet = build(story, fixed)
        self.assertEqual(original, before)
        self.assertEqual(fixed['catalog']['entities'][1], before['catalog']['entities'][1])
        self.assertEqual(fixed['catalog']['entities'][0], {**before['catalog']['entities'][0], 'aliases': []})
        self.assertEqual(match(packet, 'Аня')['name'], 'Анна')
        self.assertIsNone(match(packet, 'хеттский мир'))
        self.assertEqual(len(packet['entities']), 2)
        self.assertEqual(fixed[AUDIT_KEY][0]['decision'], 'retract')
        payload = json.loads(client._post_json.call_args.args[0]['messages'][1]['content'])
        self.assertEqual(len(payload['issues']), 1)
        self.assertEqual(payload['source_options'], refs.wire_options(menu))

    def test_confirm_can_correct_evidence_without_renaming_candidate(self):
        story = 'Анна пришла. Анна, она же Аня, ждёт.'
        menu = refs.options(story, [{'start': 0, 'end': len(story), 'text': story}])
        value = {'catalog': {'entities': [{'key': 'a', 'name': 'Анна', 'kind': 'person',
            'quote': refs.bind(['q1'], menu, story), 'aliases': [{'name': 'Аня', 'quote': refs.bind(['q1'], menu, story)}]}],
            'relations': [], 'states': []}}
        fixed = repair(story, value, menu, self.client([self.decision('confirm', ['q2'])]))
        self.assertEqual(match(build(story, fixed), 'Аня')['name'], 'Анна')
        self.assertEqual(fixed[AUDIT_KEY][0]['accepted_evidence']['text'], menu[1]['text'])

    def test_valid_catalog_requires_no_extra_request(self):
        story, menu, raw, value = self.fixture()
        value['catalog']['entities'][0]['aliases'] = []
        client = self.client([])
        self.assertIs(repair(story, value, menu, client), value)
        client._post_json.assert_not_called()

    def test_guard_still_rejects_invalid_alias_without_repair(self):
        story, menu, raw, value = self.fixture()
        with self.assertRaisesRegex(ValueError, 'Псевдоним'): build(story, value)

    def test_unknown_proof_ids_are_not_accepted(self):
        story, menu, raw, value = self.fixture()
        client = self.client([self.decision('confirm', ['q999'])])
        with self.assertRaises(AliasRepairError): repair(story, value, menu, client)
        self.assertEqual(client._post_json.call_count, 2)
        self.assertEqual(client.discard_last_response.call_count, 2)

    def test_repeated_invalid_proof_is_not_silently_removed_or_accepted(self):
        story, menu, raw, value = self.fixture()
        client = self.client([self.decision('confirm', ['q2'])])
        with self.assertRaises(AliasRepairError): repair(story, value, menu, client)
        self.assertEqual(len(value['catalog']['entities'][0]['aliases']), 1)

    def test_pronoun_cannot_be_confirmed_even_with_both_names_in_quote(self):
        story, menu, raw, value = self.fixture()
        value['catalog']['entities'][1]['aliases'][0]['name'] = 'она'
        client = self.client([self.decision(), self.decision('confirm', ['q3'], 'a2')])
        with self.assertRaises(AliasRepairError): repair(story, value, menu, client)

    def test_missing_duplicate_extra_and_unknown_decisions_rejected(self):
        story, menu, raw, value = self.fixture()
        for rows in ([], [self.decision(), self.decision()], [self.decision(issue='a99')],
                     [{**self.decision(), 'entity_name': 'Другая сущность'}],
                     [self.decision('retract', ['q1'])], [self.decision('drop')]):
            with self.subTest(rows=rows), self.assertRaises(AliasRepairError):
                repair(story, value, menu, self.client(rows))

    def test_retry_sees_previous_answer_and_specific_error(self):
        story, menu, raw, value = self.fixture()
        client = self.client([])
        client._post_json.side_effect = [response({'repairs': []}), response({'repairs': [self.decision()]})]
        fixed = repair(story, value, menu, client)
        messages = client._post_json.call_args.args[0]['messages']
        self.assertEqual(messages[-2]['role'], 'assistant')
        self.assertIn('Не все', messages[-1]['content'])
        self.assertEqual(fixed['catalog']['entities'][0]['aliases'], [])

    def test_multiple_retractions_do_not_shift_indices_or_delete_valid_alias(self):
        story, menu, raw, value = self.fixture()
        entity = value['catalog']['entities'][1]
        entity['aliases'] = [{'name': 'он', 'quote': entity['quote']},
            *entity['aliases'], {'name': 'они', 'quote': entity['quote']}]
        fixed = repair(story, value, menu, self.client([self.decision(issue=i) for i in ('a3', 'a1', 'a2')]))
        self.assertEqual([a['name'] for a in fixed['catalog']['entities'][1]['aliases']], ['Аня'])
        self.assertEqual(len(fixed[AUDIT_KEY]), 3)

    def test_checkpoint_preserves_repair_provenance(self):
        story, menu, raw, value = self.fixture()
        fixed = repair(story, value, menu, self.client([self.decision()]))
        with tempfile.TemporaryDirectory() as folder:
            save(folder, story, MODEL, True, fixed, {'provider': 'nvidia', 'model': MODEL})
            checkpoint = load(folder, story, MODEL, True)
            self.assertEqual(checkpoint['overview'][AUDIT_KEY], fixed[AUDIT_KEY])
            build(story, checkpoint['overview'])

    def test_model_cannot_forge_app_review_history(self):
        story, menu, raw, value = self.fixture()
        raw[AUDIT_KEY] = [{'decision': 'confirm'}]
        with self.assertRaises(ValueError): refs.overview(raw, menu, story)

    def test_full_semantic_route_repairs_alias_without_regenerating_overview(self):
        story, menu, raw, value = self.fixture()
        doc = DocumentIR('test')
        client = self.client([])
        client._post_json.side_effect = [response(raw), response({'repairs': [self.decision()]})]
        def downstream(parser, document, text, provider):
            document.metadata['semantic_verification'] = {}
            return True
        with patch('imagegen.analyzer_v3.semantic_first.SemanticFirstPass.run', downstream):
            self.assertTrue(NemotronSemanticPass().run(doc, story, client))
        self.assertEqual(client._post_json.call_count, 2)
        self.assertIsNone(match(doc.metadata['story_catalog'], 'хеттский мир'))
        self.assertEqual(doc.metadata['story_catalog_review']['alias_repairs'][0]['decision'], 'retract')

    def test_non_alias_catalog_error_remains_fatal(self):
        story, menu, raw, value = self.fixture()
        value = repair(story, value, menu, self.client([self.decision()]))
        value['catalog']['entities'][0]['name'] = 'Вымышленная держава'
        with self.assertRaisesRegex(ValueError, 'Имя сущности'): build(story, value)

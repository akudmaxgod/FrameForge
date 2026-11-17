import hashlib
import unittest
from imagegen.analyzer_v3.models import (DocumentIR, SectionIR, EventIR, MentionIR, EntityIR,
    SourceSpan, CoreferenceDecision, CoreferenceStatus, VisualPolicy)
from imagegen.analyzer_v3.semantic_first import SemanticFirstPass
from imagegen.analyzer_v3.two_layer_audit import TwoLayerSemanticAuditor


STORY = 'Иван. Он.'


def fixture():
    whole = SourceSpan(0, len(STORY), STORY)
    name = SourceSpan(0, 4, 'Иван')
    pronoun = SourceSpan(6, 8, 'Он')
    doc = DocumentIR(hashlib.sha256(STORY.encode()).hexdigest(),
        sections=[SectionIR('sec_0', None, whole, event_ids=['ev'])],
        events=[EventIR('ev', 'sec_0', whole, mentioned_entity_ids=['ent_10'])],
        mentions=[MentionIR('name', 'Иван', 'иван', name, event_id='ev'),
                  MentionIR('pronoun', 'Он', 'он', pronoun, mention_type='pronoun', event_id='ev')],
        entities=[EntityIR('ent_0', 'Иван', mention_ids=['name'], evidence_spans=[name]),
                  EntityIR('ent_10', 'человек', mention_ids=['name'], evidence_spans=[name])],
        coreferences=[CoreferenceDecision('pronoun', ['ent_10'], candidate_entity_ids=['ent_10'],
            rejected_candidates=[{'entity_id': 'ent_10', 'reason': 'earlier candidate'}], event_id='ev')],
        metadata={'semantic_first_coreference_hints': [{'mention_id': 'pronoun',
            'resolved_entity_ids': ['ent_10'], 'status': 'resolved', 'confidence': .9,
            'reason': 'Source antecedent.', 'evidence_spans': [name.to_dict()]}]})
    return doc


class EntityReferenceUpdateTests(unittest.TestCase):
    def test_merge_updates_prior_chunk_hints_and_all_coreference_lists(self):
        doc = fixture()
        self.assertEqual(doc.validate_invariants(STORY), [])
        SemanticFirstPass._remap_entities(doc, {'ent_10': 'ent_0'})
        doc.entities = [e for e in doc.entities if e.entity_id != 'ent_10']
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, ['ent_0'])
        self.assertEqual(doc.coreferences[0].candidate_entity_ids, ['ent_0'])
        self.assertEqual(doc.coreferences[0].rejected_candidates[0]['entity_id'], 'ent_0')
        # Applying hints later must never resurrect the removed ID.
        SemanticFirstPass.apply_coreference_hints(doc)
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, ['ent_0'])
        self.assertEqual(doc.validate_invariants(STORY), [])

    def test_drop_invalidates_resolution_without_substituting_another_person(self):
        doc = fixture()
        SemanticFirstPass._drop_entities(doc, {'ent_10'})
        doc.entities = [e for e in doc.entities if e.entity_id != 'ent_10']
        decision = doc.coreferences[0]
        self.assertEqual(decision.resolved_entity_ids, [])
        self.assertEqual(decision.candidate_entity_ids, [])
        self.assertEqual(decision.rejected_candidates, [])
        self.assertEqual(decision.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(decision.visual_policy, VisualPolicy.SAFE_NEUTRAL)
        SemanticFirstPass.apply_coreference_hints(doc)
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, [])
        self.assertEqual(doc.validate_invariants(STORY), [])

    def test_qwen_identity_merge_uses_same_hint_update(self):
        doc = fixture()
        TwoLayerSemanticAuditor._apply_identity([{'keep': True, 'member_entity_ids': ['ent_10'],
            'merge_target_id': 'ent_0', 'canonical_name': 'Иван',
            'global_modality': doc.entities[0].modality, 'evidence_spans': doc.entities[0].evidence_spans}], doc)
        SemanticFirstPass.apply_coreference_hints(doc)
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, ['ent_0'])
        self.assertEqual(doc.validate_invariants(STORY), [])

    def test_chain_is_resolved_and_cycle_or_unknown_reference_is_not_hidden(self):
        doc = fixture()
        SemanticFirstPass._remap_entities(doc, {'ent_10': 'intermediate', 'intermediate': 'ent_0'})
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, ['ent_0'])
        before = doc.to_dict()
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            SemanticFirstPass._remap_entities(doc, {'ent_10': 'ent_0', 'ent_0': 'ent_10'})
        self.assertEqual(doc.to_dict(), before)
        doc.coreferences[0].resolved_entity_ids = ['unrelated_missing_entity']
        self.assertTrue(any('unrelated_missing_entity' in error for error in doc.validate_invariants(STORY)))


if __name__ == '__main__':
    unittest.main()

import json
import unittest
from types import SimpleNamespace

from imagegen.analyzer_v3.models import CoreferenceStatus, EntityType
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.two_layer_audit import (
    SemanticCertificationError,
    TwoLayerAuditResult,
    TwoLayerSemanticAuditor,
)


PARTICIPANTS = {EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE}


class FakeAuditClient:
    def __init__(self, doc, story, *, omit_event=False):
        self.doc = doc
        self.story = story
        self.omit_event = omit_event
        self.calls = 0
        self.model = "fake-qwen"

    def get_usage(self):
        return {"calls": self.calls}

    def _post_json(self, payload):
        self.calls += 1
        body = json.loads(payload["messages"][1]["content"])
        if "participants" in body:
            result = {"entity_groups": []}
            for raw in body["participants"]:
                entity = next(e for e in self.doc.entities if e.entity_id == raw["entity_id"])
                span = entity.evidence_spans[0]
                result["entity_groups"].append({
                    "canonical_name": entity.canonical_name,
                    "member_entity_ids": [entity.entity_id],
                    "keep": True,
                    "global_modality": entity.modality.value,
                })
            result["discarded_entity_ids"] = []
            return self._response(result)

        event_ids = [x["event_id"] for x in body["events"]]
        if self.omit_event and event_ids:
            event_ids = event_ids[:-1]
        events = []
        participant_ids = {e.entity_id for e in self.doc.entities if e.entity_type in PARTICIPANTS}
        for event_id in event_ids:
            event = next(e for e in self.doc.events if e.event_id == event_id)
            physical = list(dict.fromkeys(
                p.entity_id for p in event.presences
                if p.entity_id in participant_ids and p.physical_presence
            ))
            mentioned = list(dict.fromkeys(
                p.entity_id for p in event.presences
                if p.entity_id in participant_ids and not p.physical_presence and p.entity_id not in physical
            ))
            speakers = [x for x in event.dialogue_speakers if x in physical]
            locations = self.doc.metadata.get("spatial_grounding", {}).get("event_locations", {})
            events.append({
                "event_id": event_id,
                "physical_entity_ids": physical,
                "mentioned_entity_ids": mentioned,
                "dialogue_speaker_entity_ids": speakers,
                "location_name": locations.get(event_id),
            })
        decisions = {x.mention_id: x for x in self.doc.coreferences}
        corefs = []
        for raw in body["pronouns"]:
            mention = next(m for m in self.doc.mentions if m.mention_id == raw["mention_id"])
            decision = decisions.get(mention.mention_id)
            resolved = [x for x in (decision.resolved_entity_ids if decision else []) if x in participant_ids]
            corefs.append({
                "mention_id": mention.mention_id,
                "resolved_entity_ids": resolved,
                "status": "resolved" if resolved else "unresolved_safe",
                "confidence": decision.confidence if decision else 0.5,
                "evidence_spans": [mention.span.to_dict()],
            })
        return self._response({"events": events, "coreferences": corefs})

    @staticmethod
    def _response(result):
        return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}


class PassiveAuditor:
    def run(self, doc, story, local_client, repair_client=None, progress_callback=None):
        return TwoLayerAuditResult(
            entity_groups=0,
            events_checked=len(doc.events),
            pronouns_checked=sum(1 for item in doc.mentions if item.mention_type == "pronoun"),
            local_calls=0,
            repair_calls=0,
        )


class CloudFailureClient:
    model = "unavailable-groq"

    def _post_json(self, payload):
        raise RuntimeError("Groq unavailable")

    def get_usage(self):
        return {"calls": 0}


class CapturingAuditor:
    def __init__(self):
        self.event_ids = None

    @staticmethod
    def select_adaptive_event_ids(doc, maximum_events=18, required_event_ids=None):
        return TwoLayerSemanticAuditor.select_adaptive_event_ids(
            doc, maximum_events=maximum_events, required_event_ids=required_event_ids,
        )

    def run(self, doc, story, local_client, repair_client=None, progress_callback=None, event_ids=None):
        self.event_ids = list(event_ids or [])
        return TwoLayerAuditResult(
            entity_groups=0,
            events_checked=len(self.event_ids),
            pronouns_checked=0,
            local_calls=1,
            repair_calls=0,
        )


class TwoLayerAuditTests(unittest.TestCase):
    STORY = "Кузнец Мартин встретил гонца в кузнице. Он взял свиток. Гонец ушёл."

    def make_doc(self):
        return StoryAnalyzerV3Pipeline().analyze_structural(self.STORY)

    def test_exhaustive_audit_certifies_every_event_and_pronoun(self):
        doc = self.make_doc()
        client = FakeAuditClient(doc, self.STORY)
        result = TwoLayerSemanticAuditor(batch_size=2).run(doc, self.STORY, client)
        self.assertEqual(result.events_checked, len(doc.events))
        self.assertEqual(
            result.pronouns_checked,
            sum(1 for mention in doc.mentions if mention.mention_type == "pronoun"),
        )
        self.assertGreaterEqual(result.local_calls, 2)
        self.assertEqual(doc.validate_invariants(self.STORY), [])

    def test_adaptive_audit_limits_the_checked_events(self):
        doc = self.make_doc()
        client = FakeAuditClient(doc, self.STORY)
        selected = [doc.events[0].event_id]
        result = TwoLayerSemanticAuditor(batch_size=2).run(
            doc, self.STORY, client, event_ids=selected,
        )
        self.assertEqual(result.events_checked, 1)

    def test_adaptive_selector_is_bounded_and_keeps_story_edges(self):
        doc = SimpleNamespace(
            events=[SimpleNamespace(event_id=f"event-{index}", presences=[], dialogue_speakers=[])
                    for index in range(30)],
            entities=[],
            mentions=[],
        )
        selected = TwoLayerSemanticAuditor().select_adaptive_event_ids(doc, maximum_events=8)
        self.assertLessEqual(len(selected), 8)
        self.assertIn("event-0", selected)
        self.assertIn("event-29", selected)

    def test_adaptive_selector_keeps_failed_cloud_window(self):
        doc = SimpleNamespace(
            events=[SimpleNamespace(event_id=f"event-{index}", presences=[], dialogue_speakers=[])
                    for index in range(30)],
            entities=[],
            mentions=[],
        )
        required = [f"event-{index}" for index in range(10, 18)]
        selected = TwoLayerSemanticAuditor.select_adaptive_event_ids(
            doc, maximum_events=8, required_event_ids=required,
        )
        self.assertTrue(set(required).issubset(selected))

    def test_missing_event_is_retried_then_fails_closed(self):
        doc = self.make_doc()
        client = FakeAuditClient(doc, self.STORY, omit_event=True)
        with self.assertRaisesRegex(SemanticCertificationError, "certification failed"):
            TwoLayerSemanticAuditor(batch_size=6).run(doc, self.STORY, client)
        self.assertEqual(client.calls, 3)  # identity plus two rejected event attempts

    def test_keep_without_redundant_modality_preserves_grounded_modality(self):
        doc = self.make_doc()
        entity = next(item for item in doc.entities if item.entity_type in PARTICIPANTS)
        result = TwoLayerSemanticAuditor()._validate_identity_decisions(
            {"decisions": [{
                "entity_id": entity.entity_id,
                "action": "keep",
                "canonical_name": entity.canonical_name,
            }]},
            doc,
            self.STORY,
            expected_ids={entity.entity_id},
        )
        self.assertEqual(result[0]["global_modality"], entity.modality)

    def test_invalid_merge_target_preserves_source_grounded_identity(self):
        doc = self.make_doc()
        mention_map = {item.mention_id: item for item in doc.mentions}
        is_named = lambda entity: any(
            mention_map[mention_id].mention_type == "proper_noun"
            for mention_id in entity.mention_ids if mention_id in mention_map
        )
        participant = next(item for item in doc.entities if is_named(item))
        # ``гонец`` is a valid participant ID but has no proper-name mention.
        # This mimics Qwen's generic-target output and must not abort the
        # entire adaptive audit.
        generic_participant = next(item for item in doc.entities if not is_named(item))
        result = TwoLayerSemanticAuditor()._validate_identity_decisions(
            {"decisions": [{
                "entity_id": participant.entity_id,
                "action": "merge",
                "target_entity_id": generic_participant.entity_id,
            }]},
            doc,
            self.STORY,
            expected_ids={participant.entity_id},
        )
        self.assertTrue(result[0]["keep"])
        self.assertEqual(result[0]["canonical_name"], participant.canonical_name)
        self.assertEqual(result[0]["global_modality"], participant.modality)

    def test_local_runtime_hook_runs_only_at_the_audit_boundary(self):
        started = []
        pipeline = StoryAnalyzerV3Pipeline(
            semantic_auditor=PassiveAuditor(),
            local_semantic_client=object(),
        )
        pipeline.analyze_structural(
            self.STORY,
            before_local_audit=lambda: started.append("qwen"),
        )
        self.assertEqual(started, ["qwen"])

    def test_pipeline_settings_enable_real_two_layer_components_only_when_requested(self):
        disabled = StoryAnalyzerV3Pipeline.from_settings({"text_backend": "hybrid"})
        self.assertIsNone(disabled.semantic_auditor)
        enabled = StoryAnalyzerV3Pipeline.from_settings({
            "text_backend": "hybrid",
            "v3_two_layer_audit": True,
            "text_url": "http://127.0.0.1:11435",
            "text_model": "Qwen.gguf",
        })
        self.assertIsNotNone(enabled.semantic_auditor)
        self.assertEqual(enabled.local_semantic_client.model, "Qwen.gguf")
        self.assertEqual(enabled.two_layer_audit_mode, "adaptive")

        strict = StoryAnalyzerV3Pipeline.from_settings({
            "text_backend": "hybrid",
            "v3_two_layer_audit": True,
            "v3_two_layer_audit_mode": "strict",
            "text_url": "http://127.0.0.1:11435",
            "text_model": "Qwen.gguf",
        })
        self.assertEqual(strict.two_layer_audit_mode, "strict")

    def test_adaptive_mode_stays_bounded_when_groq_fails(self):
        story = " ".join(f"Житель {index} осматривает квартал." for index in range(30))
        auditor = CapturingAuditor()
        pipeline = StoryAnalyzerV3Pipeline(
            arbitrator=CloudFailureClient(),
            semantic_first_enabled=True,
            semantic_auditor=auditor,
            local_semantic_client=object(),
            two_layer_audit_mode="adaptive",
        )
        doc = pipeline.analyze_structural(story)
        audit = doc.metadata["two_layer_audit"]
        self.assertEqual(audit["status"], "adaptive_degraded")
        self.assertEqual(audit["mode"], "adaptive")
        self.assertLessEqual(len(auditor.event_ids), 18)

    def test_model_cannot_override_proven_subject_object_ambiguity(self):
        story = "Алексей посмотрел на Виктора. Он улыбнулся."
        doc = StoryAnalyzerV3Pipeline().analyze_structural(story)
        mention = next(m for m in doc.mentions if m.text == "Он")
        prior = next(c for c in doc.coreferences if c.mention_id == mention.mention_id)
        self.assertEqual(prior.status, CoreferenceStatus.UNRESOLVED_SAFE)
        candidate = prior.candidate_entity_ids[0]
        event = next(e for e in doc.events if e.event_id == mention.event_id)
        TwoLayerSemanticAuditor._apply_events({
            "events": [{
                "event_id": event.event_id,
                "physical_entity_ids": [candidate],
                "mentioned_entity_ids": [],
                "dialogue_speaker_entity_ids": [],
                "location_name": None,
                "location_evidence_span": None,
            }],
            "coreferences": [{
                "mention_id": mention.mention_id,
                "resolved_entity_ids": [candidate],
                "status": "resolved",
                "confidence": 0.99,
                "evidence_spans": [mention.span],
            }],
        }, doc, story)
        result = next(c for c in doc.coreferences if c.mention_id == mention.mention_id)
        self.assertEqual(result.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(result.resolved_entity_ids, [])
        self.assertIn("TWO_LAYER_SAFETY_FLOOR", result.reason_codes)


if __name__ == "__main__":
    unittest.main()

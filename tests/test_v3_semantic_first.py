import unittest
import json

from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.models import CoreferenceStatus, EntityModalityV3
from imagegen.analyzer_v3.semantic_first import SemanticFirstPass, _span


class FakeSemanticClient:
    model = "fake-semantic-model"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def _post_json(self, payload):
        self.calls += 1
        return {"choices": [{"message": {"content": __import__("json").dumps(self.response, ensure_ascii=False)}}]}

    def get_usage(self):
        return {"calls": self.calls, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


class ChunkedSemanticClient:
    model = "fake-semantic-model"

    def __init__(self):
        self.calls = 0
        self.payload_sizes = []

    def _post_json(self, payload):
        self.calls += 1
        content = payload["messages"][1]["content"]
        self.payload_sizes.append(len(content))
        events_json = content.split("\n\nEVENTS:\n", 1)[1].split("\n\nPRONOUN MENTIONS", 1)[0]
        events = json.loads(events_json)
        return {"choices": [{"message": {"content": json.dumps({
            "entities": [],
            "discarded_existing_entities": [],
            "events": [{
                "event_id": item["event_id"], "location_name": None,
                "location_evidence_span": None, "presences": [],
                "dialogue_speaker_semantic_id": None, "time_shift": "none",
            } for item in events],
            "coreferences": [],
        }, ensure_ascii=False)}}]}

    def get_usage(self):
        return {"calls": self.calls}


class PartiallyFailingChunkedSemanticClient(ChunkedSemanticClient):
    def _post_json(self, payload):
        self.calls += 1
        content = payload["messages"][1]["content"]
        self.payload_sizes.append(len(content))
        events_json = content.split("\n\nEVENTS:\n", 1)[1].split("\n\nPRONOUN MENTIONS", 1)[0]
        events = json.loads(events_json)
        # The second window is malformed twice. The first certified window
        # must remain usable instead of triggering a full cloud rollback.
        if self.calls in (2, 3):
            events = []
        return {"choices": [{"message": {"content": json.dumps({
            "entities": [], "discarded_existing_entities": [],
            "events": [{
                "event_id": item["event_id"], "location_name": None,
                "location_evidence_span": None, "presences": [],
                "dialogue_speaker_semantic_id": None, "time_shift": "none",
            } for item in events], "coreferences": [],
        }, ensure_ascii=False)}}]}


class SemanticFirstPipelineTests(unittest.TestCase):
    TEXT = "Анна вошла в кухню. Она позвонила Борису. По телефону он сказал: «Мария в офисе». Анна вернулась в зал."

    def _span(self, text, occurrence=0):
        start = -1
        for _ in range(occurrence + 1):
            start = self.TEXT.index(text, start + 1)
        return {"start": start, "end": start + len(text), "text": text}

    def test_cloud_span_uses_source_text_when_offsets_are_valid(self):
        raw = {"start": 0, "end": 4, "text": "Анна  "}
        span = _span(raw, self.TEXT)
        self.assertEqual((span.start, span.end, span.text), (0, 4, "Анна"))

    def _response(self):
        return {
            "entities": [
                {"semantic_id": "anna", "canonical_name": "Анна", "entity_type": "person",
                 "gender": "female", "modality": "physical", "existing_entity_ids": ["ent_0"],
                 "aliases": ["Анна"], "evidence_spans": [self._span("Анна", 0), self._span("Анна", 1)]},
                {"semantic_id": "boris", "canonical_name": "Борис", "entity_type": "person",
                 "gender": "male", "modality": "voice_only", "existing_entity_ids": ["ent_1"],
                 "aliases": ["Борису"], "evidence_spans": [self._span("Борису")]},
                {"semantic_id": "maria", "canonical_name": "Мария", "entity_type": "person",
                 "gender": "female", "modality": "mentioned", "existing_entity_ids": ["ent_2"],
                 "aliases": ["Мария"], "evidence_spans": [self._span("Мария")]},
            ],
            "discarded_existing_entities": [],
            "events": [
                {"event_id": "ev_0", "location_name": "Кухня", "location_evidence_span": self._span("кухню"),
                 "presences": [{"semantic_id": "anna", "modality": "physical", "role": "actor", "evidence_span": self._span("Анна", 0)}],
                 "dialogue_speaker_semantic_id": None, "time_shift": "none"},
                {"event_id": "ev_1", "location_name": "Кухня", "location_evidence_span": self._span("кухню"),
                 "presences": [
                     {"semantic_id": "anna", "modality": "physical", "role": "actor", "evidence_span": self._span("Она")},
                     {"semantic_id": "boris", "modality": "mentioned", "role": "mentioned", "evidence_span": self._span("Борису")},
                 ], "dialogue_speaker_semantic_id": None, "time_shift": "same_scene"},
                {"event_id": "ev_2", "location_name": "Кухня", "location_evidence_span": self._span("кухню"),
                 "presences": [
                     {"semantic_id": "boris", "modality": "voice_only", "role": "speaker", "evidence_span": self._span("он", 2)},
                     {"semantic_id": "maria", "modality": "mentioned", "role": "mentioned", "evidence_span": self._span("Мария")},
                 ], "dialogue_speaker_semantic_id": "boris", "time_shift": "same_scene"},
                {"event_id": "ev_3", "location_name": "Зал", "location_evidence_span": self._span("зал"),
                 "presences": [{"semantic_id": "anna", "modality": "physical", "role": "actor", "evidence_span": self._span("Анна", 1)}],
                 "dialogue_speaker_semantic_id": None, "time_shift": "same_scene"},
            ],
            "coreferences": [
                {"mention_id": "m_4", "resolved_semantic_ids": ["anna"], "status": "resolved", "confidence": .99,
                 "evidence_spans": [self._span("Она")], "reason": "Анна is the continuing caller"},
                {"mention_id": "m_5", "resolved_semantic_ids": ["boris"], "status": "resolved", "confidence": .99,
                 "evidence_spans": [self._span("он", 2)], "reason": "Boris is the phone speaker"},
            ],
        }

    def test_hybrid_runs_semantic_first_before_authoritative_coreference(self):
        client = FakeSemanticClient(self._response())
        pipeline = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True)
        doc = pipeline.analyze_structural(self.TEXT)
        self.assertEqual(doc.validate_invariants(self.TEXT), [])
        self.assertEqual(doc.metadata["semantic_verification"]["mode"], "cloud_semantic_first")
        self.assertEqual(client.calls, 1)
        self.assertEqual(doc.metadata["spatial_grounding"]["event_locations"], {
            "ev_0": "Кухня", "ev_1": "Кухня", "ev_2": "Кухня", "ev_3": "Зал"
        })
        entities = {e.canonical_name: e for e in doc.entities}
        self.assertEqual(entities["Борис"].modality, EntityModalityV3.VOICE_ONLY)
        decisions = {next(m.text for m in doc.mentions if m.mention_id == c.mention_id): c for c in doc.coreferences}
        self.assertEqual(decisions["Она"].status, CoreferenceStatus.RESOLVED_CLOUD)
        self.assertEqual(decisions["он"].status, CoreferenceStatus.RESOLVED_CLOUD)
        self.assertEqual(decisions["он"].resolved_entity_ids, [entities["Борис"].entity_id])
        self.assertFalse(any(p.entity_id == entities["Борис"].entity_id and p.physical_presence for p in doc.events[2].presences))
        json.dumps(doc.to_dict(), ensure_ascii=False)

    def test_invalid_cloud_evidence_fails_closed_to_local(self):
        response = self._response()
        response["events"][0]["location_evidence_span"] = {"start": 0, "end": 4, "text": "ложь"}
        client = FakeSemanticClient(response)
        pipeline = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True)
        doc = pipeline.analyze_structural(self.TEXT)
        self.assertEqual(doc.validate_invariants(self.TEXT), [])
        self.assertEqual(doc.metadata["semantic_verification"]["mode"], "cloud_failed_local_fallback")
        self.assertTrue(doc.metadata["semantic_verification"]["cloud_attempted"])
        self.assertFalse(any(c.status == CoreferenceStatus.RESOLVED_CLOUD for c in doc.coreferences))

    def test_missing_events_are_repaired_once_then_completed_locally(self):
        response = self._response()
        response["events"] = response["events"][:-1]
        client = FakeSemanticClient(response)
        pipeline = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True)

        doc = pipeline.analyze_structural(self.TEXT)

        self.assertEqual(doc.validate_invariants(self.TEXT), [])
        semantic = doc.metadata["semantic_verification"]
        self.assertEqual(semantic["mode"], "cloud_semantic_first_partial")
        self.assertEqual(client.calls, 2)
        self.assertEqual(semantic["events_synthesized"], 3)
        self.assertEqual(semantic["events_total"], 4)
        self.assertEqual(semantic["events_completed_locally"], 1)
        self.assertEqual(semantic["missing_event_ids"], ["ev_3"])
        self.assertIn("ev_3", doc.metadata["spatial_grounding"]["event_locations"])
        self.assertTrue(doc.metadata["spatial_grounding"]["event_locations"]["ev_3"])
        self.assertTrue(any(c.status == CoreferenceStatus.RESOLVED_CLOUD for c in doc.coreferences))

    def test_omitted_local_person_is_preserved_without_rejecting_cloud_window(self):
        response = self._response()
        omitted = response["entities"].pop()
        omitted_id = omitted["existing_entity_ids"][0]
        omitted_semantic_id = omitted["semantic_id"]
        for event in response["events"]:
            event["presences"] = [
                presence for presence in event["presences"]
                if presence["semantic_id"] != omitted_semantic_id
            ]
            if event.get("dialogue_speaker_semantic_id") == omitted_semantic_id:
                event["dialogue_speaker_semantic_id"] = None
        for coreference in response["coreferences"]:
            coreference["resolved_semantic_ids"] = [
                entity_id for entity_id in coreference["resolved_semantic_ids"]
                if entity_id != omitted_semantic_id
            ]
            if coreference["status"] == "resolved" and not coreference["resolved_semantic_ids"]:
                coreference["status"] = "unresolved_safe"

        doc = StoryAnalyzerV3Pipeline().analyze_structural(self.TEXT)
        semantic = SemanticFirstPass()
        normalized = semantic._validate(response, doc, self.TEXT)
        self.assertIn(omitted_id, normalized["preserved_local_entity_ids"])
        semantic._apply(normalized, doc, self.TEXT)
        self.assertTrue(any(entity.entity_id == omitted_id for entity in doc.entities))
        self.assertEqual(doc.validate_invariants(self.TEXT), [])

    def test_zero_event_cloud_response_is_not_reported_as_partial_success(self):
        response = self._response()
        response["events"] = []
        response["coreferences"] = []
        client = FakeSemanticClient(response)

        doc = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True).analyze_structural(self.TEXT)

        self.assertEqual(client.calls, 2)
        self.assertEqual(doc.metadata["semantic_verification"]["mode"], "cloud_failed_local_fallback")
        self.assertIn("zero event records", doc.metadata["semantic_verification"]["cloud_error"])

    def test_cloud_can_add_a_locally_missing_grounded_character(self):
        text = "В комнату вошёл некто. Он закрыл дверь."
        def span(value):
            start = text.index(value)
            return {"start": start, "end": start + len(value), "text": value}
        response = {
            "entities": [{
                "semantic_id": "stranger", "canonical_name": "Незнакомец",
                "entity_type": "person", "gender": "male", "modality": "physical",
                "existing_entity_ids": [], "aliases": ["некто"],
                "evidence_spans": [span("некто")],
            }],
            "discarded_existing_entities": [],
            "events": [
                {"event_id": "ev_0", "location_name": "Комната", "location_evidence_span": span("комнату"),
                 "presences": [{"semantic_id": "stranger", "modality": "physical", "role": "actor", "evidence_span": span("некто")}],
                 "dialogue_speaker_semantic_id": None, "time_shift": "none"},
                {"event_id": "ev_1", "location_name": "Комната", "location_evidence_span": span("комнату"),
                 "presences": [{"semantic_id": "stranger", "modality": "physical", "role": "actor", "evidence_span": span("Он")}],
                 "dialogue_speaker_semantic_id": None, "time_shift": "same_scene"},
            ],
            "coreferences": [{
                "mention_id": "m_0", "resolved_semantic_ids": ["stranger"],
                "status": "resolved", "confidence": .97,
                "evidence_spans": [span("Он")], "reason": "continuing subject",
            }],
        }
        client = FakeSemanticClient(response)
        doc = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True).analyze_structural(text)
        stranger = next(e for e in doc.entities if e.canonical_name == "Незнакомец")
        self.assertIn("cloud_semantic_first", stranger.attributes)
        self.assertEqual(doc.coreferences[0].resolved_entity_ids, [stranger.entity_id])
        self.assertEqual(doc.metadata["semantic_verification"]["mode"], "cloud_semantic_first")

    def test_physically_present_dialogue_speaker_is_valid(self):
        response = self._response()
        response["entities"][1]["modality"] = "physical"
        response["events"][2]["presences"][0]["modality"] = "physical"
        client = FakeSemanticClient(response)

        doc = StoryAnalyzerV3Pipeline(arbitrator=client, semantic_first_enabled=True).analyze_structural(self.TEXT)

        self.assertEqual(doc.metadata["semantic_verification"]["mode"], "cloud_semantic_first")
        boris = next(entity for entity in doc.entities if entity.canonical_name == "Борис")
        self.assertTrue(any(
            presence.entity_id == boris.entity_id and presence.physical_presence
            for presence in doc.events[2].presences
        ))

    def test_common_groq_json_variants_are_normalized_before_validation(self):
        doc = StoryAnalyzerV3Pipeline().analyze_structural(self.TEXT)
        normalized = SemanticFirstPass._normalize_response({
            "participants": [{
                "id": "anna", "name": "Анна", "entity_type": "human",
                "modality": "physically_present", "existing_ids": "ent_0",
                "aliases": "Анна", "evidence": self._span("Анна"),
            }],
            "event_results": [{
                "id": "ev_0", "location": "unknown",
                "participants": [{
                    "entity_id": "anna", "modality": "visible", "role": "speaking",
                    "evidence": self._span("Анна"),
                }],
            }],
            "pronouns": [],
            "discarded_entities": ["non_participant_object"],
        }, doc)
        entity = normalized["entities"][0]
        presence = normalized["events"][0]["presences"][0]
        self.assertEqual(entity["entity_type"], "person")
        self.assertEqual(entity["modality"], "physical")
        self.assertEqual(entity["existing_entity_ids"], ["ent_0"])
        self.assertEqual(presence["role"], "speaker")
        self.assertEqual(presence["modality"], "physical")
        self.assertIsNone(normalized["events"][0]["location_name"])
        self.assertEqual(normalized["discarded_existing_entities"], [])

    def test_physical_presence_with_generic_mentioned_role_becomes_actor(self):
        doc = StoryAnalyzerV3Pipeline().analyze_structural(self.TEXT)
        normalized = SemanticFirstPass._normalize_response({
            "entities": [], "coreferences": [],
            "events": [{"event_id": "ev_0", "location_name": None, "presences": [{
                "semantic_id": "anna", "modality": "physical", "role": "mentioned",
                "evidence_span": self._span("Анна"),
            }]}],
        }, doc)
        self.assertEqual(normalized["events"][0]["presences"][0]["role"], "actor")

    def test_presence_with_exact_neighbour_event_span_is_reassigned(self):
        doc = StoryAnalyzerV3Pipeline().analyze_structural(self.TEXT)
        anna = next(entity for entity in doc.entities if entity.canonical_name == "Анна")
        normalized = SemanticFirstPass._normalize_response({
            "entities": [], "coreferences": [],
            "events": [
                {"event_id": "ev_0", "location_name": None, "presences": [{
                    "semantic_id": anna.entity_id, "modality": "physical", "role": "actor",
                    "evidence_span": self._span("Она"),
                }]},
                {"event_id": "ev_1", "location_name": None, "presences": []},
            ],
        }, doc)
        self.assertEqual(normalized["events"][0]["presences"], [])
        self.assertEqual(normalized["events"][1]["presences"][0]["evidence_span"], self._span("Она"))

    def test_long_story_is_sent_to_groq_in_bounded_event_windows(self):
        text = " ".join(f"Наступил рассвет номер {index}." for index in range(18))
        doc = StoryAnalyzerV3Pipeline().analyze_structural(text)
        client = ChunkedSemanticClient()

        self.assertTrue(SemanticFirstPass().run(doc, text, client))
        semantic = doc.metadata["semantic_verification"]
        self.assertEqual(semantic["mode"], "cloud_semantic_first_chunked")
        self.assertGreater(client.calls, 1)
        self.assertTrue(all(size < 15000 for size in client.payload_sizes))

    def test_one_bad_cloud_window_preserves_other_certified_windows(self):
        text = " ".join(f"Наступил рассвет номер {index}." for index in range(18))
        doc = StoryAnalyzerV3Pipeline().analyze_structural(text)
        client = PartiallyFailingChunkedSemanticClient()

        self.assertTrue(SemanticFirstPass().run(doc, text, client))
        semantic = doc.metadata["semantic_verification"]
        self.assertEqual(semantic["mode"], "cloud_semantic_first_chunked_partial")
        self.assertEqual(len(semantic["failed_chunks"]), 1)
        self.assertGreater(len(semantic["missing_event_ids"]), 0)


if __name__ == "__main__":
    unittest.main()

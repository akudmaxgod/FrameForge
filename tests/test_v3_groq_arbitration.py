"""Unit & Integration Tests for Story Analyzer V3 Targeted Groq Arbitration.

Covers all Stage 5 requirements:
1. Locally resolved decisions do not call Groq.
2. UNRESOLVED_SAFE does not call Groq.
3. Single UNRESOLVED_BLOCKING creates exactly 1 compact request; payload lacks full story/chunk/scenes.
4. Valid response selects allowed candidate and updates only the target CoreferenceDecision.
5. Unknown entity returned by Groq is rejected.
6. Wrong coordinates or mention_id is rejected.
7. Physical confirmation of VOICE_ONLY/DEPICTED/MENTIONED candidates is rejected.
8. Invalid response triggers only 1 retry (max 2 attempts per signature).
9. After 2 failures, non-essential conflict becomes UNRESOLVED_SAFE.
10. After 2 failures, essential blocking conflict remains UNRESOLVED_BLOCKING.
11. Re-running the same document does not exceed signature limits and preserves deterministic results.
12. SourceSpan and JSON round trip.
"""
from __future__ import annotations

import copy
import json
import unittest
from typing import Any, Dict, List, Optional

from imagegen.analyzer_v3.arbitration import (
    ArbitrationAttemptRegistry,
    CoreferenceArbitrationRequest,
    CoreferenceArbitrationResponse,
    CoreferenceArbitrationValidator,
    TargetedArbitrationOrchestrator,
    V3GroqArbitrator,
)
from imagegen.analyzer_v3.models import (
    CoreferenceDecision,
    CoreferenceStatus,
    DocumentIR,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    PresenceRole,
    SectionIR,
    SourceSpan,
    SyntacticRole,
    VisualPolicy,
)
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline


class MockGroqArbitrator:
    """Mock arbitrator simulating various Groq responses, errors, and attempt tracking."""

    def __init__(self, handler=None):
        self.handler = handler
        self.calls: List[CoreferenceArbitrationRequest] = []
        self.usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165, "calls": 0}

    def arbitrate(self, request: CoreferenceArbitrationRequest) -> CoreferenceArbitrationResponse:
        self.calls.append(request)
        self.usage["calls"] += 1
        if self.handler:
            return self.handler(request, len(self.calls))
        # Default valid response
        decision_entity = request.allowed_entity_ids[0] if request.allowed_entity_ids else None
        return CoreferenceArbitrationResponse(
            mention_id=request.mention_id,
            decision=decision_entity,
            confidence=0.92,
            evidence_spans=[request.mention_span],
            visual_policy=VisualPolicy.REFERENT_CONFIRMED if decision_entity else VisualPolicy.SAFE_NEUTRAL,
            reason_code="SUBJECT_CONTINUATION",
            reason="Mock verified antecedent from context",
        )

    def get_usage(self) -> Dict[str, Any]:
        return dict(self.usage)


class TestV3GroqArbitration(unittest.TestCase):
    """Test suite for targeted Groq arbitration in Story Analyzer V3."""

    def setUp(self):
        self.pipeline = StoryAnalyzerV3Pipeline()

    # -------------------------------------------------------------------------
    # 1. Locally resolved decision does NOT call Groq
    # -------------------------------------------------------------------------
    def test_locally_resolved_decision_does_not_call_groq(self):
        """Unambiguous local pronoun resolution (RESOLVED_LOCAL) must never trigger Groq."""
        text = "Алексей вошел в комнату. Он включил свет."
        mock_arb = MockGroqArbitrator()
        pipe = StoryAnalyzerV3Pipeline(arbitrator=mock_arb)
        doc = pipe.analyze_structural(text)

        self.assertEqual(len(mock_arb.calls), 0, "RESOLVED_LOCAL must not call Groq")
        self.assertEqual(len(doc.coreferences), 1)
        self.assertEqual(doc.coreferences[0].status, CoreferenceStatus.RESOLVED_LOCAL)
        self.assertEqual(doc.metadata["groq_arbitration"]["requests_count"], 0)

    # -------------------------------------------------------------------------
    # 2. UNRESOLVED_SAFE does NOT call Groq
    # -------------------------------------------------------------------------
    def test_unresolved_safe_does_not_call_groq(self):
        """Safe ambiguous pronouns (UNRESOLVED_SAFE / SAFE_NEUTRAL) must never trigger Groq."""
        text = "Алексей посмотрел на Виктора. Он улыбнулся."
        mock_arb = MockGroqArbitrator()
        pipe = StoryAnalyzerV3Pipeline(arbitrator=mock_arb)
        doc = pipe.analyze_structural(text)

        # Subject-object ambiguity is UNRESOLVED_SAFE locally
        self.assertEqual(len(mock_arb.calls), 0, "UNRESOLVED_SAFE must not call Groq")
        self.assertEqual(doc.metadata["groq_arbitration"]["requests_count"], 0)
        self.assertTrue(any(c.status == CoreferenceStatus.UNRESOLVED_SAFE for c in doc.coreferences))

    # -------------------------------------------------------------------------
    # 3. Compact payload contract (no full story/chunk/scenes)
    # -------------------------------------------------------------------------
    def test_compact_payload_structure(self):
        """Request payload must contain only target mention, +/-2 event window, and candidate entities."""
        span = SourceSpan(start=10, end=12, text="он")
        req = CoreferenceArbitrationRequest(
            signature=CoreferenceArbitrationRequest.compute_signature("m_0", span, ["ent_0", "ent_1"]),
            mention_id="m_0",
            mention_span=span,
            event_id="ev_1",
            section_id="sec_0",
            event_text="Он открыл дверь.",
            context_events=[
                {"event_id": "ev_0", "section_id": "sec_0", "text": "Иван подошел к дому.", "actor_ids": ["ent_0"], "dialogue_speakers": []},
                {"event_id": "ev_1", "section_id": "sec_0", "text": "Он открыл дверь.", "actor_ids": [], "dialogue_speakers": []},
            ],
            candidates=[
                {"entity_id": "ent_0", "canonical_name": "Иван", "aliases": ["Иван"], "entity_type": "person", "gender": "masculine", "modality": "physical", "first_mention": None},
                {"entity_id": "ent_1", "canonical_name": "Петр", "aliases": ["Петр"], "entity_type": "person", "gender": "masculine", "modality": "physical", "first_mention": None},
            ],
            local_decision={"status": "unresolved_blocking", "confidence": 0.4},
            allowed_entity_ids=["ent_0", "ent_1"],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL", "IDENTITY_REQUIRED_UNRESOLVED"],
        )

        d = req.to_dict()
        # Verify no full story, chunk, scenes, or secrets in payload
        self.assertNotIn("full_story", d)
        self.assertNotIn("scenes", d)
        self.assertNotIn("api_key", d)
        self.assertIn("signature", d)
        self.assertEqual(len(d["candidates"]), 2)
        self.assertEqual(len(d["context_events"]), 2)

    # -------------------------------------------------------------------------
    # 4. Valid response updates only target CoreferenceDecision
    # -------------------------------------------------------------------------
    def test_valid_arbitration_updates_only_target_decision(self):
        """Valid response updates resolved_entity_ids, status to RESOLVED_CLOUD, and visual_policy."""
        text = "Сергей увидел капитана. Он отдал честь."
        # Inject UNRESOLVED_BLOCKING decision to simulate critical conflict
        doc = self.pipeline.analyze_structural(text)
        self.assertTrue(len(doc.coreferences) > 0)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities if e.entity_type == EntityType.PERSON]

        cand_id = target_dec.candidate_entity_ids[0]
        ev1 = next(ev for ev in doc.events if ev.event_id == target_dec.event_id)
        ev1.presences.append(
            EventEntityPresenceIR(
                presence_id=f"pres_{ev1.event_id}_{cand_id}",
                event_id=ev1.event_id,
                entity_id=cand_id,
                mention_id="m_custom",
                modality=EntityModalityV3.PHYSICAL,
                physical_presence=True,
                role=PresenceRole.ACTOR,
            )
        )

        def handler(req, call_count):
            return CoreferenceArbitrationResponse(
                mention_id=req.mention_id,
                decision=cand_id,
                confidence=0.95,
                evidence_spans=[req.mention_span],
                visual_policy=VisualPolicy.REFERENT_CONFIRMED,
                reason_code="MILITARY_RANK_CONTINUITY",
                reason="Subordinate actor salutes ranking officer",
            )

        mock_arb = MockGroqArbitrator(handler=handler)
        orchestrator = TargetedArbitrationOrchestrator()
        doc = orchestrator.arbitrate_document(doc, text, mock_arb)

        self.assertEqual(len(mock_arb.calls), 1)
        self.assertEqual(target_dec.status, CoreferenceStatus.RESOLVED_CLOUD)
        self.assertEqual(target_dec.resolved_entity_ids, [cand_id])
        self.assertEqual(target_dec.visual_policy, VisualPolicy.REFERENT_CONFIRMED)
        self.assertIn("GROQ_ARBITRATION:MILITARY_RANK_CONTINUITY", target_dec.reason_codes)
        self.assertEqual(doc.metadata["groq_arbitration"]["accepted_count"], 1)

    # -------------------------------------------------------------------------
    # 5. Unknown entity returned by Groq is rejected
    # -------------------------------------------------------------------------
    def test_unknown_entity_rejected(self):
        """Groq returning an entity_id not in candidates or doc_ir.entities is rejected."""
        text = "Сергей увидел капитана. Он отдал честь."
        doc = self.pipeline.analyze_structural(text)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities]

        def handler(req, call_count):
            return CoreferenceArbitrationResponse(
                mention_id=req.mention_id,
                decision="ent_alien_ghost_999",  # Invalid unknown entity
                confidence=0.99,
                evidence_spans=[req.mention_span],
                visual_policy=VisualPolicy.REFERENT_CONFIRMED,
                reason_code="UNKNOWN",
                reason="Invented character",
            )

        mock_arb = MockGroqArbitrator(handler=handler)
        orchestrator = TargetedArbitrationOrchestrator()
        doc = orchestrator.arbitrate_document(doc, text, mock_arb)

        # 2 attempts made, both rejected, signature exhausted
        self.assertEqual(len(mock_arb.calls), 2)
        self.assertEqual(doc.metadata["groq_arbitration"]["rejected_count"], 2)
        self.assertIn(target_dec.status, (CoreferenceStatus.UNRESOLVED_BLOCKING, CoreferenceStatus.UNRESOLVED_SAFE))
        self.assertNotIn("ent_alien_ghost_999", target_dec.resolved_entity_ids)

    # -------------------------------------------------------------------------
    # 6. Wrong coordinates or mention_id is rejected
    # -------------------------------------------------------------------------
    def test_wrong_coordinates_or_mention_id_rejected(self):
        """Responses with mismatched mention_id or out-of-bounds evidence spans are rejected."""
        text = "Сергей увидел капитана. Он отдал честь."
        doc = self.pipeline.analyze_structural(text)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities]

        cand_id = target_dec.candidate_entity_ids[0]

        # 1. Wrong mention_id
        resp1 = CoreferenceArbitrationResponse(
            mention_id="m_wrong_id",
            decision=cand_id,
            confidence=0.9,
            evidence_spans=[],
        )
        req = CoreferenceArbitrationRequest(
            signature="test_sig",
            mention_id=target_dec.mention_id,
            mention_span=SourceSpan(0, 5, "текст"),
            event_id="ev_0",
            section_id="sec_0",
            event_text="текст",
            context_events=[],
            candidates=[],
            local_decision={},
            allowed_entity_ids=[cand_id],
            allowed_visual_policies=["REFERENT_CONFIRMED"],
        )
        is_val1, err1 = CoreferenceArbitrationValidator.validate(resp1, req, doc, text)
        self.assertFalse(is_val1)
        self.assertIn("Mention ID mismatch", err1)

        # 2. Corrupt evidence span coordinate
        resp2 = CoreferenceArbitrationResponse(
            mention_id=target_dec.mention_id,
            decision=cand_id,
            confidence=0.9,
            evidence_spans=[SourceSpan(start=1000, end=1005, text="несуществующий_текст")],
        )
        is_val2, err2 = CoreferenceArbitrationValidator.validate(resp2, req, doc, text)
        self.assertFalse(is_val2)
        self.assertIn("does not match story text slice", err2)

    # -------------------------------------------------------------------------
    # 7. Physical confirmation of VOICE_ONLY/DEPICTED/MENTIONED is rejected
    # -------------------------------------------------------------------------
    def test_non_physical_entity_confirmation_rejected(self):
        """Groq attempting to assign REFERENT_CONFIRMED to VOICE_ONLY or DEPICTED entity is rejected."""
        text = "Мария услышала по радио голос Андрея. Он говорил тихо."
        doc = self.pipeline.analyze_structural(text)
        andrey = next(e for e in doc.entities if "андрей" in e.canonical_name.lower())
        andrey.modality = EntityModalityV3.VOICE_ONLY

        target_dec = doc.coreferences[0] if doc.coreferences else CoreferenceDecision(mention_id="m_test", candidate_entity_ids=[andrey.entity_id])
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [andrey.entity_id]

        req = CoreferenceArbitrationRequest(
            signature="test_sig",
            mention_id=target_dec.mention_id,
            mention_span=SourceSpan(0, 2, "Он"),
            event_id=doc.events[0].event_id,
            section_id="sec_0",
            event_text=doc.events[0].span.text,
            context_events=[{"event_id": doc.events[0].event_id}],
            candidates=[{"entity_id": andrey.entity_id}],
            local_decision={},
            allowed_entity_ids=[andrey.entity_id],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL"],
        )
        resp = CoreferenceArbitrationResponse(
            mention_id=target_dec.mention_id,
            decision=andrey.entity_id,
            confidence=0.9,
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
        )

        is_val, err = CoreferenceArbitrationValidator.validate(resp, req, doc, text)
        self.assertFalse(is_val)
        self.assertIn("Cannot assign REFERENT_CONFIRMED to entity", err)
        self.assertIn("non-physical modality", err)

    # -------------------------------------------------------------------------
    # 8. Invalid response triggers only 1 retry (max 2 attempts per signature)
    # -------------------------------------------------------------------------
    def test_retry_on_invalid_response_max_2_attempts(self):
        """First invalid response causes retry with diagnostic; second valid response is accepted."""
        text = "Сергей увидел капитана. Он отдал честь."
        doc = self.pipeline.analyze_structural(text)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities]

        valid_cand = target_dec.candidate_entity_ids[0]
        ev1 = next(ev for ev in doc.events if ev.event_id == target_dec.event_id)
        ev1.presences.append(
            EventEntityPresenceIR(
                presence_id=f"pres_{ev1.event_id}_{valid_cand}",
                event_id=ev1.event_id,
                entity_id=valid_cand,
                mention_id="m_custom",
                modality=EntityModalityV3.PHYSICAL,
                physical_presence=True,
                role=PresenceRole.ACTOR,
            )
        )

        def handler(req, call_count):
            if call_count == 1:
                # 1st attempt: invalid confidence
                return CoreferenceArbitrationResponse(
                    mention_id=req.mention_id,
                    decision=valid_cand,
                    confidence=1.5,  # Invalid
                    visual_policy=VisualPolicy.REFERENT_CONFIRMED,
                )
            else:
                # 2nd attempt: corrected
                self.assertIsNotNone(req.diagnostic_retry_reason)
                self.assertIn("Confidence 1.5 outside valid", req.diagnostic_retry_reason)
                return CoreferenceArbitrationResponse(
                    mention_id=req.mention_id,
                    decision=valid_cand,
                    confidence=0.9,
                    visual_policy=VisualPolicy.REFERENT_CONFIRMED,
                )

        mock_arb = MockGroqArbitrator(handler=handler)
        orchestrator = TargetedArbitrationOrchestrator()
        doc = orchestrator.arbitrate_document(doc, text, mock_arb)

        self.assertEqual(len(mock_arb.calls), 2)
        self.assertEqual(doc.metadata["groq_arbitration"]["retries_count"], 1)
        self.assertEqual(doc.metadata["groq_arbitration"]["accepted_count"], 1)
        self.assertEqual(target_dec.status, CoreferenceStatus.RESOLVED_CLOUD)

    # -------------------------------------------------------------------------
    # 9. After 2 errors, non-essential conflict becomes UNRESOLVED_SAFE
    # -------------------------------------------------------------------------
    def test_non_essential_conflict_exhaustion_fallback_to_safe_neutral(self):
        """Exhausted non-essential pronoun becomes UNRESOLVED_SAFE / SAFE_NEUTRAL."""
        text = "Дмитрий вошел в комнату. Рядом с ним стоял чемодан."
        doc = self.pipeline.analyze_structural(text)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities]

        def handler(req, call_count):
            raise RuntimeError("Cloud service timeout")

        mock_arb = MockGroqArbitrator(handler=handler)
        orchestrator = TargetedArbitrationOrchestrator()
        doc = orchestrator.arbitrate_document(doc, text, mock_arb)

        self.assertEqual(len(mock_arb.calls), 2)
        self.assertEqual(target_dec.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(target_dec.visual_policy, VisualPolicy.SAFE_NEUTRAL)
        self.assertIn("neutralized safely", target_dec.reason)

    # -------------------------------------------------------------------------
    # 10. After 2 errors, essential blocking conflict remains UNRESOLVED_BLOCKING
    # -------------------------------------------------------------------------
    def test_essential_blocking_conflict_exhaustion_remains_blocking(self):
        """Exhausted primary actor conflict remains UNRESOLVED_BLOCKING with diagnostic."""
        text = "Сергей и капитан вышли на плац. Он отдал честь."
        doc = self.pipeline.analyze_structural(text)
        target_dec = doc.coreferences[0]
        target_dec.status = CoreferenceStatus.UNRESOLVED_BLOCKING
        target_dec.candidate_entity_ids = [e.entity_id for e in doc.entities]

        def handler(req, call_count):
            return CoreferenceArbitrationResponse(
                mention_id=req.mention_id,
                decision="ent_invalid_non_candidate",
                confidence=0.8,
            )

        mock_arb = MockGroqArbitrator(handler=handler)
        orchestrator = TargetedArbitrationOrchestrator()
        doc = orchestrator.arbitrate_document(doc, text, mock_arb)

        self.assertEqual(len(mock_arb.calls), 2)
        self.assertEqual(target_dec.status, CoreferenceStatus.UNRESOLVED_BLOCKING)
        self.assertEqual(target_dec.visual_policy, VisualPolicy.IDENTITY_REQUIRED_UNRESOLVED)
        self.assertIn("Cloud arbitration exhausted", target_dec.reason)
        self.assertEqual(doc.metadata["groq_arbitration"]["residual_blocking_conflicts"], 1)

    # -------------------------------------------------------------------------
    # 11. Repeated run on same document does not exceed signature limits
    # -------------------------------------------------------------------------
    def test_repeated_run_preserves_deterministic_signature_limit(self):
        """Repeated pass on document with shared registry does not re-issue exhausted requests."""
        text = "Сергей и капитан вышли на плац. Он отдал честь."
        doc = self.pipeline.analyze_structural(text)
        doc.coreferences[0].status = CoreferenceStatus.UNRESOLVED_BLOCKING
        doc.coreferences[0].candidate_entity_ids = [e.entity_id for e in doc.entities]

        def handler(req, call_count):
            raise RuntimeError("Persistent network loss")

        mock_arb = MockGroqArbitrator(handler=handler)
        registry = ArbitrationAttemptRegistry(max_attempts_per_signature=2)
        orchestrator = TargetedArbitrationOrchestrator(registry=registry)

        # Pass 1: exhausts 2 attempts
        doc1 = orchestrator.arbitrate_document(doc, text, mock_arb)
        self.assertEqual(len(mock_arb.calls), 2)

        # Pass 2: should make 0 additional calls because signature is exhausted
        doc2 = copy.deepcopy(doc1)
        doc2.coreferences[0].status = CoreferenceStatus.UNRESOLVED_BLOCKING
        doc2 = orchestrator.arbitrate_document(doc2, text, mock_arb)
        self.assertEqual(len(mock_arb.calls), 2, "Must not exceed max 2 attempts for exhausted signature")

    # -------------------------------------------------------------------------
    # 12. JSON round-trip and SourceSpan preservation
    # -------------------------------------------------------------------------
    def test_arbitration_models_json_round_trip(self):
        """CoreferenceArbitrationRequest and Response serialize and deserialize without data loss."""
        span = SourceSpan(10, 15, "Виктор")
        req = CoreferenceArbitrationRequest(
            signature="test_sig_123",
            mention_id="m_1",
            mention_span=span,
            event_id="ev_0",
            section_id="sec_0",
            event_text="Виктор подошел.",
            context_events=[{"event_id": "ev_0", "text": "Виктор подошел."}],
            candidates=[{"entity_id": "ent_0", "canonical_name": "Виктор"}],
            local_decision={"confidence": 0.5},
            allowed_entity_ids=["ent_0"],
            allowed_visual_policies=["REFERENT_CONFIRMED"],
            diagnostic_retry_reason="Retry reason",
        )
        req_dict = req.to_dict()
        req_restored = CoreferenceArbitrationRequest.from_dict(req_dict)
        self.assertEqual(req, req_restored)

        resp = CoreferenceArbitrationResponse(
            mention_id="m_1",
            decision="ent_0",
            confidence=0.88,
            evidence_spans=[span],
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
            reason_code="SUBJECT_CONTINUITY",
            reason="Clear antecedent",
        )
        resp_dict = resp.to_dict()
        resp_restored = CoreferenceArbitrationResponse.from_dict(resp_dict)
        self.assertEqual(resp.mention_id, resp_restored.mention_id)
        self.assertEqual(resp.decision, resp_restored.decision)
        self.assertEqual(resp.confidence, resp_restored.confidence)
        self.assertEqual(resp.visual_policy, resp_restored.visual_policy)

    def test_multiline_api_key_pool_is_split_and_never_logged(self):
        from imagegen.analyzer_v3.arbitration import V3GroqArbitrator, redact_groq_secrets
        arb = V3GroqArbitrator("gsk_first\ngsk_second;gsk_third,gsk_fourth")
        self.assertEqual(arb.api_keys, ["gsk_first", "gsk_second", "gsk_third", "gsk_fourth"])
        self.assertEqual(redact_groq_secrets("Bearer gsk_secret123 failed"), "Bearer [REDACTED_GROQ_KEY] failed")


    # -------------------------------------------------------------------------
    # 13. Modality & Presence Rejections: MENTIONED & PERCEIVED_VOICE
    # -------------------------------------------------------------------------
    def test_mentioned_modality_confirmation_rejected(self):
        """Entity with modality=MENTIONED cannot be confirmed as physical referent."""
        text = "Иван встретил Петра. Он был мастером."
        doc = self.pipeline.analyze_structural(text)
        petr = next(e for e in doc.entities if "петр" in e.canonical_name.lower() or "пётр" in e.canonical_name.lower())
        petr.modality = EntityModalityV3.MENTIONED

        req = CoreferenceArbitrationRequest(
            signature="test_sig_ment",
            mention_id="m_test",
            mention_span=SourceSpan(0, 2, "Он"),
            event_id=doc.events[0].event_id,
            section_id="sec_0",
            event_text=doc.events[0].span.text,
            context_events=[{"event_id": doc.events[0].event_id}],
            candidates=[{"entity_id": petr.entity_id}],
            local_decision={},
            allowed_entity_ids=[petr.entity_id],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL"],
        )
        resp = CoreferenceArbitrationResponse(
            mention_id="m_test",
            decision=petr.entity_id,
            confidence=0.9,
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
            reason_code="TEST",
            reason="Test",
        )
        is_val, err = CoreferenceArbitrationValidator.validate(resp, req, doc, text)
        self.assertFalse(is_val)
        self.assertIn("non-physical modality 'mentioned'", err)

    def test_perceived_voice_modality_confirmation_rejected(self):
        """Entity with modality=PERCEIVED_VOICE cannot be confirmed as physical referent."""
        text = "Ивану показалось, что он услышал голос отца."
        doc = self.pipeline.analyze_structural(text)
        father = doc.entities[0]
        father.modality = EntityModalityV3.PERCEIVED_VOICE

        req = CoreferenceArbitrationRequest(
            signature="test_sig_pv",
            mention_id="m_test",
            mention_span=SourceSpan(0, 2, "Он"),
            event_id=doc.events[0].event_id,
            section_id="sec_0",
            event_text=doc.events[0].span.text,
            context_events=[{"event_id": doc.events[0].event_id}],
            candidates=[{"entity_id": father.entity_id}],
            local_decision={},
            allowed_entity_ids=[father.entity_id],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL"],
        )
        resp = CoreferenceArbitrationResponse(
            mention_id="m_test",
            decision=father.entity_id,
            confidence=0.9,
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
            reason_code="TEST",
            reason="Test",
        )
        is_val, err = CoreferenceArbitrationValidator.validate(resp, req, doc, text)
        self.assertFalse(is_val)
        self.assertIn("non-physical modality 'perceived_voice'", err)

    def test_event_non_physical_presence_confirmation_rejected(self):
        """Globally PHYSICAL entity with only non-physical presence in target event cannot get REFERENT_CONFIRMED."""
        text = "Мария услышала по телефону голос Андрея."
        doc = self.pipeline.analyze_structural(text)
        andrey = next(e for e in doc.entities if "андрей" in e.canonical_name.lower())
        # Andrey is globally PHYSICAL or VOICE_ONLY, ensure presence in event is non-physical
        ev = doc.events[0]
        for p in ev.presences:
            if p.entity_id == andrey.entity_id:
                p.physical_presence = False
                p.modality = EntityModalityV3.VOICE_ONLY

        andrey.modality = EntityModalityV3.PHYSICAL  # Globally physical human

        req = CoreferenceArbitrationRequest(
            signature="test_sig_ev_nonphys",
            mention_id="m_test",
            mention_span=SourceSpan(0, 5, "Мария"),
            event_id=ev.event_id,
            section_id=ev.section_id,
            event_text=ev.span.text,
            context_events=[{"event_id": ev.event_id}],
            candidates=[{"entity_id": andrey.entity_id}],
            local_decision={},
            allowed_entity_ids=[andrey.entity_id],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL"],
        )
        resp = CoreferenceArbitrationResponse(
            mention_id="m_test",
            decision=andrey.entity_id,
            confidence=0.9,
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
            reason_code="TEST",
            reason="Test",
        )
        is_val, err = CoreferenceArbitrationValidator.validate(resp, req, doc, text)
        self.assertFalse(is_val)
        self.assertIn("has no physical presence in event", err)

    def test_globally_physical_entity_with_no_event_presence_rejected(self):
        """Globally PHYSICAL entity with NO EventEntityPresenceIR in target event must reject REFERENT_CONFIRMED."""
        text = "Алексей вошел в комнату. В соседнем зале погас свет."
        doc = self.pipeline.analyze_structural(text)
        alex = next(e for e in doc.entities if "алексей" in e.canonical_name.lower())
        alex.modality = EntityModalityV3.PHYSICAL

        # Target event 1 has no presences for Alex
        ev1 = doc.events[1]
        self.assertFalse(any(p.entity_id == alex.entity_id for p in ev1.presences))

        req = CoreferenceArbitrationRequest(
            signature="test_sig_no_pres",
            mention_id="m_test_no_pres",
            mention_span=SourceSpan(0, 7, "Алексей"),
            event_id=ev1.event_id,
            section_id=ev1.section_id,
            event_text=ev1.span.text,
            context_events=[{"event_id": ev1.event_id}],
            candidates=[{"entity_id": alex.entity_id}],
            local_decision={},
            allowed_entity_ids=[alex.entity_id],
            allowed_visual_policies=["REFERENT_CONFIRMED", "SAFE_NEUTRAL"],
        )
        resp = CoreferenceArbitrationResponse(
            mention_id="m_test_no_pres",
            decision=alex.entity_id,
            confidence=0.9,
            visual_policy=VisualPolicy.REFERENT_CONFIRMED,
            reason_code="TEST",
            reason="Test",
        )
        is_val, err = CoreferenceArbitrationValidator.validate(resp, req, doc, text)
        self.assertFalse(is_val)
        self.assertIn("has no presence in target event", err)

    # -------------------------------------------------------------------------
    # 14. Fail-Closed Parsing: Missing Fields & Unknown Enum
    # -------------------------------------------------------------------------
    def test_missing_required_response_fields_fail_closed(self):
        """Missing required response fields must raise ValueError, not silently default."""
        with self.assertRaises(ValueError) as ctx:
            CoreferenceArbitrationResponse.from_dict({
                "mention_id": "m_0",
                # Missing 'confidence'
                "visual_policy": "REFERENT_CONFIRMED",
                "reason_code": "TEST",
                "reason": "Test",
            })
        self.assertIn("Missing required response field 'confidence'", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            CoreferenceArbitrationResponse.from_dict({
                "mention_id": "m_0",
                "confidence": 0.8,
                "visual_policy": "REFERENT_CONFIRMED",
                # Missing 'reason_code'
                "reason": "Test",
            })
        self.assertIn("Missing required response field 'reason_code'", str(ctx2.exception))

    def test_unknown_visual_policy_fail_closed(self):
        """Unknown visual_policy value must raise ValueError in from_dict."""
        with self.assertRaises(ValueError) as ctx:
            CoreferenceArbitrationResponse.from_dict({
                "mention_id": "m_0",
                "confidence": 0.8,
                "visual_policy": "UNKNOWN_INVENTED_POLICY_XYZ",
                "reason_code": "TEST",
                "reason": "Test",
            })
        self.assertIn("Unknown visual_policy value", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 15. Pipeline from_settings Factory
    # -------------------------------------------------------------------------
    def test_pipeline_from_settings_deterministic_default(self):
        """Default settings create pipeline with arbitrator=None (100% deterministic & local)."""
        pipe1 = StoryAnalyzerV3Pipeline.from_settings({})
        self.assertIsNone(pipe1.arbitrator)

        pipe2 = StoryAnalyzerV3Pipeline.from_settings({"text_backend": "local", "groq_api_key": ""})
        self.assertIsNone(pipe2.arbitrator)

        # Enabled when groq key + hybrid/groq backend configured
        pipe3 = StoryAnalyzerV3Pipeline.from_settings({
            "text_backend": "hybrid",
            "groq_api_key": "gsk_dummy_test_key_12345",
            "groq_model": "openai/gpt-oss-120b",
        })
        self.assertIsNotNone(pipe3.arbitrator)
        self.assertEqual(pipe3.arbitrator.model, "openai/gpt-oss-120b")
        self.assertTrue(pipe3.semantic_first_enabled)


if __name__ == "__main__":
    unittest.main()


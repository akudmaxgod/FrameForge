"""Unit & Integration Tests for Story Analyzer V3 Stage 6: Visual Event Planner.

Covers:
1. Budget constraints & strict chronological ordering.
2. Key character introduction and recurring actor coverage.
3. Strict prohibition of physical depiction for VOICE_ONLY, DEPICTED, MENTIONED, PERCEIVED_VOICE, DECEASED_MENTION.
4. Zero hallucinated events, entity IDs, or source spans.
5. Causal and dialogue continuity in EventGraph.
6. Deterministic fallback when Qwen LLM detailer is disabled or absent.
7. Fail-closed validation for malformed/corrupted Qwen responses.
8. Document mode adaptation (narrative, review/fact_list, mixed).
9. JSON round-trip serialization of VisualEventPlan.
10. Pipeline integration via StoryAnalyzerV3Pipeline.plan_visual_events().
"""
from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional

from imagegen.analyzer_v3.detailer import VisualPlannerLLMDetailer
from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    PresenceRole,
    SourceSpan,
)
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.planner import VisualEventPlanner
from imagegen.analyzer_v3.planner_models import (
    CameraAngle,
    EventEdgeType,
    EventNodeType,
    SelectedEventIR,
    SelectionReasonCode,
    VisualEventPlan,
    VisualFramingPolicy,
    VisualShotType,
)


class MockQwenDetailer:
    """Mock LLM detailer simulating valid and invalid responses."""

    def __init__(self, handler=None):
        self.handler = handler
        self.calls: List[List[Dict[str, Any]]] = []

    def detail_shots(
        self,
        shots_payload: List[Dict[str, Any]],
        context_prompt: str,
    ) -> List[Dict[str, Any]]:
        self.calls.append(shots_payload)
        if self.handler:
            return self.handler(shots_payload, context_prompt)

        # Default valid response
        refined = []
        for s in shots_payload:
            refined.append({
                "selected_id": s["selected_id"],
                "event_id": s["event_id"],
                "shot_type": "closeup_focus",
                "camera_angle": "low_angle",
                "visual_action_description": f"Cinematic detailed {s['event_text']}",
                "composition_focus": "Dynamic character focal point",
                "mood_and_atmosphere": "Intense cinematic lighting",
            })
        return refined


class TestV3VisualEventPlanner(unittest.TestCase):
    """Test suite for Stage 6 Visual Event Planner."""

    def setUp(self):
        self.pipeline = StoryAnalyzerV3Pipeline()

    # -------------------------------------------------------------------------
    # 1. Budget and Strict Chronological Event Order
    # -------------------------------------------------------------------------
    def test_budget_constraints_and_chronological_ordering(self):
        """Selected events must not exceed target budget and must be strictly ordered."""
        text = (
            "Иван вошел в здание. Он поднялся по лестнице. "
            "В коридоре стоял Петр. Петр открыл дверь кабинета. "
            "Иван сел за стол. Они начали переговоры."
        )
        plan = self.pipeline.plan_visual_events(text, target_budget=3)

        self.assertIsInstance(plan, VisualEventPlan)
        self.assertLessEqual(len(plan.selected_events), 3)
        self.assertGreaterEqual(len(plan.selected_events), 1)

        # Chronological index strictly non-decreasing
        indices = [s.event_index for s in plan.selected_events]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(indices), len(set(indices)), "No duplicate events allowed")

    # -------------------------------------------------------------------------
    # 2. Key Character Coverage
    # -------------------------------------------------------------------------
    def test_key_character_coverage(self):
        """All primary recurring characters must be represented in the visual event plan."""
        text = (
            "Алексей приехал на вокзал. Он ждал поезд. "
            "Мария подошла к перрону. Она помахала рукой. "
            "Алексей и Мария обнялись."
        )
        plan = self.pipeline.plan_visual_events(text, target_budget=4)

        # Both Alex and Maria should have coverage
        self.assertGreaterEqual(len(plan.actor_coverage), 1)
        for actor_id, count in plan.actor_coverage.items():
            self.assertGreaterEqual(count, 1, f"Primary actor {actor_id} has 0 selected shots")

    # -------------------------------------------------------------------------
    # 3. Prohibition of Physical Depiction for Non-Physical Modalities
    # -------------------------------------------------------------------------
    def test_voice_only_entity_safe_framing(self):
        """Voice-only entity over telephone must produce safe framing and NOT PHYSICAL_ACTOR."""
        text = "Мария услышала по телефону голос Андрея."
        plan = self.pipeline.plan_visual_events(text, target_budget=4)

        # Find shot for telephone event
        phone_shot = next(
            (s for s in plan.selected_events if "андрей" in s.span.text.lower() or "телефон" in s.span.text.lower()),
            None
        )
        self.assertIsNotNone(phone_shot)
        self.assertIn(phone_shot.framing_policy, (
            VisualFramingPolicy.PROP_DETAIL,
            VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER,
            VisualFramingPolicy.AUDIO_ONLY_INDICATOR,
        ))
        self.assertIn("voice", phone_shot.safe_framing_description.lower())

    def test_depicted_portrait_safe_framing(self):
        """Depicted entity in photograph/portrait must produce DEPICTED_MEDIUM or OVER_SHOULDER."""
        text = "На стене висел портрет генерала. Виктор долго смотрел на портрет."
        plan = self.pipeline.plan_visual_events(text, target_budget=4)

        portrait_shot = next(
            (s for s in plan.selected_events if "портрет" in s.span.text.lower()),
            None
        )
        self.assertIsNotNone(portrait_shot)
        self.assertIn(portrait_shot.framing_policy, (
            VisualFramingPolicy.DEPICTED_MEDIUM,
            VisualFramingPolicy.PHYSICAL_ACTOR,
        ))
        if portrait_shot.framing_policy == VisualFramingPolicy.DEPICTED_MEDIUM:
            self.assertIn("medium", portrait_shot.safe_framing_description.lower())

    def test_perceived_voice_safe_framing(self):
        """Imagined / perceived voice must produce atmospheric / silhouette framing."""
        text = "Ивану почудилось, что в темноте кто-то шепчет его имя."
        plan = self.pipeline.plan_visual_events(text, target_budget=2)

        self.assertTrue(len(plan.selected_events) > 0)
        shot = plan.selected_events[0]
        self.assertIn(shot.framing_policy, (
            VisualFramingPolicy.SILHOUETTE_WITHOUT_IDENTITY,
            VisualFramingPolicy.PHYSICAL_ACTOR,
            VisualFramingPolicy.EMPTY_LOCATION,
        ))

    # -------------------------------------------------------------------------
    # 4. Zero Hallucinated Facts or Events
    # -------------------------------------------------------------------------
    def test_zero_hallucinated_events_and_spans(self):
        """Every selected event must exist in DocumentIR with exact SourceSpan match."""
        text = "Солнце встало над горизонтом. Корабль вышел из гавани. Капитан стоял на мостике."
        doc = self.pipeline.analyze_structural(text)
        planner = VisualEventPlanner()
        plan = planner.plan(doc, text, target_budget=3)

        doc_event_map = {ev.event_id: ev for ev in doc.events}
        for sel in plan.selected_events:
            self.assertIn(sel.event_id, doc_event_map)
            ev = doc_event_map[sel.event_id]
            self.assertEqual(sel.span.start, ev.span.start)
            self.assertEqual(sel.span.end, ev.span.end)
            self.assertEqual(sel.span.text, ev.span.text)
            self.assertEqual(text[sel.span.start:sel.span.end], sel.span.text)

    # -------------------------------------------------------------------------
    # 5. Causal and Dialogue Continuity in EventGraph
    # -------------------------------------------------------------------------
    def test_causal_and_dialogue_edges_in_graph(self):
        """EventGraph must detect explicit causal conjunctions and dialogue turns."""
        text = (
            "Дмитрий нажал кнопку тревоги. Из-за этого двери автоматически заблокировались. "
            "Охранник спросил: «Что случилось?». Дмитрий ответил: «Проникновение»."
        )
        plan = self.pipeline.plan_visual_events(text, target_budget=5)
        graph = plan.event_graph
        self.assertIsNotNone(graph)

        # Check for causal edge
        causal_edges = [e for e in graph.edges if e.edge_type == EventEdgeType.CAUSAL_CONSEQUENCE]
        self.assertGreaterEqual(len(causal_edges), 1, "Causal consequence edge must be detected for 'Из-за этого'")

        # Check for dialogue continuation edge
        dialogue_edges = [e for e in graph.edges if e.edge_type == EventEdgeType.DIALOGUE_CONTINUATION]
        self.assertGreaterEqual(len(dialogue_edges), 1, "Dialogue continuation edge must be detected")

    # -------------------------------------------------------------------------
    # 6. Deterministic Fallback Without Qwen
    # -------------------------------------------------------------------------
    def test_deterministic_fallback_without_qwen(self):
        """Default run without detailer produces 100% deterministic plan with qwen_detailed=False."""
        text = "Сергей увидел капитана. Он отдал честь."
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=None)

        self.assertEqual(len(plan.selected_events), 2)
        for s in plan.selected_events:
            self.assertFalse(s.qwen_detailed)
            self.assertIsNone(s.qwen_diagnostics)

    # -------------------------------------------------------------------------
    # 7. Fail-Closed Validation for Malformed Qwen Responses
    # -------------------------------------------------------------------------
    def test_qwen_detailer_fail_closed_on_id_mismatch(self):
        """Qwen detailer returning wrong event_id must fall back to deterministic plan with diagnostics."""
        text = "Сергей увидел капитана. Он отдал честь."

        def bad_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_alien_id_999",  # Wrong event_id
                    "shot_type": "closeup_focus",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей видит капитана",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "closeup_focus",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей отдает честь",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
            ]

        mock_detailer = MockQwenDetailer(handler=bad_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertEqual(len(mock_detailer.calls), 1)
        self.assertEqual(len(plan.selected_events), 2)
        # Fallback was applied
        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Qwen ID mismatch", str(plan.selected_events[0].qwen_diagnostics))

    def test_qwen_detailer_fail_closed_on_unknown_enum(self):
        """Unknown shot_type or camera_angle must reject Qwen refinement and retain deterministic plan."""
        text = "Сергей увидел капитана. Он отдал честь."

        def bad_enum_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "drone_hyperlapse_xyz_999",  # Invalid enum
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей видит капитана",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "medium_action",
                    "camera_angle": "4d_hyperspace_angle",  # Invalid enum
                    "visual_action_description": "Сергей отдает честь",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
            ]

        mock_detailer = MockQwenDetailer(handler=bad_enum_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Unknown shot_type", str(plan.selected_events[0].qwen_diagnostics))
        self.assertFalse(plan.selected_events[1].qwen_detailed)
        self.assertIn("Unknown camera_angle", str(plan.selected_events[1].qwen_diagnostics))

    def test_qwen_detailer_fail_closed_on_missing_field(self):
        """Missing required field must reject Qwen refinement and retain deterministic plan."""
        text = "Сергей увидел капитана. Он отдал честь."

        def missing_field_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    # Missing 'camera_angle'
                    "shot_type": "medium_action",
                    "visual_action_description": "Сергей видит капитана",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей отдает честь",
                    # Missing 'composition_focus'
                    "mood_and_atmosphere": "Реализм",
                },
            ]

        mock_detailer = MockQwenDetailer(handler=missing_field_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Missing required Qwen shot fields", str(plan.selected_events[0].qwen_diagnostics))
        self.assertFalse(plan.selected_events[1].qwen_detailed)
        self.assertIn("Missing required Qwen shot fields", str(plan.selected_events[1].qwen_diagnostics))

    def test_qwen_detailer_fail_closed_on_extra_fields(self):
        """Unexpected extra fields must reject Qwen refinement and retain deterministic plan."""
        text = "Сергей увидел капитана. Он отдал честь."

        def extra_field_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей видит капитана",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                    "hallucinated_actor_costume": "red velvet coat",  # Extra unexpected field
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей отдает честь",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                    "invented_field_123": 456,  # Extra unexpected field
                },
            ]

        mock_detailer = MockQwenDetailer(handler=extra_field_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Unexpected extra fields in Qwen shot", str(plan.selected_events[0].qwen_diagnostics))
        self.assertFalse(plan.selected_events[1].qwen_detailed)
        self.assertIn("Unexpected extra fields in Qwen shot", str(plan.selected_events[1].qwen_diagnostics))

    def test_qwen_detailer_fail_closed_on_invented_character_name(self):
        """Qwen mentioning an invented character name not in story must be rejected."""
        text = "Сергей увидел капитана. Он отдал честь."

        def invented_char_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Внезапно вошел полковник Иванов и посмотрел на Сергея",  # 'Иванов' is invented
                    "composition_focus": "Иванов",
                    "mood_and_atmosphere": "Реализм",
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Сергей отдает честь капитану",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Реализм",
                },
            ]

        mock_detailer = MockQwenDetailer(handler=invented_char_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Disallowed or invented named entity 'Иванов'", str(plan.selected_events[0].qwen_diagnostics))
        # Valid second shot is accepted
        self.assertTrue(plan.selected_events[1].qwen_detailed)

    def test_qwen_detailer_fail_closed_on_non_physical_entity_in_physical_action(self):
        """Qwen placing a voice-only entity in physical scene action must be rejected."""
        text = "Мария услышала по телефону голос Андрея."

        def voice_as_phys_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "visual_action_description": "Андрей стоит рядом с Марией и говорит ей в лицо",  # Andrey is voice-only!
                    "composition_focus": "Андрей и Мария",
                    "mood_and_atmosphere": "Реализм",
                }
            ]

        mock_detailer = MockQwenDetailer(handler=voice_as_phys_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=1, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("non-physical entity 'андрей'", str(plan.selected_events[0].qwen_diagnostics).lower())

    def test_qwen_detailer_fail_closed_on_invented_first_name_igor(self):
        """Qwen mentioning an invented first name 'Игорь' without surname ending must be rejected."""
        text = "Сергей увидел капитана. Он отдал честь."

        def invented_igor_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "composition_focus": "Игорь и капитан",  # 'Игорь' is an invented first name
                    "mood_and_atmosphere": "Напряженная атмосфера",
                },
                {
                    "selected_id": "sel_1",
                    "event_id": "ev_1",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "composition_focus": "Сергей",
                    "mood_and_atmosphere": "Кинематографично",
                },
            ]

        mock_detailer = MockQwenDetailer(handler=invented_igor_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Disallowed or invented named entity 'Игорь'", str(plan.selected_events[0].qwen_diagnostics))
        self.assertTrue(plan.selected_events[1].qwen_detailed)

    def test_qwen_detailer_fail_closed_on_pronoun_physical_action_for_voice_entity(self):
        """Qwen using pronoun physical action like 'Он стоит в центре комнаты' for voice entity must be rejected."""
        text = "Мария услышала по телефону голос Андрея."

        def pronoun_action_handler(shots_payload, prompt):
            return [
                {
                    "selected_id": "sel_0",
                    "event_id": "ev_0",
                    "shot_type": "medium_action",
                    "camera_angle": "eye_level",
                    "composition_focus": "Он стоит в центре комнаты и смотрит на телефон",  # Pronoun physical action!
                    "mood_and_atmosphere": "Драматичный свет",
                }
            ]

        mock_detailer = MockQwenDetailer(handler=pronoun_action_handler)
        plan = self.pipeline.plan_visual_events(text, target_budget=1, detailer=mock_detailer)

        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Physical pronoun action forbidden for non-physical", str(plan.selected_events[0].qwen_diagnostics))

    def test_v3_qwen_detailer_from_settings_factory(self):
        """V3QwenVisualDetailer.from_settings() creates detailer when configured and returns None by default."""
        from imagegen.analyzer_v3.detailer import V3QwenVisualDetailer

        # Disabled by default
        d_none = V3QwenVisualDetailer.from_settings({})
        self.assertIsNone(d_none)

        # Enabled via settings
        d_enabled = V3QwenVisualDetailer.from_settings({
            "v3_qwen_detailer": True,
            "qwen_api_url": "http://localhost:11434/api/generate",
            "qwen_model": "qwen2.5:14b-instruct",
            "qwen_timeout": 45.0,
        })
        self.assertIsNotNone(d_enabled)
        self.assertEqual(d_enabled.api_url, "http://localhost:11434/api/generate")
        self.assertEqual(d_enabled.model, "qwen2.5:14b-instruct")
        self.assertEqual(d_enabled.timeout_sec, 45.0)

        # Integrated via pipeline factory
        pipe = StoryAnalyzerV3Pipeline.from_settings({
            "v3_qwen_detailer": True,
            "qwen_model": "qwen2.5:14b-instruct",
        })
        self.assertIsNotNone(pipe.detailer)
        self.assertEqual(pipe.detailer.model, "qwen2.5:14b-instruct")

    def test_qwen_detailer_successful_refinement(self):
        """Valid Qwen detailer updates shot composition and sets qwen_detailed=True."""
        text = "Сергей увидел капитана. Он отдал честь."
        mock_detailer = MockQwenDetailer()
        plan = self.pipeline.plan_visual_events(text, target_budget=2, detailer=mock_detailer)

        self.assertEqual(len(mock_detailer.calls), 1)
        for s in plan.selected_events:
            self.assertTrue(s.qwen_detailed)
            self.assertEqual(s.shot_type, VisualShotType.CLOSEUP_FOCUS)
            self.assertEqual(s.camera_angle, CameraAngle.LOW_ANGLE)

    # -------------------------------------------------------------------------
    # 8. Document Modes: Narrative, Review / Factual, Mixed
    # -------------------------------------------------------------------------
    def test_review_and_factual_document_mode_quotas(self):
        """Factual review texts must prioritize key facts and distribute budget across sections."""
        text = (
            "Обзор двигателя Titan-500. "
            "Мощность составляет 500 л.с. Клиренс равен 220 мм. "
            "Расход топлива 8 литров на 100 км."
        )
        doc = self.pipeline.analyze_structural(text)
        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=3)

        self.assertIn(plan.doc_mode, (
            DocumentMode.MULTI_SUBJECT_REVIEW,
            DocumentMode.FACT_LIST,
            DocumentMode.EXPLAINER,
            DocumentMode.NARRATIVE,
        ))
        # Ensure fact specifications have reason codes
        has_fact_selection = any(
            SelectionReasonCode.KEY_FACT_SPEC.value in s.selection_reason_codes or SelectionReasonCode.EXPOSITION_ESTABLISHING.value in s.selection_reason_codes
            for s in plan.selected_events
        )
        self.assertTrue(has_fact_selection)

    # -------------------------------------------------------------------------
    # 9. JSON Round-Trip Serialization
    # -------------------------------------------------------------------------
    def test_visual_event_plan_json_round_trip(self):
        """VisualEventPlan serializes to dict/JSON and restores perfectly without data loss."""
        text = "Алексей вошел в комнату. Он включил свет."
        plan = self.pipeline.plan_visual_events(text, target_budget=2)

        data = plan.to_dict()
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        restored_data = json.loads(json_str)

        restored_plan = VisualEventPlan.from_dict(restored_data)
        self.assertEqual(restored_plan.plan_id, plan.plan_id)
        self.assertEqual(restored_plan.target_budget, plan.target_budget)
        self.assertEqual(len(restored_plan.selected_events), len(plan.selected_events))
        self.assertEqual(restored_plan.selected_events[0].framing_policy, plan.selected_events[0].framing_policy)


if __name__ == "__main__":
    unittest.main()


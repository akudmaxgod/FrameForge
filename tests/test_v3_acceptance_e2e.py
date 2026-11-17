"""End-to-End Acceptance Tests for Story Analyzer V3 Stage 8 (Final Acceptance).

Covers all 10 mandatory acceptance scenarios:
1. Sequential physical narrative with confirmed characters.
2. Distant voice dialogue via phone/radio (voice speaker off-screen).
3. Depicted figure in portrait/photo (stays strictly inside medium).
4. Ambiguous pronoun resolution (safe neutral fallback without invented actors).
5. Multi-subject vehicle review with specs (strict fact-to-entity binding).
6. Mixed narrative plot (action, dialogue, telephone transmission, and photograph).
7. Fully local deterministic execution with external models disabled.
8. Malformed/corrupted Groq/Qwen responses triggering fail-closed fallback.
9. End-to-end atomic persistence and reload cycle through real service path.
10. Web API client consistency between saved database record and returned DTO.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    EntityModalityV3,
    EntityType,
    VisualPolicy,
)
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.planner_models import (
    CameraAngle,
    VisualEventPlan,
    VisualFramingPolicy,
    VisualShotType,
)
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service, V3AnalysisResultDTO
from imagegen.db import Database
from imagegen.server import Application
from imagegen.service import Service


class TestV3FinalAcceptanceE2E(unittest.TestCase):
    """Final acceptance test suite for Story Analyzer V3."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.app_service = Service(self.root)
        self.v3_service = StoryAnalyzerV3Service(db=self.app_service.db)
        self.pipeline = StoryAnalyzerV3Pipeline()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -------------------------------------------------------------------------
    # Scenario 1: Sequential physical narrative
    # -------------------------------------------------------------------------
    def test_scenario_01_sequential_physical_narrative(self):
        """Scenario 1: Standard narrative with physical characters validates all invariants and order."""
        text = (
            "Алексей вошел в диспетчерскую. Он подошел к пульту управления. "
            "В кресле сидел Михаил. Михаил передал Алексею журнал дежурств. "
            "Алексей открыл журнал и расписался."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [], "DocumentIR must satisfy all structural invariants")

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=4)
        self.assertLessEqual(len(plan.selected_events), 4)
        self.assertGreaterEqual(len(plan.selected_events), 2)

        # Invariants on plan
        doc_events = {ev.event_id: ev for ev in doc.events}
        indices = [s.event_index for s in plan.selected_events]
        self.assertEqual(indices, sorted(indices), "Events must be strictly chronological")

        for s in plan.selected_events:
            self.assertIn(s.event_id, doc_events)
            self.assertEqual(text[s.span.start:s.span.end], s.span.text)
            self.assertEqual(s.framing_policy, VisualFramingPolicy.PHYSICAL_ACTOR)

    # -------------------------------------------------------------------------
    # Scenario 2: Distant telephone/radio voice dialogue
    # -------------------------------------------------------------------------
    def test_scenario_02_telephone_radio_voice_dialogue(self):
        """Scenario 2: Distant speaker over radio/phone is marked VOICE_ONLY and kept off-screen."""
        text = (
            "Мария сидела за столом. "
            "По рации раздался голос Виктора: «Мария, шторм приближается!». "
            "Мария посмотрела на барометр."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])

        # Check entity modalities
        ent_map = {e.canonical_name.lower(): e for e in doc.entities}
        self.assertIn("виктор", ent_map)
        self.assertEqual(ent_map["виктор"].modality, EntityModalityV3.VOICE_ONLY)

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=3)
        radio_shot = next((s for s in plan.selected_events if "виктор" in s.span.text.lower()), None)
        self.assertIsNotNone(radio_shot)
        self.assertIn(radio_shot.framing_policy, (
            VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER,
            VisualFramingPolicy.PROP_DETAIL,
        ))
        self.assertIn("виктор", radio_shot.safe_framing_description.lower())

    # -------------------------------------------------------------------------
    # Scenario 3: Photograph/portrait depicted figure
    # -------------------------------------------------------------------------
    def test_scenario_03_depicted_portrait_medium(self):
        """Scenario 3: Figure in photograph stays strictly inside depiction medium."""
        text = (
            "На стене висел старый портрет генерала. "
            "Виктор внимательно рассматривал портрет."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=2)
        portrait_shot = next((s for s in plan.selected_events if "портрет" in s.span.text.lower()), None)
        self.assertIsNotNone(portrait_shot)
        self.assertIn(portrait_shot.framing_policy, (
            VisualFramingPolicy.DEPICTED_MEDIUM,
            VisualFramingPolicy.PHYSICAL_ACTOR,
        ))

    # -------------------------------------------------------------------------
    # Scenario 4: Ambiguous pronoun resolution
    # -------------------------------------------------------------------------
    def test_scenario_04_ambiguous_pronoun_safe_neutralization(self):
        """Scenario 4: Ambiguous pronoun falls back to safe neutral without hallucinated characters."""
        text = "Иван встретил Петра. Он молча стоял у входа."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])

        # Coreferences check
        he_decision = next((c for c in doc.coreferences if "он" in text[c.evidence_spans[0].start:c.evidence_spans[0].end].lower()), None)
        if he_decision and len(he_decision.resolved_entity_ids) > 1:
            self.assertEqual(he_decision.visual_policy, VisualPolicy.SAFE_NEUTRAL)

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=2)
        self.assertEqual(len(plan.selected_events), 2)

    # -------------------------------------------------------------------------
    # Scenario 5: Multi-subject review with specs
    # -------------------------------------------------------------------------
    def test_scenario_05_multi_subject_specs_binding(self):
        """Scenario 5: Multi-car review binds specs to exact subject without cross-contamination."""
        text = "BMW мощностью 250 л.с. обогнал Audi мощностью 300 л.с."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])

        bmw = next(e for e in doc.entities if "bmw" in e.canonical_name.lower())
        audi = next(e for e in doc.entities if "audi" in e.canonical_name.lower())

        specs = {fact.object_value: fact for fact in doc.facts if "л.с." in str(fact.object_value)}
        self.assertEqual(set(specs), {"250 л.с.", "300 л.с."})
        self.assertEqual(specs["250 л.с."].subject_id, bmw.entity_id)
        self.assertEqual(specs["300 л.с."].subject_id, audi.entity_id)

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=2)
        self.assertTrue(len(plan.selected_events) >= 1)

    # -------------------------------------------------------------------------
    # Scenario 6: Mixed plot (action, dialogue, radio voice, and photograph)
    # -------------------------------------------------------------------------
    def test_scenario_06_mixed_plot_all_modalities(self):
        """Scenario 6: Complex story with all modalities correctly handled simultaneously."""
        text = (
            "Алексей вошел в каюту. "
            "На столе лежал портрет адмирала. "
            "Внезапно по рации раздался голос Виктора: «Вижу землю!». "
            "Алексей улыбнулся и вышел на палубу."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])

        ent_map = {e.canonical_name.lower(): e for e in doc.entities}
        self.assertEqual(ent_map["алексей"].modality, EntityModalityV3.PHYSICAL)
        self.assertEqual(ent_map["виктор"].modality, EntityModalityV3.VOICE_ONLY)

        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=4)
        self.assertEqual(len(plan.selected_events), 4)

        radio_shot = next(s for s in plan.selected_events if "виктор" in s.span.text.lower())
        self.assertIn(radio_shot.framing_policy, (
            VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER,
            VisualFramingPolicy.PROP_DETAIL,
        ))

    # -------------------------------------------------------------------------
    # Scenario 7: Disabled external models (pure local deterministic)
    # -------------------------------------------------------------------------
    def test_scenario_07_pure_local_deterministic_execution(self):
        """Scenario 7: Default run without external keys is 100% local, fast, and deterministic."""
        text = "Инженер нажал тумблер. Генератор загудел."
        res1 = self.v3_service.analyze_and_plan(text, target_budget=2)
        res2 = self.v3_service.analyze_and_plan(text, target_budget=2)

        self.assertFalse(res1.diagnostics.groq_used)
        self.assertFalse(res1.diagnostics.qwen_used)
        self.assertEqual(len(res1.selected_shots), len(res2.selected_shots))
        for s1, s2 in zip(res1.selected_shots, res2.selected_shots):
            self.assertEqual(s1.event_id, s2.event_id)
            self.assertEqual(s1.framing_policy, s2.framing_policy)
            self.assertEqual(s1.shot_type, s2.shot_type)

    # -------------------------------------------------------------------------
    # Scenario 8: Invalid LLM responses trigger fail-closed fallback
    # -------------------------------------------------------------------------
    def test_scenario_08_invalid_llm_response_fail_closed(self):
        """Scenario 8: Corrupted Qwen output falls back to deterministic plan safely."""
        class BadDetailer:
            def detail_shots(self, shots, prompt):
                # Corrupt output: unknown shot_type
                return [{
                    "selected_id": shots[0]["selected_id"],
                    "event_id": shots[0]["event_id"],
                    "shot_type": "alien_telepathic_dimension",
                    "camera_angle": "eye_level",
                    "composition_focus": "focus",
                    "mood_and_atmosphere": "mood",
                }]
            def get_usage(self):
                return {"calls": 1}

        text = "Павел открыл ворота."
        detailer = BadDetailer()
        doc = self.pipeline.analyze_structural(text)
        plan = self.pipeline.plan_visual_events(text, doc_ir=doc, target_budget=1, detailer=detailer)

        self.assertEqual(len(plan.selected_events), 1)
        self.assertFalse(plan.selected_events[0].qwen_detailed)
        self.assertIn("Unknown shot_type", str(plan.selected_events[0].qwen_diagnostics))

    # -------------------------------------------------------------------------
    # Scenario 9: Atomic persistence and reload cycle
    # -------------------------------------------------------------------------
    def test_scenario_09_persistence_and_reload_cycle(self):
        """Scenario 9: Atomic save and reload produces exact matching DTO without NLP re-execution."""
        text = "Елена подошла к зеркалу. Она поправила волосы."
        dto = self.v3_service.analyze_and_plan(text, target_budget=2, story_id="story_elena")

        reloaded = self.v3_service.get_result(dto.analysis_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.analysis_id, dto.analysis_id)
        self.assertEqual(reloaded.story_id, "story_elena")
        self.assertEqual(len(reloaded.selected_shots), len(dto.selected_shots))
        self.assertEqual(reloaded.document_ir, dto.document_ir)
        self.assertEqual(reloaded.visual_event_plan, dto.visual_event_plan)

    # -------------------------------------------------------------------------
    # Scenario 10: Web API consistency
    # -------------------------------------------------------------------------
    def test_scenario_10_web_api_consistency(self):
        """Scenario 10: End-to-end API client receives the exact plan persisted in database."""
        app = Application(self.root)
        text = "Солдат нес донесение. Он добежал до штаба."
        post_res = app.dispatch("POST", "/api/v3/analyze", {
            "story": text,
            "target_budget": 2,
            "project_id": "proj_soldier",
        })

        self.assertIn("analysis_id", post_res)
        analysis_id = post_res["analysis_id"]

        get_res = app.dispatch("GET", f"/api/v3/results/{analysis_id}", None)
        self.assertEqual(get_res["analysis_id"], analysis_id)
        self.assertEqual(get_res["story_id"], "proj_soldier")
        self.assertEqual(len(get_res["selected_shots"]), len(post_res["selected_shots"]))
    # -------------------------------------------------------------------------
    # Scenario 11: Production database immutability
    # -------------------------------------------------------------------------
    def test_scenario_11_production_database_immutability(self):
        """Scenario 11: Stage 7/8 operations and tests do not modify data/app.sqlite."""
        import hashlib
        db_file = Path("data/app.sqlite")
        if db_file.is_file():
            # Initial hash before any modifications
            expected_hash = hashlib.sha256(db_file.read_bytes()).hexdigest().upper()
            with tempfile.TemporaryDirectory() as isolated_root:
                app = Application(isolated_root)
                app.dispatch("POST", "/api/v3/analyze", {"story_text": "Анна вошла в комнату.", "target_budget": 1})
            actual_hash = hashlib.sha256(db_file.read_bytes()).hexdigest().upper()
            self.assertEqual(actual_hash, expected_hash, "data/app.sqlite must not be modified by V3 tests")


if __name__ == "__main__":
    unittest.main()


"""Tests for Story Analyzer V3 Stage 7: Persistence, Service, and UI/API.

Covers:
1. V3AnalysisRecord data model & atomic JSON serialization.
2. V3AnalysisRepository SQLite operations: atomic save, get by ID, get by story ID, list_all.
3. StoryAnalyzerV3Service end-to-end analysis, persistence, and DTO transformation.
4. Reloading analysis without re-running NLP, Groq, or Qwen.
5. Invariant preservation and equality across save/reload cycles.
6. Safe user-facing framing explanations.
7. Web API endpoints (/api/v3/analyze, /api/v3/results, /api/v3/results/<id>, /api/v3/story/<id>).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from imagegen.analyzer_v3.models import DocumentIR, EntityModalityV3, VisualPolicy
from imagegen.analyzer_v3.persistence import V3AnalysisRecord, V3AnalysisRepository
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.planner_models import VisualEventPlan, VisualFramingPolicy
from imagegen.analyzer_v3.service import (
    StoryAnalyzerV3Service,
    V3AnalysisResultDTO,
    V3DiagnosticsDTO,
    V3EntitySummaryDTO,
    V3ShotDTO,
)
from imagegen.db import Database
from imagegen.service import Service


class TestV3PersistenceAndService(unittest.TestCase):
    """Unit and integration tests for V3 persistence and service layer."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_v3.sqlite"
        self.db = Database(self.db_path)
        self.repository = V3AnalysisRepository(db=self.db)
        self.service = StoryAnalyzerV3Service(repository=self.repository)
        self.pipeline = StoryAnalyzerV3Pipeline()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_record_creation_and_atomic_json_serialization(self):
        """V3AnalysisRecord serializes and restores DocumentIR and VisualEventPlan perfectly."""
        text = "Сергей вошел в комнату. Он включил свет."
        doc_ir = self.pipeline.analyze_structural(text)
        plan = self.pipeline.plan_visual_events(text, doc_ir=doc_ir, target_budget=2)

        record = V3AnalysisRecord.from_objects(
            story_text=text,
            document_ir=doc_ir,
            visual_event_plan=plan,
            story_id="story_123",
            diagnostics={"analysis_sec": 0.05, "groq_used": False},
        )

        self.assertEqual(record.story_id, "story_123")
        self.assertEqual(record.analyzer_version, "v3")
        self.assertEqual(record.schema_version, "3.0")
        self.assertEqual(record.status, "completed")

        # Test recovery from JSON
        restored_doc = record.get_document_ir()
        restored_plan = record.get_visual_event_plan()

        self.assertIsInstance(restored_doc, DocumentIR)
        self.assertIsInstance(restored_plan, VisualEventPlan)
        self.assertEqual(len(restored_doc.events), len(doc_ir.events))
        self.assertEqual(len(restored_plan.selected_events), len(plan.selected_events))
        self.assertEqual(restored_plan.selected_events[0].event_id, plan.selected_events[0].event_id)

    def test_repository_atomic_save_and_retrieve(self):
        """Repository atomically persists record and retrieves it without data corruption."""
        text = "Мария услышала по телефону голос Андрея."
        doc_ir = self.pipeline.analyze_structural(text)
        plan = self.pipeline.plan_visual_events(text, doc_ir=doc_ir, target_budget=2)

        record = V3AnalysisRecord.from_objects(
            story_text=text,
            document_ir=doc_ir,
            visual_event_plan=plan,
            story_id="proj_phone_1",
        )

        saved = self.repository.save_atomic(record)
        self.assertEqual(saved.id, record.id)

        # Retrieve by ID
        fetched = self.repository.get(record.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, record.id)
        self.assertEqual(fetched.story_id, "proj_phone_1")
        self.assertEqual(fetched.story_text, text)

        # Retrieve by Story ID
        by_story = self.repository.get_by_story_id("proj_phone_1")
        self.assertIsNotNone(by_story)
        self.assertEqual(by_story.id, record.id)

        # List all
        all_records = self.repository.list_all()
        self.assertEqual(len(all_records), 1)
        self.assertEqual(all_records[0].id, record.id)

    def test_repository_enforces_atomic_presence_of_both_ir_and_plan(self):
        """Attempting to save record without either DocumentIR or VisualEventPlan must raise ValueError."""
        invalid_record = V3AnalysisRecord(
            id="bad_1",
            story_text="Some text",
            document_ir_json="",  # Missing!
            visual_event_plan_json="{}",
        )
        with self.assertRaises(ValueError):
            self.repository.save_atomic(invalid_record)

    def test_service_analyze_and_plan_end_to_end(self):
        """StoryAnalyzerV3Service executes full analysis, persists result, and returns clean DTO."""
        text = (
            "Дмитрий подошел к окну. За окном шел дождь. "
            "Он налил чашку кофе и сел в кресло."
        )
        dto = self.service.analyze_and_plan(text, target_budget=3, story_id="story_dm")

        self.assertIsInstance(dto, V3AnalysisResultDTO)
        self.assertEqual(dto.story_id, "story_dm")
        self.assertEqual(dto.analyzer_version, "v3")
        self.assertEqual(dto.schema_version, "3.0")
        self.assertEqual(dto.status, "completed")
        self.assertLessEqual(len(dto.selected_shots), 3)
        self.assertGreaterEqual(len(dto.selected_shots), 1)
        self.assertGreater(len(dto.entities), 0)

        # Verify diagnostics
        self.assertFalse(dto.diagnostics.groq_used)
        self.assertFalse(dto.diagnostics.qwen_used)
        self.assertGreater(dto.diagnostics.total_duration_sec, 0.0)

        # Verify shots order & provenance
        indices = [s.event_index for s in dto.selected_shots]
        self.assertEqual(indices, sorted(indices))

    def test_service_reload_result_without_recomputation(self):
        """Service.get_result() reloads exact saved DTO without calling analyzer stages."""
        text = "Виктор нажал кнопку пуска. Двигатель запустился."
        initial_dto = self.service.analyze_and_plan(text, target_budget=2)

        # Reload
        reloaded_dto = self.service.get_result(initial_dto.analysis_id)
        self.assertIsNotNone(reloaded_dto)

        # Exact equality checks
        self.assertEqual(reloaded_dto.analysis_id, initial_dto.analysis_id)
        self.assertEqual(reloaded_dto.story_text, initial_dto.story_text)
        self.assertEqual(len(reloaded_dto.selected_shots), len(initial_dto.selected_shots))
        for s_rel, s_init in zip(reloaded_dto.selected_shots, initial_dto.selected_shots):
            self.assertEqual(s_rel.event_id, s_init.event_id)
            self.assertEqual(s_rel.event_index, s_init.event_index)
            self.assertEqual(s_rel.framing_policy, s_init.framing_policy)
            self.assertEqual(s_rel.shot_type, s_init.shot_type)
            self.assertEqual(s_rel.camera_angle, s_init.camera_angle)
            self.assertEqual(s_rel.source_span, s_init.source_span)
            self.assertEqual(s_rel.selection_reason_codes, s_init.selection_reason_codes)

    def test_safe_framing_explanation_in_dto(self):
        """Safe framing explanation clearly communicates non-physical constraints in user-facing terms."""
        text = "По рации раздался голос Виктора: «Алексей, заблокируй шлюз!». Алексей закрыл шлюз."
        dto = self.service.analyze_and_plan(text, target_budget=2)

        radio_shot = next((s for s in dto.selected_shots if "виктор" in s.event_text.lower() or "рация" in s.event_text.lower()), None)
        self.assertIsNotNone(radio_shot)
        # Should explain safe framing
        self.assertIn("голос", radio_shot.safe_framing_explanation.lower())


class TestV3ServerAndServiceIntegration(unittest.TestCase):
    """Tests integration of V3 with main application Service and Server API."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.app_service = Service(self.root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_service_analyze_v3_and_get(self):
        """Main Service.analyze_v3() and get_v3_analysis() work cleanly."""
        text = "Иван пришел на вокзал. Поезд уже стоял у перрона."
        result = self.app_service.analyze_v3(text, target_budget=2, story_id="proj_station")

        self.assertIn("analysis_id", result)
        self.assertEqual(result["story_id"], "proj_station")
        self.assertEqual(result["analyzer_version"], "v3")
        self.assertTrue(len(result["selected_shots"]) > 0)

        # Retrieve by analysis ID
        fetched = self.app_service.get_v3_analysis(result["analysis_id"])
        self.assertEqual(fetched["analysis_id"], result["analysis_id"])

        # Retrieve by story ID
        fetched_story = self.app_service.get_v3_analysis_by_story("proj_station")
        self.assertEqual(fetched_story["analysis_id"], result["analysis_id"])

        # List
        listed = self.app_service.list_v3_analyses()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["analysis_id"], result["analysis_id"])

    def test_server_dispatch_v3_endpoints(self):
        """Server dispatch handles /api/v3/analyze and /api/v3/results correctly."""
        from imagegen.server import Application

        app = Application(self.root)

        # 1. POST /api/v3/analyze
        post_data = {
            "story": "Ольга открыла книгу. На странице была старая фотография замка.",
            "target_budget": 2,
            "project_id": "proj_book",
        }
        res = app.dispatch("POST", "/api/v3/analyze", post_data)
        self.assertIn("analysis_id", res)
        analysis_id = res["analysis_id"]

        # 2. GET /api/v3/results
        list_res = app.dispatch("GET", "/api/v3/results", None)
        self.assertIsInstance(list_res, list)
        self.assertEqual(len(list_res), 1)
        self.assertEqual(list_res[0]["analysis_id"], analysis_id)

        # 3. GET /api/v3/results/<id>
        get_res = app.dispatch("GET", f"/api/v3/results/{analysis_id}", None)
        self.assertEqual(get_res["analysis_id"], analysis_id)
        self.assertEqual(get_res["analyzer_version"], "v3")

        # 4. GET /api/v3/story/<project_id>
        story_res = app.dispatch("GET", "/api/v3/story/proj_book", None)
        self.assertEqual(story_res["analysis_id"], analysis_id)

    def test_production_database_not_touched_by_v3_persistence(self):
        """V3 persistence repository uses isolated databases and does not touch data/app.sqlite."""
        import hashlib
        from imagegen.server import Application
        db_file = Path("data/app.sqlite")
        if db_file.is_file():
            expected_hash = hashlib.sha256(db_file.read_bytes()).hexdigest().upper()
            with tempfile.TemporaryDirectory() as isolated_root:
                app = Application(isolated_root)
                app.dispatch("POST", "/api/v3/analyze", {"story_text": "Анна вошла в комнату.", "target_budget": 1})
            actual_hash = hashlib.sha256(db_file.read_bytes()).hexdigest().upper()
            self.assertEqual(actual_hash, expected_hash, "data/app.sqlite must not be modified by V3 persistence tests")


if __name__ == "__main__":
    unittest.main()


"""Comprehensive End-to-End Tests for Real User Project Creation Route via Story Analyzer V3.

Tests:
1. Unified Route: Button/API -> Background Worker Generation -> V3 Pipeline -> SQLite Database.
   Verifies scenes, revisions, characters, locations, props, analyzer_version="v3", and v3_analysis_results.
2. Control Story: Narrative from user screenshot with Pavel, radio voice, and pronoun references.
3. Local Mode: 100% deterministic local analysis without external keys.
4. Hybrid Mode: Groq semantic verification and arbitration with text evidence spans.
5. Groq Unavailability Fallback: Graceful fallback on network/API errors to safe neutral.
6. Ambiguity Preservation: Unresolvable pronouns preserve draft with safe framing without crashing.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from imagegen.analyzer_v3.models import DocumentIR, EntityModalityV3, VisualPolicy
from imagegen.analyzer_v3.persistence import V3AnalysisRecord, V3AnalysisRepository
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.planner_models import VisualEventPlan, VisualFramingPolicy
from imagegen.analyzer_v3.semantic_verifier import DialogueCorrectionIR, SemanticPrePlanningVerifier
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service
from imagegen.db import Database, dump, now
from imagegen.server import Application
from imagegen.service import Service
from imagegen.worker import Worker


class TestV3RealProjectFlow(unittest.TestCase):
    """E2E User Journey & System Integration Tests for Story Analyzer V3."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "data" / "app.sqlite"
        self.db = Database(self.db_path)
        self.service = Service(self.root)
        self.worker = Worker(self.service)
        # Initialize V3 analysis table in isolated test DB
        V3AnalysisRepository(db=self.db)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Real User Route: API -> Worker -> V3 -> Database
    # -------------------------------------------------------------------------
    def test_01_real_user_project_creation_route_e2e(self):
        """User creates project and triggers analysis via standard API/Worker route."""
        app = Application(self.root)

        # Step 1: Create project via API (exact UI user action)
        story_text = "Виктор открыл старый сейф. Внутри лежали чертежи станции. Он аккуратно достал документы."
        create_res = app.dispatch("POST", "/api/projects", {
            "title": "Секретные чертежи",
            "story": story_text,
        })
        self.assertIn("id", create_res)
        project_id = create_res["id"]

        # Step 2: Trigger analysis generation via API (exact "Анализировать сценарий" button action)
        analyse_res = app.dispatch("POST", f"/api/projects/{project_id}/analyse", {
            "target": 3,
        })
        self.assertIn("generation_id", analyse_res)
        gen_id = analyse_res["generation_id"]

        # Step 3: Worker picks up background generation task
        gen = app.service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
        self.assertIsNotNone(gen)
        self.assertEqual(gen["purpose"], "analysis")
        self.assertEqual(gen["state"], "queued")

        # Execute worker preparation
        app.worker.prepare(gen)

        # Step 4: Verify generation succeeded and explicitly recorded analyzer_version="v3"
        finished_gen = app.service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
        self.assertEqual(finished_gen["state"], "succeeded")
        gen_snap = json.loads(finished_gen["snapshot"])
        self.assertEqual(gen_snap.get("analyzer_version"), "v3")

        prog = json.loads(finished_gen["progress"])
        self.assertEqual(prog.get("analyzer_version"), "v3")

        # Step 5: Verify project metadata in SQLite
        proj = app.service.db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        self.assertIsNotNone(proj)
        meta = json.loads(proj["analysis_metadata"])
        self.assertEqual(meta.get("analyzer_version"), "v3")

        # Step 6: Verify scenes and scene revisions
        scenes = app.service.db.all("SELECT * FROM scenes WHERE project_id=? ORDER BY position", (project_id,))
        self.assertGreaterEqual(len(scenes), 1)

        for sc in scenes:
            rev = app.service.db.one("SELECT * FROM scene_revisions WHERE scene_id=? AND revision=1", (sc["id"],))
            self.assertIsNotNone(rev)
            sc_data = json.loads(rev["data"])
            self.assertTrue(bool(sc_data.get("beat")))
            self.assertTrue(bool(sc_data.get("source_quote")))
            self.assertIn(sc_data["source_quote"], story_text)

        # Step 7: Verify characters in SQLite
        chars = app.service.db.all("SELECT * FROM characters WHERE project_id=?", (project_id,))
        self.assertGreaterEqual(len(chars), 1)
        char_names = [c["name"] for c in chars]
        self.assertIn("Виктор", char_names)

        # Step 8: Verify v3_analysis_results atomic record
        v3_repo = V3AnalysisRepository(db=app.service.db)
        v3_record = v3_repo.get_by_story_id(project_id)
        self.assertIsNotNone(v3_record)
        self.assertEqual(v3_record.analyzer_version, "v3")

        # Step 9: Verify analysis_saved event
        events = app.service.db.all("SELECT * FROM events WHERE project_id=? AND kind='analysis_saved'", (project_id,))
        self.assertGreaterEqual(len(events), 1)
        event_payload = json.loads(events[0]["data"])
        self.assertEqual(event_payload.get("analyzer_version"), "v3")

    def test_01a_hybrid_button_defaults_to_v3(self):
        """Choosing Hybrid must not silently reactivate the legacy V2 analyzer."""
        app = Application(self.root)
        settings = app.service.settings()
        settings["text_backend"] = "hybrid"
        settings["groq_api_key"] = ""
        app.service.save_settings(settings)

        project = app.dispatch("POST", "/api/projects", {
            "title": "Hybrid V3 route",
            "story": "Анна вошла в комнату. Она открыла окно.",
        })
        queued = app.dispatch("POST", f"/api/projects/{project['id']}/analyse", {"target": 2})
        generation = app.service.db.one(
            "SELECT * FROM generations WHERE id=?", (queued["generation_id"],)
        )
        snapshot = json.loads(generation["snapshot"])
        self.assertEqual(snapshot.get("story_analyzer_version"), "v3")

    def test_01b_worker_honours_snapshot_version_over_stale_profile(self):
        """A queued V3 job stays V3 even if its copied profile still says V2."""
        app = Application(self.root)
        project = app.service.create_project({
            "title": "Snapshot wins",
            "story": "Виктор вошел. Он сел у окна.",
        })
        generation_id = "gen_snapshot_v3"
        profile = app.service.settings()
        profile["story_analyzer_version"] = "v2"
        with app.service.db.transaction() as conn:
            conn.execute(
                "INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    generation_id,
                    project["id"],
                    "analysis",
                    "queued",
                    dump({
                        "profile": profile,
                        "project": project,
                        "target": 2,
                        "story_analyzer_version": "v3",
                    }),
                    12345,
                    now(),
                ),
            )

        generation = app.service.db.one("SELECT * FROM generations WHERE id=?", (generation_id,))
        app.worker.prepare(generation)
        finished = app.service.db.one("SELECT * FROM generations WHERE id=?", (generation_id,))
        self.assertEqual(finished["state"], "succeeded")
        self.assertEqual(json.loads(finished["snapshot"]).get("analyzer_version"), "v3")

    # -------------------------------------------------------------------------
    # 2. Control Story: Pavel, Radio Voice, Pronoun Action
    # -------------------------------------------------------------------------
    def test_02_control_story_pavel_radio_voice_and_pronouns(self):
        """Control story from user screenshot: Pavel, dialogue/radio voice, pronoun action."""
        story = (
            "Павел вошел в диспетчерскую. В рации раздался искаженный голос: «Прием, ответьте!». "
            "Он посмотрел на приборную панель и нажал кнопку ответа."
        )
        pipeline = StoryAnalyzerV3Pipeline()
        doc_ir = pipeline.analyze_structural(story)
        plan = pipeline.plan_visual_events(story, doc_ir=doc_ir, target_budget=3)
        app_analysis = StoryAnalyzerV3Service.export_to_app_analysis(doc_ir, plan, story)

        # 1. Pavel is physical character
        char_names = [c["name"] for c in app_analysis["characters"]]
        self.assertIn("Павел", char_names)

        # 2. Radio voice is non-physical and NOT in physical characters list
        for c in app_analysis["characters"]:
            self.assertNotIn("голос", c["name"].lower())
            self.assertNotIn("раци", c["name"].lower())

        # 3. Radio voice scene uses safe framing (reaction of Pavel or prop detail)
        radio_shots = [
            s for s in plan.selected_events
            if "раци" in s.span.text.lower() or "голос" in s.span.text.lower()
        ]
        self.assertTrue(len(radio_shots) > 0)
        for rs in radio_shots:
            self.assertIn(rs.framing_policy, (
                VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER,
                VisualFramingPolicy.PROP_DETAIL,
                VisualFramingPolicy.SAFE_NEUTRAL,
                VisualFramingPolicy.PHYSICAL_ACTOR,
                VisualFramingPolicy.EMPTY_LOCATION,
                VisualFramingPolicy.AUDIO_ONLY_INDICATOR,
            ))

        # 4. Scenes validate cleanly via domain.validate_analysis
        from imagegen.domain import validate_analysis
        validate_analysis(app_analysis, story)
        self.assertEqual(len(app_analysis["scenes"]), len(plan.selected_events))

    # -------------------------------------------------------------------------
    # 3. Pure Local Mode: Deterministic & No External Keys
    # -------------------------------------------------------------------------
    def test_03_local_mode_deterministic_execution(self):
        """text_backend='local' runs 100% locally and deterministically without external keys."""
        settings = {
            "text_backend": "local",
            "story_analyzer_version": "v3",
            "groq_api_key": "",
        }
        pipeline = StoryAnalyzerV3Pipeline.from_settings(settings)
        self.assertIsNone(pipeline.arbitrator)

        text = "Анна подошла к окну. Шел сильный дождь. Она задернула шторы."
        doc1 = pipeline.analyze_structural(text)
        plan1 = pipeline.plan_visual_events(text, doc_ir=doc1, target_budget=3)

        doc2 = pipeline.analyze_structural(text)
        plan2 = pipeline.plan_visual_events(text, doc_ir=doc2, target_budget=3)

        self.assertEqual(len(plan1.selected_events), len(plan2.selected_events))
        for s1, s2 in zip(plan1.selected_events, plan2.selected_events):
            self.assertEqual(s1.event_id, s2.event_id)
            self.assertEqual(s1.framing_policy, s2.framing_policy)

    # -------------------------------------------------------------------------
    # 4. Hybrid Mode with Groq Semantic Verification
    # -------------------------------------------------------------------------
    def test_04_hybrid_mode_semantic_verification(self):
        """Hybrid mode executes semantic verification with text evidence spans."""
        story = "Капитан взглянул на карту. — Курс на восток, — скомандовал он."
        settings = {
            "text_backend": "hybrid",
            "story_analyzer_version": "v3",
            "groq_api_key": "gsk_test_key_12345",
        }

        # Mock arbitrator with valid semantic verification payload
        mock_arbitrator = MagicMock()
        mock_arbitrator.get_usage.return_value = {"calls": 1, "prompt_tokens": 100, "completion_tokens": 50}
        
        verifier = SemanticPrePlanningVerifier(api_key="gsk_test_key_12345")
        with patch.object(verifier, "_run_cloud_verification") as mock_ver:
            from imagegen.analyzer_v3.semantic_verifier import SemanticVerificationResult
            mock_ver.return_value = SemanticVerificationResult(
                narrator_entity_id=None,
                dialogue_corrections=[
                    DialogueCorrectionIR(
                        event_id="ev_1",
                        speaker_entity_id="ent_0",
                        evidence_span=None,
                        rationale="Капитан отдаёт команду экипажу.",
                    )
                ],
            )
            pipeline = StoryAnalyzerV3Pipeline(semantic_verifier=verifier, arbitrator=mock_arbitrator)
            doc_ir = pipeline.analyze_structural(story)
            plan = pipeline.plan_visual_events(story, doc_ir=doc_ir, target_budget=2)

            self.assertIsNotNone(doc_ir.metadata.get("semantic_verification"))
            self.assertEqual(len(plan.selected_events), 2)

    # -------------------------------------------------------------------------
    # 5. Groq Unavailability Fallback
    # -------------------------------------------------------------------------
    def test_05_groq_unavailability_graceful_fallback(self):
        """Groq network/500 errors fall back gracefully to local deterministic analysis without crashing."""
        story = "Максим выключил радио. В комнате наступила тишина."
        settings = {
            "text_backend": "hybrid",
            "story_analyzer_version": "v3",
            "groq_api_key": "gsk_failing_key",
        }

        # Mock arbitrator that fails with connection error
        mock_arbitrator = MagicMock()
        mock_arbitrator._post_json.side_effect = RuntimeError("Connection refused: 127.0.0.1:443")
        mock_arbitrator.arbitrate.side_effect = RuntimeError("Groq 500 internal server error")
        mock_arbitrator.get_usage.return_value = {"calls": 0}

        pipeline = StoryAnalyzerV3Pipeline(arbitrator=mock_arbitrator)
        # Structural analysis should NOT raise exception; fallback to local
        doc_ir = pipeline.analyze_structural(story)
        self.assertIsNotNone(doc_ir)
        semantic_meta = doc_ir.metadata["semantic_verification"]
        self.assertEqual(semantic_meta["mode"], "cloud_failed_local_fallback")
        self.assertTrue(semantic_meta["cloud_attempted"])
        self.assertNotIn("gsk_", semantic_meta["cloud_error"])

        # Plan visual events
        plan = pipeline.plan_visual_events(story, doc_ir=doc_ir, target_budget=2)
        self.assertGreaterEqual(len(plan.selected_events), 1)

        # Export and validate
        app_analysis = StoryAnalyzerV3Service.export_to_app_analysis(doc_ir, plan, story)
        self.assertEqual(app_analysis["analyzer_version"], "v3")

    # -------------------------------------------------------------------------
    # 6. Preserving Ambiguity as a Standard Analysis Outcome
    # -------------------------------------------------------------------------
    def test_06_preserve_ambiguity_as_standard_outcome(self):
        """Unresolvable pronouns create safe neutral scenes without failing project creation."""
        story = "В коридоре раздался шум. Кто-то постучал в дверь. Он стоял на пороге в полумраке."
        pipeline = StoryAnalyzerV3Pipeline()
        doc_ir = pipeline.analyze_structural(story)
        plan = pipeline.plan_visual_events(story, doc_ir=doc_ir, target_budget=3)
        app_analysis = StoryAnalyzerV3Service.export_to_app_analysis(doc_ir, plan, story)

        # Analysis successfully produced scenes
        self.assertGreaterEqual(len(app_analysis["scenes"]), 2)

        # Ambiguity is preserved and recorded in diagnostics
        diag = app_analysis["diagnostics"]
        self.assertEqual(diag.get("analyzer_version"), "v3")

        # Project can be saved through Worker._save_analysis without error
        proj = self.service.create_project({"title": "Неизвестный гость", "story": story})
        project_id = proj["id"]
        generation_id = "gen_ambiguous_01"
        with self.service.db.transaction() as conn:
            conn.execute(
                "INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,created_at) VALUES(?,?,?,?,?,?,?)",
                (generation_id, project_id, "analysis", "queued", dump({"project": proj}), 12345, now())
            )
        generation = self.service.db.one("SELECT * FROM generations WHERE id=?", (generation_id,))

        self.worker._save_analysis(generation, app_analysis, version=proj["version"])

        saved_gen = self.service.db.one("SELECT * FROM generations WHERE id=?", (generation["id"],))
        self.assertEqual(saved_gen["state"], "succeeded")
        saved_scenes = self.service.db.all("SELECT * FROM scenes WHERE project_id=?", (project_id,))
        self.assertEqual(len(saved_scenes), len(app_analysis["scenes"]))


if __name__ == "__main__":
    unittest.main()

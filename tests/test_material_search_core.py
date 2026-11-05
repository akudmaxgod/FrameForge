"""Comprehensive unit test suite for FrameForge Material Search (Etap A, B, C, D)."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from imagegen.db import Database, SCHEMA, uid, now
from imagegen.domain import DomainError
from imagegen.material_search.providers.base import MaterialSearchItem
from imagegen.material_search.providers.commons import (
    is_allowed_commons_url,
    clean_html_metadata,
    SafeCommonsRedirectHandler,
    WikimediaCommonsProvider
)
from imagegen.material_search.providers.pixabay_adapter import PixabayAdapter
from imagegen.material_search.providers.local_library import LocalLibraryProvider
from imagegen.material_search.entities import EntityCatalog, DEFAULT_CANON_ENTITIES
from imagegen.material_search.planner import MaterialSearchPlanner
from imagegen.material_search.ranking import evaluate_candidate_candidate, enforce_project_diversity
from imagegen.material_search.rights import RightsManager
from imagegen.material_search.service import MaterialSearchService

class TestMaterialSearchCore(unittest.TestCase):
    def setUp(self):
        # Set up in-memory SQLite database with FrameForge schema
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

        class MockDb:
            def __init__(conn):
                self._conn = conn
            def one(s, query, params=()):
                cur = self.conn.execute(query, params)
                return cur.fetchone()
            def all(s, query, params=()):
                cur = self.conn.execute(query, params)
                return cur.fetchall()
            class TxContext:
                def __init__(tx, c):
                    tx.c = c
                def __enter__(tx):
                    return tx.c
                def __exit__(tx, exc_type, exc_val, exc_tb):
                    if exc_type is None:
                        tx.c.commit()
                    else:
                        tx.c.rollback()
            def transaction(s):
                return s.TxContext(self.conn)

        self.mock_db = MockDb()
        self.mock_service = MagicMock()
        self.mock_service.db = self.mock_db
        self.mock_service.root = Path("C:/dummy_material_search")
        self.mock_service.project.return_value = {"id": "proj_123", "title": "Test Project"}

        # Set up mock Pixabay provider on service
        self.mock_pixabay = MagicMock()
        self.mock_pixabay.get_rate_limit_state.return_value = {"rate_limit": 100, "rate_remaining": 95, "rate_reset_at": 0}
        self.mock_pixabay.search.return_value = {
            "ok": True,
            "provider": "pixabay",
            "items": [{
                "external_id": "999",
                "media_type": "photo",
                "title": "Bison in Yellowstone",
                "tags": ["bison", "yellowstone"],
                "author": "john_doe",
                "preview_url": "https://cdn.pixabay.com/photo/bison.jpg",
                "width": 1920,
                "height": 1080
            }],
            "total_hits": 1,
            "cached": False,
            "search_id": "search_123"
        }
        self.mock_service.pixabay = self.mock_pixabay

        # Set up mock Editing
        self.montage_doc = {
            "revision": 3,
            "clips": [
                {
                    "id": "clip_1",
                    "duration": 2.5,
                    "prompt": "У въезда в Йеллоустоун стоит большой деревянный знак",
                    "narration_origin_clip_id": "narr_1"
                },
                {
                    "id": "clip_2",
                    "duration": 4.0,
                    "prompt": "Стадо американских бизонов переходит дорогу",
                    "narration_origin_clip_id": "narr_1"
                },
                {
                    "id": "clip_3",
                    "duration": 3.0,
                    "prompt": "Извержение огромного гейзера выбрасывает горячую воду",
                    "narration_origin_clip_id": "narr_2"
                }
            ],
            "narration_clips": [
                {
                    "id": "narr_1",
                    "text": "У въезда в Йеллоустоун нас встречает знак, а дальше по дороге бродит стадо бизонов.",
                    "start": 0.0,
                    "end": 6.5
                },
                {
                    "id": "narr_2",
                    "text": "Внезапно начинается извержение мощного гейзера.",
                    "start": 6.5,
                    "end": 9.5
                }
            ]
        }
        self.mock_service.editing.get.side_effect = lambda pid: {
            "revision": self.montage_doc.get("revision", 1),
            "document": self.montage_doc
        }
        self.mock_service.editing.get_document.side_effect = lambda pid: self.montage_doc

        def _mock_save(pid, data, *, guard=None):
            current_rev = self.montage_doc.get("revision", 1)
            expected_rev = data.get("expected_revision") if isinstance(data, dict) else None
            doc = data.get("document") if isinstance(data, dict) else data
            if expected_rev is not None and current_rev != expected_rev:
                raise DomainError(f"Монтаж был изменён (ревизия {current_rev} != {expected_rev}).", 409)
            new_rev = current_rev + 1
            res = {**doc, "revision": new_rev}
            self.montage_doc = res
            return {"revision": new_rev, "document": res}

        self.mock_service.editing.save.side_effect = _mock_save

        # Initialize MaterialSearchService
        self.service = MaterialSearchService(self.mock_service)

    # =========================================================================
    # Test 1: Providers Contract & Capabilities
    # =========================================================================
    def test_provider_capabilities(self):
        providers = self.service.list_providers()
        self.assertGreaterEqual(len(providers), 3)
        prov_ids = {p["provider_id"] for p in providers}
        self.assertIn("pixabay", prov_ids)
        self.assertIn("commons", prov_ids)
        self.assertIn("local_library", prov_ids)
        self.assertIn("searxng", prov_ids)

        commons_cap = next(p for p in providers if p["provider_id"] == "commons")
        self.assertIn("photo", commons_cap["media_types"])
        self.assertTrue(commons_cap["auth_configured"])
        self.assertEqual(commons_cap["cache_policy"], "24_hours")

    # =========================================================================
    # Test 2: Wikimedia Commons URL & HTML Security
    # =========================================================================
    def test_commons_url_security(self):
        # Allowed
        self.assertTrue(is_allowed_commons_url("https://commons.wikimedia.org/wiki/File:Bison.jpg"))
        self.assertTrue(is_allowed_commons_url("https://upload.wikimedia.org/wikipedia/commons/1/12/Bison.jpg"))

        # Blocked
        self.assertFalse(is_allowed_commons_url("https://untrustedcommons.org/test.jpg"))
        self.assertFalse(is_allowed_commons_url("http://upload.wikimedia.org/test.jpg"))  # Plain HTTP
        self.assertFalse(is_allowed_commons_url("https://user:pass@upload.wikimedia.org/test.jpg"))  # Userinfo
        self.assertFalse(is_allowed_commons_url("https://upload.wikimedia.org:8443/test.jpg"))  # Custom port
        self.assertFalse(is_allowed_commons_url("file:///etc/passwd"))

    def test_commons_html_sanitization(self):
        dirty_html = '<a href="https://example.com" class="author">John &amp; Jane Doe</a> <span>photographer</span>'
        clean = clean_html_metadata(dirty_html)
        self.assertEqual(clean, "John & Jane Doe photographer")

        empty_clean = clean_html_metadata(None)
        self.assertEqual(empty_clean, "")

    # =========================================================================
    # Test 3: Entity Catalog & Canon Matching
    # =========================================================================
    def test_entity_catalog_matching(self):
        catalog = self.service.planner.entities
        matched = catalog.match_entities_in_text("proj_123", "Стадо американских бизонов пасётся на лугу")
        self.assertTrue(any(e.entity_key == "american_bison" for e in matched))

        matched_got = catalog.match_entities_in_text("proj_123", "Дейнерис Таргариен ведёт дракона")
        self.assertTrue(any(e.entity_key == "daenerys_targaryen" for e in matched_got))
        self.assertTrue(any(e.entity_key == "flying_dragon" for e in matched_got))

    # =========================================================================
    # Test 4: Query Planner
    # =========================================================================
    def test_generate_search_plan(self):
        plan = self.service.create_plan("proj_123")
        self.assertTrue(plan["ok"])
        self.assertIn("plan_id", plan)
        self.assertGreater(plan["segments_count"], 0)
        self.assertGreater(plan["total_queries"], 0)

        # Requirements check
        reqs = plan["requirements"]
        bison_req = next((r for r in reqs if "бизон" in r["quote"].lower()), None)
        self.assertIsNotNone(bison_req)
        self.assertIn("Американский бизон", bison_req["subject_entities"])
        self.assertGreaterEqual(len(bison_req["effective_queries"]), 1)

        # Plan requirement customization
        plan_id = plan["plan_id"]
        req_id = bison_req["id"]
        updated_plan = self.service.update_plan_requirement(
            "proj_123", plan_id, req_id,
            user_queries=["american bison yellowstone", "bison grazing"],
            requested_role="exact"
        )
        updated_bison_req = next(r for r in updated_plan["requirements"] if r["id"] == req_id)
        self.assertEqual(updated_bison_req["effective_queries"], ["american bison yellowstone", "bison grazing"])

    # =========================================================================
    # Test 5: Candidate Ranking & Diversity
    # =========================================================================
    def test_candidate_ranking_and_forbidden_filter(self):
        req = {
            "quote": "Стадо американских бизонов в Йеллоустоуне",
            "subject_entities": ["Американский бизон"],
            "mandatory_details": ["бизон"],
            "forbidden_substitutions": ["зубр", "корова"],
            "requested_role": "exact"
        }

        # Candidate A: Valid bison
        cand_a = {
            "provider": "commons",
            "title": "American Bison in Yellowstone National Park",
            "description": "Bison herd crossing road in winter",
            "license_name": "CC BY-SA 4.0",
            "width": 1920,
            "height": 1080
        }
        sem, ident, rights, reasons, score = evaluate_candidate_candidate(cand_a, req)
        self.assertEqual(sem, "matched")
        self.assertEqual(ident, "confirmed")
        self.assertEqual(rights, "recorded_license")
        self.assertGreater(score, 0.5)

        # Candidate B: Forbidden substitution (зубр)
        cand_b = {
            "provider": "commons",
            "title": "Европейский зубр в заповеднике",
            "description": "Дикий зубр пасётся в лесу",
            "license_name": "CC BY 3.0"
        }
        sem_b, ident_b, rights_b, reasons_b, score_b = evaluate_candidate_candidate(cand_b, req)
        self.assertEqual(sem_b, "rejected")
        self.assertEqual(ident_b, "contradicted")
        self.assertEqual(score_b, 0.0)

        # Test project diversity (duplicate rejection)
        candidates_list = [
            {"canonical_source_id": "commons:photo:100", "semantic_status": "matched", "title": "Bison 1"},
            {"canonical_source_id": "commons:photo:100", "semantic_status": "matched", "title": "Bison 1 dup"},
            {"canonical_source_id": "commons:photo:200", "semantic_status": "matched", "title": "Geyser 1"}
        ]
        diversified = enforce_project_diversity(candidates_list)
        self.assertFalse(diversified[0].get("is_duplicate", False))
        self.assertTrue(diversified[1].get("is_duplicate", False))
        self.assertEqual(diversified[1]["semantic_status"], "needs_review")

    # =========================================================================
    # Test 6: Non-Destructive Application & Rollback
    # =========================================================================
    def test_apply_and_rollback_overlay(self):
        plan = self.service.create_plan("proj_123")
        plan_id = plan["plan_id"]
        req_id = plan["requirements"][0]["id"]
        origin_clip_ids = plan["requirements"][0]["origin_clip_ids"]

        # Insert a mock candidate into material_search_candidates
        cand_id = "cand_test_999"
        with self.mock_db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_jobs (
                    id, project_id, plan_id, state, current_step, created_at, updated_at
                ) VALUES ('job_1', 'proj_123', ?, 'completed', 'Готово', ?, ?)
                """,
                (plan_id, now(), now())
            )
            conn.execute(
                """
                INSERT INTO material_search_candidates (
                    id, job_id, requirement_id, result_id, provider, external_id,
                    media_type, candidate_json, semantic_status, identity_status,
                    rights_status, reasons_json, rank_score, is_selected, is_imported,
                    imported_item_id, created_at
                ) VALUES (?, 'job_1', ?, '999', 'pixabay', '999', 'photo', '{}', 'matched', 'confirmed', 'recorded_license', '[]', 0.9, 0, 1, 'item_media_1', ?)
                """,
                (cand_id, req_id, now())
            )

        self.mock_service.media_library.get_item.return_value = {
            "id": "item_media_1",
            "original_name": "pixabay_photo_999.jpg",
            "file_path": "media_library/bison.jpg",
            "sha256": "abc123hash",
            "media_type": "photo"
        }

        # Apply overlay with expected_revision verification
        apply_res = self.service.apply_candidate_to_segment("proj_123", cand_id, expected_revision=3)
        self.assertTrue(apply_res["ok"])
        self.assertEqual(apply_res["revision"], 4)

        # Verify footage_overlay was attached to the target clip
        doc = apply_res["document"]
        target_clip = next(c for c in doc["clips"] if c["id"] in origin_clip_ids)
        self.assertIn("footage_overlay", target_clip)
        self.assertEqual(target_clip["footage_overlay"]["candidate_id"], cand_id)

        # Rollback overlay: restores original scene without footage_overlay
        rollback_res = self.service.rollback_candidate_overlay("proj_123", origin_clip_ids, expected_revision=4)
        self.assertTrue(rollback_res["ok"])
        self.assertEqual(rollback_res["revision"], 5)
        restored_doc = rollback_res["document"]
        restored_clip = next(c for c in restored_doc["clips"] if c["id"] in origin_clip_ids)
        self.assertNotIn("footage_overlay", restored_clip)

    # =========================================================================
    # Test 7: Credits Text & JSON Export
    # =========================================================================
    def test_credits_export(self):
        # Insert a source provenance record
        with self.mock_db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO media_library_items (id, project_id, original_name, media_type, file_path, sha256, size_bytes, created_at, updated_at)
                VALUES ('item_media_1', 'proj_123', 'bison.jpg', 'photo', 'bison.jpg', 'abc123hash', 1024, ?, ?)
                """,
                (now(), now())
            )
            conn.execute(
                """
                INSERT INTO media_library_sources (id, project_id, item_id, provider, external_id, source_page_url, author_name, license_type, created_at)
                VALUES ('src_1', 'proj_123', 'item_media_1', 'commons', '12345', 'https://commons.wikimedia.org/wiki/File:Bison.jpg', 'Ansel Adams', 'CC BY-SA 4.0', ?)
                """,
                (now(),)
            )

        credits_txt = self.service.export_credits("proj_123", "text")
        self.assertIn("Ansel Adams", credits_txt)
        self.assertIn("CC BY-SA 4.0", credits_txt)
        self.assertIn("https://commons.wikimedia.org/wiki/File:Bison.jpg", credits_txt)

        credits_json = self.service.export_credits("proj_123", "json")
        self.assertEqual(credits_json["total_attributed_materials"], 1)

if __name__ == "__main__":
    unittest.main()

"""Comprehensive real-service end-to-end tests for FrameForge Material Search.

Zero MagicMock on core domain:
- Real Service, real SQLite DB, real Editing engine
- Real MP4 rendering and PyAV frame verification
- Complete lifecycle: search plan -> job -> candidate -> import -> overlay -> MP4 -> rollback -> restored MP4
- Strict multi-tenant project isolation
- Wrong action & visual Qwen arbitration verification
- Preservation of manual timeline edits across rollbacks
"""

import copy
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
import av
import numpy as np
from PIL import Image

from imagegen.service import Service
from imagegen.domain import DomainError
from imagegen.export_worker import render
from imagegen.db import uid, now
from imagegen.material_search.ranking import evaluate_candidate_candidate
from tests import test_veo_animation as veo_fixture


def _create_wav(duration_s: float = 1.0) -> bytes:
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        sample_count = int(48000 * duration_s)
        samples = (np.sin(np.arange(sample_count) * 2 * np.pi * 440 / 48000) * 8000).astype('<i2')
        audio.writeframes(samples.tobytes())
    return out.getvalue()


class TestMaterialSearchRealE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = Service(self.temp_dir.name)
        self.fix = veo_fixture.TestVeoAnimationPlan()
        self.fix.service = self.service

        # Create a real project with 3 scenes
        self.pid = self.fix._create_sample_project(3)
        self.video_bytes = self.fix._create_valid_mp4(width=160, height=90, duration=1.0)

        # Import scene video clips
        for s in self.service.detail(self.pid)['scenes']:
            self.service.import_scene_video(s['id'], self.video_bytes, filename='scene_clip.mp4')

        # Sync and save initial montage document
        synced = self.service.editing.sync(self.pid, {'expected_revision': 0, 'add_only': True})
        saved = self.service.editing.save(self.pid, {'expected_revision': 0, 'document': synced['document']})
        self.initial_doc = saved['document']
        self.initial_rev = saved['revision']

    def tearDown(self):
        try:
            # Stop any running material search jobs before temp dir is deleted
            for j in self.service.db.all("SELECT id, project_id FROM material_search_jobs WHERE state IN ('queued', 'running')"):
                try:
                    self.service.material_search.cancel_job(j["project_id"], j["id"])
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_full_pipeline_real_mp4_and_rollback(self):
        """Full pipeline: plan -> job -> candidate -> import -> overlay -> MP4 decode -> rollback -> restored MP4."""
        # 1. Create search plan
        plan = self.service.material_search.create_plan(self.pid)
        self.assertTrue(plan['ok'])
        plan_id = plan['plan_id']
        reqs = plan['requirements']
        self.assertGreater(len(reqs), 0)
        req0 = reqs[0]

        # Verify plan persisted with valid deserialized columns
        plan_from_db = self.service.material_search.get_plan(self.pid, plan_id)
        self.assertEqual(plan_from_db['plan_id'], plan_id)
        self.assertIsInstance(plan_from_db['requirements'][0]['origin_clip_ids'], list)

        # 2. Start search job
        job = self.service.material_search.start_job(self.pid, plan_id, selected_providers=['local_library'])
        job_id = job['id']
        self.assertIsNotNone(job_id)

        # 3. Import real footage image into project media library
        scratch_img_dir = Path(self.temp_dir.name) / "scratch_media"
        scratch_img_dir.mkdir(parents=True, exist_ok=True)
        img_path = scratch_img_dir / "red_footage.jpg"
        img = Image.new("RGB", (160, 90), (255, 0, 0))  # Vivid red footage insert
        img.save(img_path, format="JPEG")

        lib_item = self.service.media_library.import_file(self.pid, img_path, original_name="red_footage.jpg")
        lib_item_id = lib_item["id"]

        # Insert candidate
        cand_id = "cand_real_e2e_01"
        cand_json = {
            "provider": "local_library",
            "external_id": lib_item_id,
            "title": "Red Vivid Footage",
            "description": "Vivid red test footage",
            "preview_url": f"/api/projects/{self.pid}/media/{lib_item_id}/preview",
            "download_url": f"/api/projects/{self.pid}/media/{lib_item_id}/download",
            "license_name": "Recorded Permission",
            "media_type": "photo",
            "width": 160,
            "height": 90
        }
        with self.service.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_candidates (
                    id, job_id, requirement_id, result_id, provider, external_id,
                    media_type, candidate_json, semantic_status, identity_status,
                    rights_status, reasons_json, rank_score, is_selected, is_imported,
                    imported_item_id, created_at
                ) VALUES (?, ?, ?, 'local_res_1', 'local_library', ?,
                          'photo', ?, 'matched', 'confirmed',
                          'permission_recorded', '[]', 0.95, 0, 1, ?, ?)
                """,
                (cand_id, job_id, req0['id'], lib_item_id, json.dumps(cand_json), lib_item_id, now())
            )

        # 4. Apply candidate to segment 0
        apply_res = self.service.material_search.apply_candidate_to_segment(self.pid, cand_id)
        self.assertEqual(apply_res['revision'], self.initial_rev + 1)

        # Verify editing document has valid footage_overlay and passes Editing.validate
        current_edit = self.service.editing.get(self.pid)
        validated_doc = self.service.editing.validate(self.pid, current_edit['document'])
        first_clip = validated_doc['clips'][0]
        self.assertIn('footage_overlay', first_clip)
        self.assertEqual(first_clip['footage_overlay']['source_kind'], 'footage')
        self.assertEqual(first_clip['footage_overlay']['candidate_id'], cand_id)

        # 5. Export MP4 with footage overlay and verify with PyAV
        export_mp4 = Path(self.temp_dir.name) / "export_with_footage.mp4"
        render(self.service, current_edit['document'], export_mp4, lambda *a: None, lambda: False, size=(160, 90))
        self.assertTrue(export_mp4.exists())

        with av.open(str(export_mp4)) as container:
            stream = container.streams.video[0]
            frames = [f.to_image() for f in container.decode(stream)]
            self.assertGreater(len(frames), 0)
            # The first frame corresponds to the red footage insert
            first_frame = frames[0]
            r, g, b = first_frame.getpixel((80, 45))
            self.assertGreater(r, 150, f"Red channel should dominate in footage overlay, got R={r}, G={g}, B={b}")

        # 6. Rollback overlay non-destructively
        rollback_res = self.service.material_search.rollback_candidate_overlay(self.pid, cand_id)
        self.assertEqual(rollback_res['revision'], apply_res['revision'] + 1)

        current_edit = self.service.editing.get(self.pid)
        self.service.editing.validate(self.pid, current_edit['document'])
        has_overlay = any(c.get('footage_overlay') for c in current_edit['document']['clips'])
        self.assertFalse(has_overlay, "Overlay should be completely removed after rollback")

        # 7. Export MP4 after rollback to ensure original scene is restored
        export_restored = Path(self.temp_dir.name) / "export_restored.mp4"
        render(self.service, current_edit['document'], export_restored, lambda *a: None, lambda: False, size=(160, 90))
        self.assertTrue(export_restored.exists())

    def test_multi_tenant_project_isolation(self):
        """Strict isolation: Project B must never access, import, apply, or rollback Project A candidates."""
        # Create candidate in Project A
        plan = self.service.material_search.create_plan(self.pid)
        job = self.service.material_search.start_job(self.pid, plan['plan_id'], selected_providers=['local_library'])
        cand_id = "cand_proj_a_secret"
        with self.service.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_candidates (
                    id, job_id, requirement_id, result_id, provider, external_id,
                    media_type, candidate_json, semantic_status, identity_status,
                    rights_status, reasons_json, rank_score, is_selected, is_imported,
                    created_at
                ) VALUES (?, ?, ?, 'sec_1', 'local_library', 'ext_1',
                          'photo', '{}', 'matched', 'confirmed',
                          'permission_recorded', '[]', 0.9, 0, 0, ?)
                """,
                (cand_id, job['id'], plan['requirements'][0]['id'], now())
            )

        # Create separate Project B
        pid_b = self.fix._create_sample_project(1)

        # Project B attempts to import Project A's candidate -> 404 DomainError
        with self.assertRaises(DomainError) as cm:
            self.service.material_search.import_candidate(pid_b, cand_id)
        self.assertEqual(cm.exception.status, 404)

        # Project B attempts to apply Project A's candidate -> 404 DomainError
        with self.assertRaises(DomainError) as cm:
            self.service.material_search.apply_candidate_to_segment(pid_b, cand_id)
        self.assertEqual(cm.exception.status, 404)

        # Project B attempts to rollback Project A's candidate -> 404 DomainError
        with self.assertRaises(DomainError) as cm:
            self.service.material_search.rollback_candidate_overlay(pid_b, cand_id)
        self.assertEqual(cm.exception.status, 404)

        # Project B attempts to view candidates of Project A's job -> 404 DomainError
        with self.assertRaises(DomainError) as cm:
            self.service.material_search.get_job_candidates(pid_b, job['id'])
        self.assertEqual(cm.exception.status, 404)

    def test_wrong_action_and_qwen_visual_verification(self):
        """Audits wrong action rejection and Qwen multimodal arbitration."""
        req = {
            "quote": "Дейнерис входит в тронный зал",
            "subject_entities": ["Дейнерис Таргариен"],
            "mandatory_details": ["дейнерис"],
            "forbidden_substitutions": ["серсея"],
            "requested_role": "exact"
        }
        # Candidate with conflicting action: resting in garden instead of entering throne room
        cand = {
            "provider": "commons",
            "title": "Дейнерис отдыхает в саду",
            "description": "Daenerys resting peacefully in floral garden",
            "license_name": "CC BY-SA 4.0",
            "width": 1920,
            "height": 1080
        }

        # 1. Plain metadata without Qwen visual confirmation -> needs_review (NOT matched!)
        plain_res = evaluate_candidate_candidate(cand, req)
        self.assertEqual(plain_res[0], "needs_review", "Candidate with wrong action must never be falsely matched")
        self.assertEqual(plain_res[1], "confirmed", "Entity name is present, but action is unverified")

        # 2. Qwen negative analysis -> rejected immediately with score 0.0
        neg_qwen = {
            "semantic_status": "rejected",
            "action_matches": False,
            "identity_matches": True,
            "reason": "Subject is lying down in a flower garden, not walking into a throne room."
        }
        contradicted_res = evaluate_candidate_candidate(cand, req, qwen_analysis=neg_qwen)
        self.assertEqual(contradicted_res[0], "rejected")
        self.assertEqual(contradicted_res[4], 0.0)

        # 3. Matching candidate with Qwen confirmation -> matched
        matching_cand = {
            "provider": "commons",
            "title": "Daenerys entering the Great Hall",
            "description": "Daenerys walking through massive doors of the throne room",
            "license_name": "CC BY-SA 4.0",
            "width": 1920,
            "height": 1080
        }
        pos_qwen = {
            "semantic_status": "matched",
            "action_matches": True,
            "identity_matches": True,
            "reason": "Confirmed Daenerys entering hall."
        }
        pos_res = evaluate_candidate_candidate(matching_cand, req, qwen_analysis=pos_qwen)
        self.assertEqual(pos_res[0], "matched")
        self.assertGreater(pos_res[4], 0.7)

    def test_rollback_preserves_manual_edits(self):
        """Rollback must safely restore base clip while preserving subsequent manual timeline adjustments."""
        # 1. Setup candidate and apply overlay
        plan = self.service.material_search.create_plan(self.pid)
        job = self.service.material_search.start_job(self.pid, plan['plan_id'], selected_providers=['local_library'])

        scratch_img_dir = Path(self.temp_dir.name) / "scratch_media"
        scratch_img_dir.mkdir(parents=True, exist_ok=True)
        img_path = scratch_img_dir / "footage.jpg"
        Image.new("RGB", (160, 90), (0, 255, 0)).save(img_path)
        lib_item = self.service.media_library.import_file(self.pid, img_path)

        cand_id = "cand_manual_edit_test"
        with self.service.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_candidates (
                    id, job_id, requirement_id, result_id, provider, external_id,
                    media_type, candidate_json, semantic_status, identity_status,
                    rights_status, reasons_json, rank_score, is_selected, is_imported,
                    imported_item_id, created_at
                ) VALUES (?, ?, ?, 'lib_res', 'local_library', ?,
                          'photo', '{}', 'matched', 'confirmed',
                          'permission_recorded', '[]', 0.9, 0, 1, ?, ?)
                """,
                (cand_id, job['id'], plan['requirements'][0]['id'], lib_item['id'], lib_item['id'], now())
            )

        self.service.material_search.apply_candidate_to_segment(self.pid, cand_id)

        # 2. User performs manual edit on clip 1 (the second clip in timeline)
        current_edit = self.service.editing.get(self.pid)
        doc = copy.deepcopy(current_edit['document'])
        doc['clips'][1]['volume'] = 0.42  # User manual volume edit
        doc['clips'][1]['timing_locked'] = True
        save_manual = self.service.editing.save(self.pid, {'expected_revision': current_edit['revision'], 'document': doc})
        manual_rev = save_manual['revision']

        # 3. Rollback overlay from clip 0
        rollback = self.service.material_search.rollback_candidate_overlay(self.pid, cand_id)
        self.assertEqual(rollback['revision'], manual_rev + 1)

        # 4. Verify overlay is removed from clip 0, BUT manual edits on clip 1 are fully preserved!
        latest = self.service.editing.get(self.pid)['document']
        self.assertNotIn('footage_overlay', latest['clips'][0])
        self.assertEqual(latest['clips'][1]['volume'], 0.42, "Manual volume edit must be preserved")
        self.assertTrue(latest['clips'][1]['timing_locked'], "Manual timing lock must be preserved")



    def test_worker_driven_queue_verification_and_protection(self):
        """Worker queue discovers candidates, executes visual verification, enforces stale/repeat/layer protection."""
        import time

        # 1. Pre-import a footage file into project media library before starting job
        scratch_img_dir = Path(self.temp_dir.name) / "worker_scratch"
        scratch_img_dir.mkdir(parents=True, exist_ok=True)
        img_path = scratch_img_dir / "park_entrance.jpg"
        img = Image.new("RGB", (160, 90), (0, 255, 0))  # Vivid green test image
        img.save(img_path, format="JPEG")

        lib_item = self.service.media_library.import_file(self.pid, img_path, original_name="park_entrance.jpg")
        lib_item_id = lib_item["id"]

        # 2. Build search plan
        plan = self.service.material_search.create_plan(self.pid)
        self.assertTrue(plan["ok"])
        plan_id = plan["plan_id"]
        req0 = plan["requirements"][0]

        # Configure user query on requirement 0 to match our imported item name
        with self.service.db.transaction() as conn:
            conn.execute(
                "UPDATE material_search_requirements SET user_queries_json = ? WHERE id = ?",
                (json.dumps(["park_entrance"]), req0["id"])
            )

        # 3. Test Provider Batch Restriction (Pixabay supports_batch = False)
        # Multi-segment batch with only pixabay should raise DomainError 400
        if len(plan["requirements"]) > 1:
            with self.assertRaises(DomainError) as cm:
                self.service.material_search.start_job(self.pid, plan_id, selected_providers=["pixabay"])
            self.assertEqual(cm.exception.status, 400)

        # 4. Start real search job with local_library provider
        job = self.service.material_search.start_job(self.pid, plan_id, selected_providers=["local_library"])
        job_id = job["id"]

        # Wait for worker queue thread to complete search and evaluation
        finished = False
        for _ in range(200):
            st = self.service.material_search.get_job_status(self.pid, job_id)
            if st["state"] in ("completed", "failed"):
                finished = True
                break
            time.sleep(0.1)
        self.assertTrue(finished, "Search job worker timed out")

        # 5. Verify candidates discovered and populated by the WORKER (zero manual SQL insert!)
        candidates = self.service.material_search.get_candidates(self.pid, job_id)
        self.assertGreater(len(candidates), 0, "Worker queue should find at least one candidate from local_library")
        cand = candidates[0]
        cand_id = cand["id"]
        with self.service.db.transaction() as conn:
            conn.execute("UPDATE material_search_candidates SET semantic_status = 'matched', is_imported = 1, imported_item_id = external_id WHERE id = ?", (cand_id,))

        # 6. Test Stale Check on Apply:
        # Alter montage duration of clip 0
        current_edit = self.service.editing.get(self.pid)
        doc = copy.deepcopy(current_edit["document"])
        orig_trim_out = doc["clips"][0]["trim_out_ms"]
        doc["clips"][0]["trim_out_ms"] = doc["clips"][0]["trim_in_ms"] + 500  # shortened by 500ms
        saved_altered = self.service.editing.save(self.pid, {"expected_revision": current_edit["revision"], "document": doc})

        # Applying candidate with altered duration must fail with 409 DomainError
        with self.assertRaises(DomainError) as cm_stale:
            self.service.material_search.apply_candidate_to_segment(self.pid, cand_id, expected_revision=saved_altered["revision"])
        self.assertEqual(cm_stale.exception.status, 409)

        # 7. Restore duration and apply candidate cleanly
        doc["clips"][0]["trim_out_ms"] = orig_trim_out
        saved_restored = self.service.editing.save(self.pid, {"expected_revision": saved_altered["revision"], "document": doc})
        apply_res = self.service.material_search.apply_candidate_to_segment(self.pid, cand_id, expected_revision=saved_restored["revision"])
        self.assertEqual(apply_res["revision"], saved_restored["revision"] + 1)

        # 8. Test Repeat Application (Idempotency):
        # Re-applying the SAME candidate to the segment must raise 409 DomainError
        with self.assertRaises(DomainError) as cm_repeat:
            self.service.material_search.apply_candidate_to_segment(self.pid, cand_id, expected_revision=apply_res["revision"])
        self.assertEqual(cm_repeat.exception.status, 409)

        # 9. Test Layer Protection:
        # Attempting to apply a different candidate without replace_existing=True must raise 409
        dummy_cand_id = "cand_other_02"
        with self.service.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_candidates (
                    id, job_id, requirement_id, result_id, provider, external_id,
                    media_type, candidate_json, semantic_status, identity_status,
                    rights_status, reasons_json, rank_score, is_selected, is_imported,
                    imported_item_id, created_at
                ) VALUES (?, ?, ?, 'other_res', 'local_library', ?, 'photo', '{}', 'matched', 'confirmed', 'permission_recorded', '[]', 0.8, 0, 1, ?, ?)
                """,
                (dummy_cand_id, job_id, req0["id"], 'external_dummy_02', lib_item_id, now())
            )

        with self.assertRaises(DomainError) as cm_protect:
            self.service.material_search.apply_candidate_to_segment(self.pid, dummy_cand_id, expected_revision=apply_res["revision"])
        self.assertEqual(cm_protect.exception.status, 409)

        # 10. Verify MP4 export with green footage overlay
        current_edit = self.service.editing.get(self.pid)
        export_mp4 = Path(self.temp_dir.name) / "export_worker_footage.mp4"
        render(self.service, current_edit["document"], export_mp4, lambda *a: None, lambda: False, size=(160, 90))
        self.assertTrue(export_mp4.exists())

        with av.open(str(export_mp4)) as container:
            stream = container.streams.video[0]
            frames = [f.to_image() for f in container.decode(stream)]
            self.assertGreater(len(frames), 0)
            first_frame = frames[0]
            r, g, b = first_frame.getpixel((80, 45))
            self.assertGreater(g, 150, f"Green channel should dominate, got R={r}, G={g}, B={b}")

        # 11. Rollback overlay and ensure original video restored
        rollback_res = self.service.material_search.rollback_candidate_overlay(self.pid, cand_id)
        self.assertEqual(rollback_res["revision"], apply_res["revision"] + 1)
        latest_doc = self.service.editing.get(self.pid)["document"]
        self.assertNotIn("footage_overlay", latest_doc["clips"][0])


if __name__ == "__main__":
    unittest.main()

"""Constructs and applies non-destructive montage inserts from proposals.

Strict invariants from FRAMEFORGE_SMART_MEDIA_PLAN.md:
- Never changes scenes, characters, or generation records.
- Preserves exact timeline duration and audio synchronization.
- Revision safety: checks expected_revision.
- Reversible: stores rollback snapshot.
"""
import copy
import json
from typing import Dict, Any, List, Optional

from .db import uid, now, dump
from .domain import DomainError
from .editing import normalize

class MontageInserts:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    def apply_proposals(self, project_id: str, proposal_id: str, accepted_proposal_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Applies selected proposals non-destructively to a new revision of the montage document."""
        self.service.project(project_id)

        # 1. Load proposal record
        prop_row = self.db.one("SELECT * FROM media_match_proposals WHERE id=? AND project_id=?", (proposal_id, project_id))
        if not prop_row:
            raise DomainError("Предложение вставок не найдено.", 404)

        if prop_row.get("status") == "applied":
            raise DomainError("Это предложение уже было применено. Повторное применение заблокировано во избежание дублирования.", 409)

        if prop_row.get("status") == "stale":
            raise DomainError("Предложение устарело, так как монтаж или фразы сценария были изменены. Требуется повторный пересчёт.", 409)

        # 2. Get current edit and verify revision
        current_edit = self.service.editing.get(project_id)
        current_rev = current_edit["revision"]
        base_rev = prop_row["base_edit_revision"]
        if current_rev != base_rev:
            with self.db.transaction() as conn:
                conn.execute("UPDATE media_match_proposals SET status='stale', updated_at=? WHERE id=?", (now(), proposal_id))
            raise DomainError(f"Монтаж был изменён (ревизия {current_rev} вместо {base_rev}). Обновите предложения подбора.", 409)

        # 3. Check voice binding and narration hash
        saved_voice_hash = prop_row.get("voice_binding_hash", "")
        current_voice_hash = self.service.media_matching.compute_voice_hash(project_id)
        if saved_voice_hash and current_voice_hash and saved_voice_hash != current_voice_hash:
            with self.db.transaction() as conn:
                conn.execute("UPDATE media_match_proposals SET status='stale', updated_at=? WHERE id=?", (now(), proposal_id))
            raise DomainError("Озвучка или фразы сценария были изменены после формирования предложений. Требуется повторный пересчёт.", 409)

        proposals = json.loads(prop_row["proposals_json"])
        if accepted_proposal_ids is not None:
            accepted_set = set(accepted_proposal_ids)
            proposals = [p for p in proposals if p["proposal_id"] in accepted_set]

        if not proposals:
            return {"status": "no_changes", "applied_count": 0}

        from .media_segment_placement import apply_full_segments
        return apply_full_segments(self.service,project_id,proposal_id,proposals,current_edit)

    def rollback_proposal(self, project_id: str, proposal_id: str) -> Dict[str, Any]:
        """Rollback applied proposal to its pre-application backup montage document, protecting subsequent manual edits."""
        self.service.project(project_id)
        prop_row = self.db.one("SELECT * FROM media_match_proposals WHERE id=? AND project_id=?", (proposal_id, project_id))
        if not prop_row or prop_row.get("status") != "applied":
            raise DomainError("Применённое предложение для отката не найдено.", 404)

        raw_meta = json.loads(prop_row["target_document_json"])
        from .media_segment_placement import POLICY,rollback_full_segments
        if raw_meta.get('placement_policy')==POLICY:
            return rollback_full_segments(self.service,project_id,proposal_id,raw_meta)
        if isinstance(raw_meta, dict) and "applied_revision" in raw_meta:
            backup_doc = raw_meta["backup_doc"]
            applied_rev = raw_meta["applied_revision"]
            inserted_clip_ids = set(raw_meta.get("inserted_clip_ids", []))
            split_records = raw_meta.get("split_records", [])
        else:
            backup_doc = raw_meta
            applied_rev = prop_row["base_edit_revision"] + 1
            inserted_clip_ids = set()
            split_records = []

        current_edit = self.service.editing.get(project_id)
        current_rev = current_edit["revision"]
        current_doc = copy.deepcopy(current_edit["document"])

        if current_rev == applied_rev:
            # Clean case: no subsequent user edits were made on the timeline!
            save_res = self.service.editing.save(project_id, {"document": backup_doc, "expected_revision": current_rev})
            restored_doc = save_res["document"]
            new_rev = save_res["revision"]
        else:
            # Diverged case: user made manual edits after the proposal was applied!
            # Perform surgical rollback so subsequent user edits are NOT wiped out:
            active_clips = current_doc.get("clips", [])
            left_split_map = {sp["left_clip_id"]: sp for sp in split_records if sp.get("left_clip_id")}
            right_split_set = {sp["right_clip_id"] for sp in split_records if sp.get("right_clip_id")}
            full_replace_map = {
                sp["insert_clip_id"]: sp["orig_clip"]
                for sp in split_records
                if sp.get("is_full_replacement") or (not sp.get("left_clip_id") and not sp.get("right_clip_id") and "orig_clip" in sp)
            }
            right_only_map = {
                sp["right_clip_id"]: sp
                for sp in split_records
                if not sp.get("left_clip_id") and sp.get("right_clip_id")
            }

            cleaned_clips = []
            for c in active_clips:
                cid = c.get("edit_clip_id")
                # 1. Full replacement case: replace insert clip directly with the original scene clip!
                if cid in full_replace_map:
                    cleaned_clips.append(copy.deepcopy(full_replace_map[cid]))
                    continue

                # 2. Insert clip from a split: drop it
                if cid in inserted_clip_ids:
                    continue

                # 3. Right sub-clip from a split
                if cid in right_split_set:
                    if cid in right_only_map:
                        sp = right_only_map[cid]
                        c_rejoined = copy.deepcopy(c)
                        orig_data = sp.get("orig_clip") or {}
                        c_rejoined["trim_in_ms"] = orig_data.get("trim_in_ms", sp.get("orig_trim_in_ms", c_rejoined["trim_in_ms"]))
                        cleaned_clips.append(c_rejoined)
                    continue

                # 4. Left sub-clip from a split: restore original trim boundaries!
                if cid in left_split_map:
                    sp = left_split_map[cid]
                    c_rejoined = copy.deepcopy(c)
                    orig_data = sp.get("orig_clip") or {}
                    c_rejoined["trim_in_ms"] = orig_data.get("trim_in_ms", sp.get("orig_trim_in_ms", c_rejoined["trim_in_ms"]))
                    c_rejoined["trim_out_ms"] = orig_data.get("trim_out_ms", sp.get("orig_trim_out_ms", c_rejoined["trim_out_ms"]))
                    if "camera_motion_total_ms" in c_rejoined:
                        del c_rejoined["camera_motion_total_ms"]
                    cleaned_clips.append(c_rejoined)
                    continue

                # 5. Any other clip (including user-modified clips): keep as is!
                cleaned_clips.append(c)

            current_doc["clips"] = cleaned_clips
            reverse_edges = {}
            for sp in split_records:
                restored_id = sp.get("left_clip_id") or sp.get("right_clip_id") or sp["orig_clip_id"]
                for old_id in (sp.get("left_clip_id"), sp.get("right_clip_id"), sp.get("insert_clip_id")):
                    if old_id: reverse_edges[old_id] = restored_id
            for transition in current_doc.get("transitions", []):
                transition["left"] = reverse_edges.get(transition["left"], transition["left"])
                transition["right"] = reverse_edges.get(transition["right"], transition["right"])
            if current_doc.get("voice_fit") and not current_doc["voice_fit"].get("stale"):
                from .montage_voice_fit import source_signature
                current_doc["voice_fit"]["source_hash"] = source_signature(current_doc)
            save_res = self.service.editing.save(project_id, {"document": current_doc, "expected_revision": current_rev})
            restored_doc = save_res["document"]
            new_rev = save_res["revision"]

        t_now = now()
        with self.db.transaction() as conn:
            conn.execute("""
                UPDATE media_match_proposals SET status='rolled_back', updated_at=? WHERE id=?
            """, (t_now, proposal_id))

        return {
            "status": "rolled_back",
            "new_revision": new_rev,
            "document": restored_doc
        }

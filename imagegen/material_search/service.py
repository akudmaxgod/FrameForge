"""Main coordinating service for material search in FrameForge Studio."""
import json
import logging
from typing import Dict, Any, List, Optional

from ..db import uid, now
from ..domain import DomainError
from .providers.base import MaterialSearchProvider
from .providers.pixabay_adapter import PixabayAdapter
from .providers.commons import WikimediaCommonsProvider
from .providers.local_library import LocalLibraryProvider
from .providers.searxng import SearxImagesProvider
from .providers.openverse import search_images as search_openverse_images
from .query_builder import build_targeted_queries, detect_universe_and_entities, UNIVERSES
from .web_image_fetch import fetch_safe_web_image, import_web_image, validate_image_url
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from .planner import MaterialSearchPlanner
from .jobs import MaterialSearchJobManager
from .rights import RightsManager

logger = logging.getLogger(__name__)

class MaterialSearchService:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.root = service.root
        from .browse_store import BrowseStore
        self.browse_db = BrowseStore(self.db)
        self._browse_preview_cache = {}

        # Initialize provider adapters
        self.providers: Dict[str, MaterialSearchProvider] = {
            "pixabay": PixabayAdapter(service),
            "commons": WikimediaCommonsProvider(service),
            "local_library": LocalLibraryProvider(service),
            "searxng": SearxImagesProvider(service)
        }

        self.planner = MaterialSearchPlanner(service)
        self.jobs = MaterialSearchJobManager(service, self.providers)
        self.rights = RightsManager(service)

        # Mark any previously interrupted jobs on boot
        try:
            self.jobs.mark_interrupted_jobs()
        except Exception:
            pass

    def list_providers(self) -> List[Dict[str, Any]]:
        """Return capabilities and authentication status of all supported providers."""
        return [prov.capabilities() for prov in self.providers.values()]

    def create_plan(
        self,
        project_id: str,
        scene_ids: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self.planner.generate_plan(project_id, scene_ids, options)

    def get_plan(self, project_id: str, plan_id: str) -> Dict[str, Any]:
        return self.planner.get_plan(project_id, plan_id)

    def update_plan_requirement(
        self,
        project_id: str,
        plan_id: str,
        requirement_id: str,
        user_queries: Optional[List[str]] = None,
        requested_role: Optional[str] = None,
        enabled: Optional[bool] = None,
        quote: Optional[str] = None,
        mandatory_details: Optional[List[str]] = None,
        forbidden_substitutions: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return self.planner.update_requirement(
            project_id, plan_id, requirement_id, user_queries, requested_role, enabled,
            quote, mandatory_details, forbidden_substitutions
        )

    def start_job(
        self,
        project_id: str,
        plan_id: str,
        selected_providers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return self.jobs.start_job(project_id, plan_id, selected_providers)

    def get_job(self, project_id: str, job_id: str) -> Dict[str, Any]:
        return self.jobs.get_job_status(project_id, job_id)

    def get_job_status(self, project_id: str, job_id: str) -> Dict[str, Any]:
        return self.jobs.get_job_status(project_id, job_id)

    def cancel_job(self, project_id: str, job_id: str) -> Dict[str, Any]:
        return self.jobs.cancel_job(project_id, job_id)

    def get_job_candidates(self, project_id: str, job_id: str) -> List[Dict[str, Any]]:
        return self.jobs.get_job_candidates(project_id, job_id)

    def get_candidates(self, project_id: str, job_id: str) -> List[Dict[str, Any]]:
        return self.jobs.get_job_candidates(project_id, job_id)

    def import_candidate(self, project_id: str, candidate_id: str) -> Dict[str, Any]:
        """Download candidate file safely and register in project media library."""
        self.service.project(project_id)
        cand_row = self.db.one(
            """
            SELECT c.*, j.project_id AS job_project_id
            FROM material_search_candidates c
            JOIN material_search_jobs j ON j.id = c.job_id
            WHERE c.id = ? AND j.project_id = ?
            """,
            (candidate_id, project_id)
        )
        if not cand_row:
            raise DomainError("Кандидат не найден в данном проекте.", 404)

        cand = dict(cand_row)
        provider_key = cand["provider"]
        external_id = cand["external_id"]
        media_type = cand["media_type"]

        prov = self.providers.get(provider_key)
        if not prov:
            raise DomainError(f"Провайдер '{provider_key}' недоступен.", 400)

        import_res = prov.import_selected(project_id, external_id, media_type)
        item = import_res["item"]

        # Re-verify visually now that file is in local media library
        req_row = self.db.one("SELECT * FROM material_search_requirements WHERE id = ?", (cand["requirement_id"],))
        updated_semantic = cand.get("semantic_status")
        if req_row:
            cand_item = {
                "provider": "local_library",
                "external_id": item["id"],
                "title": item.get("original_name") or cand.get("title", ""),
                "description": cand.get("description", ""),
                "media_type": media_type
            }
            try:
                v_res = self.jobs._verify_candidate_visual(cand_item, dict(req_row))
                if v_res and v_res.get("semantic_status"):
                    updated_semantic = v_res["semantic_status"]
            except Exception as e:
                logger.warning("Post-import visual verification error: %s", e)

        # Mark candidate as imported and update semantic status
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_search_candidates
                SET is_imported = 1, imported_item_id = ?, semantic_status = ?
                WHERE id = ? AND job_id IN (SELECT id FROM material_search_jobs WHERE project_id = ?)
                """,
                (item["id"], updated_semantic, candidate_id, project_id)
            )

        return {
            "ok": True,
            "candidate_id": candidate_id,
            "item": item,
            "source": import_res.get("source", {}),
            "semantic_status": updated_semantic
        }

    def apply_candidate_to_segment(
        self,
        project_id: str,
        candidate_id: str,
        expected_revision: Optional[int] = None,
        replace_existing: bool = False,
        confirm_needs_review: bool = False
    ) -> Dict[str, Any]:
        """
        Applies chosen material as a non-destructive footage_overlay across the entire segment.
        Verifies plan staleness, idempotency, and layer protection before importing or saving.
        Saves new revision via service.editing.save with expected_revision verification.
        """
        self.service.project(project_id)
        cand_row = self.db.one(
            """
            SELECT c.*,
                   r.origin_clip_ids_json, r.quote, r.duration_ms, r.requested_role,
                   r.user_queries_json, r.mandatory_details_json, r.forbidden_substitutions_json,
                   r.subject_entities_json,
                   p.id AS plan_id, p.version AS plan_version, p.updated_at AS plan_updated_at,
                   p.options_json AS plan_options_json,
                   j.project_id AS job_project_id, j.created_at AS job_created_at
            FROM material_search_candidates c
            JOIN material_search_jobs j ON j.id = c.job_id
            JOIN material_search_requirements r ON r.id = c.requirement_id
            JOIN material_search_plans p ON p.id = r.plan_id
            WHERE c.id = ? AND j.project_id = ?
            """,
            (candidate_id, project_id)
        )
        if not cand_row:
            raise DomainError("Кандидат не найден в данном проекте.", 404)

        cand = dict(cand_row)
        origin_clip_ids = json.loads(cand.get("origin_clip_ids_json") or "[]")
        if not origin_clip_ids:
            raise DomainError("У кандидата нет связанных сцен монтажа.", 400)

        # 1. Early staleness, revision and conflict checks on current montage document BEFORE any side-effects
        current_edit = self.service.editing.get(project_id)
        current_rev = current_edit["revision"]
        current_doc = current_edit["document"]
        if expected_revision is not None and current_rev != expected_revision:
            raise DomainError(f"Монтаж был изменён (ожидалась ревизия {expected_revision}, актуальная {current_rev}). Обновите предложения.", 409)

        # Check requirement & plan version staleness:
        cand_meta = json.loads(cand.get("candidate_json") or "{}")
        snapshot = cand_meta.get("requirement_snapshot")
        if snapshot:
            curr_user_q = json.loads(cand.get("user_queries_json") or "[]")
            curr_role = cand.get("requested_role") or "exact"
            curr_quote = cand.get("quote", "").strip()
            curr_mand = sorted(json.loads(cand.get("mandatory_details_json") or "[]"))
            curr_forb = sorted(json.loads(cand.get("forbidden_substitutions_json") or "[]"))
            curr_ents = sorted(json.loads(cand.get("subject_entities_json") or "[]"))

            snap_user_q = snapshot.get("user_queries") or []
            snap_role = snapshot.get("requested_role") or "exact"
            snap_quote = (snapshot.get("quote") or "").strip()
            snap_mand = sorted(snapshot.get("mandatory_details") or [])
            snap_forb = sorted(snapshot.get("forbidden_substitutions") or [])
            snap_ents = sorted(snapshot.get("subject_entities") or [])
            snap_ver = snapshot.get("plan_version")

            if (
                snap_user_q != curr_user_q
                or snap_role != curr_role
                or snap_quote != curr_quote
                or snap_mand != curr_mand
                or snap_forb != curr_forb
                or snap_ents != curr_ents
                or (snap_ver is not None and snap_ver != cand["plan_version"])
            ):
                raise DomainError("План поиска устарел: параметры требования были изменены после отбора кандидата. Запустите поиск повторно.", 409)

        cand_created = cand.get("created_at", "")
        plan_updated = cand.get("plan_updated_at", "")
        if plan_updated and cand_created and plan_updated > cand_created:
            raise DomainError("План поиска устарел: параметры требования были изменены после отбора кандидата. Запустите поиск повторно.", 409)

        if cand.get("plan_version", 1) > 1 and not snapshot and plan_updated > cand_created:
            raise DomainError("План поиска устарел: параметры требования были изменены после отбора кандидата. Запустите поиск повторно.", 409)

        clips = current_doc.get("clips", [])
        clips_map = {c.get("edit_clip_id") or c.get("id"): c for c in clips}

        # Check that all origin clips still exist in montage
        missing_clips = [cid for cid in origin_clip_ids if cid not in clips_map]
        if missing_clips:
            raise DomainError("План поиска устарел: клипы сегмента были удалены или разделены в монтаже.", 409)

        # Check segment duration staleness: compare current clips duration with requirement duration
        target_clips = [clips_map[cid] for cid in origin_clip_ids]
        current_dur_ms = 0.0
        for c in target_clips:
            dur = (c.get("trim_out_ms", 0) - c.get("trim_in_ms", 0)) / max(0.001, c.get("playback_rate", 1)) + c.get("hold_last_ms", 0)
            if dur <= 0:
                dur = c.get("duration_ms") or (float(c.get("duration", 0)) * 1000) or 2000.0
            current_dur_ms += dur

        req_dur_ms = float(cand.get("duration_ms") or 0.0)
        if req_dur_ms > 0 and abs(current_dur_ms - req_dur_ms) > 50.0:
            raise DomainError(f"План поиска устарел: тайминг сегмента изменился (в плане {req_dur_ms:.0f} мс, в монтаже {current_dur_ms:.0f} мс). Обновите план поиска.", 409)

        # Check if source asset in montage was replaced with another asset of identical duration
        plan_opts = json.loads(cand.get("plan_options_json") or "{}")
        clip_sigs = plan_opts.get("clip_signatures") or (snapshot.get("clip_signatures") if snapshot else {})
        if clip_sigs:
            for c in target_clips:
                cid = c.get("edit_clip_id") or c.get("id")
                orig_sig = clip_sigs.get(cid)
                if orig_sig:
                    cur_img = c.get("source_image_asset_id")
                    cur_vid = c.get("source_video_asset_id")
                    cur_sc = c.get("scene_id") or c.get("origin_scene_id")
                    if (
                        (orig_sig.get("source_image_asset_id") and cur_img != orig_sig.get("source_image_asset_id"))
                        or (orig_sig.get("source_video_asset_id") and cur_vid != orig_sig.get("source_video_asset_id"))
                        or (orig_sig.get("scene_id") and cur_sc != orig_sig.get("scene_id"))
                    ):
                        raise DomainError("План поиска устарел: исходные кадры в монтаже были заменены. Запустите поиск повторно.", 409)

        # Check if voiceover or scene narration quote changed
        plan_voice_hash = plan_opts.get("voice_hash")
        if plan_voice_hash and hasattr(self.service, "media_matching"):
            curr_voice_hash = self.service.media_matching.compute_voice_hash(project_id)
            if curr_voice_hash != plan_voice_hash:
                raise DomainError("План поиска устарел: озвучка или реплики сценария были изменены. Запустите поиск повторно.", 409)

        # Check existing overlays (idempotency and layer protection)
        for c in target_clips:
            existing_overlay = c.get("footage_overlay")
            if existing_overlay:
                if existing_overlay.get("candidate_id") == candidate_id:
                    raise DomainError("Данный материал уже применён к данному сегменту.", 409)
                elif not replace_existing:
                    raise DomainError("Сегмент уже содержит применённый слой монтажа. Для замены укажите replace_existing=True.", 409)

        # 2. Ensure item is imported into media library
        cand_semantic_status = cand.get("semantic_status")
        if not cand.get("is_imported") or not cand.get("imported_item_id"):
            import_result = self.import_candidate(project_id, candidate_id)
            imported_item_id = import_result["item"]["id"]
            cand_semantic_status = import_result.get("semantic_status") or cand_semantic_status
        else:
            imported_item_id = cand["imported_item_id"]
            fresh_cand = self.db.one("SELECT semantic_status FROM material_search_candidates WHERE id = ?", (candidate_id,))
            if fresh_cand:
                cand_semantic_status = dict(fresh_cand).get("semantic_status") or cand_semantic_status

        # Stop apply if verified status of original is rejected
        if cand_semantic_status == "rejected":
            raise DomainError("Материал отклонён по результатам визуальной проверки оригинала и не может быть добавлен в монтаж.", 400)

        # Re-verify montage revision after download and verification
        current_edit = self.service.editing.get(project_id)
        if expected_revision is not None and current_edit["revision"] != expected_revision:
            raise DomainError(f"Монтаж был изменён во время импорта материала (ожидалась ревизия {expected_revision}, актуальная {current_edit['revision']}). Обновите предложения.", 409)
        current_rev = current_edit["revision"]
        current_doc = current_edit["document"]

        media_item = self.service.media_library.get_item(project_id, imported_item_id)
        if not media_item:
            raise DomainError("Импортированный медиа-файл не найден в медиатеке.", 404)

        # Ensure asset is registered in assets table for Editing validation
        media_path = media_item.get("file_path") or f"media_library/{imported_item_id}.jpg"
        media_sha = media_item.get("sha256") or "unknown_sha"
        asset = self.db.one("SELECT id, generation_id FROM assets WHERE path = ? AND project_id = ?", (media_path, project_id))
        if asset:
            asset_id = asset["id"]
        else:
            asset_id = uid()
            gen_id = uid()
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO generations(id, project_id, purpose, state, snapshot, seed, asset_id, created_at) VALUES(?, ?, 'footage_insert', 'succeeded', '{}', 0, ?, ?)",
                    (gen_id, project_id, asset_id, now())
                )
                conn.execute(
                    "INSERT INTO assets(id, project_id, generation_id, path, sha256, width, height, bytes) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (asset_id, project_id, gen_id, media_path, media_sha, media_item.get("width", 1920), media_item.get("height", 1080), media_item.get("size_bytes", 1024))
                )

        # 3. Apply footage_overlay across all clips of this segment
        updated_clips = []
        applied_count = 0
        target_set = set(origin_clip_ids)
        is_image = media_item.get("media_type") in ("image", "photo")

        for c in clips:
            c_copy = dict(c)
            cid = c.get("edit_clip_id") or c.get("id")
            if cid in target_set:
                # Do not overwrite if media_replacement_locked is True
                if c.get("media_replacement_locked"):
                    updated_clips.append(c_copy)
                    continue

                base_dur = (c.get("trim_out_ms", 0) - c.get("trim_in_ms", 0)) / max(0.001, c.get("playback_rate", 1)) + c.get("hold_last_ms", 0)
                if base_dur <= 0:
                    base_dur = c.get("duration_ms") or (float(c.get("duration", 0)) * 1000) or 2000.0

                overlay_data = {
                    "edit_clip_id": uid(),
                    "title": "Вставка: " + media_item.get("original_name", "материал"),
                    "source_kind": "footage",
                    "scene_id": None,
                    "origin_scene_id": c.get("scene_id") or c.get("origin_scene_id"),
                    "source_scene_revision": 0,
                    "media_type": "image" if is_image else "video",
                    "source_image_asset_id": asset_id if is_image else None,
                    "source_video_asset_id": asset_id if not is_image else None,
                    "trim_in_ms": 0.0,
                    "trim_out_ms": float(base_dur),
                    "hold_last_ms": 0.0,
                    "playback_rate": 1.0,
                    "source_audio": False,
                    "volume": 1.0,
                    "muted": True,
                    "framing": "fit",
                    "illustrative": False,
                    "excluded": False,
                    "enabled": True,
                    "candidate_id": candidate_id,
                    "media_library_item_id": imported_item_id,
                    "provider": cand.get("provider", ""),
                    "external_id": cand.get("external_id", "")
                }

                c_copy["footage_overlay"] = overlay_data
                applied_count += 1
            updated_clips.append(c_copy)

        if applied_count == 0:
            raise DomainError("Сцены сегмента заблокированы от замены или отсутствуют в текущем монтаже.", 400)

        new_doc = dict(current_doc)
        new_doc["clips"] = updated_clips

        # 4. Save atomically through official Editing pipeline
        saved_edit = self.service.editing.save(project_id, {"expected_revision": current_rev, "document": new_doc})
        saved_doc = saved_edit["document"]
        saved_rev = saved_edit["revision"]

        # Mark candidate as selected
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_search_candidates
                SET is_selected = 1
                WHERE id = ? AND job_id IN (SELECT id FROM material_search_jobs WHERE project_id = ?)
                """,
                (candidate_id, project_id)
            )

        return {
            "ok": True,
            "candidate_id": candidate_id,
            "applied_clip_ids": origin_clip_ids,
            "revision": saved_rev,
            "document": saved_doc
        }

    def rollback_candidate_overlay(
        self,
        project_id: str,
        target: Any,
        expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Non-destructively removes footage_overlay from specified clips or by candidate_id,
        restoring generator scenes and preserving manual timeline edits.
        """
        self.service.project(project_id)
        current_edit = self.service.editing.get(project_id)
        current_rev = current_edit["revision"]
        current_doc = current_edit["document"]
        if expected_revision is not None and current_rev != expected_revision:
            raise DomainError(f"Несоответствие ревизий монтажа (текущая {current_rev} != ожидаемая {expected_revision}).", 409)

        target_cand_id = None
        target_clip_ids = set()

        if isinstance(target, (list, tuple, set)):
            target_clip_ids = set(target)
        elif isinstance(target, str):
            # Check if target is a candidate_id
            cand_row = self.db.one(
                """
                SELECT c.id, j.project_id
                FROM material_search_candidates c
                JOIN material_search_jobs j ON j.id = c.job_id
                WHERE c.id = ?
                """,
                (target,)
            )
            if cand_row:
                if cand_row["project_id"] != project_id:
                    raise DomainError("Кандидат принадлежит другому проекту.", 404)
                target_cand_id = target
            else:
                target_clip_ids = {target}

        clips = current_doc.get("clips", [])
        updated_clips = []
        removed_count = 0

        for c in clips:
            c_copy = dict(c)
            cid = c.get("edit_clip_id") or c.get("id")
            overlay = c.get("footage_overlay")
            should_remove = False
            if overlay:
                if target_cand_id and overlay.get("candidate_id") == target_cand_id:
                    should_remove = True
                elif cid in target_clip_ids:
                    should_remove = True

            if should_remove and "footage_overlay" in c_copy:
                del c_copy["footage_overlay"]
                removed_count += 1
            updated_clips.append(c_copy)

        new_doc = dict(current_doc)
        new_doc["clips"] = updated_clips

        saved_edit = self.service.editing.save(project_id, {"expected_revision": current_rev, "document": new_doc})
        saved_doc = saved_edit["document"]
        saved_rev = saved_edit["revision"]

        # Unmark candidate selection
        with self.db.transaction() as conn:
            if target_cand_id:
                conn.execute(
                    """
                    UPDATE material_search_candidates
                    SET is_selected = 0
                    WHERE id = ? AND job_id IN (SELECT id FROM material_search_jobs WHERE project_id = ?)
                    """,
                    (target_cand_id, project_id)
                )
            else:
                conn.execute(
                    """
                    UPDATE material_search_candidates
                    SET is_selected = 0
                    WHERE requirement_id IN (
                        SELECT id FROM material_search_requirements
                        WHERE plan_id IN (SELECT id FROM material_search_plans WHERE project_id = ?)
                    ) AND job_id IN (
                        SELECT id FROM material_search_jobs WHERE project_id = ?
                    )
                    """,
                    (project_id, project_id)
                )

        return {
            "ok": True,
            "removed_count": removed_count,
            "revision": saved_rev,
            "document": saved_doc
        }

    def export_credits(self, project_id: str, format_type: str = "text") -> Any:
        if format_type == "json":
            return self.rights.export_credits_json(project_id)
        return self.rights.export_credits_text(project_id)


    # ---------------------------------------------------------
    # Browse Sessions & Manual Web Image Search (SearXNG)
    # ---------------------------------------------------------

    def browse_materials(
        self,
        project_id: str,
        query: str,
        universe_id: Optional[str] = None,
        adaptation: str = "any",
        image_type: str = "any",
        filters: Optional[Dict[str, Any]] = None,
        provider: str = "searxng",
    ) -> Dict[str, Any]:
        """
        Execute freeform or universe-scoped web image search.
        Creates an ephemeral browse session and records result items.
        """
        self.service.project(project_id)
        raw_q = query.strip() if isinstance(query, str) else ""
        if not 0 < len(raw_q) <= 400:
            raise DomainError("Введите запрос длиной до 400 символов.", 400)
        if provider not in ("commons", "openverse", "searxng"):
            raise DomainError("Неизвестный источник изображений.", 400)
        filters = filters if isinstance(filters, dict) else {}

        # Expire volatile metadata; never retain an unlimited search history.
        with self.browse_db.transaction() as conn:
            conn.execute("DELETE FROM material_browse_items WHERE session_id IN (SELECT id FROM material_browse_sessions WHERE expires_at<=?)",(now(),))
            conn.execute("DELETE FROM material_browse_sessions WHERE expires_at<=?",(now(),))
            old=conn.execute("SELECT id FROM material_browse_sessions ORDER BY created_at DESC LIMIT -1 OFFSET 9").fetchall()
            for row in old:
                conn.execute("DELETE FROM material_browse_items WHERE session_id=?",(row['id'],))
                conn.execute("DELETE FROM material_browse_sessions WHERE id=?",(row['id'],))
        self._browse_preview_cache.clear()
        targeted_queries = []
        search_res = {}
        search_query = raw_q
        if provider == "searxng":
            targeted_queries = build_targeted_queries(raw_q, universe_id, adaptation, image_type)
            search_query = targeted_queries[0] if targeted_queries else raw_q
            search_res = self.providers[provider].search(search_query, filters=filters)
            items = search_res.get("results") or []
        elif provider == "commons":
            items = self.providers[provider].search(raw_q).get("items") or []
        else:
            items = search_openverse_images(raw_q, commercial=filters.get("commercial") is not False)
        orientation = filters.get("orientation")
        items = [
            item for item in items
            if item.get("media_type") == "photo" and item.get("download_url")
            and not (
                item.get("width") and item.get("height")
                and (orientation == "horizontal" and item["width"] < item["height"]
                     or orientation == "vertical" and item["width"] > item["height"])
            )
        ]

        session_id = uid()
        t_now = now()
        expires_at = datetime.fromtimestamp(time.time() + 7200, tz=timezone.utc).isoformat()

        session_filters = {
            "raw_query": raw_q,
            "targeted_queries": targeted_queries,
            "universe_id": universe_id,
            "adaptation": adaptation,
            "image_type": image_type,
            **(filters or {})
        }

        with self.browse_db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_browse_sessions (
                    id, project_id, provider, query, filters_json, mode, state,
                    total_results, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'manual', 'completed', ?, ?, ?)
                """,
                (session_id, project_id, provider, search_query, json.dumps(session_filters), len(items), t_now, expires_at)
            )

            for it in items:
                item_id = uid()
                conn.execute(
                    """
                    INSERT INTO material_browse_items (
                        id, session_id, project_id, provider, external_id, title, description,
                        source_page_url, preview_url, thumbnail_url, large_url, download_url,
                        width, height, format, license_name, rights_status, author,
                        metadata_json, is_imported, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        item_id, session_id, project_id, provider, it["external_id"], it.get("title", ""),
                        it.get("description", ""), it.get("source_page_url", ""),
                        it.get("preview_url", ""), it.get("thumbnail_url", ""),
                        it.get("large_url", ""), it.get("download_url", ""),
                        it.get("width", 0), it.get("height", 0),
                        it.get("provider_metadata", {}).get("format", "jpeg"),
                        it.get("license_name", ""), it.get("rights_status", "unknown"),
                        it.get("author", ""), json.dumps({
                            **it.get("provider_metadata", {}),
                            "license_url": it.get("license_url", ""),
                            "author_url": it.get("author_url", ""),
                        }),
                        t_now
                    )
                )

        # Retrieve inserted items with their DB IDs
        db_items = self.browse_db.all(
            "SELECT * FROM material_browse_items WHERE session_id = ? ORDER BY rowid ASC",
            (session_id,)
        )
        return {
            "ok": True,
            "session_id": session_id,
            "query": search_query,
            "raw_query": raw_q,
            "targeted_queries": targeted_queries,
            "total_results": len(db_items),
            "engines_unavailable": bool(search_res.get("unresponsive_engines")),
            "results": [dict(r) for r in db_items]
        }

    def import_image_link(self, project_id: str, url: str) -> Dict[str, Any]:
        if not isinstance(url, str) or len(url) > 8192:
            raise DomainError("Укажите прямую HTTPS-ссылку на изображение.", 400)
        parsed = validate_image_url(url)["parsed"]
        source_url = parsed._replace(query="", fragment="").geturl()
        return import_web_image(
            self.service, project_id, url, provider="web_link",
            external_id=hashlib.sha256(source_url.encode()).hexdigest()[:16],
            source_page_url=source_url,
        )

    def get_browse_session(self, project_id: str, session_id: str) -> Dict[str, Any]:
        """Fetch browse session details and item cards with strict project isolation."""
        self.service.project(project_id)
        s_row = self.browse_db.one(
            "SELECT * FROM material_browse_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id)
        )
        if s_row and s_row["expires_at"] <= now():
            with self.browse_db.transaction() as conn:
                conn.execute("DELETE FROM material_browse_items WHERE session_id=?",(session_id,))
                conn.execute("DELETE FROM material_browse_sessions WHERE id=?",(session_id,))
            self._browse_preview_cache.clear()
            raise DomainError("Сессия поиска истекла. Повторите поиск.",410)
        if not s_row:
            raise DomainError(f"Сессия поиска '{session_id}' не найдена в данном проекте.", 404)

        items = self.browse_db.all(
            "SELECT * FROM material_browse_items WHERE session_id = ? AND project_id = ? ORDER BY rowid ASC",
            (session_id, project_id)
        )
        res = dict(s_row)
        res["filters"] = json.loads(res.get("filters_json") or "{}")
        res["results"] = [dict(it) for it in items]
        return {"ok": True, "session": res}

    def get_browse_item_preview(self, project_id: str, session_id: str, item_id: str) -> Dict[str, Any]:
        """Safely fetch and return cached thumbnail for a browse item."""
        self.get_browse_session(project_id, session_id)
        item_row = self.browse_db.one(
            """
            SELECT i.* FROM material_browse_items i
            JOIN material_browse_sessions s ON s.id = i.session_id
            WHERE i.id = ? AND i.session_id = ? AND i.project_id = ?
            """,
            (item_id, session_id, project_id)
        )
        if not item_row:
            raise DomainError("Элемент поиска не найден в данном проекте.", 404)

        preview_url = item_row["thumbnail_url"] or item_row["preview_url"] or item_row["download_url"]
        if not preview_url:
            raise DomainError("URL превью отсутствует.", 404)

        cache_key=(session_id,item_id)
        cached=self._browse_preview_cache.get(cache_key)
        if cached:return cached
        fetched=fetch_safe_web_image(preview_url,is_preview=True,max_bytes=5*1024*1024)
        result={'ok':True,'bytes':fetched['bytes'],'content_type':fetched['content_type']}
        # Bounded process-local previews; expiry is checked before cache access.
        if sum(len(v['bytes']) for v in self._browse_preview_cache.values())+len(result['bytes'])>32*1024*1024:
            self._browse_preview_cache.clear()
        self._browse_preview_cache[cache_key]=result
        return result

    def import_browse_item(self, project_id: str, session_id: str, item_id: str) -> Dict[str, Any]:
        """
        Download selected image into project media library without requiring Qwen.
        Idempotent: re-returns existing library asset if already imported.
        """
        self.get_browse_session(project_id, session_id)
        item_row = self.browse_db.one(
            """
            SELECT * FROM material_browse_items
            WHERE id = ? AND session_id = ? AND project_id = ?
            """,
            (item_id, session_id, project_id)
        )
        if not item_row:
            raise DomainError("Материал не найден в сессии поиска проекта.", 404)

        rd = dict(item_row)
        if rd.get("is_imported") and rd.get("imported_item_id"):
            try:
                existing = self.service.media_library.get_item(project_id, rd["imported_item_id"])
            except DomainError as error:
                if error.status != 404: raise
                existing = None
            if existing:
                return {"ok": True, "item": existing, "already_imported": True}

        metadata = json.loads(rd["metadata_json"])
        import_res = import_web_image(
            self.service, project_id, rd["download_url"] or rd["large_url"],
            provider=rd["provider"], external_id=rd["external_id"],
            source_page_url=rd["source_page_url"], author=rd["author"],
            author_url=metadata.get("author_url", ""),
            license_name=rd["license_name"] if rd["rights_status"] == "recorded_license" else "",
        )
        imported_item = import_res["item"]

        with self.browse_db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_browse_items
                SET is_imported = 1, imported_item_id = ?
                WHERE id = ? AND project_id = ?
                """,
                (imported_item["id"], item_id, project_id)
            )

        return {
            "ok": True,
            "item": imported_item,
            "source": import_res.get("source", {})
        }

    def apply_browse_item_to_clip(
        self,
        project_id: str,
        session_id: str,
        item_id: str,
        clip_id: Optional[str] = None,
        expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Apply selected browse item directly onto timeline clip as a non-destructive footage_overlay.
        Manual mode: NEVER blocks on Qwen status or require matched verdict!
        """
        self.get_browse_session(project_id, session_id)
        item_row = self.browse_db.one(
            "SELECT * FROM material_browse_items WHERE id = ? AND session_id = ? AND project_id = ?",
            (item_id, session_id, project_id)
        )
        if not item_row:
            raise DomainError("Элемент поиска не найден в проекте.", 404)

        if not clip_id or type(expected_revision) is not int:
            raise DomainError("Выберите кадр и передайте актуальную ревизию монтажа.",400)

        # 2. Get and check montage revision
        current_edit = self.service.editing.get(project_id)
        cur_rev = current_edit["revision"]
        if expected_revision is not None and cur_rev != expected_revision:
            raise DomainError(
                f"Монтаж был изменён другим действием (ожидалась ревизия {expected_revision}, текущая {cur_rev}).",
                409
            )

        doc = current_edit["document"]
        clips = doc.get("clips") or []
        if not clips:
            raise DomainError("В монтажном документе проекта отсутствуют сцены.", 400)

        # Target clip resolution
        target_clip = None
        if clip_id:
            for c in clips:
                if (c.get("edit_clip_id") or c.get("id")) == clip_id:
                    target_clip = c
                    break
            if not target_clip:
                raise DomainError(f"Сцена монтажа '{clip_id}' не найдена.", 404)
        else:
            target_clip = clips[0]

        if target_clip.get("source_kind","scene") != "scene":
            raise DomainError("Выберите исходный кадр для наложения картинки.",400)
        cid = target_clip.get("edit_clip_id") or target_clip.get("id")
        import_res = self.import_browse_item(project_id, session_id, item_id)
        lib_item = import_res["item"]

        # Copy into independently owned montage storage; library deletion is safe.
        independent = self.service.editing.library_clip(project_id, lib_item['id'])
        asset_id = independent['asset_id']

        base_dur = (target_clip.get("trim_out_ms", 0) - target_clip.get("trim_in_ms", 0)) / max(0.001, target_clip.get("playback_rate", 1)) + target_clip.get("hold_last_ms", 0)
        if base_dur <= 0:
            base_dur = target_clip.get("duration_ms") or (float(target_clip.get("duration", 0)) * 1000) or 2000.0

        # Attach footage overlay conforming to Editing.validate and montage_layers contracts
        target_clip["footage_overlay"] = {
            "edit_clip_id": uid(),
            "title": "Вставка: " + (item_row.get("title") or lib_item.get("original_name") or "Интернет-картинка"),
            "source_kind": "footage",
            "scene_id": None,
            "origin_scene_id": target_clip.get("scene_id") or target_clip.get("origin_scene_id"),
            "source_scene_revision": 0,
            "media_type": "image",
            "source_image_asset_id": asset_id,
            "source_video_asset_id": None,
            "trim_in_ms": 0.0,
            "trim_out_ms": float(base_dur),
            "hold_last_ms": 0.0,
            "playback_rate": 1.0,
            "source_audio": False,
            "volume": 1.0,
            "muted": True,
            "framing": "fit",
            "illustrative": False,
            "excluded": False,
            "enabled": True,
            "media_library_item_id": lib_item["id"],
            "provider": item_row["provider"],
            "external_id": item_row.get("external_id", "")
        }

        # 4. Save new revision via standard Editing.save
        saved_edit = self.service.editing.save(
            project_id,
            {"expected_revision": cur_rev, "document": doc}
        )

        return {
            "ok": True,
            "revision": saved_edit["revision"],
            "clip_id": cid,
            "item_id": lib_item["id"],
            "document": saved_edit["document"],
            "sources": saved_edit.get("sources", {})
        }

    def verify_browse_item(
        self,
        project_id: str,
        session_id: str,
        item_id: str,
        quote: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Optional AI verification of a browse item via local Qwen model.
        Useful when user explicitly clicks 'Проверить соответствие', but never mandatory.
        """
        self.get_browse_session(project_id, session_id)
        item_row = self.browse_db.one(
            "SELECT * FROM material_browse_items WHERE id = ? AND session_id = ? AND project_id = ?",
            (item_id, session_id, project_id)
        )
        if not item_row:
            raise DomainError("Элемент поиска не найден в проекте.", 404)

        if not quote or not quote.strip():
            raise DomainError("Укажите фразу сценария для проверки соответствия.",400)
        # Import or retrieve local preview
        imported = self.import_browse_item(project_id, session_id, item_id)
        lib_item = imported["item"]

        req = {
            "id": f"browse-verify-{item_id[:8]}",
            "quote": quote or "",
            "requested_role": "exact",
            "mandatory_details_json": "[]",
            "forbidden_substitutions_json": "[]",
            "subject_entities_json": "[]"
        }
        cand_item = {
            "provider": "local_library",
            "external_id": lib_item["id"],
            "title": item_row["title"],
            "description": item_row["description"],
            "media_type": "photo"
        }

        v_res = self.jobs._verify_candidate_visual(cand_item, req) or {"semantic_status":"needs_review"}
        v_res["identity_status"]="needs_review"
        v_res["identity_reason"]="Личность героя требует ручной проверки: эталоны в этом ручном режиме не сопоставляются."
        return {
            "ok": True,
            "item_id": item_id,
            "verification": v_res
        }

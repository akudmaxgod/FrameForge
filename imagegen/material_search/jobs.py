from pathlib import Path
"""Background job execution, state machine, and resilience for material search."""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from ..db import uid, now
from ..domain import DomainError
from .ranking import evaluate_candidate_candidate, enforce_project_diversity

logger = logging.getLogger(__name__)

class MaterialSearchJobManager:
    def __init__(self, service, providers: Dict[str, Any]):
        self.service = service
        self.db = service.db
        self.providers = providers
        self._active_cancellations: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def mark_interrupted_jobs(self) -> None:
        """Called at application startup: mark any running jobs as interrupted."""
        t_now = now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_search_jobs
                SET state = 'interrupted', current_step = 'Прервано перезапуском сервера', updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (t_now,)
            )

    def start_job(
        self,
        project_id: str,
        plan_id: str,
        selected_providers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        self.service.project(project_id)

        # Check existing running job
        running = self.db.one(
            "SELECT * FROM material_search_jobs WHERE project_id = ? AND state IN ('queued', 'running')",
            (project_id,)
        )
        if running:
            return self.get_job_status(project_id, running["id"])

        plan_row = self.db.one(
            "SELECT * FROM material_search_plans WHERE id = ? AND project_id = ?",
            (plan_id, project_id)
        )
        if not plan_row:
            raise DomainError("План поиска не найден.", 404)

        req_rows = self.db.all(
            "SELECT * FROM material_search_requirements WHERE plan_id = ? AND enabled = 1 ORDER BY created_at ASC",
            (plan_id,)
        )
        if not req_rows:
            raise DomainError("В плане нет включённых требований для поиска.", 400)

        # Determine target providers
        if not selected_providers:
            opts = json.loads(plan_row.get("options_json") or "{}")
            selected_providers = opts.get("providers", ["pixabay", "commons", "local_library"])

        valid_providers = [p for p in selected_providers if p in self.providers]
        if not valid_providers:
            raise DomainError("Не выбрано ни одного доступного провайдера для поиска.", 400)

        # Enforce provider batch restrictions (e.g. Pixabay supports_batch = False)
        is_batch = len(req_rows) > 1
        filtered_providers = []
        for p in valid_providers:
            prov_instance = self.providers[p]
            caps = prov_instance.capabilities() if hasattr(prov_instance, "capabilities") else {}
            if caps.get("manual_only"):
                continue
            if is_batch and caps.get("supports_batch") is False:
                logger.info("Excluding provider '%s' from multi-segment batch job (supports_batch=False)", p)
                continue
            filtered_providers.append(p)

        if not filtered_providers:
            raise DomainError(
                "Выбранные источники недоступны для этого режима автопоиска. "
                "Используйте ручной одиночный поиск по конкретному сегменту или выберите источники с поддержкой пакетного режима.",
                400
            )
        valid_providers = filtered_providers

        job_id = uid()
        t_now = now()
        total_queries = 0
        for r in req_rows:
            rd = dict(r)
            gen_q = json.loads(rd.get("generated_queries_json") or "[]")
            user_q = json.loads(rd.get("user_queries_json") or "[]")
            eff = user_q if user_q else gen_q
            total_queries += len(eff) * len(valid_providers)

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_jobs (
                    id, project_id, plan_id, state, stage, progress,
                    total_queries, processed_queries, total_candidates, verified_candidates,
                    current_step, checkpoint_json, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 'searching', 0.0, ?, 0, 0, 0, 'В очереди запуска', '{}', 0, ?, ?)
                """,
                (job_id, project_id, plan_id, total_queries, t_now, t_now)
            )

        cancel_ev = threading.Event()
        with self._lock:
            self._active_cancellations[job_id] = cancel_ev

        worker = threading.Thread(
            target=self._run_job,
            args=(project_id, job_id, plan_id, valid_providers, req_rows, cancel_ev),
            daemon=True
        )
        worker.start()

        return self.get_job_status(project_id, job_id)

    def cancel_job(self, project_id: str, job_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        row = self.db.one("SELECT * FROM material_search_jobs WHERE id = ? AND project_id = ?", (job_id, project_id))
        if not row:
            raise DomainError("Задание поиска не найдено в данном проекте.", 404)

        with self._lock:
            ev = self._active_cancellations.get(job_id)
            if ev:
                ev.set()

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_search_jobs
                SET cancel_requested = 1, state = 'cancelled', current_step = 'Задание отменено пользователем', updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (now(), job_id, project_id)
            )
        return self.get_job_status(project_id, job_id)


    def _verify_candidate_visual(
        self,
        candidate_item: Dict[str, Any],
        requirement_dict: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Runs visual verification through LlamaServerManager inference lock if an image is available,
        with SQLite caching by canonical requirement hash and frames content hash.
        For videos: extracts 3 representative frames (beginning, middle, end) of the target insertion interval,
        forwards all frames in segment_info['keyframe_paths_json'], and hashes all frames + interval into cache key.
        Strict fallback ensures candidates do not reach 'matched' if visual verification fails or times out.
        """
        img_path = None
        keyframe_paths = []
        prov = candidate_item.get("provider")
        ext_id = str(candidate_item.get("external_id") or "")
        media_type = candidate_item.get("media_type", "photo")
        row = None

        start_ms = float(candidate_item.get("start_ms") or 0.0)
        end_ms = candidate_item.get("end_ms")
        if end_ms is not None:
            end_ms = float(end_ms)
        elif candidate_item.get("duration_ms"):
            end_ms = start_ms + float(candidate_item["duration_ms"])
        elif requirement_dict.get("duration_ms"):
            end_ms = start_ms + float(requirement_dict["duration_ms"])

        if prov == "local_library":
            row = self.db.one("SELECT file_path, sha256, media_type, thumbnail_path, duration_ms FROM media_library_items WHERE id = ?", (ext_id,))
            if row:
                row_dict = dict(row)
                mtype = row_dict.get("media_type")
                if mtype == "video":
                    media_type = "video"
                    raw_p = Path(row["file_path"])
                    if not raw_p.is_absolute():
                        raw_p = Path(self.service.root) / raw_p
                    if raw_p.exists() and raw_p.is_file():
                        img_path = str(raw_p)
                else:
                    raw_path = row["file_path"]
                    candidate_path = Path(raw_path)
                    if not candidate_path.is_absolute():
                        candidate_path = Path(self.service.root) / raw_path
                    if candidate_path.exists() and candidate_path.is_file():
                        img_path = str(candidate_path)

        if not img_path:
            local_fp = candidate_item.get("provider_metadata", {}).get("local_file_path") or candidate_item.get("file_path")
            if local_fp:
                p = Path(local_fp)
                if not p.is_absolute():
                    p = Path(self.service.root) / local_fp
                if p.exists() and p.is_file():
                    img_path = str(p)

        # Handle remote preview download to local preview cache for stock candidates
        if not img_path:
            remote_url = candidate_item.get("preview_url") or candidate_item.get("thumbnail_url")
            if remote_url and isinstance(remote_url, str) and remote_url.strip():
                remote_url = remote_url.strip()
                import urllib.request
                from .providers.commons import is_allowed_commons_url, SafeCommonsRedirectHandler
                from ..stock_secrets import is_allowed_stock_url, SafeStockRedirectHandler

                handler = None
                if prov == "commons" and is_allowed_commons_url(remote_url):
                    handler = SafeCommonsRedirectHandler()
                elif prov == "pixabay" and is_allowed_stock_url(remote_url):
                    handler = SafeStockRedirectHandler()
                elif is_allowed_commons_url(remote_url):
                    handler = SafeCommonsRedirectHandler()
                elif is_allowed_stock_url(remote_url):
                    handler = SafeStockRedirectHandler()

                if handler:
                    cache_dir = Path(self.service.root) / "data" / "cache" / "stock_previews"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    ext = ".jpg"
                    if "." in remote_url.split("?")[0]:
                        cand_ext = "." + remote_url.split("?")[0].split(".")[-1].lower()
                        if cand_ext in (".jpg", ".jpeg", ".png", ".webp"):
                            ext = cand_ext
                    preview_file = cache_dir / f"{prov}_{ext_id}_preview{ext}"
                    if preview_file.is_file() and preview_file.stat().st_size > 0:
                        img_path = str(preview_file)
                    else:
                        try:
                            req = urllib.request.Request(
                                remote_url,
                                headers={"User-Agent": "FrameForge-Studio/2.0 (Windows; preview-fetch)"}
                            )
                            opener = urllib.request.build_opener(handler)
                            with opener.open(req, timeout=5.0) as resp:
                                data = resp.read(5 * 1024 * 1024 + 1)
                                if len(data) > 5 * 1024 * 1024:
                                    raise ValueError("Размер превью превышает допустимый лимит (5 МБ).")
                                preview_file.write_bytes(data)
                                img_path = str(preview_file)
                        except Exception as download_err:
                            logger.warning("Remote preview download failed for %s (%s): %s", ext_id, remote_url, download_err)
                            return {
                                "semantic_status": "needs_review",
                                "status_label": "нужна проверка",
                                "reason": f"Превью интернет-материала не удалось загрузить для проверки: {download_err}"
                            }

        if not img_path:
            return {
                "semantic_status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Визуальная проверка Qwen недоступна (изображение не сохранено локально)."
            }

        # Resolve frames: if video, extract 3 keyframes across insertion interval
        is_video = media_type == "video" or str(img_path).lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm'))
        if is_video:
            from ..media_matching import extract_video_keyframes
            extracted = extract_video_keyframes(img_path, num_frames=3, start_ms=start_ms, end_ms=end_ms)
            if extracted:
                keyframe_paths = extracted
                img_path = extracted[0]
            else:
                return {
                    "semantic_status": "needs_review",
                    "status_label": "нужна проверка",
                    "reason": "Не удалось извлечь ключевые кадры из видеофайла для визуальной проверки."
                }
        else:
            keyframe_paths = [img_path]

        import hashlib
        frame_hashes = []
        for fp in keyframe_paths:
            try:
                with open(fp, "rb") as f:
                    frame_hashes.append(hashlib.sha256(f.read()).hexdigest())
            except Exception as read_err:
                logger.warning("Cannot read frame file for sha256: %s", read_err)
                return {
                    "semantic_status": "needs_review",
                    "status_label": "нужна проверка",
                    "reason": f"Не удалось прочитать файл изображения: {read_err}"
                }

        if not frame_hashes:
            return {
                "semantic_status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Не удалось прочитать ключевые кадры для визуальной проверки."
            }

        frames_sha = hashlib.sha256(f"{':'.join(frame_hashes)}:{start_ms}:{end_ms}".encode("utf-8")).hexdigest()

        quote = requirement_dict.get("quote", "").strip()
        req_id = requirement_dict.get("id", "")
        mandatory = sorted(json.loads(requirement_dict.get("mandatory_details_json") or "[]") if isinstance(requirement_dict.get("mandatory_details_json"), str) else requirement_dict.get("mandatory_details") or [])
        forbidden = sorted(json.loads(requirement_dict.get("forbidden_substitutions_json") or "[]") if isinstance(requirement_dict.get("forbidden_substitutions_json"), str) else requirement_dict.get("forbidden_substitutions") or [])
        entities = sorted(json.loads(requirement_dict.get("subject_entities_json") or "[]") if isinstance(requirement_dict.get("subject_entities_json"), str) else requirement_dict.get("subject_entities") or [])
        role = requirement_dict.get("requested_role") or "exact"
        refs_hash = requirement_dict.get("references_hash") or ""

        req_payload = {
            "req_id": req_id,
            "quote": quote,
            "mandatory_details": mandatory,
            "forbidden_substitutions": forbidden,
            "requested_role": role,
            "subject_entities": entities,
            "references_hash": refs_hash
        }
        req_canonical_str = json.dumps(req_payload, sort_keys=True, ensure_ascii=False)
        req_hash = hashlib.sha256(req_canonical_str.encode("utf-8")).hexdigest()

        model_name = "qwen3-vl-4b"
        cache_key = hashlib.sha256(f"{frames_sha}:{req_hash}:{model_name}:v4".encode("utf-8")).hexdigest()

        # Check cache
        try:
            cached = self.db.one(
                "SELECT status, status_label, reason FROM material_qwen_verification_cache WHERE cache_key = ?",
                (cache_key,)
            )
            if cached:
                return {
                    "semantic_status": cached["status"],
                    "status_label": cached["status_label"],
                    "reason": cached["reason"]
                }
        except Exception as e:
            logger.warning("Error reading qwen verification cache: %s", e)

        # Call visual verification with lock
        try:
            from ..media_matching import verify_candidate_with_qwen
            segment_info = {
                "dominant_subject": candidate_item.get("title") or "",
                "analysis_summary": candidate_item.get("description") or "",
                "actions": candidate_item.get("actions") or [],
                "objects": candidate_item.get("objects") or [],
                "mandatory_details": mandatory,
                "forbidden_substitutions": forbidden,
                "requested_role": role,
                "subject_entities": entities,
                "keyframe_paths_json": json.dumps(keyframe_paths),
                "start_ms": start_ms,
                "end_ms": end_ms
            }
            res = verify_candidate_with_qwen(
                self.service,
                quote,
                segment_info,
                keyframe_path=keyframe_paths[0]
            )
            status = res.get("status", "needs_review")
            status_label = res.get("status_label", "")
            reason = res.get("reason", "")

            # Save to cache ONLY on genuine model verdict
            if status in ("matched", "needs_review", "rejected"):
                try:
                    with self.db.transaction() as conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO material_qwen_verification_cache
                            (cache_key, status, status_label, reason, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (cache_key, status, status_label, reason, now())
                        )
                except Exception as e:
                    logger.warning("Error saving to qwen verification cache: %s", e)

            return {
                "semantic_status": status,
                "status_label": status_label,
                "reason": reason
            }
        except Exception as exc:
            logger.warning("Visual Qwen verification error or timeout: %s", exc)
            return {
                "semantic_status": "needs_review",
                "status_label": "нужна проверка",
                "reason": f"Визуальная проверка Qwen недоступна или завершилась с ошибкой: {exc}"
            }

    def get_job_status(self, project_id: str, job_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        row = self.db.one(
            "SELECT * FROM material_search_jobs WHERE id = ? AND project_id = ?",
            (job_id, project_id)
        )
        if not row:
            raise DomainError("Задание поиска не найдено в данном проекте.", 404)
        return dict(row)

    def get_job_candidates(self, project_id: str, job_id: str) -> List[Dict[str, Any]]:
        self.service.project(project_id)
        job_row = self.db.one("SELECT id FROM material_search_jobs WHERE id = ? AND project_id = ?", (job_id, project_id))
        if not job_row:
            raise DomainError("Задание поиска не найдено в данном проекте.", 404)

        rows = self.db.all(
            """
            SELECT c.*, r.segment_key, r.quote, r.origin_clip_ids_json, r.duration_ms
            FROM material_search_candidates c
            JOIN material_search_requirements r ON r.id = c.requirement_id
            JOIN material_search_jobs j ON j.id = c.job_id
            WHERE c.job_id = ? AND j.project_id = ?
            ORDER BY c.rank_score DESC, c.created_at ASC
            """,
            (job_id, project_id)
        )
        result = []
        for r in rows:
            rd = dict(r)
            rd["candidate"] = json.loads(rd.get("candidate_json") or "{}")
            rd["reasons"] = json.loads(rd.get("reasons_json") or "[]")
            rd["origin_clip_ids"] = json.loads(rd.get("origin_clip_ids_json") or "[]")
            result.append(rd)
        return result

    def _update_job_progress(
        self,
        job_id: str,
        state: str,
        stage: str,
        processed_queries: int,
        total_queries: int,
        total_candidates: int,
        current_step: str,
        error_msg: Optional[str] = None
    ) -> None:
        prog = min(1.0, round(processed_queries / max(1, total_queries), 2)) if total_queries > 0 else 1.0
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE material_search_jobs
                SET state = ?, stage = ?, progress = ?,
                    processed_queries = ?, total_candidates = ?, current_step = ?,
                    error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, stage, prog, processed_queries, total_candidates, current_step, error_msg, now(), job_id)
            )

    def _run_job(
        self,
        project_id: str,
        job_id: str,
        plan_id: str,
        providers_list: List[str],
        req_rows: List[Any],
        cancellation: threading.Event
    ) -> None:
        try:
            self._update_job_progress(job_id, "running", "searching", 0, len(req_rows), 0, "Запуск поиска...")
            processed_q = 0
            total_cand_count = 0
            collected_candidates_by_req: Dict[str, List[Dict[str, Any]]] = {}

            # Phase 1: Searching
            for r in req_rows:
                if cancellation.is_set():
                    self._update_job_progress(job_id, "cancelled", "searching", processed_q, len(req_rows), total_cand_count, "Поиск отменён")
                    return

                rd = dict(r)
                req_id = rd["id"]
                gen_q = json.loads(rd.get("generated_queries_json") or "[]")
                user_q = json.loads(rd.get("user_queries_json") or "[]")
                queries = user_q if user_q else gen_q

                collected_candidates_by_req[req_id] = []

                for q in queries:
                    for prov_key in providers_list:
                        if cancellation.is_set():
                            return
                        prov = self.providers.get(prov_key)
                        if not prov:
                            continue

                        # Check capabilities
                        caps = prov.capabilities()
                        if caps.get("auth_required") and not caps.get("auth_configured"):
                            logger.info("Skipping %s: auth not configured", prov_key)
                            continue

                        self._update_job_progress(
                            job_id, "running", "searching", processed_q, len(req_rows),
                            total_cand_count, f"Поиск '{q[:30]}' в {caps.get('name', prov_key)}..."
                        )

                        try:
                            search_res = prov.search(
                                query=q,
                                filters={"project_id": project_id, "per_page": 10},
                                cancellation=cancellation
                            )
                            items = search_res.get("items", [])
                            for it in items:
                                collected_candidates_by_req[req_id].append(it)
                                total_cand_count += 1
                        except Exception as e:
                            logger.warning("Search query error (%s, %s): %s", prov_key, q, e)

                        processed_q += 1

            # Phase 2: Verifying and Ranking
            self._update_job_progress(
                job_id, "running", "verifying", processed_q, processed_q,
                total_cand_count, "Оценка соответствия и отбор кандидатов..."
            )

            all_evaluated: List[Dict[str, Any]] = []

            for r in req_rows:
                rd = dict(r)
                req_id = rd["id"]
                raw_items = collected_candidates_by_req.get(req_id, [])

                # Deduplicate items by canonical source id for this requirement
                seen_can = set()
                deduped = []
                for it in raw_items:
                    cid = it.get("canonical_source_id") or f"{it.get('provider')}:{it.get('external_id')}"
                    if cid not in seen_can:
                        seen_can.add(cid)
                        deduped.append(it)

                # Evaluate candidates with visual verification
                for it in deduped:
                    qwen_analysis = self._verify_candidate_visual(it, rd)
                    sem_stat, id_stat, r_stat, reasons, score = evaluate_candidate_candidate(
                        it, rd, qwen_analysis=qwen_analysis
                    )
                    cand_id = uid()
                    t_now = now()

                    plan_row = self.db.one("SELECT version, options_json FROM material_search_plans WHERE id = ?", (plan_id,))
                    plan_ver = plan_row["version"] if plan_row else 1
                    plan_opts = json.loads(plan_row.get("options_json") or "{}") if plan_row else {}
                    it_copy = dict(it)
                    it_copy["requirement_snapshot"] = {
                        "plan_version": plan_ver,
                        "requirement_id": req_id,
                        "quote": rd.get("quote", ""),
                        "user_queries": json.loads(rd.get("user_queries_json") or "[]"),
                        "requested_role": rd.get("requested_role", "exact"),
                        "mandatory_details": json.loads(rd.get("mandatory_details_json") or "[]"),
                        "forbidden_substitutions": json.loads(rd.get("forbidden_substitutions_json") or "[]"),
                        "subject_entities": json.loads(rd.get("subject_entities_json") or "[]"),
                        "clip_signatures": plan_opts.get("clip_signatures", {})
                    }

                    all_evaluated.append({
                        "id": cand_id,
                        "job_id": job_id,
                        "requirement_id": req_id,
                        "result_id": it.get("external_id"),
                        "provider": it.get("provider"),
                        "external_id": it.get("external_id"),
                        "media_type": it.get("media_type", "photo"),
                        "candidate_json": json.dumps(it_copy),
                        "semantic_status": sem_stat,
                        "identity_status": id_stat,
                        "rights_status": r_stat,
                        "reasons_json": json.dumps(reasons),
                        "rank_score": score,
                        "canonical_source_id": it.get("canonical_source_id"),
                        "created_at": t_now
                    })

            # Phase 3: Enforce Project-wide Diversity
            diversified = enforce_project_diversity(all_evaluated)

            # Store in database
            with self.db.transaction() as conn:
                for cand in diversified:
                    conn.execute(
                        """
                        INSERT INTO material_search_candidates (
                            id, job_id, requirement_id, result_id, provider,
                            external_id, media_type, candidate_json, semantic_status,
                            identity_status, rights_status, reasons_json, rank_score,
                            is_selected, is_imported, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                        ON CONFLICT(job_id, requirement_id, provider, external_id) DO UPDATE SET
                            semantic_status = excluded.semantic_status,
                            identity_status = excluded.identity_status,
                            rights_status = excluded.rights_status,
                            reasons_json = excluded.reasons_json,
                            rank_score = excluded.rank_score
                        """,
                        (
                            cand["id"], cand["job_id"], cand["requirement_id"], cand["result_id"],
                            cand["provider"], cand["external_id"], cand["media_type"],
                            cand["candidate_json"], cand["semantic_status"], cand["identity_status"],
                            cand["rights_status"], cand["reasons_json"], cand["rank_score"], cand["created_at"]
                        )
                    )

            # Completed successfully
            self._update_job_progress(
                job_id, "completed", "ready_for_review", processed_q, processed_q,
                len(diversified), f"Поиск завершён. Найдено кандидатов: {len(diversified)}"
            )

        except Exception as e:
            logger.exception("Material search job failed: %s", e)
            self._update_job_progress(
                job_id, "failed", "searching", 0, 1, 0, "Ошибка выполнения поиска", str(e)
            )
        finally:
            with self._lock:
                self._active_cancellations.pop(job_id, None)

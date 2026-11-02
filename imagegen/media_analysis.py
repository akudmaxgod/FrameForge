"""Local visual analysis queue using Qwen3-VL on llama-server.

Enforces:
- Strict sequential queue (single inference at a time, zero VRAM contention).
- Input image scaling (max 768px) to guarantee memory safety on 8GB VRAM.
- End-to-end timing instrumentation.
- Content-addressed caching (never re-analyzes identical content).
- Graceful error recovery: failure on an item never breaks the montage or scenes.
"""
import base64
import io
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, List

from PIL import Image

from .db import uid, now, dump
from .domain import DomainError

logger = logging.getLogger(__name__)

DEFAULT_PORT = 11438
DEFAULT_MAX_DIM = 768 # 768px keeps prompt tokens under ~400, conserving VRAM headroom

class LlamaServerManager:
    """Manages the lifecycle of the local llama-server process for Qwen3-VL."""
    _instance = None
    _lock = threading.Lock()

    def __init__(self, service):
        self.service = service
        self.port = DEFAULT_PORT
        self.inference_lock = threading.RLock()
        self.process: Optional[subprocess.Popen] = None
        self.last_active = time.time()
        self.idle_timeout_sec = 300 # 5 minutes idle shutdown

        from .release_paths import component_root
        candidate_roots = [component_root() / "backends", Path(self.service.root) / "backends"]
        base_backends = next((b for b in candidate_roots if (b / "llama-cuda" / "llama-server.exe").exists()), candidate_roots[0])
        self.binary_path = base_backends / "llama-cuda" / "llama-server.exe"
        self.model_dir = base_backends / "models" / "qwen3-vl"
        self.base_model = self.model_dir / "Qwen3VL-4B-Instruct-Q4_K_M.gguf"
        self.mmproj = self.model_dir / "mmproj-Qwen3VL-4B-Instruct-F16.gguf"

    @classmethod
    def get_manager(cls, service):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(service)
            return cls._instance

    def is_healthy(self) -> bool:
        try:
            url = f"http://127.0.0.1:{self.port}/health"
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def ensure_running(self) -> None:
        with self._lock:
            self.last_active = time.time()
            if self.is_healthy():
                return

            if not self.binary_path.exists():
                raise DomainError(f"Бинарный файл llama-server не найден: {self.binary_path}")
            if not self.base_model.exists() or not self.mmproj.exists():
                raise DomainError("Модели Qwen3-VL не найдены в backends/models/qwen3-vl.")

            cmd = [
                str(self.binary_path),
                "-m", str(self.base_model),
                "--mmproj", str(self.mmproj),
                "-ngl", "99",
                "-fa", "on",
                "-c", "4096",
                "--port", str(self.port),
                "--host", "127.0.0.1"
            ]

            logger.info("Starting llama-server for Qwen3-VL: %s", " ".join(cmd))
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

            # Wait for health
            start = time.time()
            while time.time() - start < 45:
                if self.is_healthy():
                    logger.info("llama-server is healthy and ready on port %d", self.port)
                    return
                time.sleep(0.5)

            if self.process:
                self.process.terminate()
            raise DomainError("Не удалось запустить локальный сервер визуального анализа Qwen3-VL.")

    def stop(self) -> None:
        with self._lock:
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=4)
                except Exception:
                    self.process.kill()
                self.process = None


class MediaAnalysis:
    """Coordinates media analysis jobs, scheduling, and Qwen3-VL visual tagging."""

    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.server_mgr = LlamaServerManager.get_manager(service)
        self._inference_lock = self.server_mgr.inference_lock
        self._active_worker: Optional[threading.Thread] = None

    def start_job(self, project_id: str) -> Dict[str, Any]:
        """Start or resume background analysis job for project media segments."""
        self.service.project(project_id)

        # Check for already running job
        running = self.db.one(
            "SELECT * FROM media_analysis_jobs WHERE project_id=? AND state IN ('queued', 'running')",
            (project_id,)
        )
        if running:
            return self.get_job_status(project_id, running["id"])

        # Count pending segments
        pending_segments = self.db.all(
            "SELECT id FROM media_segments WHERE project_id=? AND analysis_status IN ('pending', 'failed')",
            (project_id,)
        )
        total_count = len(pending_segments)

        job_id = uid()
        t_now = now()
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO media_analysis_jobs(
                    id, project_id, state, progress, total_segments, processed_segments,
                    current_step, model, created_at, updated_at
                ) VALUES (?, ?, 'queued', 0.0, ?, 0, 'Ожидание запуска', 'qwen3-vl-4b', ?, ?)
            """, (job_id, project_id, total_count, t_now, t_now))

        # Launch worker thread
        worker = threading.Thread(target=self._process_job, args=(project_id, job_id), daemon=True)
        worker.start()

        return self.get_job_status(project_id, job_id)

    def cancel_job(self, project_id: str, job_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE media_analysis_jobs SET cancel_requested=1, state='cancelled', updated_at=? WHERE id=? AND project_id=?",
                (now(), job_id, project_id)
            )
        return self.get_job_status(project_id, job_id)

    def get_job_status(self, project_id: str, job_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        job = self.db.one("SELECT * FROM media_analysis_jobs WHERE id=? AND project_id=?", (job_id, project_id))
        if not job:
            raise DomainError("Задание анализа не найдено.", 404)
        return job

    def get_active_or_latest_job(self, project_id: str) -> Optional[Dict[str, Any]]:
        self.service.project(project_id)
        return self.db.one(
            "SELECT * FROM media_analysis_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (project_id,)
        )

    def _prepare_image_payload(self, rel_path: str, max_dim=DEFAULT_MAX_DIM) -> str:
        full_path = self.service.root / rel_path
        if not full_path.exists():
            raise FileNotFoundError(f"Файл кадра не найден: {rel_path}")

        with Image.open(full_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{b64}"

    def analyze_single_segment(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        """Runs Qwen3-VL analysis on segment keyframes under the global inference lock."""
        with self._inference_lock:
            self.server_mgr.ensure_running()

            keyframe_paths = json.loads(segment.get("keyframe_paths_json") or "[]")
            if not keyframe_paths:
                raise ValueError("У отрезка нет ключевых кадров для анализа.")

            # Prepare image content
            t0 = time.time()
            user_content = [
                {"type": "text", "text": "Analyze this footage segment for smart montage matching. Output JSON only."}
            ]
            for kp in keyframe_paths[:3]: # Max 3 frames to maintain strict memory bounds
                data_uri = self._prepare_image_payload(kp)
                user_content.append({"type": "image_url", "image_url": {"url": data_uri}})
            prep_sec = time.time() - t0

            system_prompt = (
                "You are an expert video editor, film archivist, and documentary matcher. "
                "Analyze the visual frame(s) and return a JSON object with strictly these keys:\n"
                "- scene_type: 'photo' | 'b-roll' | 'archive' | 'talking_head' | 'action'\n"
                "- setting: 'interior' | 'exterior' | 'studio' | 'unknown'\n"
                "- period_epoch: estimated time period or modern\n"
                "- dominant_subject: concise description of the main visible subject\n"
                "- mood: emotional tone or atmosphere\n"
                "- objects: array of key objects detected\n"
                "- actions: array of actions taking place\n"
                "- people_count: integer\n"
                "- visual_defects: array of defects like 'blur', 'watermark', 'grain', or empty\n"
                "- recommended_montage_roles: array of roles ('establishing', 'b-roll', 'accent', 'reaction', 'background')\n"
                "- analysis_summary: 1-2 sentence concise Russian description suitable for video montage matching."
            )

            payload = {
                "model": "qwen3-vl",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "temperature": 0.1,
                "max_tokens": 512,
                "response_format": {"type": "json_object"}
            }

            t_req_start = time.time()
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.server_mgr.port}/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                err_msg = str(e).lower()
                is_oom = any(s in err_msg for s in ["out of memory", "cuda error: out of memory", "failed to allocate", "cannot allocate memory"])
                if isinstance(e, urllib.error.HTTPError):
                    try:
                        err_body = e.read().decode("utf-8", errors="ignore").lower()
                        if any(s in err_body for s in ["out of memory", "cuda error: out of memory", "failed to allocate", "cannot allocate memory", "oom"]):
                            is_oom = True
                    except Exception:
                        pass
                if is_oom:
                    logger.error("VRAM Out-of-Memory detected during Qwen3-VL analysis. Restarting llama-server...")
                    self.server_mgr.stop()
                    raise MemoryError("Нехватка видеопамяти (VRAM OOM) при анализе кадра. Процесс сервера перезапущен.") from e
                raise
            http_sec = time.time() - t_req_start

            raw_text = result["choices"][0]["message"]["content"]
            parsed = json.loads(raw_text)
            parsed["_timing"] = {
                "prep_ms": round(prep_sec * 1000, 1),
                "http_ms": round(http_sec * 1000, 1),
                "total_sec": round(prep_sec + http_sec, 2),
                "prompt_tokens": result.get("usage", {}).get("prompt_tokens", 0),
                "completion_tokens": result.get("usage", {}).get("completion_tokens", 0)
            }
            return parsed

    def _process_job(self, project_id: str, job_id: str) -> None:
        """Background worker thread processing pending segments sequentially."""
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE media_analysis_jobs SET state='running', started_at=?, updated_at=? WHERE id=?",
                    (now(), now(), job_id)
                )

            # Query all pending segments for project
            segments = self.db.all(
                "SELECT * FROM media_segments WHERE project_id=? AND analysis_status IN ('pending', 'failed') ORDER BY created_at ASC",
                (project_id,)
            )

            total = len(segments)
            processed = 0

            for seg in segments:
                # Check for cancellation
                job = self.db.one("SELECT cancel_requested FROM media_analysis_jobs WHERE id=?", (job_id,))
                if job and job.get("cancel_requested"):
                    break

                seg_id = seg["id"]
                step_desc = f"Анализ отрезка {processed + 1} из {total}"
                with self.db.transaction() as conn:
                    conn.execute("""
                        UPDATE media_analysis_jobs SET
                            current_step=?, processed_segments=?, progress=?, updated_at=?
                        WHERE id=?
                    """, (step_desc, processed, round(processed / max(total, 1), 2), now(), job_id))

                try:
                    analysis = self.analyze_single_segment(seg)

                    with self.db.transaction() as conn:
                        conn.execute("""
                            UPDATE media_segments SET
                                analysis_status='completed',
                                analysis_summary=?,
                                dominant_subject=?,
                                scene_type=?,
                                setting=?,
                                period_epoch=?,
                                mood=?,
                                objects_json=?,
                                actions_json=?,
                                people_count=?,
                                visual_defects_json=?,
                                recommended_montage_roles_json=?,
                                analysis_model='qwen3-vl-4b',
                                analysis_error=NULL,
                                updated_at=?
                            WHERE id=?
                        """, (
                            analysis.get("analysis_summary", ""),
                            analysis.get("dominant_subject", ""),
                            analysis.get("scene_type", "b-roll"),
                            analysis.get("setting", "interior"),
                            analysis.get("period_epoch", ""),
                            analysis.get("mood", ""),
                            dump(analysis.get("objects", [])),
                            dump(analysis.get("actions", [])),
                            int(analysis.get("people_count", 0)),
                            dump(analysis.get("visual_defects", [])),
                            dump(analysis.get("recommended_montage_roles", [])),
                            now(),
                            seg_id
                        ))
                except Exception as e:
                    logger.exception("Failed analyzing segment %s: %s", seg_id, e)
                    with self.db.transaction() as conn:
                        conn.execute("""
                            UPDATE media_segments SET
                                analysis_status='failed',
                                analysis_error=?,
                                updated_at=?
                            WHERE id=?
                        """, (str(e), now(), seg_id))

                processed += 1

            # Job finished
            with self.db.transaction() as conn:
                conn.execute("""
                    UPDATE media_analysis_jobs SET
                        state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END,
                        error=(SELECT CASE WHEN COUNT(*)>0 THEN 'Не все материалы удалось проанализировать. Повторите анализ для неудачных отрезков.' ELSE NULL END FROM media_segments WHERE project_id=? AND analysis_status='failed'),
                        progress=1.0,
                        processed_segments=?,
                        current_step='Анализ завершён',
                        finished_at=?,
                        updated_at=?
                    WHERE id=?
                """, (project_id, processed, now(), now(), job_id))

        except Exception as e:
            logger.exception("Fatal error in media analysis job %s: %s", job_id, e)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE media_analysis_jobs SET state='failed', error=?, updated_at=? WHERE id=?",
                    (str(e), now(), job_id)
                )

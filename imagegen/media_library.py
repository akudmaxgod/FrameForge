"""Project media library for montage inserts.

Never modifies scenes, characters, generations, or project_edits directly.
"""
import copy
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional

from PIL import Image

try:
    import av
except ImportError:
    av = None

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

from .db import uid, now, dump
from .domain import DomainError

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def probe_video(path: Path) -> Dict[str, Any]:
    if av is None:
        raise DomainError("PyAV недоступен для анализа видеофайла.")
    try:
        with av.open(str(path)) as media:
            if not media.streams.video:
                raise ValueError("Видеопоток не найден.")
            vstream = media.streams.video[0]
            fps = float(vstream.average_rate or vstream.base_rate or 24.0)
            dur_sec = float(vstream.duration * vstream.time_base) if vstream.duration else (media.duration or 0) / av.time_base
            width = vstream.width or 1920
            height = vstream.height or 1080
            return {
                "duration_ms": round(dur_sec * 1000.0, 2),
                "width": width,
                "height": height,
                "fps": round(fps, 2)
            }
    except Exception as e:
        raise DomainError(f"Не удалось прочитать видео {path.name}: {e}")

def create_thumbnail(src_path: Path, dst_path: Path, max_size=(320, 180)) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        im.thumbnail(max_size, Image.Resampling.LANCZOS)
        im.save(dst_path, format="JPEG", quality=85)

def detect_video_shots(video_path: Path, duration_ms: float, fps: float) -> List[Dict[str, float]]:
    """Detect shot transitions or create logical segments of 4-8s for video montage."""
    if duration_ms <= 0:
        return [{"start_ms": 0.0, "end_ms": 0.0, "duration_ms": 0.0}]

    # If video is short (under 7 seconds), use 1 segment
    if duration_ms <= 7000:
        return [{"start_ms": 0.0, "end_ms": duration_ms, "duration_ms": duration_ms}]

    segments = []
    # If OpenCV is available, detect significant visual transitions
    cuts = [0.0]
    if cv2 is not None and av is not None:
        try:
            with av.open(str(video_path)) as container:
                stream = container.streams.video[0]
                # Sample at roughly 2 frames per second
                step = max(1, int(fps / 2))
                prev_hist = None
                frame_idx = 0
                for frame in container.decode(stream):
                    if frame_idx % step == 0:
                        img = frame.to_ndarray(format="bgr24")
                        small = cv2.resize(img, (160, 90))
                        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
                        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
                        cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
                        
                        if prev_hist is not None:
                            corr = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
                            time_ms = float(frame.pts * stream.time_base * 1000.0) if frame.pts else (frame_idx / fps * 1000.0)
                            # Shot boundary detected if correlation drops sharply and min 2.0s passed
                            if corr < 0.55 and (time_ms - cuts[-1]) >= 2000:
                                cuts.append(round(time_ms, 2))
                        prev_hist = hist
                    frame_idx += 1
        except Exception:
            pass

    if cuts[-1] < duration_ms - 1000:
        cuts.append(duration_ms)
    else:
        cuts[-1] = duration_ms

    # Subdivide long shots (> 10s) into usable montage segments (5-8s)
    final_cuts = [0.0]
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i+1]
        shot_dur = b - a
        if shot_dur > 10000:
            num_sub = max(2, int(round(shot_dur / 6000)))
            sub_step = shot_dur / num_sub
            for s in range(1, num_sub):
                final_cuts.append(round(a + s * sub_step, 2))
        final_cuts.append(b)

    final_cuts = sorted(list(set(final_cuts)))
    for i in range(len(final_cuts) - 1):
        start = final_cuts[i]
        end = final_cuts[i+1]
        segments.append({
            "start_ms": start,
            "end_ms": end,
            "duration_ms": round(end - start, 2)
        })

    return segments or [{"start_ms": 0.0, "end_ms": duration_ms, "duration_ms": duration_ms}]

def extract_keyframe(video_path: Path, time_ms: float, out_path: Path) -> bool:
    if av is None:
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with av.open(str(video_path)) as media:
            stream = media.streams.video[0]
            target_sec = time_ms / 1000.0
            # Seek near target
            target_pts = int(target_sec / float(stream.time_base))
            media.seek(target_pts, stream=stream)
            for frame in media.decode(stream):
                pts_sec = float(frame.pts * stream.time_base) if frame.pts else 0.0
                if pts_sec >= target_sec - 0.2:
                    img = frame.to_image()
                    # Resize to max 1024 for safe memory bounds
                    if max(img.width, img.height) > 1024:
                        scale = 1024 / max(img.width, img.height)
                        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
                    img.save(out_path, format="JPEG", quality=88)
                    return True
        return False
    except Exception:
        return False


class MediaLibrary:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.storage_root = Path(service.root) / "outputs" / "media_library"

    def _project_dir(self, project_id: str) -> Path:
        pdir = self.storage_root / project_id
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "thumbs").mkdir(parents=True, exist_ok=True)
        (pdir / "keyframes").mkdir(parents=True, exist_ok=True)
        return pdir

    def import_file(self, project_id: str, src_path: Path, original_name: Optional[str] = None) -> Dict[str, Any]:
        """Import photo or video file into the project's media library."""
        self.service.project(project_id)
        src_path = Path(src_path).resolve()
        if not src_path.is_file():
            raise DomainError(f"Файл не найден: {src_path.name}")

        name = original_name or src_path.name
        ext = src_path.suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTS:
            media_type = "image"
        elif ext in SUPPORTED_VIDEO_EXTS:
            media_type = "video"
        else:
            raise DomainError(f"Неподдерживаемый формат медиа: {ext}. Допустимы: {', '.join(sorted(SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS))}")

        sha = file_sha256(src_path)
        size_bytes = src_path.stat().st_size

        # Check if already imported in this project
        existing = self.db.one("SELECT * FROM media_library_items WHERE project_id=? AND sha256=?", (project_id, sha))
        if existing:
            return self.get_item(project_id, existing["id"])

        pdir = self._project_dir(project_id)
        dest_filename = f"{sha}{ext}"
        dest_path = pdir / dest_filename
        if not dest_path.exists():
            shutil.copy2(src_path, dest_path)

        thumb_filename = f"{sha}.jpg"
        thumb_path = pdir / "thumbs" / thumb_filename

        item_id = uid()
        created_at = now()

        if media_type == "image":
            with Image.open(dest_path) as im:
                width, height = im.size
            duration_ms = 0.0
            fps = 0.0
            create_thumbnail(dest_path, thumb_path)
            segments = [{"start_ms": 0.0, "end_ms": 0.0, "duration_ms": 0.0}]
        else:
            meta = probe_video(dest_path)
            width = meta["width"]
            height = meta["height"]
            duration_ms = meta["duration_ms"]
            fps = meta["fps"]
            # Extract first frame as thumbnail
            extract_keyframe(dest_path, min(500.0, duration_ms / 2), thumb_path)
            segments = detect_video_shots(dest_path, duration_ms, fps)

        # Relative paths for DB portability
        rel_file_path = str(dest_path.relative_to(self.service.root)).replace("\\", "/")
        rel_thumb_path = str(thumb_path.relative_to(self.service.root)).replace("\\", "/") if thumb_path.exists() else ""

        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO media_library_items(
                    id, project_id, file_path, original_name, media_type, sha256, size_bytes,
                    duration_ms, width, height, fps, thumbnail_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item_id, project_id, rel_file_path, name, media_type, sha, size_bytes,
                duration_ms, width, height, fps, rel_thumb_path, created_at, created_at
            ))

            for idx, seg in enumerate(segments):
                seg_id = uid()
                start_ms = seg["start_ms"]
                end_ms = seg["end_ms"]
                dur = seg["duration_ms"]

                # Extract keyframe for each segment
                keyframe_rel_paths = []
                if media_type == "image":
                    keyframe_rel_paths.append(rel_file_path)
                else:
                    mid_ms = start_ms + (dur / 2.0)
                    kf_path = pdir / "keyframes" / f"{seg_id}_0.jpg"
                    if extract_keyframe(dest_path, mid_ms, kf_path):
                        keyframe_rel_paths.append(str(kf_path.relative_to(self.service.root)).replace("\\", "/"))

                # Content hash for caching analysis
                chash = hashlib.sha256(f"{sha}:{start_ms}:{end_ms}:v1".encode()).hexdigest()

                # Check if analysis can be reused from another project with the exact same content_hash
                reusable = conn.execute(
                    "SELECT * FROM media_segments WHERE content_hash=? AND analysis_status='completed' LIMIT 1",
                    (chash,)
                ).fetchone()

                if reusable:
                    conn.execute("""
                        INSERT INTO media_segments(
                            id, item_id, project_id, segment_index, start_ms, end_ms, duration_ms,
                            keyframe_paths_json, analysis_summary, dominant_subject, scene_type, setting,
                            period_epoch, mood, objects_json, actions_json, people_count, visual_defects_json,
                            recommended_montage_roles_json, analysis_status, analysis_error, analysis_model,
                            analysis_version, content_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', NULL, ?, ?, ?, ?, ?)
                    """, (
                        seg_id, item_id, project_id, idx, start_ms, end_ms, dur,
                        dump(keyframe_rel_paths), reusable["analysis_summary"], reusable["dominant_subject"],
                        reusable["scene_type"], reusable["setting"], reusable["period_epoch"], reusable["mood"],
                        reusable["objects_json"], reusable["actions_json"], reusable["people_count"],
                        reusable["visual_defects_json"], reusable["recommended_montage_roles_json"],
                        reusable["analysis_model"], reusable["analysis_version"], chash, created_at, created_at
                    ))
                else:
                    conn.execute("""
                        INSERT INTO media_segments(
                            id, item_id, project_id, segment_index, start_ms, end_ms, duration_ms,
                            keyframe_paths_json, analysis_summary, dominant_subject, scene_type, setting,
                            period_epoch, mood, objects_json, actions_json, people_count, visual_defects_json,
                            recommended_montage_roles_json, analysis_status, analysis_error, analysis_model,
                            analysis_version, content_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', 'b-roll', 'interior', '', '', '[]', '[]', 0, '[]', '[]', 'pending', NULL, '', 1, ?, ?, ?)
                    """, (
                        seg_id, item_id, project_id, idx, start_ms, end_ms, dur,
                        dump(keyframe_rel_paths), chash, created_at, created_at
                    ))

        return self.get_item(project_id, item_id)

    def get_item(self, project_id: str, item_id: str) -> Dict[str, Any]:
        item = self.db.one("SELECT * FROM media_library_items WHERE id=? AND project_id=?", (item_id, project_id))
        if not item:
            raise DomainError("Элемент медиатеки не найден.", 404)
        segments = self.db.all("SELECT * FROM media_segments WHERE item_id=? ORDER BY segment_index", (item_id,))
        for s in segments:
            s["keyframe_paths"] = json.loads(s.get("keyframe_paths_json") or "[]")
            s["objects"] = json.loads(s.get("objects_json") or "[]")
            s["actions"] = json.loads(s.get("actions_json") or "[]")
            s["visual_defects"] = json.loads(s.get("visual_defects_json") or "[]")
            s["recommended_montage_roles"] = json.loads(s.get("recommended_montage_roles_json") or "[]")
        item["segments"] = segments
        return item

    def list_items(self, project_id: str) -> List[Dict[str, Any]]:
        self.service.project(project_id)
        items = self.db.all("SELECT * FROM media_library_items WHERE project_id=? ORDER BY created_at DESC", (project_id,))
        segments_by_item = {}
        for s in self.db.all("SELECT * FROM media_segments WHERE project_id=? ORDER BY item_id, segment_index", (project_id,)):
            s["keyframe_paths"] = json.loads(s.get("keyframe_paths_json") or "[]")
            s["objects"] = json.loads(s.get("objects_json") or "[]")
            s["actions"] = json.loads(s.get("actions_json") or "[]")
            s["visual_defects"] = json.loads(s.get("visual_defects_json") or "[]")
            s["recommended_montage_roles"] = json.loads(s.get("recommended_montage_roles_json") or "[]")
            segments_by_item.setdefault(s["item_id"], []).append(s)

        for it in items:
            it["segments"] = segments_by_item.get(it["id"], [])
        return items

    def delete_item(self, project_id: str, item_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        item = self.db.one("SELECT * FROM media_library_items WHERE id=? AND project_id=?", (item_id, project_id))
        if not item:
            raise DomainError("Элемент медиатеки не найден.", 404)

        # Delete database records
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM media_segments WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM media_library_items WHERE id=?", (item_id,))

        # Clean up files if not referenced by another project
        try:
            file_path = self.service.root / item["file_path"]
            if file_path.exists() and not self.db.one("SELECT id FROM assets WHERE path=? LIMIT 1",(item["file_path"],)) and not self.db.one("SELECT id FROM media_library_items WHERE sha256=? AND id!=?", (item["sha256"], item_id)):
                file_path.unlink(missing_ok=True)
            if item.get("thumbnail_path"):
                (self.service.root / item["thumbnail_path"]).unlink(missing_ok=True)
        except Exception:
            pass

        return {"status": "deleted", "id": item_id}

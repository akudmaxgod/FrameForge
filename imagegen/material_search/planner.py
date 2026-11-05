"""Search query planner and segment requirement extractor based on montage voiceover."""
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set

from ..db import uid, now
from ..domain import DomainError
from ..media_segment_placement import logical_segments
from .entities import EntityCatalog, CatalogEntity

logger = logging.getLogger(__name__)

# Basic dictionary of action verbs and keywords to extract clean search terms
STOP_WORDS = {
    "и", "в", "на", "с", "по", "у", "к", "о", "об", "за", "из", "от", "для", "до",
    "что", "как", "это", "то", "же", "все", "он", "она", "они", "мы", "вы", "был",
    "была", "были", "будет", "тот", "этот", "когда", "где", "куда", "откуда", "так",
    "a", "an", "the", "in", "on", "at", "by", "for", "with", "from", "into", "to", "and"
}

def clean_search_phrase(phrase: str) -> str:
    """Extract 2-4 key words from a narration sentence suitable for stock search."""
    if not phrase:
        return ""
    words = re.sub(r"[^\w\s-]", " ", phrase.lower(), flags=re.UNICODE).split()
    meaningful = [w for w in words if len(w) > 2 and w not in STOP_WORDS]
    if not meaningful:
        return phrase.strip()[:60]
    return " ".join(meaningful[:4])


@dataclass
class SegmentRequirement:
    id: str
    plan_id: str
    segment_key: str
    origin_clip_ids: List[str]
    narration_origin_clip_id: str
    quote: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    duration_ms: int = 0
    subject_entities: List[str] = field(default_factory=list)
    action: str = ""
    location: str = ""
    period: str = ""
    mandatory_details: List[str] = field(default_factory=list)
    forbidden_substitutions: List[str] = field(default_factory=list)
    universe_id: str = ""
    requested_role: str = "exact"  # 'exact' | 'illustrative' | 'atmosphere'
    generated_queries: List[str] = field(default_factory=list)
    user_queries: List[str] = field(default_factory=list)
    enabled: bool = True

    def effective_queries(self) -> List[str]:
        return self.user_queries if self.user_queries else self.generated_queries

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["effective_queries"] = self.effective_queries()
        return d


class MaterialSearchPlanner:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.entities = EntityCatalog(service)

    def generate_plan(
        self,
        project_id: str,
        scene_ids: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Creates a structured search plan from current montage document segments.
        Extracts voiceover bounds, recognized entities, and generates 1-3 editable queries per segment.
        """
        self.service.project(project_id)
        edit_res = self.service.editing.get(project_id)
        doc = edit_res.get("document", {}) if isinstance(edit_res, dict) else {}
        clips = doc.get("clips", [])
        if not clips:
            raise DomainError("Проект не содержит сцен для поиска материалов.", 400)

        options = options or {}
        preferred_providers = options.get("providers", ["pixabay", "commons", "local_library"])
        media_type = options.get("media_type", "both")

        # 1. Extract logical segments tied to narration
        try:
            raw_segments = logical_segments(doc)
        except Exception:
            raw_segments = []

        if raw_segments:
            for seg in raw_segments:
                if "origin_clip_ids" not in seg:
                    seg["origin_clip_ids"] = seg.get("replace_clip_ids") or [seg.get("edit_clip_id") or seg.get("id")]
                if "segment_key" not in seg:
                    seg["segment_key"] = f"seg_{seg.get('narration_origin_clip_id') or seg['origin_clip_ids'][0]}"
                if not seg.get("quote"):
                    origin = seg.get("narration_origin_clip_id")
                    narr_clip = next((n for n in doc.get("narration_clips", []) if n.get("id") == origin), None)
                    if narr_clip and narr_clip.get("text"):
                        seg["quote"] = narr_clip["text"]
                    elif seg.get("scene_id"):
                        row = self.db.one(
                            """
                            SELECT sr.data
                            FROM scene_revisions sr
                            JOIN scenes s ON s.id = sr.scene_id AND s.current_revision = sr.revision
                            WHERE s.id = ?
                            """,
                            (seg["scene_id"],)
                        )
                        if row and row.get("data"):
                            s_data = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
                            seg["quote"] = s_data.get("source_quote") or s_data.get("beat") or s_data.get("prompt") or s_data.get("text")
                if not seg.get("quote"):
                    first_clip = next((c for c in clips if (c.get("edit_clip_id") or c.get("id")) in seg["origin_clip_ids"]), {})
                    seg["quote"] = first_clip.get("prompt") or first_clip.get("title") or "Сцена"
        else:
            # Fallback if no voiceover is attached yet: one segment per clip
            raw_segments = []
            for c in clips:
                cid = c.get("edit_clip_id") or c.get("id") or "clip"
                dur = c.get("duration_ms") or int((c.get("duration", 2.0)) * 1000)
                raw_segments.append({
                    "segment_key": f"clip_{cid}",
                    "origin_clip_ids": [cid],
                    "narration_origin_clip_id": c.get("narration_origin_clip_id", ""),
                    "quote": c.get("prompt") or c.get("title") or f"Сцена #{cid[:6]}",
                    "start_ms": None,
                    "end_ms": None,
                    "duration_ms": dur
                })

        # Filter by selected scene_ids if provided
        selected_set = set(scene_ids or [])
        if selected_set:
            raw_segments = [
                s for s in raw_segments
                if any(cid in selected_set for cid in s.get("origin_clip_ids", []))
            ]

        if not raw_segments:
            raise DomainError("Не найдено подходящих сегментов монтажа для выбранных сцен.", 400)

        # 2. Build snapshot hash to detect if montage or narration changes later
        snapshot_content = json.dumps([
            {"k": s["segment_key"], "q": s.get("quote"), "dur": s.get("duration_ms")}
            for s in raw_segments
        ], sort_keys=True)
        snapshot_hash = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()[:16]

        plan_id = uid()
        t_now = now()

        # 3. Process requirements and generate queries
        requirements: List[SegmentRequirement] = []
        for idx, seg in enumerate(raw_segments):
            req_id = uid()
            seg_key = seg["segment_key"]
            quote = (seg.get("quote") or "").strip()
            clip_ids = seg.get("origin_clip_ids") or []
            narration_id = seg.get("narration_origin_clip_id") or ""
            dur_ms = seg.get("duration_ms") or 0
            start_ms = seg.get("start_ms")
            end_ms = seg.get("end_ms")

            # Match known entities from project catalog
            matched_ents = self.entities.match_entities_in_text(project_id, quote)
            subject_names = [e.canonical_name for e in matched_ents]
            mandatory: List[str] = []
            forbidden: List[str] = []
            universe = ""

            for ent in matched_ents:
                mandatory.extend(ent.mandatory_details)
                forbidden.extend(ent.forbidden_substitutions)
                if ent.universe_id and not universe:
                    universe = ent.universe_id

            # Role: if entity matched -> 'exact', otherwise 'illustrative'
            role = "exact" if matched_ents else "illustrative"

            # Generate 1-3 short queries
            queries: List[str] = []
            seen_q = set()

            # Strategy A: Use canonical entity names / primary aliases
            for ent in matched_ents:
                q_cand = ent.canonical_name
                if q_cand.lower() not in seen_q:
                    queries.append(q_cand)
                    seen_q.add(q_cand.lower())
                for al in ent.aliases[:2]:
                    if al.lower() not in seen_q:
                        queries.append(al)
                        seen_q.add(al.lower())

            # Strategy B: Extract clean keywords from quote
            clean_q = clean_search_phrase(quote)
            if clean_q and clean_q.lower() not in seen_q:
                queries.append(clean_q)
                seen_q.add(clean_q.lower())

            # Limit to 3 queries max per segment
            queries = queries[:3]
            if not queries:
                queries = [quote[:50]]

            req = SegmentRequirement(
                id=req_id,
                plan_id=plan_id,
                segment_key=seg_key,
                origin_clip_ids=clip_ids,
                narration_origin_clip_id=narration_id,
                quote=quote,
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=dur_ms,
                subject_entities=subject_names,
                action="",
                location="",
                period="",
                mandatory_details=list(set(mandatory)),
                forbidden_substitutions=list(set(forbidden)),
                universe_id=universe,
                requested_role=role,
                generated_queries=queries,
                user_queries=[],
                enabled=True
            )
            requirements.append(req)

        voice_hash = ""
        if hasattr(self.service, "media_matching") and hasattr(self.service.media_matching, "compute_voice_hash"):
            try:
                vh = self.service.media_matching.compute_voice_hash(project_id)
                if isinstance(vh, str):
                    voice_hash = vh
            except Exception:
                voice_hash = ""
        clip_signatures = {}
        for c in clips:
            cid = c.get("edit_clip_id") or c.get("id")
            if cid:
                clip_signatures[str(cid)] = {
                    "source_image_asset_id": c.get("source_image_asset_id") if isinstance(c.get("source_image_asset_id"), str) else None,
                    "source_video_asset_id": c.get("source_video_asset_id") if isinstance(c.get("source_video_asset_id"), str) else None,
                    "scene_id": (c.get("scene_id") or c.get("origin_scene_id")) if isinstance(c.get("scene_id") or c.get("origin_scene_id"), str) else None,
                    "quote": (c.get("prompt") or c.get("title") or "") if isinstance(c.get("prompt") or c.get("title"), str) else ""
                }
        options_to_save = dict(options)
        options_to_save["voice_hash"] = voice_hash
        options_to_save["clip_signatures"] = clip_signatures

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO material_search_plans (
                    id, project_id, snapshot_hash, version, options_json, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (plan_id, project_id, snapshot_hash, json.dumps(options_to_save), t_now, t_now)
            )

            for req in requirements:
                conn.execute(
                    """
                    INSERT INTO material_search_requirements (
                        id, plan_id, segment_key, origin_clip_ids_json,
                        narration_origin_clip_id, quote, start_ms, end_ms, duration_ms,
                        subject_entities_json, action, location, period,
                        mandatory_details_json, forbidden_substitutions_json,
                        universe_id, requested_role, generated_queries_json,
                        user_queries_json, enabled, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req.id, plan_id, req.segment_key, json.dumps(req.origin_clip_ids),
                        req.narration_origin_clip_id, req.quote, req.start_ms, req.end_ms, req.duration_ms,
                        json.dumps(req.subject_entities), "", "", "",
                        json.dumps(req.mandatory_details), json.dumps(req.forbidden_substitutions),
                        req.universe_id, req.requested_role, json.dumps(req.generated_queries),
                        json.dumps([]), 1, t_now
                    )
                )

        # 4. Calculate query budget
        total_queries = sum(len(r.effective_queries()) for r in requirements)
        provider_calls = total_queries * len(preferred_providers)

        return {
            "ok": True,
            "plan_id": plan_id,
            "project_id": project_id,
            "snapshot_hash": snapshot_hash,
            "segments_count": len(requirements),
            "total_queries": total_queries,
            "estimated_provider_calls": provider_calls,
            "preferred_providers": preferred_providers,
            "requirements": [r.to_dict() for r in requirements]
        }

    def get_plan(self, project_id: str, plan_id: str) -> Dict[str, Any]:
        self.service.project(project_id)
        plan_row = self.db.one(
            "SELECT * FROM material_search_plans WHERE id = ? AND project_id = ?",
            (plan_id, project_id)
        )
        if not plan_row:
            raise DomainError("План поиска не найден.", 404)
        plan_dict = dict(plan_row)

        req_rows = self.db.all(
            "SELECT * FROM material_search_requirements WHERE plan_id = ? ORDER BY created_at ASC",
            (plan_id,)
        )
        requirements: List[Dict[str, Any]] = []
        for r in req_rows:
            rd = dict(r)
            gen_q = json.loads(rd.get("generated_queries_json") or "[]")
            user_q = json.loads(rd.get("user_queries_json") or "[]")
            req = SegmentRequirement(
                id=rd["id"],
                plan_id=rd["plan_id"],
                segment_key=rd["segment_key"],
                origin_clip_ids=json.loads(rd.get("origin_clip_ids_json") or "[]"),
                narration_origin_clip_id=rd.get("narration_origin_clip_id", ""),
                quote=rd.get("quote", ""),
                start_ms=rd.get("start_ms"),
                end_ms=rd.get("end_ms"),
                duration_ms=rd.get("duration_ms", 0),
                subject_entities=json.loads(rd.get("subject_entities_json") or "[]"),
                action=rd.get("action", ""),
                location=rd.get("location", ""),
                period=rd.get("period", ""),
                mandatory_details=json.loads(rd.get("mandatory_details_json") or "[]"),
                forbidden_substitutions=json.loads(rd.get("forbidden_substitutions_json") or "[]"),
                universe_id=rd.get("universe_id", ""),
                requested_role=rd.get("requested_role", "exact"),
                generated_queries=gen_q,
                user_queries=user_q,
                enabled=bool(rd.get("enabled", 1))
            )
            requirements.append(req.to_dict())

        opts = json.loads(plan_dict.get("options_json") or "{}")
        total_q = sum(len(r["effective_queries"]) for r in requirements if r["enabled"])

        return {
            "ok": True,
            "plan_id": plan_id,
            "project_id": project_id,
            "snapshot_hash": plan_dict.get("snapshot_hash", ""),
            "version": plan_dict.get("version", 1),
            "options": opts,
            "total_queries": total_q,
            "requirements": requirements
        }

    def update_requirement(
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
        """Allows user to customize queries, change role, or toggle segment inclusion."""
        self.service.project(project_id)
        req_row = self.db.one(
            """
            SELECT r.* FROM material_search_requirements r
            JOIN material_search_plans p ON p.id = r.plan_id
            WHERE r.id = ? AND r.plan_id = ? AND p.project_id = ?
            """,
            (requirement_id, plan_id, project_id)
        )
        if not req_row:
            raise DomainError("Требование сегмента не найдено.", 404)

        updates = []
        params = []
        if user_queries is not None:
            clean_list = [q.strip() for q in user_queries if q.strip()]
            updates.append("user_queries_json = ?")
            params.append(json.dumps(clean_list))
        if requested_role in ("exact", "illustrative", "atmosphere"):
            updates.append("requested_role = ?")
            params.append(requested_role)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if quote is not None:
            updates.append("quote = ?")
            params.append(quote.strip())
        if mandatory_details is not None:
            updates.append("mandatory_details_json = ?")
            params.append(json.dumps(list(set(mandatory_details))))
        if forbidden_substitutions is not None:
            updates.append("forbidden_substitutions_json = ?")
            params.append(json.dumps(list(set(forbidden_substitutions))))

        if updates:
            params.extend([requirement_id, plan_id])
            with self.db.transaction() as conn:
                conn.execute(
                    f"UPDATE material_search_requirements SET {', '.join(updates)} WHERE id = ? AND plan_id = ?",
                    params
                )
                conn.execute(
                    "UPDATE material_search_plans SET version = version + 1, updated_at = ? WHERE id = ?",
                    (now(), plan_id)
                )

        return self.get_plan(project_id, plan_id)

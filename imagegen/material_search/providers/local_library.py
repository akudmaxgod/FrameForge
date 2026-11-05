"""Local project media library and collection provider under the unified MaterialSearchProvider contract."""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from .base import MaterialSearchProvider, MaterialSearchItem
from ...domain import DomainError

logger = logging.getLogger(__name__)

class LocalLibraryProvider(MaterialSearchProvider):
    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider_id": "local_library",
            "name": "Локальная медиатека проекта",
            "media_types": ["photo", "video"],
            "languages": ["ru", "en"],
            "auth_required": False,
            "auth_configured": True,
            "supports_user_queries": True,
            "supports_batch": True,
            "cache_policy": "instant_local",
            "limits": {
                "max_results_per_query": 100
            }
        }

    def search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        cursor: Optional[str] = None,
        cancellation: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        if cancellation and cancellation.is_set():
            return {"ok": False, "provider": "local_library", "cancelled": True, "items": []}

        filters = filters or {}
        project_id = filters.get("project_id", "")
        media_type = filters.get("media_type")
        page = int(cursor or 1)
        per_page = int(filters.get("per_page", 20))
        q = (query or "").strip().lower()

        # Query media_library_items
        sql = "SELECT * FROM media_library_items WHERE 1=1"
        params = []
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if media_type:
            sql += " AND media_type = ?"
            params.append(media_type)
        if q:
            sql += " AND LOWER(original_name) LIKE ?"
            params.append(f"%{q}%")

        sql += " ORDER BY created_at DESC"
        rows = self.db.all(sql, params)

        items: List[Dict[str, Any]] = []
        for r in rows:
            row_dict = dict(r)
            item_id = row_dict["id"]
            mtype = row_dict.get("media_type", "photo")
            title = row_dict.get("title") or row_dict.get("original_name") or f"Local #{item_id[:8]}"
            preview = f"/api/projects/{row_dict.get('project_id')}/media/{item_id}/preview"

            # Check if source is recorded
            src_row = self.db.one(
                "SELECT * FROM media_library_sources WHERE project_id = ? AND item_id = ?",
                (row_dict.get("project_id"), item_id)
            )
            license_name = src_row["license_type"] if src_row else "Пользовательский материал"
            author = src_row["author_name"] if src_row else "Локальный пользователь"

            item = MaterialSearchItem(
                provider="local_library",
                external_id=item_id,
                media_type=mtype,
                title=title,
                description=title,
                source_page_url="",
                preview_url=preview,
                thumbnail_url=preview,
                large_url=preview,
                download_url=preview,
                author=author,
                author_url="",
                width=int(row_dict.get("width") or 1920),
                height=int(row_dict.get("height") or 1080),
                duration_ms=int((row_dict.get("duration_sec") or 0.0) * 1000),
                license_name=license_name,
                license_url="",
                attribution_text=f"{title} ({author})",
                rights_status="permission_recorded",
                retrieved_at=row_dict.get("created_at", ""),
                canonical_source_id=f"local_library:{mtype}:{item_id}",
                provider_metadata={"local_file_path": row_dict.get("file_path", "")}
            )
            items.append(item.to_dict())

        total = len(items)
        offset = (page - 1) * per_page
        sliced = items[offset:offset + per_page]
        next_cursor = str(page + 1) if (offset + per_page) < total else None

        return {
            "ok": True,
            "provider": "local_library",
            "items": sliced,
            "next_cursor": next_cursor,
            "total_hits": total,
            "cached": True,
            "rate_limit": {"remaining": 9999, "limit": 9999, "reset_at": 0}
        }

    def resolve(self, external_id: str, media_type: str = "photo") -> Optional[MaterialSearchItem]:
        row = self.db.one("SELECT * FROM media_library_items WHERE id = ?", (external_id,))
        if not row:
            return None
        r = dict(row)
        item_id = r["id"]
        title = r.get("title") or r.get("original_name") or f"Local #{item_id[:8]}"
        preview = f"/api/projects/{r.get('project_id')}/media/{item_id}/preview"
        return MaterialSearchItem(
            provider="local_library",
            external_id=item_id,
            media_type=r.get("media_type", media_type),
            title=title,
            description=title,
            preview_url=preview,
            thumbnail_url=preview,
            large_url=preview,
            download_url=preview,
            author="Локальный пользователь",
            width=int(r.get("width") or 1920),
            height=int(r.get("height") or 1080),
            duration_ms=int((r.get("duration_sec") or 0.0) * 1000),
            license_name="Пользовательский материал",
            rights_status="permission_recorded",
            retrieved_at=r.get("created_at", ""),
            canonical_source_id=f"local_library:{r.get('media_type', media_type)}:{item_id}"
        )

    def import_selected(
        self,
        project_id: str,
        external_id: str,
        media_type: str = "photo",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM media_library_items WHERE id = ?", (external_id,))
        if not row:
            raise DomainError(f"Локальный материал #{external_id} не найден.", 404)
        return {
            "ok": True,
            "item": dict(row),
            "source": {
                "provider": "local_library",
                "external_id": external_id,
                "author_name": "Локальный пользователь",
                "license_type": "local_user"
            }
        }

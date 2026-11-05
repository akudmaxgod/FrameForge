"""Pixabay adapter wrapping existing PixabayProvider under the unified MaterialSearchProvider contract."""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from .base import MaterialSearchProvider, MaterialSearchItem
from ...stock_secrets import is_pixabay_key_configured

logger = logging.getLogger(__name__)

class PixabayAdapter(MaterialSearchProvider):
    def __init__(self, service):
        super().__init__(service)
        self.underlying = service.pixabay

    def capabilities(self) -> Dict[str, Any]:
        configured = is_pixabay_key_configured(self.root)
        rate = self.underlying.get_rate_limit_state()
        return {
            "provider_id": "pixabay",
            "name": "Pixabay (Фото и Видео)",
            "media_types": ["photo", "video"],
            "languages": ["ru", "en"],
            "auth_required": True,
            "auth_configured": configured,
            "supports_user_queries": True,
            "supports_batch": False,  # Manual user queries per TOS, no autonomous massive scrape
            "cache_policy": "24_hours",
            "limits": {
                "max_results_per_query": 200,
                "rate_limit_per_minute": rate.get("rate_limit", 100),
                "rate_remaining": rate.get("rate_remaining", 100),
                "reset_at": rate.get("rate_reset_at", 0)
            }
        }

    def _normalize_item(self, raw: Dict[str, Any], media_type: str) -> MaterialSearchItem:
        ext_id = str(raw.get("external_id") or raw.get("id") or "").replace("pixabay_photo_", "").replace("pixabay_video_", "")
        mtype = raw.get("media_type") or media_type
        dur_sec = float(raw.get("duration_sec") or 0.0)
        dur_ms = int(dur_sec * 1000)

        author = raw.get("author") or raw.get("user") or ""
        author_url = raw.get("author_url") or ""
        page_url = raw.get("page_url") or raw.get("pageURL") or ""

        # Pixabay content license
        license_name = "Pixabay Content License"
        license_url = "https://pixabay.com/service/license-summary/"
        attribution = f"Фото/видео автора {author} с Pixabay" if author else "Материал с Pixabay"

        return MaterialSearchItem(
            provider="pixabay",
            external_id=ext_id,
            media_type=mtype,
            title=raw.get("title") or f"Pixabay #{ext_id}",
            description=", ".join(raw.get("tags") or []) if isinstance(raw.get("tags"), list) else str(raw.get("tags") or ""),
            source_page_url=page_url,
            preview_url=raw.get("preview_url") or "",
            thumbnail_url=raw.get("thumbnail_url") or raw.get("preview_url") or "",
            large_url=raw.get("large_url") or "",
            download_url=raw.get("download_url") or raw.get("large_url") or raw.get("preview_url") or "",
            author=author,
            author_url=author_url,
            width=int(raw.get("width") or 1920),
            height=int(raw.get("height") or 1080),
            duration_ms=dur_ms,
            license_name=license_name,
            license_url=license_url,
            attribution_text=attribution,
            rights_status="recorded_license",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            canonical_source_id=f"pixabay:{mtype}:{ext_id}",
            provider_metadata=raw.get("metadata") or {}
        )

    def search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        cursor: Optional[str] = None,
        cancellation: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        if cancellation and cancellation.is_set():
            return {"ok": False, "provider": "pixabay", "cancelled": True, "items": []}

        filters = filters or {}
        media_type = filters.get("media_type", "photo")
        page = int(cursor or 1)
        per_page = int(filters.get("per_page", 20))
        orientation = filters.get("orientation", "horizontal")
        bypass_cache = bool(filters.get("bypass_cache", False))

        res = self.underlying.search(
            q=query,
            media_type=media_type,
            orientation=orientation,
            page=page,
            per_page=per_page,
            bypass_cache=bypass_cache
        )

        items = [self._normalize_item(it, media_type).to_dict() for it in res.get("items", [])]
        total_hits = res.get("total_hits", 0)
        next_cursor = str(page + 1) if (page * per_page) < total_hits else None

        return {
            "ok": True,
            "provider": "pixabay",
            "items": items,
            "next_cursor": next_cursor,
            "total_hits": total_hits,
            "cached": res.get("cached", False),
            "search_id": res.get("search_id"),
            "rate_limit": res.get("rate_limit", {})
        }

    def resolve(self, external_id: str, media_type: str = "photo") -> Optional[MaterialSearchItem]:
        row = self.underlying.get_stock_result("pixabay", external_id)
        if not row:
            row = self.db.one(
                "SELECT * FROM stock_results WHERE provider = 'pixabay' AND external_id = ? AND media_type = ?",
                (external_id, media_type)
            )
        if row:
            return self._normalize_item(dict(row), media_type)
        return None

    def import_selected(
        self,
        project_id: str,
        external_id: str,
        media_type: str = "photo",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self.underlying.import_stock_item(project_id, external_id, media_type)

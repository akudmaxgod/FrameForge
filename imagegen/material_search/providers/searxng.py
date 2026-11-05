"""Image search using a local SearXNG instance with JSON output enabled."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .base import MaterialSearchItem, MaterialSearchProvider
from ...db import now
from ...domain import DomainError
from ..web_image_fetch import NoWebRedirect, import_web_image

SEARCH_URL = "http://127.0.0.1:8891/search"
MAX_RESPONSE_BYTES = 4_000_000


class SearxImagesProvider(MaterialSearchProvider):
    def capabilities(self):
        return {
            "provider_id": "searxng",
            "name": "SearXNG (локальный поиск)",
            "auth_required": False,
            "supports_batch": False,
            "manual_only": True,
            "supports_user_queries": True,
            "media_types": ["photo"],
            "cache_policy": "session_ephemeral",
            "languages": ["ru", "en"],
        }

    def search(self, query, filters=None, cursor=None, cancellation=None):
        if cancellation and cancellation.is_set():
            return {"results": [], "cancelled": True}
        if not query.strip():
            raise DomainError("Введите запрос.", 400)
        params = {
            "q": query,
            "categories": "images",
            "format": "json",
            "safesearch": 1,
            "language": "auto",
        }
        request = urllib.request.Request(
            SEARCH_URL + "?" + urllib.parse.urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "FrameForge"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoWebRedirect())
        try:
            with opener.open(request, timeout=35) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise DomainError("SearXNG вернул HTTP 403. Включите формат JSON в настройках локального поиска.", 502) from error
            raise DomainError(f"SearXNG вернул HTTP {error.code}.", 502) from error
        except (urllib.error.URLError, OSError) as error:
            raise DomainError("Локальный поиск SearXNG недоступен. Запустите сервис на порту 8891 и повторите поиск.", 503) from error

        if len(raw) > MAX_RESPONSE_BYTES:
            raise DomainError("SearXNG вернул слишком большой ответ.", 502)
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise DomainError("SearXNG вернул некорректный JSON.", 502) from error
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise DomainError("SearXNG вернул данные в неожиданном формате.", 502)

        items = []
        seen_images = set()
        orientation = (filters or {}).get("orientation")
        for result in data["results"]:
            if not isinstance(result, dict):
                continue
            image_url = result.get("img_src")
            if not isinstance(image_url, str) or not image_url.startswith("https://") or image_url in seen_images:
                continue
            size = re.search(r"(\d{1,6})\s*[x×]\s*(\d{1,6})", str(result.get("resolution", "")))
            width, height = map(int, size.groups()) if size else (0, 0)
            if width and height and (orientation == "horizontal" and width < height or orientation == "vertical" and width > height):
                continue
            thumbnail = result.get("thumbnail_src")
            if not isinstance(thumbnail, str) or not thumbnail.startswith("https://"):
                thumbnail = image_url
            page_url = result.get("url")
            try:
                page = urllib.parse.urlsplit(page_url if isinstance(page_url, str) else "")
                domain = page.hostname if page.scheme in {"https", "http"} else None
            except ValueError:
                domain = None
            title = result.get("title")
            items.append(MaterialSearchItem(
                provider="searxng",
                external_id="web_" + hashlib.sha256(image_url.encode()).hexdigest()[:16],
                media_type="photo",
                title=title if isinstance(title, str) and title else "Изображение",
                source_page_url=page_url if domain else "",
                preview_url=thumbnail,
                thumbnail_url=thumbnail,
                large_url=image_url,
                download_url=image_url,
                author=domain or "",
                author_url=f"https://{domain}" if domain else "",
                width=width,
                height=height,
                rights_status="unknown",
                license_name="Права не установлены",
                retrieved_at=now(),
                provider_metadata={"engines": result.get("engines", [])},
            ).to_dict())
            seen_images.add(image_url)
            if len(items) == 100:
                break
        return {"results": items, "unresponsive_engines": data.get("unresponsive_engines", [])}

    def resolve(self, external_id, media_type="photo"):
        # Manual browse sessions already hold the selected image URL.
        return None

    def import_selected(
        self,
        project_id: str,
        external_id: str,
        media_type: str = "photo",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        options = options or {}
        if not options.get("download_url"):
            raise DomainError(f"URL для загрузки материала #{external_id} не найден.", 404)
        domain = options.get("domain") or ""
        return import_web_image(
            self.service, project_id, options["download_url"],
            provider="searxng", external_id=external_id,
            source_page_url=options.get("source_page_url", ""),
            author=domain, author_url=f"https://{domain}" if domain else "",
        )

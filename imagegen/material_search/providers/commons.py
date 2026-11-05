"""Wikimedia Commons provider implementation with 24-hour search cache, HTML sanitization, and safe download."""
import html
import io
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from uuid import uuid4
from PIL import Image

from .base import MaterialSearchProvider, MaterialSearchItem
from ...domain import DomainError
from ...stock_secrets import sanitize_url, sanitize_error

logger = logging.getLogger(__name__)

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "FrameForge-Studio/2.0 (Windows; https://github.com/akudmaxgod/FrameForge; contact: dev@frameforge.local)"

ALLOWED_COMMONS_DOMAINS = (
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "wikimedia.org",
    "wikipedia.org"
)

SUPPORTED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp"
}

def is_allowed_commons_url(url: str) -> bool:
    """Strictly validates scheme, host, port, and userinfo against Wikimedia domains."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() != "https":
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port is not None and parsed.port != 443:
            return False
        host = (parsed.hostname or "").lower().strip()
        if not host:
            return False
        return any(host == base or host.endswith("." + base) for base in ALLOWED_COMMONS_DOMAINS)
    except Exception:
        return False

class SafeCommonsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces that redirects stay strictly within allowed HTTPS Wikimedia domains."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        full_url = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_commons_url(full_url):
            raise DomainError(
                f"Запрещено перенаправление на сторонний или незащищённый адрес: {sanitize_url(full_url)}",
                400
            )
        return super().redirect_request(req, fp, code, msg, headers, full_url)

def clean_html_metadata(raw_text: str) -> str:
    """Strips HTML tags and decodes entities to produce safe, clean plain text."""
    if not raw_text or not isinstance(raw_text, str):
        return ""
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", " ", raw_text)
    # Decode HTML entities (&amp;, &quot;, &#160;, etc.)
    clean = html.unescape(clean)
    # Normalize whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


class WikimediaCommonsProvider(MaterialSearchProvider):
    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider_id": "commons",
            "name": "Wikimedia Commons (Свободные медиа)",
            "media_types": ["photo"],
            "languages": ["ru", "en"],
            "auth_required": False,
            "auth_configured": True,
            "supports_user_queries": True,
            "supports_batch": True,
            "cache_policy": "24_hours",
            "limits": {
                "max_results_per_query": 50,
                "max_file_size_bytes": 25 * 1024 * 1024,
                "max_resolution_pixels": 60_000_000
            }
        }

    def canonical_query_key(self, q: str, page: int = 1, per_page: int = 20) -> str:
        clean_q = re.sub(r"\s+", " ", (q or "").strip().lower())
        return f"commons:photo:{clean_q}:p{page}:l{per_page}"

    def search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        cursor: Optional[str] = None,
        cancellation: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        if cancellation and cancellation.is_set():
            return {"ok": False, "provider": "commons", "cancelled": True, "items": []}

        filters = filters or {}
        per_page = max(3, min(50, int(filters.get("per_page", 20))))
        page = int(cursor or 1)
        query = (query or "").strip()
        if not query:
            return {"ok": True, "provider": "commons", "items": [], "total_hits": 0, "cached": False}

        query_key = self.canonical_query_key(query, page, per_page)
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        bypass_cache = bool(filters.get("bypass_cache", False))

        # 1. Check 24-hour cache
        if not bypass_cache:
            cached_row = self.db.one(
                """
                SELECT id, response_json, total_hits, expires_at
                FROM stock_search_cache
                WHERE provider = 'commons' AND query_key = ? AND expires_at > ?
                """,
                (query_key, now_iso)
            )
            if cached_row:
                try:
                    payload = json.loads(cached_row["response_json"])
                    payload["cached"] = True
                    payload["search_id"] = cached_row["id"]
                    return payload
                except Exception:
                    pass

        # 2. Build Wikimedia Commons Action API request
        # generator=search searches page titles and text in File namespace (6)
        offset = (page - 1) * per_page
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"file:{query}" if not query.lower().startswith("file:") else query,
            "gsrnamespace": "6",
            "gsrlimit": str(per_page),
            "gsroffset": str(offset),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "640",
            "iiurlheight": "360"
        }

        api_url = f"{COMMONS_API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": USER_AGENT}
        )
        opener = urllib.request.build_opener(SafeCommonsRedirectHandler())

        try:
            with opener.open(req, timeout=15.0) as resp:
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429:
                reset_sec = int(err.headers.get("Retry-After", 60)) if hasattr(err, "headers") else 60
                raise DomainError(f"Превышен лимит запросов Wikimedia Commons. Подождите {reset_sec} с.", 429)
            safe_msg = sanitize_error(str(err.reason or err))
            raise DomainError(f"Ошибка Wikimedia Commons API: {safe_msg}", 502)
        except Exception as e:
            safe_msg = sanitize_error(str(e))
            raise DomainError(f"Ошибка подключения к Wikimedia Commons: {safe_msg}", 502)

        # 3. Parse and standardize items
        items: List[Dict[str, Any]] = []
        pages = data.get("query", {}).get("pages", [])
        total_hits = data.get("query", {}).get("searchinfo", {}).get("totalhits", len(pages))

        for page_data in pages:
            # Check page info
            page_id = str(page_data.get("pageid") or "")
            title = page_data.get("title", "")
            if not page_id or not title:
                continue

            clean_title = re.sub(r"^File:", "", title, flags=re.IGNORECASE).strip()
            imageinfo_list = page_data.get("imageinfo", [])
            if not imageinfo_list:
                continue

            info = imageinfo_list[0]
            mime = (info.get("mime") or "").lower()
            if mime not in SUPPORTED_MIME_TYPES:
                # Skip SVGs, PDFs, TIFFs, audio files for direct photo search
                continue

            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            file_url = info.get("url") or ""
            thumb_url = info.get("thumburl") or file_url
            page_url = info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}"

            extmeta = info.get("extmetadata", {})
            artist_raw = extmeta.get("Artist", {}).get("value", "")
            credit_raw = extmeta.get("Credit", {}).get("value", "")
            desc_raw = extmeta.get("ImageDescription", {}).get("value", "")
            license_short = extmeta.get("LicenseShortName", {}).get("value", "")
            license_url = extmeta.get("LicenseUrl", {}).get("value", "")
            date_time = extmeta.get("DateTimeOriginal", {}).get("value", "") or extmeta.get("DateTime", {}).get("value", "")

            author = clean_html_metadata(artist_raw) or clean_html_metadata(credit_raw) or "Wikimedia Commons contributor"
            desc = clean_html_metadata(desc_raw) or clean_title

            rights_status = "recorded_license" if license_short else "unknown"
            attribution = f"{clean_title} — Автор: {author} ({license_short})" if license_short else f"{clean_title} — {author}"

            item = MaterialSearchItem(
                provider="commons",
                external_id=page_id,
                media_type="photo",
                title=clean_title,
                description=desc,
                source_page_url=page_url,
                preview_url=thumb_url,
                thumbnail_url=thumb_url,
                large_url=file_url,
                download_url=file_url,
                author=author,
                author_url=f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}",
                width=width,
                height=height,
                duration_ms=0,
                license_name=license_short or "Commons Public / Creative Commons",
                license_url=license_url or "https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia",
                attribution_text=attribution,
                rights_status=rights_status,
                retrieved_at=now_iso,
                canonical_source_id=f"commons:photo:{page_id}",
                provider_metadata={
                    "pageid": page_id,
                    "title": title,
                    "mime": mime,
                    "date": date_time,
                    "categories": extmeta.get("Categories", {}).get("value", "")
                }
            )
            item_dict = item.to_dict()
            items.append(item_dict)

            # Store / update in stock_results table
            try:
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO stock_results (
                            id, provider, external_id, media_type, title, tags,
                            page_url, author, author_url, duration_sec, width, height,
                            preview_url, large_url, download_url, metadata_json, created_at
                        ) VALUES (?, 'commons', ?, 'photo', ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(provider, external_id) DO UPDATE SET
                            title = excluded.title,
                            tags = excluded.tags,
                            page_url = excluded.page_url,
                            author = excluded.author,
                            author_url = excluded.author_url,
                            width = excluded.width,
                            height = excluded.height,
                            preview_url = excluded.preview_url,
                            large_url = excluded.large_url,
                            download_url = excluded.download_url,
                            metadata_json = excluded.metadata_json
                        """,
                        (
                            f"commons_photo_{page_id}", page_id, clean_title, query,
                            page_url, author, item.author_url, width, height,
                            thumb_url, file_url, file_url, json.dumps(item.provider_metadata), now_iso
                        )
                    )
            except Exception as e:
                logger.warning("Failed to store stock_results for commons #%s: %s", page_id, e)

        # 4. Check cache search_id stability
        existing_cache = self.db.one(
            "SELECT id FROM stock_search_cache WHERE provider = 'commons' AND query_key = ?",
            (query_key,)
        )
        if existing_cache and existing_cache["id"]:
            search_id = existing_cache["id"]
        else:
            search_id = str(uuid4())

        expires_at = (now_utc + timedelta(hours=24)).isoformat()
        has_more = (offset + len(pages)) < total_hits
        next_cursor = str(page + 1) if has_more else None

        result_payload = {
            "ok": True,
            "provider": "commons",
            "media_type": "photo",
            "query": query,
            "page": page,
            "per_page": per_page,
            "total_hits": total_hits,
            "items": items,
            "next_cursor": next_cursor,
            "cached": False,
            "search_id": search_id,
            "rate_limit": {
                "remaining": 100,
                "limit": 100,
                "reset_at": 0
            }
        }

        # 5. Save to 24-hour cache
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO stock_search_cache (
                        id, provider, query_key, media_type, response_json, total_hits, fetched_at, expires_at
                    ) VALUES (?, 'commons', ?, 'photo', ?, ?, ?, ?)
                    ON CONFLICT(provider, query_key) DO UPDATE SET
                        id = excluded.id,
                        response_json = excluded.response_json,
                        total_hits = excluded.total_hits,
                        fetched_at = excluded.fetched_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        search_id, query_key, json.dumps(result_payload), total_hits, now_iso, expires_at
                    )
                )
        except Exception as e:
            logger.warning("Failed to save stock search cache for commons: %s", e)

        return result_payload

    def resolve(self, external_id: str, media_type: str = "photo") -> Optional[MaterialSearchItem]:
        row = self.db.one(
            "SELECT * FROM stock_results WHERE provider = 'commons' AND external_id = ?",
            (str(external_id),)
        )
        if not row:
            return None
        r = dict(row)
        meta = json.loads(r.get("metadata_json") or "{}")
        return MaterialSearchItem(
            provider="commons",
            external_id=str(r["external_id"]),
            media_type="photo",
            title=r.get("title", ""),
            description=r.get("tags", ""),
            source_page_url=r.get("page_url", ""),
            preview_url=r.get("preview_url", ""),
            thumbnail_url=r.get("preview_url", ""),
            large_url=r.get("large_url", ""),
            download_url=r.get("download_url", ""),
            author=r.get("author", ""),
            author_url=r.get("author_url", ""),
            width=int(r.get("width") or 0),
            height=int(r.get("height") or 0),
            duration_ms=0,
            license_name="Commons License",
            license_url="https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia",
            attribution_text=f"{r.get('title', '')} — {r.get('author', '')}",
            rights_status="recorded_license",
            retrieved_at=r.get("created_at", ""),
            canonical_source_id=f"commons:photo:{r['external_id']}",
            provider_metadata=meta
        )

    def import_selected(
        self,
        project_id: str,
        external_id: str,
        media_type: str = "photo",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        self.service.project(project_id)
        external_id = str(external_id).strip()
        if not external_id:
            raise DomainError("Не указан идентификатор материала Commons для импорта.", 400)

        stock_row = self.db.one(
            "SELECT * FROM stock_results WHERE provider = 'commons' AND external_id = ?",
            (external_id,)
        )
        if not stock_row:
            raise DomainError(f"Материал Commons #{external_id} не найден в результатах поиска.", 404)

        download_url = stock_row["download_url"] or stock_row["large_url"]
        if not download_url:
            raise DomainError("URL для загрузки материала Commons отсутствует.", 400)

        # Validate download_url safety (HTTPS scheme, exact or dot-prefixed Wikimedia domains)
        if not is_allowed_commons_url(download_url):
            parsed = urllib.parse.urlparse(download_url)
            host = (parsed.hostname or "").lower().strip()
            raise DomainError(f"Недопустимый хост: {host or download_url}. Разрешены только домены Wikimedia (HTTPS).", 400)

        # Download to project temp directory with 25 MB max limit
        project_media_dir = self.service.media_library._project_dir(project_id)
        tmp_dir = project_media_dir / "stock_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = ".jpg"
        if "." in download_url.split("?")[0]:
            cand = "." + download_url.split("?")[0].split(".")[-1].lower()
            if cand in (".jpg", ".jpeg", ".png", ".webp"):
                ext = cand

        tmp_file = tmp_dir / f"commons_{external_id}_{int(time.time())}{ext}"
        max_bytes = 25 * 1024 * 1024  # 25 MB limit for photo
        downloaded = 0

        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": USER_AGENT}
        )
        opener = urllib.request.build_opener(SafeCommonsRedirectHandler())
        try:
            with opener.open(req, timeout=30.0) as resp:
                with open(tmp_file, "wb") as out_f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise DomainError("Файл превышает допустимый лимит размера для фото (25 МБ).", 400)
                        out_f.write(chunk)
        except DomainError:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            safe_err = sanitize_error(str(e))
            raise DomainError(f"Ошибка скачивания файла с Wikimedia Commons: {safe_err}", 502)

        if not tmp_file.exists() or tmp_file.stat().st_size == 0:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise DomainError("Загруженный файл пуст или повреждён.", 502)

        # Validate decompression bomb protection (max 60 megapixels)
        try:
            with Image.open(tmp_file) as img:
                w, h = img.size
                if (w * h) > 60_000_000:
                    raise DomainError("Изображение превышает максимальное разрешение 60 мегапикселей.", 400)
                img.verify()
        except DomainError:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise DomainError(f"Файл повреждён или не является корректным изображением: {e}", 400)

        # Import into media library
        try:
            imported_item = self.service.media_library.import_file(
                project_id=project_id,
                src_path=tmp_file,
                original_name=f"commons_{external_id}{ext}"
            )
        finally:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)

        # Record origin in media_library_sources
        source_id = str(uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO media_library_sources (
                        id, project_id, item_id, provider, external_id,
                        source_page_url, author_name, author_url, license_type, created_at
                    ) VALUES (?, ?, ?, 'commons', ?, ?, ?, ?, 'wikimedia_commons', ?)
                    ON CONFLICT(project_id, item_id) DO UPDATE SET
                        source_page_url = excluded.source_page_url,
                        author_name = excluded.author_name,
                        author_url = excluded.author_url,
                        license_type = excluded.license_type
                    """,
                    (
                        source_id, project_id, imported_item["id"], external_id,
                        stock_row["page_url"], stock_row["author"],
                        stock_row["author_url"], now_iso
                    )
                )
        except Exception:
            pass

        return {
            "ok": True,
            "item": imported_item,
            "source": {
                "provider": "commons",
                "external_id": external_id,
                "source_page_url": stock_row["page_url"],
                "author_name": stock_row["author"],
                "author_url": stock_row["author_url"],
                "license_type": "wikimedia_commons"
            }
        }

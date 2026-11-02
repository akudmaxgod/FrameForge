"""Pixabay stock provider implementation with 24-hour search cache and rate limiting."""
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from uuid import uuid4

from ..domain import DomainError
from ..stock_secrets import (
    get_pixabay_key, sanitize_url, sanitize_error,
    ALLOWED_STOCK_DOMAINS, is_allowed_stock_url,
    SafeStockRedirectHandler, SafeApiRedirectHandler
)

class PixabayProvider:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.root = service.root

    def get_rate_limit_state(self) -> Dict[str, Any]:
        row = self.db.one(
            "SELECT rate_limit, rate_remaining, rate_reset_at, updated_at FROM stock_rate_state WHERE provider = 'pixabay'"
        )
        if not row:
            return {
                'rate_limit': 100,
                'rate_remaining': 100,
                'rate_reset_at': 0,
                'updated_at': ''
            }
        return dict(row)

    def _update_rate_limits(self, headers: dict) -> None:
        try:
            limit = int(headers.get('X-RateLimit-Limit', 100))
            remaining = int(headers.get('X-RateLimit-Remaining', 99))
            reset_sec = int(headers.get('X-RateLimit-Reset', 60))
            reset_at = time.time() + reset_sec
            now_iso = datetime.now(timezone.utc).isoformat()
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO stock_rate_state (provider, rate_limit, rate_remaining, rate_reset_at, updated_at)
                    VALUES ('pixabay', ?, ?, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        rate_limit = excluded.rate_limit,
                        rate_remaining = excluded.rate_remaining,
                        rate_reset_at = excluded.rate_reset_at,
                        updated_at = excluded.updated_at
                    """,
                    (limit, remaining, reset_at, now_iso)
                )
        except Exception:
            pass

    def canonical_query_key(
        self,
        media_type: str,
        q: str,
        orientation: str = 'horizontal',
        page: int = 1,
        per_page: int = 20,
        category: str = '',
        video_type: str = 'all'
    ) -> str:
        clean_q = re.sub(r'\s+', ' ', (q or '').strip().lower())
        media = (media_type or 'photo').strip().lower()
        orient = (orientation or 'horizontal').strip().lower()
        cat = (category or '').strip().lower()
        vtype = (video_type or 'all').strip().lower()
        return f"pixabay:{media}:{clean_q}:{orient}:{vtype}:{cat}:p{page}:l{per_page}"

    def search(
        self,
        q: str,
        media_type: str = 'photo',
        orientation: str = 'horizontal',
        page: int = 1,
        per_page: int = 20,
        category: str = '',
        video_type: str = 'all',
        bypass_cache: bool = False
    ) -> Dict[str, Any]:
        media_type = 'video' if media_type == 'video' else 'photo'
        page = max(1, int(page or 1))
        per_page = max(3, min(200, int(per_page or 20)))
        q = (q or '').strip()

        query_key = self.canonical_query_key(
            media_type=media_type,
            q=q,
            orientation=orientation,
            page=page,
            per_page=per_page,
            category=category,
            video_type=video_type
        )

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()

        # Check 24-hour cache FIRST: if found, serve immediately without contacting Pixabay or checking rate limits
        if not bypass_cache:
            cached_row = self.db.one(
                """
                SELECT id, response_json, total_hits, expires_at
                FROM stock_search_cache
                WHERE provider = 'pixabay' AND query_key = ? AND expires_at > ?
                """,
                (query_key, now_iso)
            )
            if cached_row:
                try:
                    payload = json.loads(cached_row['response_json'])
                    payload['cached'] = True
                    payload['search_id'] = cached_row['id']
                    return payload
                except Exception:
                    pass

        # Cache miss or bypass: require valid API key
        key = get_pixabay_key(self.root)
        if not key:
            raise DomainError('Ключ Pixabay API не настроен. Укажите ключ в настройках проекта.', 400)

        # Check rate limits before making HTTP request
        rate_state = self.get_rate_limit_state()
        if rate_state.get('rate_remaining', 100) <= 0 and rate_state.get('rate_reset_at', 0) > time.time():
            wait_sec = int(rate_state['rate_reset_at'] - time.time()) + 1
            raise DomainError(f'Лимит запросов к Pixabay API исчерпан. Подождите {wait_sec} сек.', 429)

        # Build request URL
        params = {
            'key': key,
            'q': q,
            'page': page,
            'per_page': per_page,
            'safesearch': 'true'
        }
        if category:
            params['category'] = category

        if media_type == 'photo':
            params['image_type'] = 'photo'
            params['orientation'] = orientation or 'horizontal'
            base_url = "https://pixabay.com/api/?"
        else:
            params['video_type'] = video_type or 'all'
            base_url = "https://pixabay.com/api/videos/?"

        request_url = base_url + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            request_url,
            headers={'User-Agent': 'FrameForge-Studio/2.0 (Windows)'}
        )

        opener = urllib.request.build_opener(SafeApiRedirectHandler())
        try:
            with opener.open(req, timeout=12.0) as resp:
                headers = dict(resp.headers)
                self._update_rate_limits(headers)
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode('utf-8'))
        except urllib.error.HTTPError as err:
            self._update_rate_limits(dict(err.headers) if hasattr(err, 'headers') else {})
            if err.code in (400, 401):
                raise DomainError('Неверный или недействительный ключ Pixabay API.', 401)
            if err.code == 429:
                reset_sec = int(err.headers.get('X-RateLimit-Reset', 60)) if hasattr(err, 'headers') else 60
                raise DomainError(f'Превышен лимит запросов Pixabay API (100 запр./мин). Подождите {reset_sec} с.', 429)
            safe_msg = sanitize_error(str(err.reason or err), key)
            raise DomainError(f'Ошибка Pixabay API: {safe_msg}', 502)
        except Exception as e:
            safe_msg = sanitize_error(str(e), key)
            raise DomainError(f'Ошибка подключения к Pixabay: {safe_msg}', 502)

        # Standardize results
        items: List[Dict[str, Any]] = []
        raw_hits = data.get('hits', [])
        total_hits = data.get('totalHits', len(raw_hits))

        for hit in raw_hits:
            ext_id = str(hit.get('id'))
            if not ext_id:
                continue

            raw_tags = hit.get('tags', '')
            tags_list = [t.strip() for t in raw_tags.split(',') if t.strip()]
            first_tag = tags_list[0].title() if tags_list else f"Pixabay #{ext_id}"

            if media_type == 'photo':
                item_id = f"pixabay_photo_{ext_id}"
                w = int(hit.get('imageWidth') or 1920)
                h = int(hit.get('imageHeight') or 1080)
                duration = 0.0
                preview_url = hit.get('webformatURL') or hit.get('previewURL') or ''
                thumb_url = hit.get('previewURL') or hit.get('webformatURL') or ''
                large_url = hit.get('largeImageURL') or hit.get('webformatURL') or ''
                download_url = large_url or preview_url
            else:
                item_id = f"pixabay_video_{ext_id}"
                duration = float(hit.get('duration') or 0.0)
                videos_dict = hit.get('videos', {})
                best = videos_dict.get('large') or videos_dict.get('medium') or videos_dict.get('small') or {}
                tiny = videos_dict.get('tiny') or videos_dict.get('small') or {}

                w = int(best.get('width') or 1920)
                h = int(best.get('height') or 1080)
                download_url = best.get('url', '')
                large_url = download_url
                preview_url = tiny.get('url') or download_url

                pic_id = hit.get('picture_id')
                if pic_id:
                    thumb_url = f"https://i.vimeocdn.com/video/{pic_id}_640x360.jpg"
                else:
                    thumb_url = preview_url

            aspect_ratio = round(w / max(1, h), 2)
            user_name = hit.get('user', '')
            user_id = hit.get('user_id', '')
            author_url = f"https://pixabay.com/users/{user_name}-{user_id}/" if user_name else ''

            standardized = {
                'id': item_id,
                'provider': 'pixabay',
                'external_id': ext_id,
                'media_type': media_type,
                'title': first_tag,
                'tags': tags_list,
                'page_url': hit.get('pageURL', ''),
                'author': user_name,
                'author_url': author_url,
                'preview_url': preview_url,
                'thumbnail_url': thumb_url,
                'large_url': large_url,
                'download_url': download_url,
                'width': w,
                'height': h,
                'duration_sec': duration,
                'aspect_ratio': aspect_ratio,
                'metadata': {
                    'views': hit.get('views', 0),
                    'downloads': hit.get('downloads', 0),
                    'likes': hit.get('likes', 0)
                }
            }
            items.append(standardized)

            # Store / update in stock_results table
            try:
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO stock_results (
                            id, provider, external_id, media_type, title, tags,
                            page_url, author, author_url, duration_sec, width, height,
                            preview_url, large_url, download_url, metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(provider, external_id) DO UPDATE SET
                            title = excluded.title,
                            tags = excluded.tags,
                            page_url = excluded.page_url,
                            author = excluded.author,
                            author_url = excluded.author_url,
                            duration_sec = excluded.duration_sec,
                            width = excluded.width,
                            height = excluded.height,
                            preview_url = excluded.preview_url,
                            large_url = excluded.large_url,
                            download_url = excluded.download_url,
                            metadata_json = excluded.metadata_json
                        """,
                        (
                            item_id, 'pixabay', ext_id, media_type, first_tag, ','.join(tags_list),
                            hit.get('pageURL', ''), user_name, author_url, duration, w, h,
                            preview_url, large_url, download_url, json.dumps(standardized['metadata']), now_iso
                        )
                    )
            except Exception:
                pass

        # Check if query already has an id in stock_search_cache to guarantee search_id stability
        existing_cache = self.db.one(
            "SELECT id FROM stock_search_cache WHERE provider = 'pixabay' AND query_key = ?",
            (query_key,)
        )
        if existing_cache and existing_cache['id']:
            search_id = existing_cache['id']
        else:
            search_id = str(uuid4())

        expires_at = (now_utc + timedelta(hours=24)).isoformat()
        current_rate = self.get_rate_limit_state()

        result_payload = {
            'ok': True,
            'provider': 'pixabay',
            'media_type': media_type,
            'query': q,
            'page': page,
            'per_page': per_page,
            'total_hits': total_hits,
            'items': items,
            'cached': False,
            'search_id': search_id,
            'rate_limit': {
                'remaining': current_rate.get('rate_remaining', 100),
                'limit': current_rate.get('rate_limit', 100),
                'reset_at': current_rate.get('rate_reset_at', 0)
            }
        }

        # Save to 24-hour cache
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO stock_search_cache (
                        id, provider, query_key, media_type, response_json, total_hits, fetched_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, query_key) DO UPDATE SET
                        id = excluded.id,
                        response_json = excluded.response_json,
                        total_hits = excluded.total_hits,
                        fetched_at = excluded.fetched_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        search_id, 'pixabay', query_key, media_type,
                        json.dumps(result_payload), total_hits, now_iso, expires_at
                    )
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to save stock search cache: %s", e)

        return result_payload

    def get_search_by_id(self, search_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one(
            "SELECT id, response_json FROM stock_search_cache WHERE id = ?",
            (search_id,)
        )
        if not row:
            return None
        try:
            data = json.loads(row['response_json'])
            data['cached'] = True
            data['search_id'] = row['id']
            return data
        except Exception:
            return None

    def get_stock_result(self, provider: str, external_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one(
            "SELECT * FROM stock_results WHERE provider = ? AND external_id = ?",
            (provider, external_id)
        )
        return dict(row) if row else None

    def import_stock_item(self, project_id: str, external_id: str, media_type: str = 'photo') -> Dict[str, Any]:
        self.service.project(project_id)
        external_id = str(external_id).strip()
        if not external_id:
            raise DomainError('Не указан идентификатор материала для импорта.', 400)

        # Look up stock result
        stock_row = self.get_stock_result('pixabay', external_id)
        if not stock_row:
            row_by_id = self.db.one(
                "SELECT * FROM stock_results WHERE id = ?",
                (external_id,)
            )
            if row_by_id:
                stock_row = dict(row_by_id)
                external_id = str(stock_row['external_id'])
            else:
                raise DomainError(f'Материал Pixabay #{external_id} не найден в результатах поиска.', 404)

        download_url = stock_row.get('download_url') or stock_row.get('large_url') or stock_row.get('preview_url')
        if not download_url:
            raise DomainError('URL для загрузки материала отсутствует.', 400)

        media_type = stock_row.get('media_type', media_type)
        ext = '.mp4' if media_type == 'video' else '.jpg'
        if '.' in download_url.split('?')[0]:
            candidate_ext = '.' + download_url.split('?')[0].split('.')[-1].lower()
            if candidate_ext in ('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.webm'):
                ext = candidate_ext

        # Temporary download path
        project_media_dir = self.service.media_library._project_dir(project_id)
        tmp_dir = project_media_dir / 'stock_tmp'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = tmp_dir / f"pixabay_{external_id}_{int(time.time())}{ext}"

        # Validate download_url safety (HTTPS scheme, exact or dot-prefixed domain whitelist)
        if not is_allowed_stock_url(download_url):
            parsed = urllib.parse.urlparse(download_url)
            host = (parsed.hostname or '').lower().strip()
            raise DomainError(f'Недопустимый хост: {host or download_url}. Разрешены только Pixabay и Vimeo CDN (HTTPS).', 400)

        # Download file with size limit (max 500 MB) and safe redirect enforcement
        max_bytes = 500 * 1024 * 1024
        downloaded = 0
        req = urllib.request.Request(
            download_url,
            headers={'User-Agent': 'FrameForge-Studio/2.0 (Windows)'}
        )
        opener = urllib.request.build_opener(SafeStockRedirectHandler())
        try:
            with opener.open(req, timeout=30.0) as resp:
                with open(tmp_file, 'wb') as out_f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise DomainError('Файл превышает допустимый лимит размера (500 МБ).', 400)
                        out_f.write(chunk)
        except DomainError:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            safe_err = sanitize_error(str(e))
            raise DomainError(f'Ошибка скачивания файла со стока: {safe_err}', 502)

        if not tmp_file.exists() or tmp_file.stat().st_size == 0:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise DomainError('Загруженный файл пуст или повреждён.', 502)

        # Import into media library
        try:
            imported_item = self.service.media_library.import_file(
                project_id=project_id,
                src_path=tmp_file,
                original_name=f"pixabay_{media_type}_{external_id}{ext}"
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
                    ) VALUES (?, ?, ?, 'pixabay', ?, ?, ?, ?, 'free_pixabay', ?)
                    ON CONFLICT(project_id, item_id) DO UPDATE SET
                        source_page_url = excluded.source_page_url,
                        author_name = excluded.author_name,
                        author_url = excluded.author_url
                    """,
                    (
                        source_id, project_id, imported_item['id'], external_id,
                        stock_row.get('page_url', ''), stock_row.get('author', ''),
                        stock_row.get('author_url', ''), now_iso
                    )
                )
        except Exception:
            pass

        source_info = {
            'provider': 'pixabay',
            'external_id': external_id,
            'source_page_url': stock_row.get('page_url', ''),
            'author_name': stock_row.get('author', ''),
            'author_url': stock_row.get('author_url', ''),
            'license_type': 'free_pixabay'
        }

        return {
            'ok': True,
            'item': imported_item,
            'source': source_info
        }

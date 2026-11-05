"""Secure network transport for fetching arbitrary web images and thumbnails.
Enforces SSRF prevention, DNS rebinding mitigation, decompression bomb protection,
format validation, and strict size limits.
"""
import io
import hashlib
import ipaddress
import logging
import re
import socket
import ssl
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from http.client import HTTPSConnection
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urljoin
from urllib.request import HTTPRedirectHandler
from PIL import Image

from ..domain import DomainError
from ..db import now, uid

logger = logging.getLogger(__name__)

MAX_PREVIEW_BYTES = 5 * 1024 * 1024       # 5 MB
MAX_IMAGE_BYTES = 25 * 1024 * 1024        # 25 MB
MAX_IMAGE_PIXELS = 40_000_000             # 40 Megapixels
MAX_REDIRECTS = 3
SOCKET_TIMEOUT = 12.0
ALLOWED_IMAGE_FORMATS = ('JPEG', 'PNG', 'WEBP')
BLOCKED_METADATA_IPS = {'169.254.169.254', '100.100.100.200'}

# User-Agent for standard web image retrieval
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FrameForgeStudio/2.0 ImageFetcher"
WIKIMEDIA_USER_AGENT = "FrameForge-Studio/2.0 (https://github.com/akudmaxgod/FrameForge)"


class NoWebRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def import_web_image(service, project_id, url, *, provider, external_id,
                     source_page_url="", author="", author_url="", license_name=""):
    """Import a validated raster and retain the source for project credits."""
    service.project(project_id)
    fetched = fetch_safe_web_image(url, is_preview=False)
    image = fetched["bytes"]
    suffix = "." + fetched["format"]
    name = f"web_{hashlib.sha256(image).hexdigest()[:16]}{suffix}"
    temp_dir = service.root / "cache" / "tmp_imports"
    temp_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=temp_dir, suffix=suffix, delete=False) as upload:
        path = Path(upload.name)
    try:
        path.write_bytes(image)
        item = service.media_library.import_file(project_id, path, original_name=name)
    finally:
        path.unlink(missing_ok=True)

    license_type = license_name or "web_unspecified"
    with service.db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO media_library_sources (
                id, project_id, item_id, provider, external_id, source_page_url,
                author_name, author_url, license_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, item_id) DO NOTHING
            """,
            (uid(), project_id, item["id"], provider, external_id, source_page_url,
             author, author_url, license_type, now())
        )
    return {
        "ok": True,
        "item": item,
        "source": {
            "provider": provider, "external_id": external_id, "source_page_url": source_page_url,
            "author_name": author, "license_type": license_type,
            "rights_status": "recorded_license" if license_name else "unknown",
        },
    }


def is_safe_public_ip(ip_str: str) -> bool:
    """Ensure IP is a valid public unicast IP address, rejecting loopback, LAN, and metadata."""
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if str(ip) in BLOCKED_METADATA_IPS:
            return False
        if not ip.is_global:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        return True
    except (ValueError, AttributeError):
        return False


def validate_image_url(url: str) -> Dict[str, Any]:
    """Validate URL scheme, port, userinfo, and hostname."""
    if not url or not isinstance(url, str):
        raise DomainError("Некорректный или пустой URL изображения.", 400)
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError as error:
        raise DomainError("Некорректный URL изображения.", 400) from error
    if parsed.scheme.lower() != "https":
        raise DomainError("Разрешены только безопасные соединения HTTPS.", 400)
    if parsed.username or parsed.password:
        raise DomainError("URL содержит учетные данные (userinfo), что запрещено.", 400)
    if port is not None and port != 443:
        raise DomainError(f"Нестандартный порт {port} запрещен (допускается только HTTPS 443).", 400)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise DomainError("В URL отсутствует имя хоста.", 400)
    if hostname in ("localhost", "127.0.0.1", "::1"):
        raise DomainError("Запрещено обращение к локальным адресам (SSRF защита).", 400)
    return {"parsed": parsed, "hostname": hostname}


class PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection pinned to a pre-validated public IP address to prevent DNS rebinding."""
    def __init__(self, host: str, ip_to_connect: str, port: int = 443, timeout: float = SOCKET_TIMEOUT):
        super().__init__(host=host, port=port, timeout=timeout)
        self._ip_to_connect = ip_to_connect

    def connect(self):
        sock = socket.create_connection((self._ip_to_connect, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def resolve_safe_ip(hostname: str) -> str:
    """Resolve hostname and ensure all returned IP addresses are safe public IPs."""
    try:
        addr_info = socket.getaddrinfo(hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise DomainError(f"Не удалось разрешить имя хоста '{hostname}': {e}", 400)
    if not addr_info:
        raise DomainError(f"Не найдены IP-адреса для хоста '{hostname}'.", 400)

    safe_ip = None
    for family, socktype, proto, canonname, sockaddr in addr_info:
        candidate_ip = sockaddr[0]
        if not is_safe_public_ip(candidate_ip):
            raise DomainError(
                f"Хост '{hostname}' разрешается в недопустимый или приватный IP-адрес ({candidate_ip}). Запрос заблокирован.",
                400
            )
        if safe_ip is None:
            safe_ip = candidate_ip

    if not safe_ip:
        raise DomainError(f"Для хоста '{hostname}' не найден ни один допустимый публичный IP-адрес.", 400)
    return safe_ip


def fetch_safe_web_image(
    url: str,
    is_preview: bool = False,
    max_bytes: Optional[int] = None,
    timeout: float = SOCKET_TIMEOUT
) -> Dict[str, Any]:
    """
    Safely download and validate an image from an arbitrary HTTPS web URL.
    - Resolves and pins public IP address (SSRF & DNS rebinding protection).
    - Follows up to 3 redirects, validating each destination.
    - Enforces strict chunk-based size limits.
    - Decodes image via PIL to verify format (JPEG/PNG/WEBP), dimensions, and pixels.
    """
    limit_bytes = max_bytes or (MAX_PREVIEW_BYTES if is_preview else MAX_IMAGE_BYTES)
    current_url = url.strip()
    deadline=time.monotonic()+timeout

    for redirect_idx in range(MAX_REDIRECTS + 1):
        if time.monotonic()>=deadline:raise DomainError("Истекло время загрузки изображения.",408)
        url_info = validate_image_url(current_url)
        hostname = url_info["hostname"]
        safe_ip = resolve_safe_ip(hostname)

        path_with_query = url_info["parsed"].path or "/"
        if url_info["parsed"].query:
            path_with_query += f"?{url_info['parsed'].query}"

        conn = None
        try:
            conn = PinnedHTTPSConnection(hostname, safe_ip, port=443, timeout=timeout)
            headers = {
                "User-Agent": WIKIMEDIA_USER_AGENT if hostname.endswith(".wikimedia.org") else DEFAULT_USER_AGENT,
                # Openverse negotiates its API response before streaming or redirecting the thumbnail.
                "Accept": "*/*" if hostname == "api.openverse.org" else "image/jpeg,image/png,image/webp,image/*;q=0.8",
                "Connection": "close"
            }
            conn.request("GET", path_with_query, headers=headers)
            resp = conn.getresponse()

            # Handle redirects safely
            if resp.status in (301, 302, 303, 307, 308):
                if redirect_idx >= MAX_REDIRECTS:
                    raise DomainError("Превышено максимальное число перенаправлений (3).", 400)
                location = resp.getheader("Location")
                if not location:
                    raise DomainError("Сервер вернул перенаправление без заголовка Location.", 400)
                current_url = urljoin(current_url, location)
                continue

            # Check status code
            if resp.status != 200:
                if resp.status == 403:
                    raise DomainError(f"Оригинал изображения защищён от прямого скачивания (HTTP 403 Forbidden).", 403)
                elif resp.status == 404:
                    raise DomainError(f"Файл изображения не найден на сервере-источнике (HTTP 404 Not Found).", 404)
                elif resp.status == 429:
                    raise DomainError(f"Сервер источника временно ограничил запросы (HTTP 429 Too Many Requests).", 429)
                else:
                    raise DomainError(f"Ошибка загрузки изображения с источника (HTTP {resp.status}).", resp.status)

            # Check content-length header early if present
            cl_header = resp.getheader("Content-Length")
            if cl_header:
                try:
                    cl = int(cl_header)
                    if cl > limit_bytes:
                        raise DomainError(
                            f"Размер изображения ({cl // 1024} КБ) превышает допустимый лимит ({limit_bytes // 1024} КБ).",
                            400
                        )
                except ValueError:
                    pass

            # Stream response in chunks with hard counter
            raw_data = bytearray()
            while True:
                if time.monotonic()>=deadline:raise DomainError("Истекло время загрузки изображения.",408)
                chunk = resp.read(65536)
                if not chunk:
                    break
                raw_data.extend(chunk)
                if len(raw_data) > limit_bytes:
                    raise DomainError(
                        f"Размер загруженного файла превысил допустимый предел ({limit_bytes // 1024} КБ).",
                        400
                    )

            final_bytes = bytes(raw_data)
            if not final_bytes:
                raise DomainError("Сервер источника вернул пустой файл (0 байт).", 400)

            # Validate decoded image
            try:
                img = Image.open(io.BytesIO(final_bytes))
                fmt = (img.format or "").upper()
                if fmt not in ALLOWED_IMAGE_FORMATS:
                    raise DomainError(
                        f"Формат файла '{fmt or 'неизвестный'}' не поддерживается. Разрешены только растровые изображения JPEG, PNG, WEBP.",
                        400
                    )
                w, h = img.size
                total_pixels = w * h
                if total_pixels > MAX_IMAGE_PIXELS:
                    raise DomainError(
                        f"Разрешение изображения ({w}x{h} = {total_pixels} пикс.) превышает лимит безопасности ({MAX_IMAGE_PIXELS} пикс.).",
                        400
                    )
                # Verify image integrity
                img.verify()
                with Image.open(io.BytesIO(final_bytes)) as decoded:
                    decoded.load()
            except DomainError:
                raise
            except Exception as e:
                raise DomainError(f"Файл не является корректным изображением или повреждён: {e}", 400)

            content_type = f"image/{fmt.lower()}"
            if fmt == "JPEG":
                content_type = "image/jpeg"

            return {
                "ok": True,
                "bytes": final_bytes,
                "width": w,
                "height": h,
                "format": fmt.lower(),
                "content_type": content_type,
                "url": current_url,
                "size_bytes": len(final_bytes)
            }

        except DomainError:
            raise
        except socket.timeout:
            raise DomainError("Таймаут подключения при загрузке изображения с внешнего сервера.", 408)
        except Exception as e:
            raise DomainError(f"Сбой загрузки изображения: {e}", 400)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    raise DomainError("Не удалось загрузить изображение после перенаправлений.", 400)

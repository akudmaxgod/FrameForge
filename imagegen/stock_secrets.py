"""Secure storage and connection verification for stock provider API keys."""
import os
import re
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any

# Windows DPAPI implementation via ctypes
def _has_dpapi() -> bool:
    return os.name == 'nt'

def _encrypt_bytes(data: bytes) -> bytes:
    if not data:
        return b''
    if _has_dpapi():
        try:
            import ctypes
            import ctypes.wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

            in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
            out_blob = DATA_BLOB()
            if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob), 'FrameForgeSecret', None, None, None, 0, ctypes.byref(out_blob)):
                res = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                ctypes.windll.kernel32.LocalFree(out_blob.pbData)
                return b'DPAPI:' + res
            raise RuntimeError("CryptProtectData returned False")
        except Exception as e:
            raise RuntimeError(f"Сбой шифрования Windows DPAPI: {e}")
    raise RuntimeError("Шифрование ключей поддерживается только в Windows через DPAPI.")

def _decrypt_bytes(data: bytes) -> bytes:
    if not data:
        return b''
    if data.startswith(b'DPAPI:') and _has_dpapi():
        payload = data[6:]
        try:
            import ctypes
            import ctypes.wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

            in_blob = DATA_BLOB(len(payload), ctypes.cast(ctypes.create_string_buffer(payload), ctypes.POINTER(ctypes.c_char)))
            out_blob = DATA_BLOB()
            if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
                res = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                ctypes.windll.kernel32.LocalFree(out_blob.pbData)
                return res
        except Exception:
            return b''
    return b''

def _secrets_dir(root_dir: Path | str) -> Path:
    p = Path(root_dir) / 'secrets'
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_pixabay_key(root_dir: Path | str) -> Optional[str]:
    """Retrieve the stored Pixabay API key, or check environment variable PIXABAY_API_KEY."""
    key_file = _secrets_dir(root_dir) / 'pixabay.dat'
    if key_file.is_file():
        try:
            raw = key_file.read_bytes()
            val = _decrypt_bytes(raw).decode('utf-8').strip()
            if val:
                return val
        except Exception:
            pass
    env_key = os.environ.get('PIXABAY_API_KEY', '').strip()
    return env_key if env_key else None

def set_pixabay_key(root_dir: Path | str, key: str) -> None:
    """Securely store the Pixabay API key using Windows DPAPI."""
    key = (key or '').strip()
    if not key:
        delete_pixabay_key(root_dir)
        return
    if not re.match(r'^[A-Za-z0-9\-_]{10,120}$', key):
        raise ValueError('Недопустимый формат ключа Pixabay API (ожидаются цифры, буквы и дефис).')
    enc = _encrypt_bytes(key.encode('utf-8'))
    key_file = _secrets_dir(root_dir) / 'pixabay.dat'
    key_file.write_bytes(enc)

def delete_pixabay_key(root_dir: Path | str) -> bool:
    """Remove the stored Pixabay API key."""
    key_file = _secrets_dir(root_dir) / 'pixabay.dat'
    if key_file.is_file():
        try:
            key_file.unlink()
            return True
        except Exception:
            return False
    return False

def is_pixabay_key_configured(root_dir: Path | str) -> bool:
    """Check whether a Pixabay key is available without exposing its value."""
    return bool(get_pixabay_key(root_dir))

def sanitize_url(url: str) -> str:
    """Mask api key parameter from URL query strings for safe logging."""
    return re.sub(r'([?&]key=)[^&]+', r'\1***', str(url))

def sanitize_error(text: str, key: Optional[str] = None) -> str:
    """Ensure sensitive key values and query parameters never appear in exception messages."""
    text = sanitize_url(str(text))
    if key and len(key) >= 6:
        text = text.replace(key, '***')
    return text

ALLOWED_STOCK_DOMAINS = ('pixabay.com', 'vimeocdn.com')
ALLOWED_API_DOMAINS = ('pixabay.com',)

def is_allowed_stock_url(url: str, allowed_domains=ALLOWED_STOCK_DOMAINS) -> bool:
    """Validate scheme, host, port, and userinfo against allowed domain whitelist."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() != 'https':
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port is not None and parsed.port != 443:
            return False
        host = (parsed.hostname or '').lower().strip()
        if not host:
            return False
        return any(host == base or host.endswith('.' + base) for base in allowed_domains)
    except Exception:
        return False

class SafeStockRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces that redirects stay within allowed HTTPS domains without userinfo or custom ports."""
    def __init__(self, allowed_domains=ALLOWED_STOCK_DOMAINS):
        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        full_url = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_stock_url(full_url, self.allowed_domains):
            from .domain import DomainError
            raise DomainError(
                f'Запрещено перенаправление на сторонний или незащищённый адрес: {sanitize_url(full_url)}',
                400
            )
        return super().redirect_request(req, fp, code, msg, headers, full_url)

class SafeApiRedirectHandler(SafeStockRedirectHandler):
    """Prevents Pixabay API key leakage via redirects by restricting to pixabay.com."""
    def __init__(self):
        super().__init__(allowed_domains=ALLOWED_API_DOMAINS)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        full_url = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_stock_url(full_url, self.allowed_domains):
            from .domain import DomainError
            raise DomainError(
                f'Запрещено перенаправление запроса API на сторонний адрес: {sanitize_url(full_url)}',
                400
            )
        return super().redirect_request(req, fp, code, msg, headers, full_url)

def check_pixabay_connection(key: str, timeout: float = 10.0) -> Dict[str, Any]:
    """
    Perform a single lightweight probe request to verify key and connection.
    Never logs the key or complete query URL.
    Returns status, remaining rate limit, and human-readable message.
    """
    key = (key or '').strip()
    if not key:
        return {
            'ok': False,
            'status': 'not_configured',
            'error': 'Ключ Pixabay API не настроен.'
        }
    probe_url = f"https://pixabay.com/api/?key={key}&per_page=3&safesearch=true"
    req = urllib.request.Request(
        probe_url,
        headers={'User-Agent': 'FrameForge-Studio/2.0 (Windows)'}
    )
    try:
        opener = urllib.request.build_opener(SafeApiRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            status_code = resp.status
            headers = dict(resp.headers)
            limit = int(headers.get('X-RateLimit-Limit', 100))
            remaining = int(headers.get('X-RateLimit-Remaining', 99))
            reset_sec = int(headers.get('X-RateLimit-Reset', 60))
            data = json.loads(resp.read().decode('utf-8'))
            return {
                'ok': True,
                'status': 'connected',
                'limit': limit,
                'remaining': remaining,
                'reset_seconds': reset_sec,
                'total_hits': data.get('totalHits', 0),
                'message': f'Подключение успешно: лимит {limit} запр./мин (осталось {remaining}, сброс через {reset_sec} с).'
            }
    except urllib.error.HTTPError as e:
        safe_msg = sanitize_error(str(e.reason or e), key)
        headers = dict(e.headers) if hasattr(e, 'headers') else {}
        remaining = int(headers.get('X-RateLimit-Remaining', 0)) if 'X-RateLimit-Remaining' in headers else None
        reset_sec = int(headers.get('X-RateLimit-Reset', 60)) if 'X-RateLimit-Reset' in headers else None
        if e.code in (400, 401):
            return {
                'ok': False,
                'status': 'invalid_key',
                'status_code': e.code,
                'error': 'Неверный ключ Pixabay API. Проверьте правильность ключа в аккаунте pixabay.com.'
            }
        if e.code == 429:
            return {
                'ok': False,
                'status': 'rate_limited',
                'status_code': 429,
                'remaining': remaining or 0,
                'reset_seconds': reset_sec or 60,
                'error': f'Превышен лимит запросов Pixabay API (100 запр./мин). Сброс через {reset_sec or 60} с.'
            }
        return {
            'ok': False,
            'status': 'http_error',
            'status_code': e.code,
            'error': f'Ошибка сервера Pixabay (код {e.code}): {safe_msg}'
        }
    except urllib.error.URLError as e:
        safe_msg = sanitize_error(str(e.reason), key)
        return {
            'ok': False,
            'status': 'network_error',
            'error': f'Ошибка подключения к pixabay.com: {safe_msg}. Проверьте доступ к интернету.'
        }
    except Exception as e:
        safe_msg = sanitize_error(str(e), key)
        return {
            'ok': False,
            'status': 'error',
            'error': f'Не удалось проверить подключение: {safe_msg}'
        }

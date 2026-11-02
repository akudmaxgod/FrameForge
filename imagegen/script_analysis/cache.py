"""Atomic disk cache and OS-level per-script lock, shared across processes."""
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4
from .errors import AnalysisError
from .prompts import SYS_REGISTRY, SYS_SCENES

log = logging.getLogger(__name__)
VERSION = hashlib.sha256(("schema-v1:deepseek-flash:" + SYS_REGISTRY + SYS_SCENES).encode()).hexdigest()


def script_key(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DiskCache:
    def __init__(self, directory: Path | None = None):
        if directory is None:
            self.directory = Path(__file__).resolve().parents[2] / ".script-analysis" / "cache"
        else:
            self.directory = Path(directory)

    @contextmanager
    def lock(self, key: str, timeout: float = 600):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / f"{key}.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            started = time.monotonic()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() - started >= timeout:
                        raise AnalysisError("Этот сценарий уже разбирается; ожидание кэша истекло.") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, key: str) -> dict | None:
        path = self.directory / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != VERSION or data.get("script_hash") != key:
                log.warning("cache_incompatible script_hash=%s", key)
                return None
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("cache_unreadable script_hash=%s", key)
            return None

    def save(self, key: str, data: dict) -> None:
        atomic_json(self.directory / f"{key}.json", {**data, "version": VERSION, "script_hash": key})

"""Exactly two logical passes; identical first two messages in both passes."""
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import time
from pydantic import ValidationError
from .cache import DiskCache, script_key
from .client import JsonClient
from .errors import AnalysisError
from .invariants import normalize_registry, normalize_scenes
from .prompts import SYS_REGISTRY, SYS_SCENES
from .schemas import Breakdown, Registry, Scene
from .stats import Stats

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    registry: Registry
    scenes: list[Scene]
    warnings: list[str] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)

    def to_dict(self) -> dict:
        return {"registry": self.registry.model_dump(), "scenes": [s.model_dump() for s in self.scenes],
                "warnings": self.warnings, "stats": asdict(self.stats)}


def prefix_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(messages[:2], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class Analyzer:
    def __init__(self, client: JsonClient | None = None, cache: DiskCache | None = None, *, api_key: str | None = None):
        self.client = client
        self.cache = cache if cache is not None else DiskCache()
        self.api_key = api_key

    def analyze(self, script: str, *, use_cache: bool = True,
                on_progress: Callable[[str, int], None] | None = None) -> AnalysisResult:
        if not isinstance(script, str) or not script.strip() or len(script) > 70000:
            raise ValueError("Сценарий должен содержать от 1 до 70000 символов и не быть пустым.")
        started = time.perf_counter()
        key = script_key(script)
        stats = Stats()
        try:
            with self.cache.lock(key) if use_cache else nullcontext():
                warnings: list[str] = []
                data = self.cache.load(key) if use_cache else None
                if data is not None:
                    try:
                        registry = Registry.model_validate(data["registry"])
                        scenes = Breakdown.model_validate({"scenes": data["scenes"]}).scenes
                        cached_warnings = data["warnings"]
                        if not isinstance(cached_warnings, list) or any(not isinstance(w, str) for w in cached_warnings):
                            raise ValueError("invalid warnings")
                        registry, remap, fixes = normalize_registry(registry)
                        scenes, scene_fixes = normalize_scenes(scenes, registry, remap)
                        # Empty location is the documented unresolved-reference sentinel.
                        extra_fixes = [w for w in scene_fixes if "неизвестная локация ''" not in w]
                        if fixes or extra_fixes:
                            raise ValueError("cache invariants")
                        stats.result_cache_hit = True
                        if on_progress:
                            on_progress("Разбор загружен из локального кэша (SHA-256)", 95)
                        return self._finish(AnalysisResult(registry, scenes, list(cached_warnings), stats), key, started)
                    except (KeyError, ValueError, TypeError, ValidationError):
                        warnings.append("Повреждённый результат кэша отклонён; выполнен новый разбор.")
                        log.warning("cache_invalid script_hash=%s", key)
                client = self.client or JsonClient(api_key=self.api_key)
                try:
                    if on_progress:
                        on_progress("DeepSeek Flash: извлечение глобального реестра (проход 1/2)", 25)
                    base = [{"role": "system", "content": SYS_REGISTRY},
                            {"role": "user", "content": "Сценарий:\n\n" + script}]
                    log.info("pass_start phase=A script_hash=%s prefix_hash=%s", key, prefix_hash(base))
                    registry = client.ask_json(base, Registry, stats, phase="A", script_hash=key)
                    registry, remap, fixes = normalize_registry(registry)
                    warnings.extend(fixes)
                    if on_progress:
                        on_progress("DeepSeek Flash: разметка сцен сценария (проход 2/2)", 60)
                    continuation = base + [{"role": "assistant", "content": registry.model_dump_json()},
                                           {"role": "user", "content": SYS_SCENES + "\n\nИспользуй ТОЛЬКО id из реестра выше."}]
                    if prefix_hash(base) != prefix_hash(continuation):
                        raise AnalysisError("Внутренняя ошибка: изменён префикс второго прохода.")
                    log.info("pass_start phase=B script_hash=%s prefix_hash=%s", key, prefix_hash(continuation))
                    breakdown = client.ask_json(continuation, Breakdown, stats, phase="B", script_hash=key)
                    if on_progress:
                        on_progress("DeepSeek Flash: проверка инвариантов и целостности связей", 85)
                    scenes, fixes = normalize_scenes(breakdown.scenes, registry, remap)
                    warnings.extend(fixes)
                    result = AnalysisResult(registry, scenes, warnings, stats)
                    if use_cache:
                        try:
                            self.cache.save(key, result.to_dict())
                        except OSError:
                            warnings.append("Не удалось сохранить кэш; повторный запуск может стоить денег.")
                            log.warning("cache_write_failed script_hash=%s", key)
                    return self._finish(result, key, started)
                finally:
                    if self.client is None:
                        client.close()
        except AnalysisError as exc:
            stats.duration_ms = int((time.perf_counter() - started) * 1000)
            exc.stats = stats
            log.error("analysis_failed script_hash=%s stats=%s", key, json.dumps(asdict(stats)))
            raise

    @staticmethod
    def _finish(result: AnalysisResult, key: str, started: float) -> AnalysisResult:
        result.stats.duration_ms = int((time.perf_counter() - started) * 1000)
        for warning in result.warnings:
            log.warning("invariant script_hash=%s warning=%s", key, warning)
        log.info("analysis_complete script_hash=%s scenes_count=%s stats=%s", key, len(result.scenes), json.dumps(asdict(result.stats)))
        return result


def analyze(script: str, *, use_cache: bool = True, api_key: str | None = None,
            on_progress: Callable[[str, int], None] | None = None) -> AnalysisResult:
    return Analyzer(api_key=api_key).analyze(script, use_cache=use_cache, on_progress=on_progress)

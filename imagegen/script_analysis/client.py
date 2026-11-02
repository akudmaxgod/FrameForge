"""OpenAI-compatible transport with bounded, explicitly accounted retries."""
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import TypeVar, cast
from uuid import uuid4

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError
from .errors import BalanceError, ConfigurationError, ProviderError, ResponseError
from .stats import Stats

MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
Messages = list[dict[str, str]]


def get_api_key(env_file: Path | None = None) -> str:
    candidates = []
    if env_file:
        candidates.append(Path(env_file))
    candidates.append(Path.cwd() / ".env")
    app_root = Path(__file__).resolve().parents[2]
    candidates.append(app_root / ".env")
    for cand in candidates:
        if cand.is_file():
            load_dotenv(cand, override=False, encoding="utf-8-sig")
            if os.environ.get("DEEPSEEK_API_KEY", "").strip():
                break
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise ConfigurationError("Добавьте DEEPSEEK_API_KEY в окружение или .env.")
    return key


def with_retries(call: Callable[[], R], sleep: Callable[[float], None] = time.sleep) -> R:
    for attempt in range(4):
        try:
            return call()
        except APIStatusError as exc:
            status = exc.status_code
            if status == 402:
                raise BalanceError("Недостаточно средств DeepSeek: пополни баланс.", 402) from None
            if status == 401:
                raise ProviderError("DeepSeek отклонил API-ключ (401).", 401) from None
            if (status == 429 or 500 <= status < 600) and attempt < 3:
                delay = 2 ** attempt
                log.warning("http_retry status=%s delay_sec=%s", status, delay)
                sleep(delay)
                continue
            raise ProviderError(f"DeepSeek: ошибка HTTP {status}.", status) from None
        except APIConnectionError:
            raise ProviderError("Нет ответа DeepSeek: соединение или тайм-аут. Автоповтор не выполнялся.") from None
    raise AssertionError("unreachable")


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class JsonClient:
    def __init__(self, sdk: OpenAI | None = None, *, api_key: str | None = None, failed_dir: Path = Path(".script-analysis/failures"),
                 env_file: Path | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        resolved_key = api_key or (sdk.api_key if sdk else None) or get_api_key(env_file)
        self.sdk = sdk or OpenAI(api_key=resolved_key, base_url=BASE_URL,
                                 max_retries=0, timeout=120.0)
        # Also disable hidden SDK retries on injected clients.
        self.sdk.max_retries = 0
        self.failed_dir, self.sleep, self.now = failed_dir, sleep, now

    def close(self) -> None:
        self.sdk.close()

    def _failure(self, raw: str, phase: str, script_hash: str, reason: str) -> ResponseError:
        path = self.failed_dir / f"{script_hash}.{phase}.{uuid4().hex}.failed.json"
        # Provider output can echo data; never persist an API key even if echoed.
        key = self.sdk.api_key
        safe_raw = raw.replace(key, "[REDACTED]") if key else raw
        try:
            self.failed_dir.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                json.dump({"phase": phase, "reason": reason, "raw": safe_raw}, handle, ensure_ascii=False, indent=2)
        except OSError:
            return ResponseError(f"{reason} Не удалось сохранить сырой ответ в каталог ошибок.")
        return ResponseError(f"{reason} Сырой ответ: {path}")

    def ask_json(self, messages: Messages, schema: type[T], stats: Stats, *, phase: str, script_hash: str) -> T:
        history = [dict(message) for message in messages]
        json_failures = 0
        schema_retried = False
        while True:
            when = self.now()
            def request():
                nonlocal when
                when = self.now()
                stats.requests += 1
                return self.sdk.chat.completions.create(
                    model=MODEL, messages=cast(list[ChatCompletionMessageParam], history),
                    temperature=0.2, response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}}, max_tokens=10000,
                    stream=False,
                )
            response = with_retries(request, self.sleep)
            raw = response.choices[0].message.content or "" if response.choices else ""
            try:
                stats.record(response.usage, phase, when)
            except ResponseError:
                raise self._failure(raw, phase, script_hash, "Отсутствует корректный usage; стоимость неизвестна.") from None
            if (not response.choices or response.choices[0].finish_reason != "stop"
                    or response.usage.completion_tokens > 10000):
                raise self._failure(raw, phase, script_hash, "Ответ оборван, заблокирован или превысил лимит 10000 токенов.")
            try:
                data = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError):
                json_failures += 1
                if json_failures >= 3:
                    raise self._failure(raw, phase, script_hash, "Невалидный JSON после трёх попыток.") from None
                correction = "Это невалидный JSON. Верни ТОЛЬКО валидный JSON по схеме."
                log.warning("json_retry phase=%s failure=%s", phase, json_failures)
            else:
                try:
                    return schema.model_validate(data)
                except ValidationError as exc:
                    if schema_retried:
                        raise self._failure(raw, phase, script_hash, "Ответ повторно не соответствует схеме.") from None
                    schema_retried = True
                    # Exclude input/context: they can contain private screenplay text.
                    details = exc.errors(include_input=False, include_context=False, include_url=False)
                    correction = "JSON не соответствует схеме. Исправь ошибки и верни только JSON: " + json.dumps(details, ensure_ascii=False)
                    log.warning("schema_retry phase=%s error_count=%s", phase, len(details))
            history.extend([{"role": "assistant", "content": raw}, {"role": "user", "content": correction}])

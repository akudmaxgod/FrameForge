"""OpenAI-compatible transport with bounded, explicitly accounted retries."""
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
import threading
import hashlib
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
MAX_PAID_REQUESTS = 2
MAX_INPUT_CHARS = 200_000

def request_context(messages,schema,phase,script_hash):
    return hashlib.sha256(json.dumps({'messages':messages,'schema':schema.model_json_schema(),'phase':phase,'script_hash':script_hash},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


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
    # Kept as a compatibility entry point; every invocation sends at most once.
    try:
        return call()
    except APIStatusError as exc:
        status = exc.status_code
        if status == 402:raise BalanceError("Недостаточно средств DeepSeek: пополни баланс.",402) from None
        if status == 401:raise ProviderError("DeepSeek отклонил API-ключ (401).",401) from None
        raise ProviderError(f"DeepSeek: ошибка HTTP {status}. Автоповтор отключён.",status) from None
    except APIConnectionError:
        raise ProviderError("Нет ответа DeepSeek: соединение или тайм-аут. Автоповтор не выполнялся.") from None


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
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), authorization=None):
        resolved_key = api_key or (sdk.api_key if sdk else None) or get_api_key(env_file)
        self.sdk = sdk or OpenAI(api_key=resolved_key, base_url=BASE_URL,
                                 max_retries=0, timeout=120.0)
        # Also disable hidden SDK retries on injected clients.
        self.sdk.max_retries = 0
        self.failed_dir, self.sleep, self.now = failed_dir, sleep, now
        self._budget_lock=threading.Lock()
        self._requests_used=0
        self._used_phases=set()
        self._authorization=authorization

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
        if phase not in ('A','B','C'):raise ResponseError('Неизвестная операция DeepSeek. Запрос заблокирован.')
        if not messages or any(set(m)!={'role','content'} or m['role'] not in ('system','user','assistant') or not isinstance(m['content'],str) for m in messages):
            raise ResponseError('Запрос нарушает разрешённый формат сообщений. DeepSeek не вызывался.')
        if sum(len(m['content']) for m in messages)>MAX_INPUT_CHARS:
            raise ResponseError('Превышен лимит входного текста. DeepSeek не вызывался.')
        with self._budget_lock:
            if phase=='C' or self._requests_used>=MAX_PAID_REQUESTS or stats.requests>=MAX_PAID_REQUESTS or phase in self._used_phases:
                from ..paid_pass_approval import PaidPassPermit
                if not isinstance(self._authorization,PaidPassPermit) or not self._authorization.consume(request_context(messages,schema,phase,script_hash)):
                    raise ResponseError('Дополнительный платный запрос DeepSeek требует отдельного подтверждения. Без ответа выбирается локальная модель.')
            self._requests_used+=1;self._used_phases.add(phase)
        when = self.now()
        stats.requests += 1
        response = with_retries(lambda: self.sdk.chat.completions.create(
            model=MODEL, messages=cast(list[ChatCompletionMessageParam], messages),
            temperature=0.2, response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}}, max_tokens=10000,
            stream=False,
        ), self.sleep)
        raw = (response.choices[0].message.content or "") if response.choices else ""
        try:
            stats.record(response.usage, phase, when)
        except ResponseError:
            raise self._failure(raw, phase, script_hash, "Отсутствует корректный usage. Стоимость неизвестна.") from None
        if (not response.choices or response.choices[0].finish_reason != "stop"
                or response.usage.completion_tokens > 10000):
            raise self._failure(raw, phase, script_hash, "Ответ оборван, заблокирован или превысил лимит 10000 токенов.")
        try:
            data = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise self._failure(raw,phase,script_hash,'Невалидный JSON. Автоповтор отключён.') from None
        try:
            return schema.model_validate(data)
        except ValidationError:
            raise self._failure(raw,phase,script_hash,'Ответ не соответствует разрешённой схеме. Автоповтор отключён.') from None

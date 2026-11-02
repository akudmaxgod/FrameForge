"""Shared Gemini key failover without credentials in URLs or errors."""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, time as day_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCK = threading.Lock()
_COOLDOWNS = {}
_PREFERRED = {}
_LAST_REQUEST = {}


def google_keys(primary='', extra=''):
    keys = []
    for key in re.split(r'[\s,;]+', f'{primary or ""}\n{extra or ""}'):
        if key and key not in keys:
            keys.append(key)
    return keys


class GeminiPoolError(Exception):
    def __init__(self, message, status=None, *, state=None, job_id=None, next_at=None):
        super().__init__(message)
        self.status = status
        self.state, self.job_id, self.next_at = state, job_id, next_at


class GeminiKeyPool:
    def __init__(self, primary='', extra='', *, dispatcher=None, project_id='application', category='api', context=''):
        self.keys = google_keys(primary, extra)
        self.identity = hashlib.sha256('\n'.join(self.keys).encode()).hexdigest()
        self.dispatcher, self.project_id, self.category, self.context = dispatcher, project_id, category, context

    def json(self, url, payload=None, *, model='', timeout=25, pace=False):
        if not self.keys:
            raise GeminiPoolError('Пул Gemini не содержит API-ключей.')
        if self.dispatcher is not None:
            from .google_queue import QueuePending
            job_id = self.dispatcher.submit(url, payload, project=self.project_id, model=model,
                category=self.category, context=self.context)
            try:
                return self.dispatcher.result(job_id)
            except QueuePending as pending:
                if pending.state not in ('queued','waiting_quota','waiting_registry','waiting_model','retry_wait'):
                    raise GeminiPoolError(str(pending),503,state=pending.state,job_id=job_id,next_at=pending.next_at) from None
            self.dispatcher.run_once(timeout=timeout)
            try:
                return self.dispatcher.result(job_id)
            except QueuePending as pending:
                raise GeminiPoolError(str(pending),429 if pending.state == 'waiting_quota' else 503,
                    state=pending.state,job_id=job_id,next_at=pending.next_at) from None
        with _LOCK:
            preferred = _PREFERRED.get((self.identity, model), 0)
        order = list(range(preferred, len(self.keys))) + list(range(preferred))
        quota_errors = False
        for index in order:
            key = self.keys[index]
            key_id = hashlib.sha256(key.encode()).hexdigest()
            with _LOCK:
                if _COOLDOWNS.get((key_id, model), 0) > time.monotonic():
                    continue
                if pace:
                    # Conservative across the whole pool: keys may share a
                    # project, so adding keys must not multiply request rate.
                    delay = max(0, 5 - (time.monotonic() - _LAST_REQUEST.get(model, 0)))
                    if delay:
                        time.sleep(delay)
                    _LAST_REQUEST[model] = time.monotonic()
            req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    result = json.loads(response.read().decode('utf-8'))
            except urllib.error.HTTPError as error:
                status = error.code
                detail = error.read(8000).decode('utf-8', errors='replace')
                bad_key = status == 400 and any(token in detail.upper() for token in ('API_KEY_INVALID', 'API KEY NOT VALID', 'API_KEY_EXPIRED'))
                if status in (401, 403, 429) or bad_key:
                    quota_errors |= status == 429
                    retry = error.headers.get('Retry-After') if error.headers else None
                    try:
                        cooldown = max(5, min(86400, float(retry))) if status == 429 else 3600
                    except (TypeError, ValueError):
                        cooldown = 60 if status == 429 else 3600
                    if status == 429 and any(token in detail.casefold() for token in ('perday', 'per_day', 'requests per day')):
                        try:
                            local_now = datetime.now(ZoneInfo('America/Los_Angeles'))
                            reset = datetime.combine(local_now.date() + timedelta(days=1), day_time(), local_now.tzinfo)
                            cooldown = max(5, reset.timestamp() - local_now.timestamp())
                        except ZoneInfoNotFoundError:
                            # Windows may lack the IANA timezone database.
                            # Recheck in an hour instead of blocking a full
                            # day past the project's actual quota reset.
                            cooldown = 3600
                    with _LOCK:
                        _COOLDOWNS[(key_id, model)] = time.monotonic() + cooldown
                    continue
                # An explicit request/content rejection is not a key problem.
                # Do not repeat it on every credential or leak Google's body.
                raise GeminiPoolError(f'Gemini API отклонил запрос (HTTP {status}).', status) from None
            except (OSError, TimeoutError, ValueError):
                # A transport failure may follow a submitted generation; do
                # not blindly duplicate it on another key.
                raise GeminiPoolError('Нет подтверждённого ответа Gemini API. Запрос автоматически не повторён.') from None
            with _LOCK:
                _PREFERRED[(self.identity, model)] = index
            return result
        label = 'Лимит или доступ к Gemini исчерпан для всех ключей' if quota_errors else 'Нет доступных ключей Gemini: проверьте доступ или дождитесь восстановления квоты'
        raise GeminiPoolError(label + '. Сохранённые результаты не потеряны.', 429 if quota_errors else 503)

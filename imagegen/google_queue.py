"""Opt-in durable, free-only Gemini dispatcher. No application DB migration.

Credentials stay in memory; the ledger stores only fingerprints. Each dispatch
reserves every applicable quota bucket in one SQLite transaction. A transport
ambiguity (including a dead worker) never causes an automatic duplicate call.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def pacific_day(timestamp):
    """Pacific midnight in UTC; explicit US DST fallback for Windows."""
    utc = datetime.fromtimestamp(timestamp, timezone.utc)
    try:
        tz = ZoneInfo('America/Los_Angeles')
    except ZoneInfoNotFoundError:
        # US rules since 2007: second Sunday in March, first Sunday in November.
        march = datetime(utc.year, 3, 8, 10, tzinfo=timezone.utc)
        march += timedelta(days=(6 - march.weekday()) % 7)
        november = datetime(utc.year, 11, 1, 9, tzinfo=timezone.utc)
        november += timedelta(days=(6 - november.weekday()) % 7)
        offset = -7 if march <= utc < november else -8
        local = utc + timedelta(hours=offset)
        start = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) - timedelta(hours=offset)
        # Midnight on transition day still uses the pre-transition offset.
        if start.date() == march.date():
            start = start.replace(hour=8)
        elif start.date() == november.date():
            start = start.replace(hour=7)
        tomorrow = start + timedelta(days=1)
        if start <= march < tomorrow:
            tomorrow -= timedelta(hours=1)
        elif start <= november < tomorrow:
            tomorrow += timedelta(hours=1)
        return start.timestamp(), tomorrow.timestamp()
    local = utc.astimezone(tz)
    start = datetime.combine(local.date(), datetime.min.time(), tz)
    end = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tz)
    return start.timestamp(), end.timestamp()


class QueuePending(Exception):
    def __init__(self, state, job_id, next_at=None):
        self.state, self.job_id, self.next_at = state, job_id, next_at
        super().__init__('Проверка сохранена: ' + state)


class GoogleQueue:
    def __init__(self, root, registry, keys=(), *, clock=time.time):
        self.path = Path(root) / 'google_queue.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.keys = {key_hash(k): k for k in keys if k}
        self.clock = clock
        self.fraction = registry.get('budget_fraction', 0.8)
        if type(self.fraction) not in (int, float) or not 0 < self.fraction <= 1:
            raise ValueError('Invalid quota budget fraction')
        seen = set()
        for group in registry.get('groups', []):
            if not isinstance(group.get('id'), str) or not group['id'] or group['id'] in seen:
                raise ValueError('Quota group IDs must be unique')
            seen.add(group['id'])
            for model, limits in group.get('models', {}).items():
                self._validate_limits(limits)
            for bucket, limits in group.get('shared_limits', {}).items():
                self._validate_limits(limits)
            for limits in group.get('models', {}).values():
                shared = limits.get('shared_buckets', [])
                if (not isinstance(shared, list) or
                    any(not isinstance(name, str) or not name for name in shared) or
                    len(shared) != len(set(shared)) or
                    any(name not in group.get('shared_limits', {}) for name in shared)):
                    raise ValueError('Shared quota buckets must be unique and defined')
        # A credential cannot imply two independent project budgets.
        projects = [g['cloud_project'] for g in registry.get('groups', []) if g.get('cloud_project')]
        if len(projects) != len(set(projects)):
            raise ValueError('A Cloud project must have exactly one quota group')
        hashes = [h for g in registry.get('groups', []) for h in g.get('key_hashes', [])]
        if len(hashes) != len(set(hashes)):
            raise ValueError('Credential appears in multiple quota groups')
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                  project TEXT NOT NULL, model TEXT NOT NULL, category TEXT NOT NULL,
                  url TEXT NOT NULL, payload TEXT, estimated INTEGER NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0,
                  created REAL NOT NULL, updated REAL NOT NULL, lease_until REAL,
                  group_id TEXT, credential_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                  response TEXT, paused INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS usage(
                  job TEXT NOT NULL, attempt INTEGER NOT NULL, group_id TEXT NOT NULL,
                  bucket TEXT NOT NULL, at REAL NOT NULL, tokens INTEGER NOT NULL,
                  actual INTEGER, PRIMARY KEY(job,attempt,group_id,bucket));
                CREATE INDEX IF NOT EXISTS usage_window ON usage(group_id,bucket,at);
                CREATE TABLE IF NOT EXISTS cooldowns(
                  group_id TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials(
                  hash TEXT PRIMARY KEY, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turns(project TEXT PRIMARY KEY, last_dispatch REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS project_pauses(project TEXT PRIMARY KEY, paused INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS recovery_actions(
                  id TEXT PRIMARY KEY, job TEXT NOT NULL, project TEXT NOT NULL,
                  previous_state TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL, at REAL NOT NULL);
            ''')

    @staticmethod
    def _validate_limits(limits):
        if not isinstance(limits, dict) or any(type(limits.get(k)) is not int or limits[k] < 0 for k in ('rpm', 'tpm', 'rpd')):
            raise ValueError('Every quota bucket requires non-negative integer RPM/TPM/RPD')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=10000')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _validate_url(url, model, payload):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'generativelanguage.googleapis.com' or parsed.query or parsed.fragment:
            raise ValueError('Only credential-free Gemini API URLs are accepted')
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9._-]+',model):
            raise ValueError('Invalid Gemini model identifier')
        if parsed.path == '/v1beta/models' and payload is None:
            return
        if parsed.path == '/v1beta/interactions':
            if not isinstance(payload,dict) or payload.get('model') != model:
                raise ValueError('Payload model differs from quota model')
            return
        match = re.fullmatch(r'/v1beta/models/([A-Za-z0-9._-]+):(generateContent|countTokens)',parsed.path)
        if not match or match[1] != model:
            raise ValueError('Request model differs from quota model or API path is unsupported')

    def submit(self, url, payload, *, project, model, category, context='', estimated=None):
        from .review_policy import category_enabled
        if not category_enabled(category):
            raise ValueError('Automatic visual review was cancelled by the user')
        self._validate_url(url,model,payload)
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')) if payload is not None else None
        if estimated is None:
            from .google_token_budget import input_reservation
            estimated = input_reservation(payload, model)
        if type(estimated) is not int or estimated < 1:
            raise ValueError('A positive input-token reservation is required')
        identity = fingerprint({'url': url, 'payload': payload, 'model': model,
                                'project': project, 'category': category, 'context': context})
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE identity=?', (identity,)).fetchone()
            if row:
                return row['id']
            job_id = str(uuid4())
            pause = db.execute('SELECT paused FROM project_pauses WHERE project=?',(project,)).fetchone()
            db.execute('''INSERT INTO jobs(id,identity,project,model,category,url,payload,estimated,
                          state,created,updated,paused) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (job_id, identity, project, model, category, url, serialized, estimated,
                        'queued', now, now,pause['paused'] if pause else 0))
            return job_id

    def inspect(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            recovery_revision = db.execute("SELECT count(*) FROM recovery_actions WHERE job=? AND action='retry_check'", (job_id,)).fetchone()[0]
        if not row:
            raise KeyError(job_id)
        return {k: row[k] for k in ('id','project','model','category','state','reason','next_at','attempts','paused')} | {'recovery_revision': recovery_revision, **self.retry_status(row)}

    @staticmethod
    def retry_status(row):
        """One explicit extra plan check after a confirmed transient HTTP failure.

        Automatic retries still stop at three. The fourth dispatch retains the
        same job and quota ledger and never automatically retries again.
        Ambiguous submissions, invalid responses and cancelled visual reviews
        cannot use this service-recovery allowance.
        """
        from .review_policy import category_enabled
        limit = 4 if (row['category'] == 'source_plan' and row['state'] == 'error_request'
                      and row['reason'] in ('http_500','http_502','http_503','http_504')) else 3
        reason = None
        if row['category'] not in ('source_image','source_prompt','source_reference','source_plan'):
            reason = 'Повтор доступен только для смысловых проверок.'
        elif not category_enabled(row['category']):
            reason = 'Автоматическая визуальная проверка отменена.'
        elif row['state'] == 'transport_unknown':
            reason = 'Исход отправки неизвестен. Повтор заблокирован, чтобы не отправить запрос дважды.'
        elif row['state'] not in ('error_access','error_request','error_response','budget_too_small'):
            reason = 'Эта проверка не требует ручного восстановления.'
        elif row['attempts'] >= limit:
            reason = 'Достигнут предел отправок этой проверки. Требуется устранить причину сбоя.'
        return {'can_retry': reason is None, 'retry_limit': limit, 'retry_blocked_reason': reason}

    def result(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT state,response,next_at FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row and row['state'] == 'succeeded':
            return json.loads(row['response'])
        raise QueuePending(row['state'] if row else 'missing', job_id, row['next_at'] if row else None)

    def recover(self, job_id, project, expected_state, action, note):
        """Explicit bounded check retry. Unknown transport can only be abandoned.

        Reservations and cooldowns survive recovery. This never schedules media
        and cannot replay a request with an unconfirmed execution outcome.
        """
        if action not in ('retry_check','abandon_check') or not isinstance(note,str) or not 5 <= len(note.strip()) <= 2000:
            raise ValueError('Укажите действие и причину решения (5–2000 символов).')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=? AND project=?',(job_id,project)).fetchone()
            if not row: raise KeyError(job_id)
            if row['state'] != expected_state: raise ValueError('Состояние проверки изменилось. Обновите список.')
            if row['category'] not in ('source_image','source_prompt','source_reference','source_plan'):
                raise ValueError('Восстановление доступно только для смысловых проверок.')
            terminal = ('error_access','error_request','error_response','budget_too_small','transport_unknown')
            if row['state'] not in terminal: raise ValueError('Эта проверка не требует ручного восстановления.')
            if action == 'retry_check':
                retry = self.retry_status(row)
                if not retry['can_retry']: raise ValueError(retry['retry_blocked_reason'])
                if row['state'] == 'budget_too_small':
                    self._refresh_unsent_estimate(db, row)
                state = 'queued'
            else: state = 'cancelled'
            stamp = self.clock()
            db.execute('UPDATE jobs SET state=?,reason=?,next_at=0,lease_until=NULL,response=NULL,updated=? WHERE id=?',
                       (state,'explicit_check_retry' if action=='retry_check' else 'abandoned_by_user',stamp,job_id))
            db.execute('INSERT INTO recovery_actions VALUES(?,?,?,?,?,?,?)',
                       (str(uuid4()),job_id,project,expected_state,action,note.strip(),stamp))
        return self.inspect(job_id)

    @staticmethod
    def _refresh_unsent_estimate(db, row):
        """Upgrade the old default byte estimate only before any submission.

        Explicit differing bounds and all existing reservations are retained.
        A formerly blocked request still requires an explicit recovery action.
        """
        job = dict(row)
        if job['attempts'] != 0:
            return job
        from .google_token_budget import encoded_size, input_reservation
        try:
            payload = json.loads(job['payload']) if job['payload'] is not None else None
        except (ValueError, TypeError):
            return job
        if job['estimated'] == max(1, encoded_size(payload)):
            estimate = input_reservation(payload, job['model'])
            if estimate != job['estimated']:
                db.execute('UPDATE jobs SET estimated=? WHERE id=?', (estimate, job['id']))
                job['estimated'] = estimate
        return job

    def request_payload(self,job_id):
        with self._db() as db:
            row=db.execute('SELECT payload FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return json.loads(row['payload']) if row['payload'] is not None else None

    def project_paused(self,project):
        with self._db() as db:
            row=db.execute('SELECT paused FROM project_pauses WHERE project=?',(project,)).fetchone()
        return bool(row and row['paused'])

    def pause(self, project, paused=True):
        with self._db() as db:
            db.execute('INSERT INTO project_pauses VALUES(?,?) ON CONFLICT(project) DO UPDATE SET paused=excluded.paused',(project,int(paused)))
            db.execute('UPDATE jobs SET paused=? WHERE project=? AND state != ?', (int(paused), project, 'succeeded'))

    def _buckets(self, group, model):
        limits = group.get('models', {}).get(model)
        if not limits:
            return []
        buckets = [('model:' + model, limits)]
        shared = group.get('shared_limits', {})
        for name in limits.get('shared_buckets', []):
            if name not in shared:
                raise ValueError('Missing shared quota bucket')
            buckets.append(('shared:' + name, shared[name]))
        return buckets

    def _available(self, db, group, job, now):
        if group.get('verified') is not True or group.get('tier') != 'free' or not group.get('cloud_project'):
            return None, 'waiting_registry', 0
        buckets = self._buckets(group, job['model'])
        if not buckets:
            return None, 'waiting_model', 0
        cooldown = db.execute('SELECT until FROM cooldowns WHERE group_id=?', (group['id'],)).fetchone()
        if cooldown and cooldown['until'] > now:
            return None, 'waiting_quota', cooldown['until']
        available_keys = []
        for h in group.get('key_hashes', []):
            row = db.execute('SELECT until FROM credentials WHERE hash=?', (h,)).fetchone()
            if h in self.keys and (not row or row['until'] <= now):
                available_keys.append(h)
        if not available_keys:
            return None, 'error_access', 0
        day_start, reset = pacific_day(now)
        wait_until = 0
        for bucket, limits in buckets:
            rpm, tpm, rpd = [math.floor(limits[k] * self.fraction) for k in ('rpm','tpm','rpd')]
            if job['estimated'] > tpm or rpm < 1 or rpd < 1:
                return None, 'budget_too_small', 0
            minute = db.execute('''SELECT at,tokens FROM usage WHERE group_id=? AND bucket=? AND at>? ORDER BY at''',
                                (group['id'], bucket, now - 60)).fetchall()
            daily = db.execute('SELECT count(*) FROM usage WHERE group_id=? AND bucket=? AND at>=? AND at<?',
                               (group['id'], bucket, day_start, reset)).fetchone()[0]
            if daily >= rpd:
                wait_until = max(wait_until, reset)
            remaining_count, remaining_tokens = len(minute), sum(r['tokens'] for r in minute)
            for row in minute:
                if remaining_count < rpm and remaining_tokens + job['estimated'] <= tpm:
                    break
                wait_until = max(wait_until, row['at'] + 60)
                remaining_count -= 1
                remaining_tokens -= row['tokens']
        if wait_until > now:
            return None, 'waiting_quota', wait_until
        return available_keys[0], '', 0

    def claim(self, lease_seconds=240):
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            # An expired claim may already have reached Google. Keep its charge
            # and demand explicit reconciliation instead of automatic resubmit.
            db.execute("UPDATE jobs SET state='transport_unknown',reason='worker_lost',updated=? WHERE state='running' AND lease_until<=?", (now, now))
            jobs = db.execute('''SELECT j.* FROM jobs j LEFT JOIN turns t ON t.project=j.project
                WHERE j.state IN ('queued','waiting_quota','waiting_registry','waiting_model','retry_wait')
                  AND j.paused=0 AND j.next_at<=?
                ORDER BY coalesce(t.last_dispatch,0),j.created,j.rowid''', (now,)).fetchall()
            for job in jobs:
                from .review_policy import category_enabled
                if not category_enabled(job['category']):
                    db.execute("UPDATE jobs SET state='cancelled',reason='quality_block_cancelled',updated=? WHERE id=?", (now, job['id']))
                    continue
                try:
                    self._validate_url(job['url'],job['model'],json.loads(job['payload']) if job['payload'] is not None else None)
                except (ValueError,TypeError):
                    db.execute("UPDATE jobs SET state='error_request',reason='invalid_saved_request',updated=? WHERE id=?",(now,job['id']))
                    continue
                job = self._refresh_unsent_estimate(db, job)
                last_state, next_at = 'waiting_registry', 0
                for group in self.registry.get('groups', []):
                    credential, state, when = self._available(db, group, job, now)
                    if not credential:
                        if state != 'waiting_registry' or last_state == 'waiting_registry':
                            last_state = state
                        if when:
                            next_at = min(next_at or when, when)
                        continue
                    attempt = job['attempts'] + 1
                    for bucket, limits in self._buckets(group, job['model']):
                        db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,NULL)',
                                   (job['id'], attempt, group['id'], bucket, now, job['estimated']))
                    db.execute("UPDATE jobs SET state='running',reason='',group_id=?,credential_hash=?,attempts=?,lease_until=?,updated=? WHERE id=?",
                               (group['id'],credential,attempt,now+lease_seconds,now,job['id']))
                    turn = db.execute('SELECT coalesce(max(last_dispatch),0)+1 FROM turns').fetchone()[0]
                    db.execute('INSERT INTO turns VALUES(?,?) ON CONFLICT(project) DO UPDATE SET last_dispatch=excluded.last_dispatch',
                               (job['project'], turn))
                    return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
                db.execute('UPDATE jobs SET state=?,reason=?,next_at=?,updated=? WHERE id=?',
                           (last_state, last_state, next_at, now, job['id']))
        return None

    def _finish(self, job, state, *, response=None, until=0, reason='', actual=None, cooldown=False, credential=False):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE jobs SET state=?,reason=?,next_at=?,updated=?,lease_until=NULL,response=?
                          WHERE id=? AND state='running' AND attempts=?''',
                       (state,reason,until,self.clock(),json.dumps(response,ensure_ascii=False) if response is not None else None,job['id'],job['attempts']))
            if actual is not None:
                db.execute('UPDATE usage SET actual=?,tokens=? WHERE job=? AND attempt=?',
                           (actual,actual,job['id'],job['attempts']))
            if cooldown:
                db.execute('INSERT INTO cooldowns VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET until=max(until,excluded.until),reason=excluded.reason',
                           (job['group_id'],until,reason))
            if credential:
                db.execute('INSERT INTO credentials VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET until=excluded.until',
                           (job['credential_hash'],until))

    def _retry_at(self, headers, body):
        now = self.clock()
        until = now + 60
        value = headers.get('Retry-After') if headers else None
        if value:
            try:
                until = max(now + 5, now + float(value))
            except (ValueError, TypeError):
                try:
                    until = max(now + 5, parsedate_to_datetime(value).timestamp())
                except (ValueError, TypeError, OverflowError):
                    pass
        try:
            details = json.loads(body).get('error', {}).get('details', [])
            for detail in details:
                if isinstance(detail, dict) and 'retryDelay' in detail:
                    until = max(until, now + float(str(detail['retryDelay']).rstrip('s')))
        except (ValueError, TypeError, AttributeError):
            pass
        normalized = body.casefold().replace('_','').replace(' ','')
        if any(s in normalized for s in ('perday', 'requestsperday', 'daily')):
            until = max(until, pacific_day(now)[1])
        return until

    def run_once(self, *, timeout=30, transport=None):
        job = self.claim(lease_seconds=max(240, timeout + 60))
        if not job:
            return None
        # Eight scene plans plus the full narration can take longer than the
        # short connectivity checks. Both direct and background dispatch must
        # use the same allowance; a competing worker must not shorten it.
        if job['category'] == 'source_plan':
            timeout = max(timeout, 120)
        # No redirects: credentials must never be forwarded to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        if transport is None:
            transport = urllib.request.build_opener(NoRedirect()).open
        request = urllib.request.Request(job['url'], data=job['payload'].encode() if job['payload'] is not None else None,
                         headers={'Content-Type':'application/json','x-goog-api-key':self.keys[job['credential_hash']]})
        try:
            with transport(request, timeout=timeout) as response:
                raw = response.read()
            try:
                result = json.loads(raw.decode('utf-8'))
            except (ValueError,UnicodeError):
                self._finish(job,'error_response',reason='invalid_json')
                return self.inspect(job['id'])
            if not isinstance(result, dict):
                self._finish(job, 'error_response', reason='invalid_json_shape')
            elif 'error' in result:
                self._finish(job, 'error_response', reason='api_error_body')
            else:
                usage = result.get('usageMetadata') or {}
                actual = usage.get('promptTokenCount') if isinstance(usage, dict) else None
                actual = actual if type(actual) is int and actual >= 0 else None
                self._finish(job, 'succeeded', response=result, actual=actual)
        except urllib.error.HTTPError as error:
            body = error.read(16000).decode('utf-8', errors='replace')
            if error.code == 429:
                self._finish(job,'waiting_quota',until=self._retry_at(error.headers,body),reason='quota_429',cooldown=True)
            elif error.code in (401,403) or (error.code == 400 and any(x in body.upper() for x in ('API_KEY_INVALID','API KEY NOT VALID','API_KEY_EXPIRED'))):
                # Credential failures cannot multiply project quota. Disable the
                # credential, expose an access error; no endless retries.
                self._finish(job,'error_access',until=self.clock()+3600,reason='credential_rejected',credential=True)
            elif error.code in (500,502,503,504) and job['attempts'] < 3:
                self._finish(job,'retry_wait',until=self.clock()+min(60,5*2**job['attempts']),reason='temporary_api_error')
            else:
                self._finish(job,'error_request',reason='http_'+str(error.code))
        except (OSError, TimeoutError, ValueError, UnicodeError):
            self._finish(job,'transport_unknown',reason='no_confirmed_response')
        return self.inspect(job['id'])

    def overview(self, project=None):
        with self._db() as db:
            states = [dict(r) for r in db.execute('SELECT project,state,count(*) AS count FROM jobs GROUP BY project,state')]
            usage = [dict(r) for r in db.execute('SELECT group_id,bucket,count(*) AS requests,sum(tokens) AS input_tokens FROM usage GROUP BY group_id,bucket')]
            reservations=db.execute('SELECT count(*) FROM (SELECT job,attempt FROM usage GROUP BY job,attempt)').fetchone()[0]
            recovery=[]
            if project:
                recovery=[dict(row) for row in db.execute('''SELECT id,project,model,category,state,reason,attempts,next_at FROM jobs
                    WHERE project=? AND state IN ('error_access','error_request','error_response','budget_too_small','transport_unknown')
                    AND category IN ('source_image','source_prompt','source_reference','source_plan') ORDER BY created''',(project,))]
                for row in recovery: row.update(self.retry_status(row))
        return {'jobs':states,'usage':usage,'reserved_requests':reservations,'budget_fraction':self.fraction,
                'external_usage_known':False,'recovery_jobs':recovery}


def configured_queue(root, settings):
    """Only explicit opt-in uses the ledger; no activation in old projects."""
    if settings.get('google_queue_enabled') is not True:
        return None
    from .google_keys import google_keys
    registry = settings.get('google_quota_registry') or {'groups':[], 'budget_fraction':0.8}
    return GoogleQueue(Path(root) / 'google_api', registry,
                       google_keys(settings.get('google_api_key'),settings.get('google_extra_api_keys')))


class GoogleQueueWorker:
    """Bounded polling resumes saved API requests, independently of generation."""
    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run,daemon=True,name='google-api-queue')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        while not self.stop_event.is_set():
            try:
                queue = configured_queue(self.service.root,self.service.settings())
                if queue is not None:
                    queue.run_once()
                    self.service.resume_google_image_reviews(queue)
                    self.service.resume_google_reference_reviews(queue)
                    self.service.resume_google_source_batches(queue)
            except Exception:
                # No credential-bearing exceptions or payloads in application logs.
                import logging
                logging.getLogger(__name__).warning('Google API queue unavailable; saved jobs retained.')
            self.stop_event.wait(2)

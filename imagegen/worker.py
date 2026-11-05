"""A single durable local GPU queue. Restart reconciles before submitting work."""
import hashlib
import io
import json
import logging
import secrets
import subprocess
import threading
import time
import tempfile
import os
from pathlib import Path
from datetime import datetime, timezone
from .db import uid, now, dump
from .domain import DomainError, SourceImageRejected, validate_analysis
from .providers import ComfyProvider, GoogleNanoBananaProvider, PollinationsProvider, ProviderError, ExecutionError, RejectedError, build_workflow
from .cast import slugify_id
from .service import ACTIVE, TERMINAL
from .review_policy import visual_reviews_enabled
from .script_analysis import analyze as deepseek_analyze
from .script_analysis.adapter import export_to_app_analysis
from .script_analysis.errors import AnalysisError

log = logging.getLogger(__name__)


class ContentRejectedError(DomainError):
    """A generated image violates a concrete project constraint."""


class DetectorUnavailableError(DomainError):
    """Infrastructure failure is not evidence that an image should be regenerated."""
    def __init__(self, message):
        super().__init__('Проверка композиции недоступна: ' + message, 503)
        self.details = {'code': 'DETECTOR_UNAVAILABLE'}


class ResourceSafetyError(DomainError):
    """The local hardware is already too hot for another GPU image job."""

class AnalysisPaused(InterruptedError):
    """Stop text review at request boundaries, retaining its checked cache."""


class Worker:
    def _check_source_review(self,generation):
        row=self.db.one('SELECT cancel_requested FROM generations WHERE id=?',(generation['id'],))
        # Text analysis has its own cancellation state; an old paused audio
        # workflow must not block a new analysis of the story.
        if self.stop_event.is_set() or not row or row['cancel_requested']:
            raise AnalysisPaused('Разбор остановлен. Проверенные ответы сохранены для продолжения.')

    def __init__(self, service, provider_factory=ComfyProvider, runtime=None):
        self.service = service
        self.db = service.db
        self.provider_factory = provider_factory
        self.runtime = runtime
        self.stop_event = threading.Event()
        self.thread = None
        self._face_detector = None
        self._hand_detector = None
        self._person_detector = None
        self._identity_analyser = None
        self._identity_embeddings = {}
        # Ultralytics otherwise tries to write into a roaming profile folder.
        # Keep its tiny settings file inside the application's writable data
        # directory so the validator works after a reboot and for every user.
        import os
        config_dir = self.service.root / 'ultralytics'
        config_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('YOLO_CONFIG_DIR', str(config_dir))

    def recover_after_unexpected_shutdown(self):
        """Keep a reboot from silently starting GPU work that was interrupted mid-frame."""
        message = ('Генерация прервана неожиданным выключением компьютера. '
                   'Запустите нужный кадр заново, когда компьютер будет готов.')
        with self.db.transaction() as connection:
            connection.execute("""UPDATE generations
                              SET state='resource_degraded', error=?, finished_at=?, progress=?
                              WHERE state IN ('preparing','submitted','running','saving','reconciling')
                              AND NOT (purpose='casting' AND COALESCE(json_extract(snapshot,'$.flow_request_id'),'')!='')""",
                               (message, now(), dump({'stage': 'Прервано аварийным выключением', 'percent': 0})))
            connection.execute("UPDATE batches SET state='paused' WHERE state='running'")

    def reconcile_cancelled_visual_reviews(self):
        """Close obsolete visual waits without silently launching image work."""
        if visual_reviews_enabled():
            return 0
        updated = 0
        for generation in self.db.all("SELECT * FROM generations WHERE state IN ('waiting_prompt','blocked_prompt') AND purpose='scene'"):
            snapshot = json.loads(generation['snapshot'])
            if not snapshot.get('prompt_review'):
                continue
            snapshot['visual_review_policy'] = 'cancelled'
            self.update(generation, state='cancelled', snapshot=snapshot,
                        finished_at=now(), error='Автоматическая оценка промпта отменена. Для создания изображения запустите кадр заново.',
                        progress={'stage': 'Автоматическая оценка отменена', 'percent': 0})
            updated += 1
        return updated

    def start(self, interrupted=False):
        self.reconcile_cancelled_visual_reviews()
        with self.db.transaction() as connection:
            if not interrupted:
                connection.execute("""UPDATE generations SET state='reconciling' WHERE state IN ('preparing','submitted','running','saving')
                  AND NOT (purpose='casting' AND COALESCE(json_extract(snapshot,'$.flow_request_id'),'')!='')""")
        if interrupted:
            self.recover_after_unexpected_shutdown()
        self.thread = threading.Thread(target=self.run, daemon=True, name='image-worker')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # The error is visible both in logs and UI; no successful result is fabricated.
                log.exception('Worker loop failed')
                with self.db.transaction() as connection:
                    self.db.event(connection, None, 'worker_error', {'message': 'Сбой worker; смотрите журнал сервера.'})
            self.stop_event.wait(1)

    def update(self, generation, **fields):
        fields = {key: dump(value) if key in ('snapshot', 'progress') else value for key, value in fields.items()}
        with self.db.transaction() as connection:
            connection.execute('UPDATE generations SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                               (*fields.values(), generation['id']))
            self.db.event(connection, generation['project_id'], 'generation_updated', {'id': generation['id'], **fields})

    def finish(self, generation, state, error=None):
        self.update(generation, state=state, error=error, finished_at=now())
        self.refresh_batches()

    def refresh_batches(self):
        with self.db.transaction() as connection:
            batches = connection.execute("SELECT id FROM batches WHERE state IN ('running','paused')").fetchall()
            for batch in batches:
                states = [row[0] for row in connection.execute('SELECT state FROM generations WHERE batch_id=?', (batch['id'],))]
                if states and all(state in TERMINAL for state in states):
                    state = 'completed' if all(s == 'succeeded' for s in states) else 'completed_with_errors'
                    connection.execute('UPDATE batches SET state=? WHERE id=?', (state, batch['id']))

    def tick(self):
        self.refresh_batches()
        # A claimed Flow portrait is owned by the browser until its exact
        # request uploads or fails. It must never enter ComfyUI monitoring or
        # monopolize the local text-analysis queue.
        generation = self.db.one("""SELECT * FROM generations
          WHERE state IN ('preparing','submitted','running','saving','reconciling')
          AND NOT (purpose='casting' AND COALESCE(json_extract(snapshot,'$.flow_request_id'),'')!='')
          ORDER BY created_at LIMIT 1""")
        if generation:
            self.monitor(generation)
            return
        if self.resume_analysis_review():
            return
        if self.resume_prompt_review():
            return
        # Google Flow is executed by the browser bridge, not by this worker.
        # Its scene jobs deliberately remain queued until the bridge uploads
        # the resulting asset.  Claiming them here would immediately cancel a
        # perfectly valid external request before the bridge can see it.
        flow_mode = self.service.settings().get('image_backend') == 'flow'
        with self.db.transaction() as connection:
            eligibility = "AND g.purpose='analysis'" if flow_mode else ''
            rows = connection.execute(f"""SELECT g.* FROM generations g LEFT JOIN batches b ON b.id=g.batch_id
              WHERE g.state='queued' AND g.cancel_requested=0 AND (g.batch_id IS NULL OR b.state='running') {eligibility}
              ORDER BY g.created_at,g.rowid""").fetchall()
            row = None
            capacities = {}
            for candidate in rows:
                if candidate['purpose']=='scene':
                    if json.loads(candidate['snapshot']).get('plan_review_wait'):
                        continue
                    project_id = candidate['project_id']
                    if project_id not in capacities:
                        capacities[project_id] = self.service.google_generation_capacity(project_id)
                    if not capacities[project_id]['allowed']:
                        progress=dump({'stage':'Ожидает проверки предыдущих кадров', 'percent':0})
                        if candidate['progress']!=progress:
                            connection.execute("UPDATE generations SET progress=? WHERE id=? AND state='queued'", (progress,candidate['id']))
                        continue
                row = candidate
                break
            if not row:
                return
            generation = dict(row)
            snapshot=json.loads(generation['snapshot'])
            increment=0 if snapshot.pop('prompt_resume',False) else 1
            generation['snapshot']=dump(snapshot)
            connection.execute("UPDATE generations SET state='preparing',started_at=?,attempts=attempts+?,snapshot=?,progress=? WHERE id=?",
                               (now(),increment,generation['snapshot'],dump({'stage': 'Подготовка', 'percent': 10}), generation['id']))
        self.prepare(generation)

    def _update_pending_prompt(self,generation,**fields):
        """Do not overwrite a cancellation, batch pause or concurrent update."""
        fields={key:dump(value) if key in ('snapshot','progress') else value for key,value in fields.items()}
        with self.db.transaction() as connection:
            if fields.get('state')=='queued':
                from .domain import fingerprint
                current=connection.execute('SELECT s.current_revision,p.story FROM scenes s JOIN projects p ON p.id=s.project_id WHERE s.id=?',(generation['scene_id'],)).fetchone()
                original=json.loads(generation['snapshot'])
                if not current or current['current_revision']!=generation['scene_revision'] or (original.get('story_hash') and original['story_hash']!=fingerprint(current['story'])):
                    return False
            changed=connection.execute('UPDATE generations SET '+','.join(key+'=?' for key in fields)+
                " WHERE id=? AND state=? AND snapshot=? AND cancel_requested=0 AND (batch_id IS NULL OR EXISTS (SELECT 1 FROM batches WHERE id=batch_id AND state='running'))",
                (*fields.values(),generation['id'],generation['state'],generation['snapshot'])).rowcount
            if changed:self.db.event(connection,generation['project_id'],'generation_updated',{'id':generation['id'],**fields})
        return bool(changed)

    def resume_prompt_review(self):
        if not visual_reviews_enabled():
            return False
        if self.service.settings().get('google_queue_enabled') is not True or self.service.settings().get('source_contract_enabled') is not True:return False
        from .google_queue import configured_queue
        from .final_prompt_review import consume_saved,saved_request_matches
        queue=configured_queue(self.service.root,self.service.settings())
        if queue is None:return False
        for generation in self.db.all("SELECT * FROM generations WHERE state IN ('waiting_prompt','blocked_prompt') AND purpose='scene' ORDER BY created_at,rowid"):
            if generation['cancel_requested']:
                self.finish(generation,'cancelled');return True
            if queue.project_paused(generation['project_id']):continue
            if generation['batch_id']:
                batch=self.db.one('SELECT state FROM batches WHERE id=?',(generation['batch_id'],))
                if not batch or batch['state']!='running':continue
            snapshot=json.loads(generation['snapshot']);review=snapshot.get('prompt_review') or {}
            if not review.get('job_id'):continue
            job=None
            try:
                scene=self.service.scene(generation['scene_id']);fresh=self.service.scene_snapshot(scene)
                if (scene['current_revision']!=generation['scene_revision'] or
                    any(fresh.get(k)!=snapshot.get(k) for k in ('source_contract','story_hash','prompt','channel_profile_id','channel_profile_version_id'))):
                    return self._update_pending_prompt(generation,state='failed',finished_at=now(),error='Задание изменилось во время проверки промпта.')
                job=queue.inspect(review['job_id'])
                if not saved_request_matches(self.service,snapshot,review,job,queue):
                    raise ValueError('Saved prompt request does not match')
                if job['state']=='succeeded' and review.get('consumed_recovery_revision')==job.get('recovery_revision',0):continue
                if job['state']!='succeeded':
                    waiting=job['state'] in ('queued','running','waiting_registry','waiting_model','waiting_quota','retry_wait')
                    state='waiting_prompt' if waiting else 'blocked_prompt'
                    if generation['state']!=state or review.get('status')!=job['state'] or review.get('next_retry_at')!=job.get('next_at'):
                        snapshot['prompt_review']={**review,'status':job['state'],'reason':job.get('reason'),
                            'next_retry_at':job.get('next_at'),'allowed':False}
                        return self._update_pending_prompt(generation,state=state,snapshot=snapshot,error=None,
                            progress={'stage':'Ожидает проверки конечного промпта' if waiting else 'Проверка промпта требует внимания','percent':0})
                    continue
                result=consume_saved(self.service,snapshot,review,job,queue);snapshot['prompt_review']=result
                if result['allowed']:
                    fresh=self.service.scene_snapshot(self.service.scene(generation['scene_id']))
                    if any(fresh.get(k)!=snapshot.get(k) for k in ('source_contract','story_hash','prompt')):
                        return self._update_pending_prompt(generation,state='failed',finished_at=now(),error='Задание изменилось при применении проверки промпта.')
                    if queue.project_paused(generation['project_id']):continue
                    snapshot['prompt_resume']=True
                    return self._update_pending_prompt(generation,state='queued',snapshot=snapshot,error=None,
                        progress={'stage':'Промпт проверен. Задание готово к продолжению','percent':0})
                return self._update_pending_prompt(generation,state='blocked_prompt',snapshot=snapshot,progress={'stage':'Промпт требует внимания','percent':0})
            except (DomainError,KeyError,ValueError,TypeError,AttributeError):
                if generation['state']=='blocked_prompt':continue
                snapshot['prompt_review']={**review,'status':'error_response','allowed':False,
                    'reason':'Связанная проверка промпта отсутствует, устарела или вернула некорректный ответ.'}
                if job and job['state']=='succeeded':snapshot['prompt_review']['consumed_recovery_revision']=job.get('recovery_revision',0)
                return self._update_pending_prompt(generation,state='blocked_prompt',snapshot=snapshot,error=snapshot['prompt_review']['reason'],
                    progress={'stage':'Проверка промпта требует внимания','percent':0})
        return False

    def resume_analysis_review(self):
        """Continue only saved analysis after a confirmed plan response."""
        from .google_queue import configured_queue
        from .forgotten_cities.fidelity import digest
        queue = configured_queue(self.service.root, self.service.settings())
        if queue is None:
            return False
        rows = self.db.all("SELECT * FROM generations WHERE purpose='analysis' AND state='waiting_review' ORDER BY created_at,rowid")
        for generation in rows:
            snapshot = json.loads(generation['snapshot'])
            wait = snapshot.get('plan_review_wait') or {}
            snapshot['plan_review_wait'] = wait
            if generation['cancel_requested']:
                self.finish(generation, 'cancelled')
                return True
            if queue.project_paused(generation['project_id']):
                continue
            project = self.service.project(generation['project_id'])
            if (project['version'] != snapshot['project']['version'] or
                wait.get('story_hash') != digest(project['story']) or
                wait.get('model') != (self.service.settings().get('google_text_model') or 'gemini-3.1-flash-lite') or
                wait.get('channel') != project.get('channel_profile_id') or
                wait.get('channel_version') != project.get('channel_profile_version_id')):
                self.finish(generation, 'failed', 'Исходный текст или настройки изменились во время проверки. Сохранённый разбор не применён.')
                return True
            job_id = wait.get('job_id')
            if not job_id and wait.get('state')=='error_response':continue
            try:
                job = queue.inspect(job_id) if job_id else None
            except KeyError:
                job = None
            if not job or job['project'] != generation['project_id'] or job['category'] != 'source_plan' or job['model'] != wait['model']:
                if wait.get('state')=='error_response':continue
                wait['state'] = 'error_response'
                self.update(generation, snapshot=snapshot, error='Не найдена связанная проверка плана. Разбор сохранён для разбора ошибки.')
                return True
            # A failed interpretation of an unchanged successful response must
            # not loop. An explicit queue recovery is a durable new revision.
            revision=job.get('recovery_revision',0)
            if (job['state']=='succeeded' and wait.get('state') in ('error_response','error_request','error_access','budget_too_small','transport_unknown','cancelled')
                and revision<=wait.get('recovery_revision',0)):
                continue
            if job['state'] != 'succeeded':
                if wait.get('state') != job['state'] or wait.get('next_at') != job.get('next_at'):
                    wait.update(state=job['state'], next_at=job.get('next_at'),recovery_revision=revision)
                    blocked=job['state'] in ('error_response','error_request','error_access','budget_too_small','transport_unknown','cancelled')
                    self.update(generation, snapshot=snapshot,
                        error='Проверка плана требует внимания. Разбор сохранён; автоматического повтора нет.' if blocked else None,
                        progress={'stage': 'Проверка плана требует внимания' if blocked else 'Ожидание проверки плана', 'percent': 90, **wait})
                continue
            # No analyzer, casting, or rendering: _save_analysis consumes the
            # saved response through the content-addressed queue/cache.
            wait['recovery_revision']=revision
            generation={**generation,'snapshot':dump(snapshot)}
            with self.db.transaction() as connection:
                claimed = connection.execute("UPDATE generations SET state='preparing',snapshot=? WHERE id=? AND state='waiting_review' AND cancel_requested=0", (generation['snapshot'],generation['id'])).rowcount
            if claimed:
                self.prepare(generation)
                return True
        return False

    def _assert_gpu_safe(self):
        """Refuse a new image pass when the GPU has no thermal headroom."""
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=temperature.gpu,memory.used,memory.total',
                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                return
            line = next((line for line in result.stdout.splitlines() if line.strip()), '')
            values = [int(value.strip()) for value in line.split(',')[:3]]
            if len(values) == 3:
                temperature, memory_used, memory_total = values
                if temperature >= 80:
                    raise ResourceSafetyError(
                        f'GPU уже нагрета до {temperature}°C. Очередь поставлена на паузу до охлаждения.')
                # The single FrameForge queue deliberately keeps ComfyUI's
                # model resident between frames. On an 8 GB card this normally
                # occupies about 90% of VRAM and is not a competing workload.
                # Leave a small emergency margin, while temperature remains the
                # primary safety brake for the reported PC shutdowns.
                if memory_total and memory_used / memory_total >= 0.96:
                    raise ResourceSafetyError(
                        f'GPU уже занята на {memory_used}/{memory_total} МБ. Очередь поставлена на паузу.')
        except ResourceSafetyError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            # Telemetry can be unavailable on a driver update or a non-NVIDIA PC.
            # The low-VRAM workflow remains usable; unavailable telemetry is not
            # mistaken for a temperature alarm.
            return

    def _pause_for_hardware_safety(self, generation, message):
        self.finish(generation, 'resource_degraded', message)
        if generation.get('batch_id'):
            with self.db.transaction() as connection:
                connection.execute("UPDATE batches SET state='paused' WHERE id=? AND state='running'", (generation['batch_id'],))

    def prepare(self, generation):
        snapshot = json.loads(generation['snapshot'])
        # Upgrade queued location cards made before semantic location anchors.
        # They must not fall back to an unrelated generic facade after restart.
        if generation['purpose'] == 'location' and snapshot.get('template_version', 0) < 12 and generation.get('location_id'):
            location = self.service.location(generation['location_id'])
            project = self.service.project(generation['project_id'])
            snapshot = self.service.location_snapshot(project, location, snapshot.get('profile'))
            self.update(generation, snapshot=snapshot,
                        progress={'stage': 'Уточнение референса локации', 'percent': 12})
        # No queued legacy job may re-enable FaceDetailer. Its per-face second
        # diffusion passes created spikes in both GPU time and memory.
        if snapshot.get('detail_faces'):
            snapshot['detail_faces'] = False
            snapshot['template_version'] = max(6, int(snapshot.get('template_version', 0)))
            self.update(generation, snapshot=snapshot,
                        progress={'stage': 'Старый тяжёлый face-detailer отключён', 'percent': 12})
        settings = snapshot['profile']
        if (settings.get('width'), settings.get('height')) not in ((1024, 576), (768, 432)):
            settings = {**settings, 'width': 1024, 'height': 576}
            snapshot['profile'] = settings
            self.update(generation, snapshot=snapshot,
                        progress={'stage': 'Небезопасное разрешение снижено до 1024×576', 'percent': 12})
        try:
            if generation['purpose']=='scene' and self.service.settings().get('source_contract_enabled') is True:
                from .final_prompt_review import check
                from .source_catalog import SourceCatalog
                if SourceCatalog(self.service).path.exists():
                    fresh=self.service.scene_snapshot(self.service.scene(generation['scene_id']))
                    if fresh.get('source_contract',{}).get('catalog_context')!=snapshot.get('source_contract',{}).get('catalog_context'):
                        self.update(generation,state='blocked_prompt',progress={'stage':'Привязка каталога изменилась; просмотрите новое задание','percent':0})
                        return
                reviewed=check(self.service,snapshot,snapshot['prompt'])
                if not reviewed['allowed']:
                    snapshot['prompt_review']=reviewed
                    self.update(generation,state='waiting_prompt' if reviewed['status'] in ('queued','running','waiting_registry','waiting_model','waiting_quota','retry_wait','waiting_queue') else 'blocked_prompt',snapshot=snapshot,
                        progress={'stage':'Ожидает проверки конечного промпта','percent':0})
                    return
            if generation['purpose'] == 'analysis' and snapshot.get('pending_analysis'):
                self._save_analysis(generation, snapshot['pending_analysis'], snapshot['project']['version'])
                return
            if generation['purpose'] == 'analysis':
                project = snapshot['project']
                story_text = project.get('story', '')
                if not story_text or not story_text.strip():
                    raise DomainError('Текст сценария пуст.', 400)

                analysis_started = time.monotonic()
                analysis_started_at = time.time()
                self.update(generation, progress={
                    'stage': 'Подготовка к анализу DeepSeek Flash',
                    'percent': 5,
                    'analyzer_version': 'deepseek',
                    'analysis_started_at': analysis_started_at,
                })

                api_key = settings.get('deepseek_api_key') or os.environ.get('DEEPSEEK_API_KEY')

                def report_progress(stage, percent):
                    elapsed = max(0, round(time.monotonic() - analysis_started))
                    self.update(generation, progress={
                        'stage': stage,
                        'percent': percent,
                        'analyzer_version': 'deepseek',
                        'elapsed_seconds': elapsed,
                        'analysis_started_at': analysis_started_at,
                        'indeterminate': percent in (20, 60),
                    })

                analysis_result = deepseek_analyze(
                    story_text,
                    api_key=api_key,
                    on_progress=report_progress,
                )

                self.update(generation, progress={
                    'stage': 'Формирование структуры проекта FrameForge',
                    'percent': 95,
                    'analyzer_version': 'deepseek',
                    'analysis_started_at': analysis_started_at,
                })

                app_analysis = export_to_app_analysis(
                    analysis_result,
                    story_text,
                    mode=project.get('mode', 'cinematic'),
                    project=project, target=snapshot.get('target'),
                )
                self._save_analysis(generation, app_analysis, project['version'])
                return
            if settings.get('image_backend') == 'google_nanobanana' and settings.get('google_api_key'):
                self.update(generation, progress={'stage': 'Запрос к Google Nano Banana (Cloud)', 'percent': 25})
                from .google_queue import configured_queue
                provider = GoogleNanoBananaProvider(settings['google_api_key'], settings.get('google_model'), settings.get('google_extra_api_keys', ''),
                    dispatcher=configured_queue(self.service.root,settings),project_id=generation['project_id'])
                refs = []
                for reference in snapshot.get('references', []):
                    asset = self.service.asset(reference['asset_id'])
                    data = (self.service.root / asset['path']).read_bytes()
                    refs.append({'asset_id': reference['asset_id'], 'role': reference.get('role', ''), 'data': data})
                self.update(generation, state='running', progress={'stage': 'Google ИИ генерирует изображение...', 'percent': 50, 'indeterminate': True})
                img_data = provider.generate(
                    prompt=snapshot['prompt'],
                    references=refs,
                    width=settings.get('width', 1024),
                    height=settings.get('height', 576),
                    seed=generation.get('seed')
                )
                self.update(generation, state='saving', progress={'stage': 'Проверка и сохранение кадра', 'percent': 90})
                self.save_image(generation, img_data)
                return
            if settings.get('image_backend') == 'pollinations':
                self.update(generation, progress={'stage': 'Запрос к Pollinations AI (Cloud Free)', 'percent': 25})
                provider = PollinationsProvider(model=settings.get('pollinations_model', 'flux'))
                self.update(generation, state='running', progress={'stage': 'Pollinations ИИ генерирует изображение...', 'percent': 50, 'indeterminate': True})
                img_data = provider.generate(
                    prompt=snapshot['prompt'],
                    width=settings.get('width', 1024),
                    height=settings.get('height', 576),
                    seed=generation.get('seed')
                )
                self.update(generation, state='saving', progress={'stage': 'Проверка и сохранение кадра', 'percent': 90})
                self.save_image(generation, img_data)
                return
            if settings.get('image_backend') == 'flow':
                self.finish(generation, 'cancelled', 'Выбран режим Google Flow: генерация выполняется через flow.google.com.')
                return
            if self.runtime:
                self._assert_gpu_safe()
                self.runtime.ensure_comfy(settings)
            self.update(generation, progress={'stage': 'Подготовка модели изображений', 'percent': 16})
            provider = self.provider_factory(settings['comfy_url'])
            provider.health()
            uploaded = []
            for reference in snapshot.get('references', []):
                asset = self.service.asset(reference['asset_id'])
                data = (self.service.root / asset['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != asset['sha256']:
                    raise DomainError('Референс повреждён: SHA-256 не совпадает.')
                uploaded.append(provider.upload(data, reference['asset_id'] + '.png'))
            self.update(generation, progress={'stage': 'Загрузка референсов', 'percent': 22})
            graph = build_workflow(settings, snapshot['prompt'], generation['seed'], uploaded,
                                   'imagegen/' + generation['id'], snapshot.get('detail_faces', False))
            provider.validate(graph)
            snapshot['workflow'] = graph
            # Persist before submission: a timeout here must never lead to a blind duplicate submit.
            self.update(generation, state='submitted', snapshot=snapshot, progress={'stage': 'Отправка workflow', 'percent': 28})
            prompt_id = provider.submit(graph, generation['id'])
            self.update(generation, provider_id=prompt_id, state='running', progress={'stage': 'В очереди ComfyUI', 'percent': 32})
        except SourceImageRejected as error:
            self._retry_source_image(generation, error)
        except ContentRejectedError as error:
            if not generation['cancel_requested']:
                fields = {'state': 'queued', 'provider_id': None, 'started_at': None,
                          'seed': secrets.randbelow(2**53), 'error': None,
                          'progress': {'stage': f'Кадр отклонён проверкой; создаётся вариант №{generation["attempts"] + 1}',
                                       'percent': 8, 'rejection_reason': str(error)}}
                self.update(generation, **fields)
            else:
                self.finish(generation, 'cancelled', str(error))
        except ResourceSafetyError as error:
            self._pause_for_hardware_safety(generation, str(error))
        except RejectedError as error:
            self.finish(generation, 'failed', str(error))
        except AnalysisPaused as error:
            self.finish(generation,'cancelled',str(error))
        except (ProviderError, DomainError, OSError, ValueError, KeyError, AnalysisError) as error:
            current = self.db.one('SELECT state,cancel_requested FROM generations WHERE id=?', (generation['id'],))
            if current['cancel_requested']:
                self.finish(generation,'cancelled',str(error));return
            if current['state'] == 'submitted':
                self.update(generation, state='reconciling', error=str(error), progress={'stage': 'Проверка принятия запроса'})
            else:
                diag = getattr(error, 'diagnostics', None)
                if diag:
                    prog = {'stage': 'Сбой анализа сценария', 'percent': 0, 'diagnostics': diag}
                    self.update(generation, progress=prog)
                self.finish(generation, 'failed', str(error))
        except Exception as error:
            log.exception('Unexpected preparation error')
            self.finish(generation, 'failed', f'{type(error).__name__}: {error}')

    def _retry_source_image(self, generation, error):
        retry = error.details['retry']
        if retry['exhausted'] or generation['cancel_requested']:
            self.finish(generation, 'cancelled' if generation['cancel_requested'] else 'failed', str(error))
            return
        # Reuse this durable job, never insert a second generation or batch.
        scene = self.service.scene(generation['scene_id'])
        if scene['current_revision'] != generation['scene_revision']:
            self.finish(generation, 'cancelled', 'Сцена изменена во время смысловой проверки.')
            return
        snapshot = self.service.scene_snapshot(scene)
        self.update(generation, state='queued', provider_id=None, started_at=None,
                    seed=secrets.randbelow(2**53), error=None, snapshot=snapshot,
                    progress={'stage': f"Исправлен промпт; вариант №{retry['attempts'] + 1}",
                              'percent': 8, 'source_fidelity_retry': retry})

    def _save_analysis(self, generation, analysis, version):
        analysis = dict(analysis)
        snapshot = json.loads(generation['snapshot']) if isinstance(generation.get('snapshot'), str) else dict(generation.get('snapshot') or {})
        settings = snapshot.get('profile', {})
        # Also cover durable checkpoints created before target propagation was
        # fixed. Casting never changes this plan or the requested frame count.
        if analysis.get('analyzer_version')=='deepseek' and snapshot.get('target') is not None:
            from .script_analysis.storyboard import plan
            story=(snapshot.get('project') or {}).get('story') or self.service.project(generation['project_id'])['story']
            analysis=plan(analysis,story,snapshot['target'])
        if not snapshot.get('pending_analysis'):
            snapshot['pending_analysis'] = json.loads(json.dumps(analysis, ensure_ascii=False))
            self.update(generation, snapshot=snapshot)

        project_id = generation['project_id']
        project_record = self.service.project(project_id)
        from .story_reliability import require_consistent_analysis
        require_consistent_analysis(analysis, project_record['story'])
        if self.service.settings().get('source_contract_enabled') is True:
            from .source_contract import require_complete_coverage
            require_complete_coverage(project_record['story'], analysis['scenes'])

        with self.db.transaction() as connection:
            project = connection.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
            if not project:
                raise DomainError('Проект не найден.', 404)
            if project['version'] != version:
                raise DomainError('Проект изменился во время анализа.', 409)
            has_scenes = connection.execute('SELECT id FROM scenes WHERE project_id=? LIMIT 1', (project_id,)).fetchone()
            if connection.execute('SELECT cancel_requested FROM generations WHERE id=?', (generation['id'],)).fetchone()[0]:
                connection.execute("UPDATE generations SET state='cancelled',finished_at=? WHERE id=?", (now(), generation['id']))
                return
            validate_analysis(analysis, project['story'])
            if (analysis.get('v3_metadata') or {}).get('story_visual_plan'):
                from .story_visual_plan import validate_scenes
                validate_scenes(project['story'], analysis['v3_metadata']['story_visual_plan'], analysis['scenes'])
            if has_scenes:
                if not snapshot.get('analysis_rebuild'):
                    raise DomainError('Сцены уже существуют; разбор не заменён.', 409)
                self.service._clear_analysis_for_replacement(connection, project_id)

            saved_characters = []
            saved_locations = []
            saved_props = []
            cast_bible = analysis.get('cast_bible') or {}
            char_id_map = {}
            char_name_map = {}
            for character in analysis.get('characters', []):
                character_id = uid()
                raw_id = character.get('id')
                cast_id = (character.get('cast_id') or raw_id or
                           slugify_id(character.get('canonical_name') or character['name']))
                if raw_id:
                    char_id_map[raw_id] = character_id
                if cast_id:
                    char_id_map[cast_id] = character_id
                char_name_map[character['name']] = character_id
                for a in character.get('aliases', []):
                    if a and a not in char_name_map:
                        char_name_map[a] = character_id
                connection.execute('''INSERT INTO characters(id,project_id,name,description,wardrobe,cast_id,canonical_name,aliases,role,status,evidence)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (character_id, project_id, character['name'], character['description'],
                     dump(character.get('wardrobe', {})),
                     cast_id,
                     character.get('canonical_name', character['name']),
                     dump(character.get('aliases', [character['name']])),
                     character.get('role', ''),
                     character.get('status', 'active'),
                     dump(character.get('evidence', []))))
                saved_characters.append({**character, 'id': character_id, 'cast_id': cast_id, 'revision': 1})

            loc_id_map = {}
            loc_name_map = {}
            for location in analysis.get('locations', []):
                location_id = uid()
                raw_lid = location.get('id')
                if raw_lid:
                    loc_id_map[raw_lid] = location_id
                loc_name_map[location['name']] = location_id
                connection.execute('INSERT INTO locations(id,project_id,name,description) VALUES(?,?,?,?)',
                    (location_id, project_id, location['name'], location['description']))
                saved_locations.append({**location, 'id': location_id, 'revision': 1, 'locked': False, 'references_json': '{}'})
            for prop in analysis.get('props', []):
                prop_id = uid()
                connection.execute('INSERT INTO props(id,project_id,name,description,importance,first_appearance,owner,default_state) VALUES(?,?,?,?,?,?,?,?)',
                    (prop_id, project_id, prop['name'], prop['description'], prop.get('importance', 'standard'),
                     prop.get('first_appearance', 1), prop.get('owner', ''), prop.get('default_state', 'absent')))
                saved_props.append({**prop, 'id': prop_id, 'revision': 1, 'locked': False, 'references_json': '{}'})

            for position, scene in enumerate(analysis.get('scenes', []), 1):
                scene_id = uid()
                # Deduplicate characters
                scene_chars = list(dict.fromkeys(scene.get('characters', [])))
                # Map character_ids from provider IDs to DB UUIDs
                scene_cids = []
                for cid in scene.get('character_ids', []):
                    db_cid = char_id_map.get(cid)
                    if db_cid and db_cid not in scene_cids:
                        scene_cids.append(db_cid)
                for cname in scene_chars:
                    db_cid = char_name_map.get(cname)
                    if db_cid and db_cid not in scene_cids:
                        scene_cids.append(db_cid)

                # Build cast list with DB UUIDs
                scene_cast = []
                for cname in scene_chars:
                    cid = char_name_map.get(cname)
                    scene_cast.append({'character_id': cid, 'name': cname})

                # Map location_id to DB UUID
                raw_loc_id = scene.get('location_id')
                loc_name = scene.get('location') or scene.get('location_ref') or ''
                db_loc_id = loc_id_map.get(raw_loc_id) or loc_name_map.get(loc_name)

                scene_data = {
                    **scene,
                    'characters': scene_chars,
                    'character_ids': scene_cids,
                    'cast': scene_cast,
                    'location_id': db_loc_id,
                    'location': loc_name,
                    'location_ref': loc_name,
                    'visible_people': len(scene_chars) + len(scene.get('episodic_people', [])),
                }

                connection.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (scene_id, project_id, position))
                connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, 1, dump(scene_data), now()))

            metadata_dict = dict(analysis.get('v3_metadata') or analysis.get('v2_metadata') or {})
            metadata_dict['analyzer_version'] = analysis.get('analyzer_version', 'deepseek')
            if 'diagnostics' in analysis:
                metadata_dict['diagnostics'] = analysis['diagnostics']
            connection.execute('UPDATE projects SET world=?,cast_bible=?,analysis_metadata=?,version=version+1 WHERE id=?',
                               (analysis.get('world', ''), dump(cast_bible),
                                dump(metadata_dict), project_id))

            gen_snap = json.loads(generation['snapshot']) if isinstance(generation.get('snapshot'), str) else dict(generation.get('snapshot') or {})
            gen_snap.pop('plan_review_wait', None)
            if analysis.get('diagnostics'):
                gen_snap['diagnostics'] = analysis['diagnostics']
            gen_snap['analyzer_version'] = analysis.get('analyzer_version', 'deepseek')

            diag = analysis.get('diagnostics') or {}
            warnings = diag.get('warnings') or []
            scenes_count = len(analysis.get('scenes', []))
            cache_hit = diag.get('result_cache_hit', False)

            if cache_hit:
                stage_text = f"Разбор DeepSeek Flash загружен из кэша: {scenes_count} сцен"
            elif warnings:
                stage_text = f"Разбор DeepSeek Flash готов: {scenes_count} сцен ({len(warnings)} заметок)"
            else:
                stage_text = f"Разбор DeepSeek Flash готов: {scenes_count} сцен"

            connection.execute("UPDATE generations SET state='succeeded',error=NULL,finished_at=?,progress=?,snapshot=? WHERE id=?",
                               (now(), dump({'stage': stage_text, 'percent': 100, 'analyzer_version': 'deepseek'}), dump(gen_snap), generation['id']))
            self.db.event(connection, project_id, 'analysis_saved', {
                'auto_casts': 0,
                'analyzer_version': 'deepseek',
            })

    def monitor(self, generation):
        if generation['purpose'] == 'analysis':
            if self.service.settings().get('google_queue_enabled') is True and json.loads(generation['snapshot']).get('pending_analysis'):
                # A normal restart can occur between writing the prepared
                # analysis and its waiting marker. Reuse that checkpoint.
                self.prepare(generation)
                return
            self.finish(generation, 'failed', 'Анализ прерван перезапуском. Запустите его заново.')
            return
        snapshot = json.loads(generation['snapshot'])
        if generation['purpose'] == 'casting' and snapshot.get('flow_request_id'):
            return  # Browser bridge owns this request, including across restarts.
        if self.runtime:
            self.runtime.ensure_comfy(snapshot['profile'])
        provider = self.provider_factory(snapshot['profile']['comfy_url'])
        try:
            prompt_id = generation['provider_id']
            if not prompt_id:
                prompt_id = provider.reconcile(generation['id'])
                if not prompt_id:
                    if self.runtime and generation['attempts'] < 2 and not generation['cancel_requested']:
                        self.update(generation, state='queued', provider_id=None, started_at=None,
                                    error='ComfyUI был перезапущен; выполняется один безопасный повтор.',
                                    progress={'stage': 'Повтор после перезапуска backend'})
                    else:
                        self.finish(generation, 'failed', 'Запрос не найден в queue/history ComfyUI после сверки. Лимит безопасного повтора исчерпан.')
                    return
                self.update(generation, provider_id=prompt_id)
            if generation['cancel_requested']:
                provider.cancel(prompt_id)
                status = provider.status(prompt_id)
                if status['state'] in ('missing', 'succeeded'):
                    timed_out = generation.get('error', '') and generation['error'].startswith('Превышен тайм-аут')
                    self.finish(generation, 'resource_degraded' if timed_out else 'cancelled', generation.get('error'))
                return
            status = provider.status(prompt_id)
            if status['state'] == 'succeeded':
                self.update(generation, state='saving', progress={'stage': 'Сохранение кадра', 'percent': 96})
                self.save_image(generation, provider.fetch(status['image']))
            elif status['state'] == 'missing':
                if self.runtime and generation['attempts'] < 2 and not generation['cancel_requested']:
                    self.update(generation, state='queued', provider_id=None, started_at=None,
                                error='ComfyUI был перезапущен; выполняется один безопасный повтор.',
                                progress={'stage': 'Повтор после перезапуска backend'})
                else:
                    self.finish(generation, 'failed', 'Provider job отсутствует в очереди и history. Лимит безопасного повтора исчерпан.')
            else:
                started = datetime.fromisoformat(generation['started_at'])
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                if elapsed > snapshot['profile']['job_timeout_seconds']:
                    provider.cancel(prompt_id)
                    # Remain active until the backend confirms interruption: never overlap GPU jobs.
                    self.update(generation, cancel_requested=1, error='Превышен тайм-аут генерации; отправлена отмена.',
                                progress={'stage': 'Отмена по тайм-ауту'})
                else:
                    percent = min(92, 38 + int(elapsed // 12)) if status['state'] == 'running' else 32
                    self.update(generation, state='running', error=None,
                                progress={'stage': 'Генерация' if status['state'] == 'running' else 'Очередь ComfyUI',
                                          'elapsed_seconds': int(elapsed), 'percent': percent,
                                          'indeterminate': status['state'] != 'running'})
        except SourceImageRejected as error:
            self._retry_source_image(generation, error)
        except ContentRejectedError as error:
            # A detector-backed retry is intentional: a new seed is required because
            # the same seed would reproduce the rejected composition.  A bad
            # seed is not a completed frame: keep trying until a valid image is
            # found, unless the user pauses/cancels or hardware safety stops us.
            if not generation['cancel_requested']:
                fields = {'state': 'queued', 'provider_id': None, 'started_at': None,
                          'seed': secrets.randbelow(2**53), 'error': None,
                          'progress': {'stage': f'Кадр отклонён проверкой; создаётся вариант №{generation["attempts"] + 1}',
                                       'percent': 8, 'rejection_reason': str(error)}}
                # A bank and a living room must be readable as their actual
                # place, not silently replaced by a generic exterior. Reassert
                # the semantic interior anchor on later seeds.
                if generation['purpose'] == 'location' and generation['attempts'] >= 3:
                    retry_snapshot = dict(snapshot)
                    anchor = retry_snapshot.get('location_anchor', 'recognisable architecture and inanimate furnishings')
                    # A broad bank lobby invites the compact model to invent a
                    # customer. A tight, clear vault composition keeps the
                    # location legible while removing that empty floor space.
                    if 'bank' in anchor.casefold() or 'vault' in anchor.casefold():
                        anchor = ('Close architectural bank-vault view: a sealed circular vault door fills most of the frame, '
                                  'with orderly safe-deposit drawers beside it, brushed steel walls, tiled floor and ceiling lights. '
                                  'Closed storage and fixed architecture only.')
                    # Never accumulate the override on every seed: repetition
                    # weakens text conditioning and made interior cards loop.
                    base_prompt = retry_snapshot['prompt'].split(' COMPOSITION OVERRIDE: ', 1)[0]
                    retry_snapshot['prompt'] = (base_prompt + ' COMPOSITION OVERRIDE: ' + anchor +
                                                ' Keep this exact type of interior; do not substitute an exterior facade. '
                                                'Architecture and inanimate furnishings only.')
                    retry_snapshot['last_rejection_reason'] = str(error)
                    fields['snapshot'] = retry_snapshot
                    fields['progress']['stage'] = f'Локация уточняется по обстановке, вариант №{generation["attempts"] + 1}'
                self.update(generation, **fields)
            else:
                self.finish(generation, 'cancelled', str(error))
        except ExecutionError as error:
            self.finish(generation, 'cancelled' if generation['cancel_requested'] else 'failed', str(error))
        except ProviderError as error:
            # Transport loss is not evidence that execution stopped; keep the queue blocked until reconciled.
            self.update(generation, state='reconciling', error=str(error), progress={'stage': 'Нет связи / требуется проверка backend'})
        except Exception as error:
            log.exception('Saving/monitoring error')
            self.finish(generation, 'failed', f'{type(error).__name__}: {error}')

    def _bundled_detector_count(self, image, model, confidence):
        standalone = Path(__file__).resolve().parent.parent / 'backends' / 'detector-venv' / 'Scripts' / 'python.exe'
        config = self.runtime.backends / 'detector-runtime.json'
        configured = Path(json.loads(config.read_text(encoding='utf8'))['python']) if config.is_file() else standalone
        python = configured if configured.is_file() else self.runtime.backends / 'comfy-venv' / 'Scripts' / 'python.exe'
        if not python.is_file():
            raise DetectorUnavailableError('Не найден Python детекторов ComfyUI. Проверьте установку backend.')
        buffer = io.BytesIO()
        image.convert('RGB').save(buffer, format='PNG')
        key = (str(model), confidence, hashlib.sha256(buffer.getvalue()).hexdigest())
        cache = getattr(self, '_detector_count_cache', {})
        if key in cache:
            return cache[key]
        inputs = self.service.root / 'detector-inputs'
        inputs.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=inputs, suffix='.png', delete=False) as file:
            file.write(buffer.getvalue())
            input_path = Path(file.name)
        try:
            result = subprocess.run([str(python), str(Path(__file__).with_name('detector_process.py')),
                '--model', str(model), '--image', str(input_path), '--confidence', str(confidence),
                '--config-dir', str(self.service.root / 'ultralytics')], capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'})
            lines = [line.split(':', 1)[1] for line in result.stdout.splitlines()
                     if line.startswith('FF_DETECTOR_RESULT:')]
            if result.returncode or len(lines) != 1:
                raise DetectorUnavailableError('CPU-детектор не завершил проверку. Результат Flow сохранён для повторной загрузки.')
            value = json.loads(lines[0]).get('count')
            if type(value) is not int or value < 0:
                raise DetectorUnavailableError('CPU-детектор вернул некорректный результат.')
            if len(cache) >= 24:
                cache.clear()
            cache[key] = value
            self._detector_count_cache = cache
            return value
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise DetectorUnavailableError('CPU-детектор не ответил; повторите загрузку сохранённого результата.') from error
        finally:
            input_path.unlink(missing_ok=True)

    def _yolo_count(self, image, attribute, pattern, confidence):
        detector = getattr(self, attribute)
        if detector is None:
            if not self.runtime:
                return 0
            candidates = sorted((self.runtime.backends / 'ComfyUI').glob(pattern))
            standalone_model = self.runtime.backends / 'detector-models' / Path(pattern).name
            if standalone_model.is_file():
                candidates.append(standalone_model)
            if not candidates:
                raise DetectorUnavailableError('Не найден локальный файл модели детектора. Проверьте установку backend.')
            try:
                from ultralytics import YOLO
            except ImportError:
                # ComfyUI owns the inference dependencies; the UI venv can stay light.
                return self._bundled_detector_count(image, candidates[-1], confidence)
            try:
                detector = YOLO(str(candidates[-1]))
                setattr(self, attribute, detector)
            except Exception as error:
                raise DetectorUnavailableError('Не удалось загрузить локальную модель детектора.') from error
        try:
            from .detector_process import count_instances
            result = detector(image, device='cpu', imgsz=640, conf=confidence, verbose=False)[0]
            return count_instances(result)
        except Exception as error:
            raise DetectorUnavailableError('Ошибка выполнения CPU-детектора.') from error

    def _detected_face_count(self, image):
        return self._yolo_count(image, '_face_detector', '*/models/ultralytics/bbox/face_yolov8m.pt', 0.45)

    def _detected_person_count(self, image, confidence=0.40):
        return self._yolo_count(image, '_person_detector', '*/models/ultralytics/segm/person_yolov8m-seg.pt', confidence)

    def _detected_hand_count(self, image):
        return self._yolo_count(image, '_hand_detector', '*/models/ultralytics/bbox/hand_yolov8s.pt', 0.30)

    def _get_identity_analyser(self):
        if self._identity_analyser is not None:
            return self._identity_analyser
        from insightface.app import FaceAnalysis
        from pathlib import Path
        candidates = [
            self.service.root / 'models' / 'insightface',
            self.service.root / 'data' / 'models' / 'insightface',
            Path(__file__).resolve().parent.parent / 'data' / 'models' / 'insightface',
            Path.cwd() / 'data' / 'models' / 'insightface',
        ]
        if self.runtime and hasattr(self.runtime, 'backends') and self.runtime.backends:
            candidates.append(Path(self.runtime.backends) / 'models' / 'insightface')
            candidates.append(Path(self.runtime.backends) / 'ComfyUI' / 'models' / 'insightface')

        selected_root = None
        for candidate in candidates:
            if (candidate / 'models' / 'buffalo_l').is_dir():
                selected_root = str(candidate)
                break
            elif (candidate / 'buffalo_l').is_dir():
                selected_root = str(candidate.parent)
                break
        if selected_root is None:
            selected_root = str(self.service.root / 'models' / 'insightface')

        self._identity_analyser = FaceAnalysis(
            name='buffalo_l', root=selected_root,
            providers=['CPUExecutionProvider'])
        self._identity_analyser.prepare(ctx_id=-1, det_size=(320, 320))
        return self._identity_analyser

    def _human_portrait_references(self, snapshot):
        """Human face comparison accepts human references, including legacy snapshots."""
        from .subject_policy import enrich, policy
        references = []
        for reference in snapshot.get('references', []):
            if reference.get('role') != 'portrait':
                continue
            kind = reference.get('entity_type')
            if not kind and reference.get('character_id'):
                character = self.service.character(reference['character_id'])
                project = self.service.project(character['project_id'])
                kind = enrich([character], project.get('cast_bible'))[0]['entity_type']
            if policy({'entity_type': kind})['entity_type'] == 'human':
                references.append(reference['asset_id'])
        return references

    def _identity_similarity(self, image, reference_asset_id):
        """Return ArcFace cosine similarity, or None when a face is not usable."""
        try:
            import numpy as np
            from PIL import Image
            analyser = self._get_identity_analyser()

            def primary_face(value):
                faces = analyser.get(np.asarray(value.convert('RGB')))
                if not faces:
                    return None
                return max(faces, key=lambda face: float((face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])))

            if reference_asset_id not in self._identity_embeddings:
                asset = self.service.asset(reference_asset_id)
                with Image.open(self.service.root / asset['path']) as reference:
                    face = primary_face(reference)
                if face is None:
                    return None
                self._identity_embeddings[reference_asset_id] = face.embedding
            cand_faces = analyser.get(np.asarray(image.convert('RGB')))
            if not cand_faces:
                return None
            reference = self._identity_embeddings[reference_asset_id]
            ref_norm = np.linalg.norm(reference)
            scores = [float(np.dot(reference, f.embedding) / (ref_norm * np.linalg.norm(f.embedding))) for f in cand_faces]
            return max(scores) if scores else None
        except Exception as error:
            log.warning('Identity check unavailable: %s', error)
            return None

    def _identity_assignment_similarity(self, image, reference_asset_ids):
        """Return the weakest score in the best one-to-one face/reference assignment.

        A scene candidate is accepted only when every locked human identity has
        a distinct matching face. None means that the optional local face model
        could not produce reliable evidence, never a fabricated match.
        """
        if not reference_asset_ids:
            return None
        if len(reference_asset_ids) == 1:
            return self._identity_similarity(image, reference_asset_ids[0])
        try:
            import itertools
            import numpy as np
            from PIL import Image
            analyser = self._get_identity_analyser()

            def embedding_faces(value):
                return [face.embedding for face in analyser.get(
                    np.asarray(value.convert('RGB')))]

            candidate_embeddings = embedding_faces(image)
            if len(candidate_embeddings) < len(reference_asset_ids):
                return None
            reference_embeddings = []
            for asset_id in reference_asset_ids:
                if asset_id not in self._identity_embeddings:
                    asset = self.service.asset(asset_id)
                    with Image.open(self.service.root / asset['path']) as reference:
                        faces = embedding_faces(reference)
                    if not faces:
                        return None
                    self._identity_embeddings[asset_id] = max(
                        faces, key=lambda value: float(np.linalg.norm(value)))
                reference_embeddings.append(self._identity_embeddings[asset_id])

            scores = [[float(np.dot(ref, candidate) / (np.linalg.norm(ref) * np.linalg.norm(candidate)))
                       for candidate in candidate_embeddings]
                      for ref in reference_embeddings]
            best = None
            for assignment in itertools.permutations(range(len(candidate_embeddings)), len(reference_embeddings)):
                assigned = [scores[row][column] for row, column in enumerate(assignment)]
                candidate_score = min(assigned)
                if best is None or candidate_score > best:
                    best = candidate_score
            return best
        except Exception as error:
            log.warning('Identity assignment check unavailable: %s', error)
            return None

    def _identity_face_too_small(self, image, expected_faces=1):
        """True when detected faces are too small for a reliable ArcFace comparison."""
        try:
            import cv2
            import numpy as np
            faces = self._get_identity_analyser().get(
                cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2BGR))
            if not faces or len(faces) < expected_faces:
                return False
            areas = sorted(faces, key=lambda face: float(
                (face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])), reverse=True)
            # A face under 56 px in width or 64 px in height (at 1024x576)
            # is too small for a reliable ArcFace cosine similarity against a close-up portrait.
            scale = min(image.width / 1024.0, image.height / 576.0)
            min_w = max(36.0, 56.0 * scale)
            min_h = max(42.0, 64.0 * scale)
            return any((face.bbox[2] - face.bbox[0]) < min_w or
                       (face.bbox[3] - face.bbox[1]) < min_h
                       for face in areas[:expected_faces])
        except Exception as error:
            log.warning('Face-size check unavailable: %s', error)
            return False

    @staticmethod
    def _has_panel_grid(image):
        """Detect genuine separators between independently generated image panels.

        A plain full-frame dark or light feature is not enough evidence: wildlife
        shots often contain tree trunks, horizons, silhouettes and hard light.
        A panel seam must therefore be narrow, straight and colour-uniform, and
        must separate two visually discontinuous image regions on both sides.
        """
        import numpy as np
        pixels = np.asarray(image.convert('RGB'))
        channel_range = pixels.max(axis=2) - pixels.min(axis=2)
        neutral_divider = ((channel_range <= 35) &
                           ((pixels.min(axis=2) >= 210) | (pixels.max(axis=2) <= 28)))

        def is_confirmed_seam(axis, start, end):
            """Confirm that a candidate is a gutter, rather than scene content."""
            # Move the divider axis to x so the same geometry handles vertical
            # and horizontal candidates. The first dimension is the long edge.
            view = pixels if axis == 'vertical' else np.swapaxes(pixels, 0, 1)
            long_edge, short_edge = view.shape[:2]
            seam = view[:, start:end, :].astype(np.float32)

            # Generated contact-sheet gutters are flat across their whole long
            # edge. Natural objects and lighting can be dark, but vary in
            # texture, colour or thickness along that edge.
            seam_luma = seam.mean(axis=2)
            if float(seam_luma.std()) > 10.0 or float(seam.std(axis=(0, 1)).max()) > 14.0:
                return False

            side_width = max(3, min(short_edge // 32, short_edge // 12))
            if start < side_width or end + side_width > short_edge:
                return False
            left = view[:, start - side_width:start, :].astype(np.float32)
            right = view[:, end:end + side_width, :].astype(np.float32)
            seam_colour = seam.mean(axis=(0, 1))
            left_colour = left.mean(axis=(0, 1))
            right_colour = right.mean(axis=(0, 1))
            # A divider is visibly distinct from both panels. This excludes a
            # naturally dark stripe that merely belongs to one continuous shot.
            if (np.linalg.norm(seam_colour - left_colour) < 28 or
                    np.linalg.norm(seam_colour - right_colour) < 28):
                return False

            # Finally, compare the immediate regions across the candidate. A
            # real panel boundary breaks visual continuity; a thin in-scene
            # stripe frequently has matching colour/texture on both sides.
            side_gap = np.abs(left.mean(axis=1) - right.mean(axis=1)).mean()
            return bool(side_gap >= 12.0)

        def has_divider(coverage, length, axis):
            # Outer borders are framing, not a split. Panel gutters are inside
            # the composition, typically around a third or half of the canvas.
            margin = max(12, length // 5)
            candidates = coverage[margin:length - margin] >= 0.86
            if not candidates.any():
                return False
            run = 0
            max_divider_width = max(12, length // 64)
            for offset, value in enumerate(candidates):
                if value:
                    run += 1
                    continue
                if (2 <= run <= max_divider_width and
                        is_confirmed_seam(axis, margin + offset - run, margin + offset)):
                    return True
                run = 0
            if run and 2 <= run <= max_divider_width:
                return is_confirmed_seam(axis, length - margin - run, length - margin)
            return False

        # A diptych has only one divider, while a triptych can have two; both
        # are invalid. Architectural lines almost never span 86% of a frame as
        # a neutral, uniform divider.
        return (has_divider(neutral_divider.mean(axis=0), image.width, 'vertical') or
                has_divider(neutral_divider.mean(axis=1), image.height, 'horizontal'))

    @staticmethod
    def _has_embedded_text(image):
        """Reject subtitle overlays and speech bubbles, not ordinary in-world text.

        A shop sign, a book cover, or a label on an object belongs to the scene and
        must not make the whole render fail.  The gate therefore looks only for the
        visual grammar of overlays: a long line of glyphs in the caption safe area,
        or a large bright rounded speech bubble containing dark glyph strokes.
        """
        try:
            import cv2
            import numpy as np
            img = np.asarray(image.convert('RGB'))
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            h, w = gray.shape

            # Subtitles are usually an overlay in the lower safe area.  This pass
            # catches the common white variant while keeping the search narrow so
            # storefronts, signs, maps and other diegetic text remain valid.
            caption_top = int(h * 0.70)
            caption = gray[caption_top:, :]
            grad_x = cv2.Sobel(caption, cv2.CV_32F, 1, 0, ksize=3)
            mag_x = np.uint8(np.clip(np.abs(grad_x), 0, 255))
            _, stroke_bin = cv2.threshold(mag_x, 45, 255, cv2.THRESH_BINARY)
            kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 3))
            connected = cv2.morphologyEx(stroke_bin, cv2.MORPH_CLOSE, kernel_h)
            contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, bw, bh = cv2.boundingRect(contour)
                if not (6 <= bh <= max(48, int(h * 0.12)) and bw >= max(90, int(w * 0.15))):
                    continue
                if bw / float(bh) < 2.5:
                    continue
                crop = stroke_bin[y:y + bh, x:x + bw]
                transitions = np.count_nonzero(np.diff(crop.astype(np.int16), axis=1))
                color = img[caption_top + y:caption_top + y + bh, x:x + bw]
                pale_neutral = np.mean((np.min(color, axis=2) > 200) &
                                       ((np.max(color, axis=2) - np.min(color, axis=2)) < 35))
                if (transitions / float(max(1, bh)) >= 10 and
                        transitions / float(max(1, bw * bh)) >= 0.045 and
                        pale_neutral >= 0.03):
                    # Edges on snow, stone and illustrated clothing also form
                    # long horizontal runs. Require pale foreground glyphs,
                    # rather than counting arbitrary light background pixels.
                    pale = ((np.min(color, axis=2) > 200) &
                            ((np.max(color, axis=2) - np.min(color, axis=2)) < 35))
                    _, _, parts, _ = cv2.connectedComponentsWithStats(
                        pale.astype(np.uint8))
                    glyphs = [part for part in parts[1:]
                              if max(2, int(bh * 0.20)) <= part[cv2.CC_STAT_HEIGHT] <= bh
                              and 1 <= part[cv2.CC_STAT_WIDTH] <= bh * 1.6
                              and 3 <= part[cv2.CC_STAT_AREA] <= bw * bh * 0.12]
                    # A subtitle has several letters on a common baseline.
                    # A broad bright patch with scattered dark strokes does not.
                    tolerance = max(2, int(bh * 0.15))
                    for glyph in glyphs:
                        baseline = glyph[cv2.CC_STAT_TOP] + glyph[cv2.CC_STAT_HEIGHT]
                        aligned = [part for part in glyphs
                                   if abs(part[cv2.CC_STAT_TOP] + part[cv2.CC_STAT_HEIGHT]
                                          - baseline) <= tolerance]
                        if len(aligned) >= 6:
                            return True

            # Generators also produce cyan, yellow and softly glowing captions.
            # Their colour separates them from ordinary dark ink, so join
            # same-colour glyphs into a line in the caption-safe band.
            # Requiring a very wide, thin, centred run avoids rejecting words on
            # books, maps, wall signs and other physical surfaces.
            rgb_band_top = int(h * 0.70)
            color_band = img[rgb_band_top:, :]
            hsv = cv2.cvtColor(color_band, cv2.COLOR_RGB2HSV)
            value = hsv[:, :, 2]
            saturation = hsv[:, :, 1]
            channel_spread = np.max(color_band, axis=2) - np.min(color_band, axis=2)
            luminous_color = ((value > 145) & (saturation < 170) &
                               (channel_spread > 8)).astype(np.uint8) * 255
            color_kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT, (max(13, w // 25), 3))
            color_lines = cv2.morphologyEx(
                luminous_color, cv2.MORPH_CLOSE, color_kernel, iterations=2)
            color_contours, _ = cv2.findContours(
                color_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in color_contours:
                x, y, bw, bh = cv2.boundingRect(contour)
                if not (8 <= bh <= max(48, int(h * 0.09)) and
                        bw >= max(150, int(w * 0.32)) and
                        bw / float(max(1, bh)) >= 5.0):
                    continue
                centre_x = x + bw / 2.0
                if not w * 0.25 <= centre_x <= w * 0.75:
                    continue
                raw_line = luminous_color[y:y + bh, x:x + bw]
                density = np.mean(raw_line > 0)
                _, _, component_stats, _ = cv2.connectedComponentsWithStats(raw_line)
                glyph_parts = [stats for stats in component_stats[1:]
                               if 2 <= stats[cv2.CC_STAT_AREA] <= 300 and
                               2 <= stats[cv2.CC_STAT_HEIGHT] <= bh]
                glyph_density = sum(stats[cv2.CC_STAT_AREA] for stats in glyph_parts) / float(bw * bh)
                if (0.045 <= density <= 0.42 and len(glyph_parts) >= 20 and
                        glyph_density >= 0.04):
                    return True

            # Whispered narration is occasionally painted as pale floating text
            # near the centre of a frame.  It is neither a subtitle nor physical
            # writing, so catch only strong text-shaped neutral clusters here.
            middle_top = int(h * 0.30)
            middle_band = img[middle_top:, :]
            middle_hsv = cv2.cvtColor(middle_band, cv2.COLOR_RGB2HSV)
            middle_spread = np.max(middle_band, axis=2) - np.min(middle_band, axis=2)
            middle_mask = ((middle_hsv[:, :, 2] > 145) &
                           (middle_hsv[:, :, 1] < 170) &
                           (middle_spread > 8)).astype(np.uint8) * 255
            middle_lines = cv2.morphologyEx(
                middle_mask, cv2.MORPH_CLOSE, color_kernel, iterations=2)
            middle_contours, _ = cv2.findContours(
                middle_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in middle_contours:
                x, y, bw, bh = cv2.boundingRect(contour)
                if not (8 <= bh <= max(69, int(h * 0.12)) and
                        bw >= max(250, int(w * 0.25)) and
                        bw / float(max(1, bh)) >= 4.0):
                    continue
                raw_line = middle_mask[y:y + bh, x:x + bw]
                _, _, component_stats, _ = cv2.connectedComponentsWithStats(raw_line)
                glyph_parts = [stats for stats in component_stats[1:]
                               if 2 <= stats[cv2.CC_STAT_AREA] <= 300 and
                               2 <= stats[cv2.CC_STAT_HEIGHT] <= bh]
                glyph_density = sum(stats[cv2.CC_STAT_AREA] for stats in glyph_parts) / float(bw * bh)
                selected = raw_line > 0
                mean_saturation = np.mean(
                    middle_hsv[y:y + bh, x:x + bw, 1][selected]) if selected.any() else 255
                if len(glyph_parts) >= 30 and glyph_density >= 0.05 and mean_saturation < 65:
                    return True

            # A dialogue bubble is a smooth, pale, enclosed region with a sizeable
            # concentration of dark glyph-like strokes inside it. Rectangular documents,
            # books, notes, signs and maps are excluded by polygon vertex count and circularity.
            blurred = cv2.GaussianBlur(gray, (7, 7), 0)
            bright = cv2.inRange(blurred, 230, 255)
            bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE,
                                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)))
            bubbles, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in bubbles:
                x, y, bw, bh = cv2.boundingRect(contour)
                area = cv2.contourArea(contour)
                perimeter = cv2.arcLength(contour, True)
                if perimeter == 0:
                    continue
                if (x <= 2 or y <= 2 or x + bw >= w - 2 or y + bh >= h - 2 or
                        area < h * w * 0.008 or area > h * w * 0.30 or
                        not 1.1 <= bw / float(max(1, bh)) <= 4.5):
                    continue
                # Quadrilateral = paper, card, note, book, or sign -> accept
                approx = cv2.approxPolyDP(contour, 0.035 * perimeter, True)
                if len(approx) == 4:
                    continue
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity < 0.58:
                    continue
                fill_ratio = area / float(max(1, bw * bh))
                if fill_ratio < 0.55 or fill_ratio > 0.92:
                    continue
                inner = gray[y:y + bh, x:x + bw]
                dark = np.count_nonzero(inner < 110)
                # Text must occupy a meaningful but not dominant part of bubble.
                if dark >= max(35, int(area * 0.008)) and dark <= int(area * 0.28):
                    return True
            return False
        except Exception as err:
            # No broad fallback: a heuristic which cannot distinguish text painted
            # on an object from a subtitle must accept the frame.  Generation
            # prompts still forbid captions, and a functioning OpenCV detector is
            # used in the packaged application.
            log.warning('Overlay detector unavailable; accepting frame (%s)', err)
            return False

    @staticmethod
    def _has_embedded_frame_or_matte(image):
        from .frame_matte import detect_matte
        return detect_matte(image)['detected']

    @staticmethod
    def _validate_matte(image):
        from .frame_matte import detect_matte
        evidence=detect_matte(image)
        if evidence['detected']:
            error=ContentRejectedError('Кадр содержит встроенные поля или рамку и не заполняет холст 16:9. Кадр не принят.',422)
            error.details={'code':'COMPOSITION_MATTE','retryable':True,'detector':evidence}
            raise error

    @staticmethod
    def _advisory_quality(snapshot):
        return (snapshot.get('generation_role') == 'reference' or
                bool((snapshot.get('scene') or {}).get('forgotten_cities_prompt_contract')))

    def validate_external_scene_image(self, snapshot, data):
        from PIL import Image
        # File integrity and dimensions are required even when content review is advisory.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.size != (snapshot['profile']['width'], snapshot['profile']['height']):
                raise ContentRejectedError('Размер изображения не соответствует профилю.')
        if snapshot.get('generation_role') == 'reference':
            return
        if not visual_reviews_enabled() and self._advisory_quality(snapshot):
            return
        try:
            self._validate_external_scene_image_strict(snapshot, data)
        except Exception:
            if not self._advisory_quality(snapshot):
                raise
            snapshot['quality_review'] = {'status': 'unverified', 'reason': 'Проверка композиции не подтверждена.'}

    def _review_scene_identity(self, snapshot, image):
        """Scene identity checks annotate images; they never veto or retry them."""
        review = {'version': 'scene-identity-advisory/1', 'enforcement': 'advisory',
                  'method': 'reference_face_similarity', 'status': 'unverified'}
        try:
            references = self._human_portrait_references(snapshot)
            review['human_references'] = len(references)
            if not references:
                review.update(status='not_applicable', reason='Нет эталонов человеческих лиц для сравнения.')
            else:
                similarity = (self._identity_similarity(image, references[0]) if len(references) == 1
                              else self._identity_assignment_similarity(image, references))
                small = self._identity_face_too_small(image, len(references))
                threshold = 0.15 if small else (0.45 if len(references) == 1 else 0.38)
                review.update(threshold=threshold, small_face=bool(small))
                import math
                from numbers import Real
                if not isinstance(similarity, Real) or isinstance(similarity, bool) or not math.isfinite(similarity):
                    review.update(similarity=None, reason='Не удалось уверенно сравнить лицо с эталоном. Кадр сохранён для просмотра.')
                elif similarity < threshold:
                    review.update(status='needs_review', similarity=float(similarity),
                        reason='Проверьте сходство героя с эталоном: автоматическая оценка низкая. Кадр сохранён.')
                else:
                    review.update(status='not_flagged', similarity=float(similarity),
                        reason='Проверка не отметила низкого сходства; окончательная оценка остаётся за пользователем.')
        except Exception as error:
            log.warning('Advisory scene identity check unavailable (%s)', type(error).__name__)
            review.update(error_type=type(error).__name__,
                reason='Автоматическая проверка сходства недоступна. Кадр сохранён; проверьте героя визуально.')
        snapshot['identity_review'] = review
        return review

    def _review_scene_geometry(self, snapshot, image):
        from .composition_review import record_population_review
        counts, errors = {}, []
        for key, detector in (('faces', self._detected_face_count),
                              ('people', self._detected_person_count),
                              ('hands', self._detected_hand_count)):
            try:
                counts[key] = detector(image)
            except (DetectorUnavailableError, ContentRejectedError) as error:
                counts[key] = None
                errors.append(type(error).__name__)
        return record_population_review(snapshot, counts, method='yolo_geometry', errors=errors)

    def _validate_external_scene_image_strict(self, snapshot, data):
        """Apply the FrameForge composition contract to an image returned by Flow."""
        from PIL import Image
        expected = (snapshot['profile']['width'], snapshot['profile']['height'])
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.size != expected:
                raise ContentRejectedError(f'Flow вернул {image.size}, ожидалось {expected}. Кадр не принят.')
            image = image.convert('RGB')
            if self._has_panel_grid(image):
                raise ContentRejectedError('Кадр выглядит как разделение на несколько сцен или панелей. Кадр не принят.')
            self._validate_matte(image)
            if self._has_embedded_text(image):
                raise ContentRejectedError('В кадре обнаружены субтитры или диалоговое облачко. Кадр не принят.')
            # This contract is intentionally explicit so the Flow bridge can
            # retry semantic composition failures without exposing them as
            # completed frames.  A future OCR/vision validator can add its
            # verdict here using the same error vocabulary.
            if self._advisory_quality(snapshot):
                return  # Source-aware pixel review replaces rigid people/hand counts.
            from .portrait_contract import marker_scene_review
            if marker_scene_review(snapshot,image):
                return
            self._review_scene_geometry(snapshot, image)
            self._review_scene_identity(snapshot, image)

    def _validate_generated_composition(self, generation, snapshot, image):
        if not visual_reviews_enabled() and self._advisory_quality(snapshot):
            return
        if self._has_panel_grid(image):
            raise ContentRejectedError('Кадр выглядит как сетка из нескольких панелей. Кадр не принят.')
        self._validate_matte(image)
        if self._has_embedded_text(image):
            raise ContentRejectedError('В кадре обнаружены субтитры или диалоговое облачко. Кадр не принят.')
        if self._advisory_quality(snapshot):
            return  # Source-aware pixel review replaces rigid people/hand counts.
        expected_people = int(snapshot.get('expected_people', 0))
        exact_people_count = not snapshot.get('people_count_unresolved')
        from .portrait_contract import validate_portrait, marker_scene_review
        from .visual_style import is_marker_style
        if is_marker_style(snapshot.get('style')):
            if generation['purpose'] == 'casting':
                snapshot['portrait_review'] = validate_portrait(image,snapshot)
            else:
                marker_scene_review(snapshot,image)
            return
        if generation['purpose'] == 'casting':
            faces = self._detected_face_count(image)
            if faces != 1:
                raise ContentRejectedError('Референс персонажа должен содержать ровно одно лицо без посторонних людей. Кадр не принят.')
        if generation['purpose'] == 'scene':
            self._review_scene_geometry(snapshot, image)
        # Strict geometry gates below belong to isolated reference assets.
        # Story-frame detector proposals are evidence for review, not a census.
        # Validate the composition by both faces and whole-person silhouettes.
        # A profile, a person seen from behind, or a tight close-up may hide
        # one of those signals, therefore absence is not a failure. Any
        # detector finding *more* humans than declared is a concrete error.
        if generation['purpose'] not in ('casting', 'scene') and (snapshot.get('uninhabited') or exact_people_count and expected_people):
            # Face detection is useful for story frames but produces false
            # positives on lamps, curtains and decor in empty interiors.
            # A location reference still rejects a detected full person;
            # its prompt independently bans portraits and reflections.
            # Interior furniture is occasionally a low-confidence person
            # false positive. Use a stronger threshold only for empty
            # location anchors; story frames keep the stricter 0.40 check.
            people = (self._detected_person_count(image, 0.65)
                      if generation['purpose'] == 'location' else self._detected_person_count(image))
            faces = 0 if generation['purpose'] == 'location' else self._detected_face_count(image)
            if snapshot.get('uninhabited') and (faces or people):
                raise ContentRejectedError('В пустой сцене найден человек. Кадр не принят.')
            if exact_people_count and expected_people and (faces > expected_people or people > expected_people):
                error = ContentRejectedError('Количество людей не совпадает с составом сцены. Кадр не принят.')
                error.details = {'code': 'COMPOSITION_PEOPLE_COUNT', 'expected': expected_people,
                                 'detected_faces': faces, 'detected_people': people}
                raise error
            # When both detectors recognise the foreground, require that at
            # least one proves the declared composition. This accepts a back
            # view, close-up or cropped two-person shot without accepting a
            # blank landscape for a human scene.
            if exact_people_count and expected_people and max(faces, people) < expected_people:
                raise ContentRejectedError('Не удалось подтвердить нужное число людей в сцене. Кадр не принят.')
        if generation['purpose'] == 'location' and self._detected_hand_count(image):
            raise ContentRejectedError('В референсе локации найдены лишние руки или части тела. Кадр не принят.')
        if generation['purpose'] != 'scene' and exact_people_count and expected_people and self._detected_hand_count(image) > expected_people * 2:
            raise ContentRejectedError('В кадре найдено больше рук, чем допускает состав персонажей. Кадр не принят.')
        if generation['purpose'] == 'scene':
            self._review_scene_identity(snapshot, image)
            return
        portrait_references = self._human_portrait_references(snapshot)
        if portrait_references:
            if len(portrait_references) == 1:
                similarity = self._identity_similarity(image, portrait_references[0])
            else:
                similarity = self._identity_assignment_similarity(image, portrait_references)
            is_small = self._identity_face_too_small(image, len(portrait_references))
            threshold = 0.15 if is_small else (0.45 if len(portrait_references) == 1 else 0.38)
            if (similarity is not None and similarity < threshold) or (similarity is None and not is_small):
                error = ContentRejectedError(
                    'Лицо в кадре недостаточно похоже на зафиксированного персонажа. Кадр не принят.')
                error.details = {'code': 'COMPOSITION_IDENTITY', 'human_references': len(portrait_references),
                                 'similarity': similarity, 'threshold': threshold, 'small_face': is_small}
                raise error

    def save_image(self, generation, data):
        from PIL import Image
        snapshot = json.loads(generation['snapshot'])
        snapshot.setdefault('scene_id', generation.get('scene_id'))
        snapshot.setdefault('scene_revision', generation.get('scene_revision'))
        expected = (snapshot['profile']['width'], snapshot['profile']['height'])
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.size != expected:
                raise DomainError(f'Backend вернул {image.size}, ожидалось {expected}. Файл не принят; проверьте workflow.')
            output = io.BytesIO()
            image = image.convert('RGB')
            if snapshot.get('generation_role') != 'reference':
                try:
                    self._validate_generated_composition(generation, snapshot, image)
                except Exception:
                    if not self._advisory_quality(snapshot):
                        raise
                    snapshot['quality_review'] = {'status': 'unverified', 'reason': 'Проверка композиции не подтверждена.'}
            image.save(output, format='PNG')
            data = output.getvalue()
        if generation['purpose'] == 'scene':
            snapshot['project_id'] = generation['project_id']
            snapshot['fidelity_candidate_id'] = f"{generation['id']}:{generation['attempts']}"
            self.service.validate_scene_source_image(snapshot, data)
        self.update(generation, snapshot=snapshot)
        asset_id = uid()
        relative = f"projects/{generation['project_id']}/{generation['id']}.png"
        path = self.service.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
        metadata = {**generation, 'snapshot': snapshot, 'sha256': hashlib.sha256(data).hexdigest(),
                    'output_size': expected, 'saved_at': now(), 'state': 'succeeded', 'error': None}
        metadata_dir = self.service.root / 'metadata'
        metadata_dir.mkdir(exist_ok=True)
        metadata_path = metadata_dir / (generation['id'] + '.json')
        metadata_tmp = metadata_path.with_suffix('.tmp')
        metadata_tmp.write_text(dump(metadata), encoding='utf-8')
        metadata_tmp.replace(metadata_path)
        with self.db.transaction() as connection:
            current = connection.execute('SELECT cancel_requested FROM generations WHERE id=?', (generation['id'],)).fetchone()
            state = 'cancelled' if (current and current['cancel_requested']) else 'succeeded'
            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, generation['project_id'], generation['id'], relative, hashlib.sha256(data).hexdigest(), *expected, len(data)))
            connection.execute('UPDATE generations SET state=?,asset_id=?,finished_at=?,error=NULL,progress=? WHERE id=?',
                               (state, asset_id, now(), dump({'stage': 'Кадр готов' if state == 'succeeded' else 'Отменено',
                                                               'percent': 100 if state == 'succeeded' else 0}), generation['id']))
            # Human selects final images; even a technically successful result is not implicitly approved.
            self.db.event(connection, generation['project_id'], 'generation_completed', {'id': generation['id'], 'state': state})
        self.refresh_batches()
        from .source_corrections import select_checked
        select_checked(self.service,generation['id'])

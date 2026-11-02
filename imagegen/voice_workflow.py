"""Durable voice-first project preparation and optional production orchestration."""
import copy
import hashlib
import json
import logging
import math
import subprocess
import threading
import time
from pathlib import Path
from .db import dump, now, uid
from .domain import DomainError, MAX_SCENES
from .editing import normalize
from .voice_alignment import build_report
from .voice_timing import timed_frames, verify_plan

log = logging.getLogger(__name__)
ACTIVE = ('queued', 'recognizing', 'analyzing', 'aligning', 'images', 'animating', 'assembling', 'exporting')


def public(row):
    if not row:
        return None
    row = dict(row)
    row['options'] = json.loads(row.pop('options_json'))
    row['result'] = json.loads(row.pop('result_json'))
    return row


class VoiceWorkflow:
    def __init__(self, service, exports=None, bridge=None):
        self.service, self.db = service, service.db
        self.exports, self.bridge = exports, bridge
        from .release_paths import component_root
        self.base = component_root()
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        from .voice_editorial import VoiceEditorial
        self.editorial = VoiceEditorial(self)

    def capability(self):
        python = self.base / 'backends' / 'voice-venv' / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')
        model = self.base / 'models' / 'voice' / 'turbo'
        ready = python.is_file() and all((model / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json'))
        return {'ready': ready, 'message': 'Распознавание речи на ПК готово.' if ready else
                'Локальное распознавание речи не установлено. Запустите InstallVoice.ps1.'}

    def start_worker(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE voice_workflows SET state='paused',stage='Работа прервана перезапуском. Нажмите «Продолжить».',updated_at=? WHERE state IN (" + ','.join('?' for _ in ACTIVE) + ')', (now(), *ACTIVE))
        self.thread = threading.Thread(target=self.run, daemon=True, name='voice-workflow')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)

    def get(self, pid):
        self.service.project(pid)
        return public(self.db.one('SELECT * FROM voice_workflows WHERE project_id=?', (pid,)))

    def narration_text(self, pid):
        from .voice_source import narration_text, link_destinations
        story=self.service.project(pid)['story']
        return {'text':narration_text(story),'source_sha256':hashlib.sha256(story.encode()).hexdigest(),
                'link_count':len(link_destinations(story))}

    def update(self, pid, **values):
        values['updated_at'] = now()
        for key in ('options_json', 'result_json'):
            if key in values:
                values[key] = dump(values[key])
        with self.db.transaction() as conn:
            conn.execute('UPDATE voice_workflows SET ' + ','.join(k + '=?' for k in values) + ' WHERE project_id=?', (*values.values(), pid))

    def start(self, pid, data):
        with self.lock:
            existing = self.get(pid)
            if existing:
                raise DomainError('Процесс уже сохранён. Используйте «Продолжить» или создайте новый проект.', 409)
            project = self.service.project(pid)
            asset = self.service.asset(data.get('audio_asset_id'))
            job = self.db.one('SELECT purpose FROM generations WHERE id=?', (asset['generation_id'],))
            if asset['project_id'] != pid or job['purpose'] != 'edit_audio':
                raise DomainError('Выберите озвучку этого проекта.')
            if not project['story'].strip():
                raise DomainError('Добавьте сценарий.')
            versioned=data.get('frame_content_focus') is True or data.get('frames_only') is True or data.get('preserve_existing') is True or self.service.settings().get('source_contract_enabled') is True
            if not versioned:self.assert_replaceable(pid)
            if not self.capability()['ready']:
                raise DomainError(self.capability()['message'], 503)
            limit = data.get('max_shot_seconds', 7)
            if type(limit) not in (int, float) or not 4 <= limit <= 20:
                raise DomainError('Длительность кадра: от 4 до 20 секунд.')
            options = {'auto_render': data.get('auto_render') is True and data.get('frame_content_focus') is not True,
                'frame_content_focus':data.get('frame_content_focus') is True, 'max_shot_seconds': limit,
                'frames_only':versioned,'versioned_plan':versioned,
                'preserve_existing': data.get('preserve_existing') is True}
            with self.db.transaction() as conn:
                conn.execute('INSERT INTO voice_workflows(project_id,audio_asset_id,story_sha256,state,stage,progress,options_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (pid, asset['id'], hashlib.sha256(project['story'].encode()).hexdigest(), 'queued',
                     'Озвучка загружена. Подготовка плана в очереди.', 0, dump(options), '{}', now(), now()))
            return self.get(pid)

    def control(self, pid, action):
        with self.lock:
            job = self.get(pid)
            if not job:
                raise DomainError('Сначала загрузите озвучку.', 409)
            if action == 'pause':
                self.update(pid, pause_requested=1, stage='Пауза после текущей операции…')
                if job['state'] in ('waiting_reference','reference_review_ready','reference_review_blocked'):
                    self.update(pid,state='paused',stage='На паузе. Проверки и эталоны сохранены.')
                bid = job['result'].get('batch_id')
                if bid:
                    self.service.control_batch(bid, 'pause')
                if self.bridge:
                    try: self.bridge('pause', pid, None)
                    except DomainError: pass
            elif action in ('resume', 'produce'):
                if job['state'] in ACTIVE:
                    raise DomainError('Работа уже выполняется.', 409)
                if job['state'] == 'completed':
                    return job
                options = job['options']
                if action == 'produce':
                    if not job['result'].get('planned'):
                        raise DomainError('Сначала дождитесь подготовки плана.', 409)
                    options['auto_render'] = True
                bid = job['result'].get('batch_id')
                if bid:
                    self.service.control_batch(bid, 'resume')
                self.update(pid, state='queued', options_json=options, pause_requested=0, error=None, stage='Продолжаю сохранённый процесс…')
            else:
                raise DomainError('Неизвестная команда.')
            return self.get(pid)

    def replan(self,pid,data):
        with self.lock:
            job=self.get(pid)
            if not job:raise DomainError('Сначала загрузите озвучку.',409)
            if job['state'] in ACTIVE:raise DomainError('Дождитесь завершения текущей операции.',409)
            project=self.service.project(pid)
            if type(data.get('project_version')) is not int or data['project_version']!=project['version']:
                raise DomainError('Проект изменился. Обновите страницу.',409)
            from .voice_plans import VoicePlans
            asset=VoicePlans(self).audio(pid,data.get('audio_asset_id') or job['audio_asset_id'])
            options={**job['options'],'frames_only':True,'versioned_plan':True,'auto_render':False,
                'frame_content_focus':data.get('frame_content_focus',job['options'].get('frame_content_focus')) is True}
            result={**job['result'],'planned':False,'candidate_plan_id':None}
            self.update(pid,state='queued',pause_requested=0,error=None,audio_asset_id=asset['id'],
                story_sha256=hashlib.sha256(project['story'].encode()).hexdigest(),options_json=options,result_json=result,
                stage='Готовлю новую версию. Текущие сцены сохраняются.')
            return self.get(pid)

    def apply_plan(self,pid,plan_id):
        from .voice_plans import VoicePlans
        with self.lock:
            before=self.get(pid)
            already=before and before['result'].get('active_plan_id')==plan_id
            job=VoicePlans(self).apply(pid,plan_id)
            if not already and job['options'].get('auto_render'):
                self.update(pid,state='queued',stage='План применён. Подготовка изображений в очереди.')
            return self.get(pid)

    def assert_replaceable(self, pid, *, preserve_references=False):
        allowed=('analysis','edit_audio','character','location','prop') if preserve_references else ('analysis','edit_audio')
        if self.db.one('SELECT id FROM generations WHERE project_id=? AND purpose NOT IN ('+
                ','.join('?' for _ in allowed)+') LIMIT 1', (pid,*allowed)):
            raise DomainError('В проекте уже есть изображения или видео. Для планирования по озвучке создайте новый проект.', 409)
        if preserve_references and self.db.one('SELECT id FROM scenes WHERE project_id=? AND (active_generation_id IS NOT NULL OR video_asset_id IS NOT NULL) LIMIT 1',(pid,)):
            raise DomainError('Готовые изображения и видео сцен требуют дополнения с сохранением исходных кадров.',409)
        if self.service.editing.get(pid)['document']['clips']:
            raise DomainError('В проекте уже есть монтаж. Создайте новый проект для озвучки.', 409)

    def check(self, pid):
        row = self.get(pid)
        if self.stop_event.is_set() or row['pause_requested']:
            raise InterruptedError()
        if hashlib.sha256(self.service.project(pid)['story'].encode()).hexdigest() != row['story_sha256']:
            raise DomainError('Сценарий изменился во время обработки. Создайте новый проект.', 409)
        return row

    def run(self):
        while not self.stop_event.is_set():
            if not self.tick():
                self.stop_event.wait(1)

    def tick(self):
        from .google_reference_reviews import ReferenceReviewPending
        row = self.db.one("SELECT project_id FROM voice_workflows WHERE state='queued' ORDER BY created_at LIMIT 1")
        if not row:
            return False
        pid = row['project_id']
        try:
            self.prepare(pid)
            if self.check(pid)['result'].get('candidate_plan_id'):
                self.update(pid,state='plan_review',stage='Новая версия плана готова отдельно. Просмотрите и примените её.',progress=100)
                return True
            if self.check(pid)['options']['auto_render']:
                if self.check(pid)['options'].get('frames_only'):self.produce_frames(pid)
                else:self.produce(pid)
            else:
                self.update(pid, state='ready', stage='План по озвучке готов', progress=100)
        except ReferenceReviewPending as error:
            pending=error.record
            blocked=pending['state'] not in ('queued','running','waiting_registry','waiting_model','waiting_quota','retry_wait')
            result=self.get(pid)['result']
            result['reference_review_wait']={'state':pending['state'],'job_id':pending.get('job_id'),
                'asset_id':pending['binding']['asset_id'],'next_retry_at':pending.get('next_at')}
            self.update(pid,state='reference_review_blocked' if blocked else 'waiting_reference',result_json=result,
                stage='Проверка эталона требует внимания.' if blocked else 'Ожидаю проверку сохранённого эталона…',error=None)
        except InterruptedError:
            self.update(pid, state='paused', stage='На паузе. Готовые результаты сохранены.')
        except Exception as error:
            log.exception('Voice workflow stopped for %s', pid)
            self.update(pid, state='failed', error=str(error), stage=str(error))
        return True

    def recognize(self, pid, asset, story):
        directory = self.service.root / 'voice' / pid
        directory.mkdir(parents=True, exist_ok=True)
        story_path = directory / 'story.txt'
        story_path.write_text(story, encoding='utf-8')
        cache = self.service.root / 'voice' / 'asr'
        cache.mkdir(parents=True, exist_ok=True)
        output = cache / (asset['sha256'] + '.json')
        if output.exists():
            result = json.loads(output.read_text(encoding='utf-8'))
            from .voice_asr import repair_cache_matches
            if repair_cache_matches(result, story):
                return result
        python = self.base / 'backends' / 'voice-venv' / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')
        progress = directory / 'speech-progress.json'
        self.update(pid, state='recognizing', progress=2, stage='Сопоставляю слова с речью на ПК…')
        with (directory / 'speech.log').open('w', encoding='utf-8') as logfile:
            process = subprocess.Popen([str(python), '-m', 'imagegen.voice_asr', '--audio', str(self.service.root / asset['path']),
                '--model', str(self.base / 'models/voice/turbo'), '--output', str(output), '--progress', str(progress), '--story', str(story_path)],
                cwd=self.base, stdout=logfile, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                while process.poll() is None:
                    self.check(pid)
                    if progress.exists():
                        p = json.loads(progress.read_text(encoding='utf-8'))
                        fraction = min(1, p['processed_seconds'] / max(1, p['total_seconds']))
                        stage = (f"Перепроверяю короткие фрагменты: {p['repair_current']} из {p['repair_total']}"
                                 if p.get('repair_total') else
                                 f"Сопоставляю речь: {round(p['processed_seconds'])} из {round(p['total_seconds'])} секунд")
                        self.update(pid, progress=2 + 43 * fraction, stage=stage)
                    self.stop_event.wait(1)
                if process.returncode:
                    raise DomainError('Распознавание речи завершилось с ошибкой. Подробности сохранены в журнале проекта.', 503)
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=10)
        return json.loads(output.read_text(encoding='utf-8'))

    def prepare(self, pid):
        job = self.check(pid)
        if job['result'].get('planned'):
            return
        if job['result'].get('candidate_plan_id'):return
        if not job['options'].get('versioned_plan'):self.assert_replaceable(pid)
        asset = self.service.asset(job['audio_asset_id'])
        if job['options'].get('versioned_plan'):
            from .voice_plans import VoicePlans
            asset=VoicePlans(self).audio(pid,asset['id'])
        project = self.service.project(pid)
        asr = self.recognize(pid, asset, project['story'])
        report = build_report({'project': project, 'scenes': []}, asr)
        metrics = report['metrics']
        expansion = job['options'].get('preserve_existing') is True
        mismatch = any(w.get('number_mismatch') for w in report['words'])
        if ((metrics['methods']['unmatched'] and not expansion) or
                metrics['acoustic_coverage'] < (.95 if expansion else .9) or
                metrics['backwards_word_starts'] or (expansion and mismatch) or report['unsupported_passages']):
            issues=[]
            for w in report['words']:
                if w['method'] not in ('unmatched','interpolated'):continue
                reason=('Число в распознавании отличается от сценария' if w.get('number_mismatch') else
                        'Нет прямой опоры в распознавании' if w['method']=='interpolated' else 'Не определено время слова')
                if issues and issues[-1]['reason']==reason and not project['story'][issues[-1]['end']:w['char_start']].strip():
                    issues[-1]['end']=w['char_end'];issues[-1]['quote']=project['story'][issues[-1]['start']:w['char_end']]
                else:issues.append({'start':w['char_start'],'end':w['char_end'],'quote':w['text'],'reason':reason})
            self.update(pid,result_json={**job['result'],'alignment_issues':issues,'alignment_metrics':metrics,
                'alignment_unsupported_passages':report['unsupported_passages'],
                'alignment_nonspoken_spans':report['nonspoken_spans']})
            fragments='; '.join(i['reason']+': «'+i['quote']+'»' for i in issues[:4])
            raise DomainError('Сопоставление сценария и озвучки требует уточнения. '+(fragments+'. ' if fragments else '')+'Точное время не подтверждено; текущий план сохранён, изображения не запущены.', 409)
        self.update(pid, state='analyzing', progress=46, stage='Анализирую смысл сцен…')
        job['result'].pop('alignment_issues', None)
        job['result'].pop('alignment_metrics', None)
        job['result'].pop('alignment_unsupported_passages', None)
        job['result'].pop('alignment_nonspoken_spans', None)
        self.update(pid, result_json=job['result'])
        detail = self.service.detail(pid)
        if not detail['scenes']:
            active = self.db.one("SELECT id FROM generations WHERE project_id=? AND purpose='analysis' AND state IN ('queued','preparing','submitted','running','saving','reconciling','waiting_review') ORDER BY created_at DESC LIMIT 1", (pid,))
            if not active:
                target=min(MAX_SCENES,max(1,math.ceil(asr['audio_duration']/8)))
                with self.db.transaction() as conn:
                    gid = self.service.insert_generation(conn, pid, 'analysis', {'profile': self.service.settings(), 'project': project,
                        'target': target, 'story_analyzer_version': 'v3'})
            else:
                gid = active['id']
            while True:
                self.check(pid)
                gen = self.db.one('SELECT state,error,progress,snapshot FROM generations WHERE id=?', (gid,))
                if gen['state'] == 'succeeded': break
                if gen['state'] in ('failed','cancelled','resource_degraded'):
                    raise DomainError(gen['error'] or 'Разбор сценария прерван.', 409)
                if gen['state'] == 'waiting_review':
                    wait = json.loads(gen.get('snapshot') or '{}').get('plan_review_wait') or {}
                    if wait.get('state') in ('error_response','error_request','error_access','budget_too_small','transport_unknown','cancelled'):
                        self.update(pid,result_json={**job['result'],'plan_review_wait':wait})
                        raise DomainError('Заключительная проверка плана остановлена. Разбор сохранён; требуется восстановить проверку.',409)
                p = json.loads(gen['progress'] or '{}')
                self.update(pid, progress=46 + .4 * float(p.get('percent') or 0), stage=p.get('stage') or 'Анализирую смысл сцен…')
                self.stop_event.wait(2)
            detail = self.service.detail(pid)
        if 'plan_review_wait' in job['result']:
            job['result'].pop('plan_review_wait')
            self.update(pid,result_json=job['result'])
        if job['options'].get('frame_content_focus') and not detail['analysis_metadata'].get('narrative_storyboard'):
            return self.prepare_general_focus(pid,asset,report,detail)
        if detail['analysis_metadata'].get('narrative_storyboard') and (job['options'].get('frame_content_focus') or not any(
                s['active_generation_id'] or s['video_asset_id'] for s in detail['scenes'])):
            return self.prepare_narrative(pid,asset,report,detail)
        if expansion:
            from .narration_expansion import NarrationExpansion
            self.update(pid, state='aligning', progress=88, stage='Добавляю планы по времени речи. Готовые кадры сохраняются…')
            candidate = NarrationExpansion(self).prepare(pid, asset, report, job['options']['max_shot_seconds'])
            doc = candidate['document']
            self.update(pid, state='plan_review', progress=100, stage='Дополнение готово. Исходные изображения и клипы сохранены.',
                result_json={**job['result'], 'planned': False, 'candidate_plan_id': candidate['id'],
                             'frame_count': len(doc['frames']), 'duration': doc['duration'], 'metrics': metrics})
            if job['options'].get('auto_render'):
                try: self.assert_replaceable(pid)
                except DomainError: return
                from .voice_plans import VoicePlans
                VoicePlans(self).apply(pid, candidate['id'])
            return
        expected_plan_base=None
        if job['options'].get('versioned_plan'):
            from .voice_plans import VoicePlans
            with self.db.transaction():
                detail=self.service.detail(pid)
                expected_plan_base=VoicePlans(self).base(pid)
        self.check(pid)
        self.update(pid, state='aligning', progress=88, stage='Распределяю кадры по времени озвучки…')
        duration = self.service.editing.media(pid, asset['id'], 'audio') / 1000
        scenes = [{**s['data'], 'id': s['id']} for s in detail['scenes']]
        # Every spoken word must be represented, not only the analyzer's selected highlights.
        intervals = sorted((s['source_start'], s['source_end']) for s in scenes)
        for word in report['words']:
            if not any(a <= word['char_start'] < b for a,b in intervals):
                raise DomainError('Анализатор пропустил часть текста. Проверьте план сцен перед генерацией.', 409)
        if any(a['source_end'] > b['source_start'] for a,b in zip(scenes, scenes[1:])):
            raise DomainError('В плане повторяются или пересекаются фрагменты сценария. Исправьте сцены.', 409)
        if job['options'].get('frames_only'):
            from .voice_timing import semantic_timed_frames
            frames=semantic_timed_frames(project['story'],report['words'],scenes,duration,job['options']['max_shot_seconds'])
        else:frames = timed_frames(project['story'], report['words'], scenes, duration, job['options']['max_shot_seconds'])
        verify_plan(frames, project['story'], duration)
        if self.service._project_profile_type(pid) == 'forgotten_cities':
            pass
        if len(frames) > MAX_SCENES:
            raise DomainError('Слишком много кадров. Выберите большую длительность кадра.', 409)
        plan = []
        for frame in frames:
            data = copy.deepcopy(frame['scene_draft'])
            data.pop('id', None)
            for key in ('source_start','source_end','source_quote'):
                data[key] = frame[key]
            if frame['parts'] > 1:
                data['beat'] = frame['source_quote']
            if self.service._project_profile_type(pid) == 'forgotten_cities':
                from .forgotten_cities.prompts import build_prompt_packet
                from .forgotten_cities.grounding import apply_packet, enforce_source_constraints
                data.pop('source_fidelity_plan', None)
                packet = build_prompt_packet(detail['analysis_metadata']['forgotten_cities'],
                    data.get('visual_event_id') or data.get('event_id') or '', frame['source_quote'], data.get('location') or '',
                    ordinal=frame['position'], total=len(frames), story_text=project['story'],
                    source_quote=frame['source_quote'], source_start=frame['source_start'])
                data = apply_packet(data, enforce_source_constraints(packet, project['story']))
            timing = {key: frame[key] for key in ('start','end','duration','speech_start','speech_end','timing_needs_review','timing_reasons')}
            data['voice_timing'] = timing
            if not job['options'].get('frames_only'):
                data['animation'] = {**data.get('animation', {}), 'selected_engine': 'ken_burns', 'status': 'planned_ken_burns',
                    'duration_seconds': math.ceil(frame['duration'] * 24) / 24, 'ken_burns_mode': 'auto', 'credit_cost': 0}
            plan.append(data)
        result = {'planned': True, 'frame_count': len(plan), 'duration': duration, 'metrics': metrics}
        edit = self.service.editing.get(pid)
        doc = edit['document']
        doc['audio_tracks'] = [a for a in doc['audio_tracks'] if a['role'] != 'voice'] + [{'role':'voice','asset_id':asset['id'],'trim_in_ms':0,'trim_out_ms':duration*1000,'offset_ms':0,'volume':1}]
        doc['source_audio'] = False
        doc['export_settings'] = {'width':1920,'height':1080,'fps':24}
        doc = self.service.editing.validate(pid, doc)
        if job['options'].get('versioned_plan'):
            from .voice_plans import VoicePlans
            manager=VoicePlans(self)
            candidate=manager.prepare(pid,plan,frames,duration,metrics,detail['analysis_metadata'],doc,report,expected_plan_base)
            result={**job['result'],'planned':False,'candidate_plan_id':candidate['id'],'frame_count':len(plan),'duration':duration,'metrics':metrics}
            self.update(pid,state='plan_review',stage='Подготовлена отдельная версия плана',progress=100,result_json=result)
            # Initial automatic preparation is already an explicit user choice.
            initial=not result.get('active_plan_id') and not any(s['data'].get('voice_timing') for s in detail['scenes'])
            if initial and job['options']['auto_render'] and not candidate['document']['conflicts']:
                try:self.assert_replaceable(pid)
                except DomainError:return
                manager.apply(pid,candidate['id'])
            return
        with self.lock, self.db.transaction() as conn:
            self.check(pid); self.assert_replaceable(pid)
            if self.service.project(pid)['version'] != detail['version']:
                raise DomainError('План изменился во время обработки. Нажмите «Продолжить».', 409)
            current_edit = conn.execute('SELECT revision FROM project_edits WHERE project_id=?',(pid,)).fetchone()
            if (current_edit['revision'] if current_edit else 0) != edit['revision']:
                raise DomainError('Монтаж изменился во время обработки. Нажмите «Продолжить».',409)
            conn.execute('DELETE FROM scene_revisions WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)', (pid,))
            conn.execute('DELETE FROM scenes WHERE project_id=?', (pid,))
            for position, data in enumerate(plan, 1):
                sid = uid()
                conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (sid,pid,position))
                conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (sid,1,dump(data),now()))
            metadata = detail['analysis_metadata']; metadata['voice_alignment'] = result
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?', (dump(metadata),pid))
            conn.execute('INSERT INTO project_edits VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,document_json=excluded.document_json,updated_at=excluded.updated_at',
                (pid,edit['revision']+1,1,dump(doc),now()))
            conn.execute("UPDATE voice_workflows SET state='ready',stage='План по озвучке готов',progress=100,result_json=?,updated_at=? WHERE project_id=?", (dump(result),now(),pid))
        directory = self.service.root / 'voice' / pid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'alignment.json').write_text(dump(report), encoding='utf-8')

    def prepare_general_focus(self,pid,asset,report,detail):
        from .narration_expansion import narration_frames
        from .story_visual_plan import compile_scenes
        story_client = None
        from .voice_plans import VoicePlans
        manager=VoicePlans(self);base=manager.base(pid);job=self.check(pid)
        duration=self.service.editing.media(pid,asset['id'],'audio')/1000
        planned=narration_frames(detail['story'],report['words'],detail['scenes'],duration,job['options']['max_shot_seconds'])
        parents={scene['id']:scene for scene in detail['scenes']};drafts=[]
        for frame in planned:
            parent=parents[frame['parent_scene_id']];data=copy.deepcopy(parent['data'])
            data.update(source_start=frame['narration_source']['start'],source_end=frame['narration_source']['end'],
                source_quote=frame['narration_source']['quote'],voice_timing=copy.deepcopy(frame['timing']),
                semantic_parent_source={k:parent['data'][k] for k in ('source_start','source_end','source_quote')},
                semantic_parent_ordinal=parent['position'],semantic_parent_title=parent['data'].get('subject') or 'Исходное событие')
            data.pop('frame_focus',None);data.pop('frame_context',None);data.pop('visual_content',None);drafts.append(data)
        settings={**self.service.settings(),'_nvidia_cache_dir':str(self.service.root/'voice'/pid/'story-visual-provider-cache')}
        def wait(seconds):
            self.check(pid)
            if self.stop_event.wait(seconds):raise InterruptedError()
            self.check(pid)
        client=story_client(settings,timeout=120,retry_wait=wait)
        if settings.get('text_backend')=='nvidia':
            client.story_visual_helper=GroqAssistClient.from_settings(settings,client)
        stage=['Содержание кадров']
        def progress(done,total,text):
            stage[0]=text;self.check(pid)
            self.update(pid,state='aligning',stage=text,progress=60+round(30*done/max(1,total)))
        def activity(text):
            self.check(pid);self.update(pid,stage=stage[0]+' · '+text)
        if hasattr(client,'on_activity'):client.on_activity=activity
        metadata=copy.deepcopy(detail['analysis_metadata'])
        from .story_visual_plan import parent_document
        from . import narrative_frame_focus as focus
        parent_doc=parent_document(detail['story'],drafts)
        if metadata.get('story_catalog'):
            parent_doc['story_catalog']=copy.deepcopy(metadata['story_catalog'])
        from .generation_scene import setting_facts
        parent_doc['setting_facts']=setting_facts({'story':detail['story'],'v3_metadata':metadata})
        parent_doc['content_hash']=focus.fingerprint({k:v for k,v in parent_doc.items() if k!='content_hash'})
        plan,contract=compile_scenes(drafts,detail['story'],client,self.service.root/'voice'/pid/'story-visual-timed',
            progress,document=parent_doc,reuse=metadata.get('story_visual_plan'))
        metadata['story_visual_plan']=contract
        edit=copy.deepcopy(self.service.editing.get(pid)['document'])
        edit['audio_tracks']=[a for a in edit['audio_tracks'] if a['role']!='voice']+[
            {'role':'voice','asset_id':asset['id'],'trim_in_ms':0,'trim_out_ms':round(duration*1000,3),'offset_ms':0,'volume':1}]
        edit['source_audio']=False;edit=self.service.editing.validate(pid,edit)
        candidate=manager.prepare(pid,plan,planned,duration,report['metrics'],metadata,edit,report,base)
        self.update(pid,state='plan_review',progress=100,stage='Проверенное содержание кадров сохранено в отдельной версии.',
            result_json={**job['result'],'planned':False,'candidate_plan_id':candidate['id'],
                'frame_count':len(plan),'duration':duration,'metrics':report['metrics']})
        return candidate

    def prepare_narrative(self,pid,asset,report,detail):
        from .narrative_contract import timed_plan
        from .voice_plans import VoicePlans
        manager=VoicePlans(self);base=manager.base(pid);job=self.check(pid)
        duration=self.service.editing.media(pid,asset['id'],'audio')/1000
        metadata=copy.deepcopy(detail['analysis_metadata'])
        metadata.pop('narrative_frame_focus',None)
        if job['options'].get('frame_content_focus'):
            from .narrative_context import VERSION as CONTEXT_VERSION
            from .generation_scene import setting_facts
            document=metadata['narrative_storyboard']
            document['context_policy_version']=CONTEXT_VERSION
            if metadata.get('story_catalog'):document['story_catalog']=copy.deepcopy(metadata['story_catalog'])
            document['setting_facts']=setting_facts({'story':detail['story'],'v3_metadata':metadata})
        frames=timed_plan(detail['story'],metadata,report['words'],duration,job['options']['max_shot_seconds'])
        if job['options'].get('frame_content_focus'):
            from .narrative_frame_focus import prepare,client_request
            story_client = None
            def focus_wait(seconds):
                self.check(pid)
                if self.stop_event.wait(seconds):raise InterruptedError()
                self.check(pid)
            focus_settings={**self.service.settings(),'_nvidia_cache_dir':str(self.service.root/'voice'/pid/'frame-focus-transport-cache')}
            client=story_client(focus_settings,timeout=120,retry_wait=focus_wait)
            focus_stage=['Содержание кадров']
            def focus_progress(done,total,stage):
                focus_stage[0]=stage
                self.check(pid)
                self.update(pid,state='aligning',progress=60+round(25*done/max(1,total)),stage=stage)
            def focus_activity(activity):
                self.check(pid)
                self.update(pid,stage=focus_stage[0]+' · '+activity)
            if hasattr(client,'on_activity'):client.on_activity=focus_activity
            metadata,frames=prepare(detail['story'],metadata,frames,client_request(client,thinking=False),
                self.service.root/'voice'/pid/'frame-focus-cache',progress=focus_progress)
        detail['analysis_metadata']=metadata
        from .voice_timing_report import summarize
        timing_summary=summarize(detail['story'],report,frames,duration,asset['sha256'],
            job['options']['max_shot_seconds'],len(detail['analysis_metadata']['narrative_storyboard']['beats']))
        detail['analysis_metadata']['narrative_timing']=timing_summary
        edit=copy.deepcopy(self.service.editing.get(pid)['document'])
        edit['audio_tracks']=[a for a in edit['audio_tracks'] if a['role']!='voice']+[
            {'role':'voice','asset_id':asset['id'],'trim_in_ms':0,'trim_out_ms':round(duration*1000,3),'offset_ms':0,'volume':1}]
        edit['source_audio']=False
        edit=self.service.editing.validate(pid,edit)
        candidate=manager.prepare(pid,[f['data'] for f in frames],frames,duration,report['metrics'],
            detail['analysis_metadata'],edit,report,base)
        self.update(pid,state='plan_review',progress=100,stage='Связный план проверен и привязан ко всей озвучке.',
            result_json={**job['result'],'planned':False,'candidate_plan_id':candidate['id'],
                'frame_count':len(frames),'duration':duration,'metrics':report['metrics'],
                'narrative_timing':timing_summary})
        if job['options'].get('auto_render') and not candidate['document']['conflicts']:
            manager.apply(pid,candidate['id'])
        return candidate

    def wait_images(self, pid):
        job = self.check(pid)
        detail = self.service.detail(pid)
        missing = [s['id'] for s in detail['scenes'] if not s['active_generation_id'] or not any(g['id']==s['active_generation_id'] and g['asset_id'] and g['state']=='succeeded' for g in detail['generations']) or s['stale']]
        if not missing:
            return
        self.update(pid, state='images', progress=0, stage='Генерирую изображения…')
        bid = job['result'].get('batch_id')
        if not bid:
            batch = self.service.batch(pid, 'voice-' + uid(), missing)
            result = job['result']; result['batch_id'] = batch['id']
            self.update(pid, result_json=result)
            bid = batch['id']
        dispatched = False
        while True:
            self.check(pid)
            generations = self.db.all('SELECT state,error FROM generations WHERE batch_id=?', (bid,))
            batch_state=self.db.one('SELECT state FROM batches WHERE id=?',(bid,))
            if batch_state and batch_state['state'] in ('waiting_review','paused'):
                self.update(pid,stage='Ожидаю проверку смысла сцен…' if batch_state['state']=='waiting_review' else 'Пакет на паузе. Сохранённые результаты не изменены.')
                self.stop_event.wait(2)
                continue
            if any(g['state'] in ('failed','cancelled','resource_degraded') for g in generations):
                raise DomainError(next((g['error'] for g in generations if g['error']), 'Не удалось сгенерировать кадр. Повторите его в студии и продолжите.'), 409)
            done = sum(g['state']=='succeeded' for g in generations)
            if generations and done == len(generations): return
            stage = f'Изображения: {done} из {len(generations)}'
            if self.service.settings().get('image_backend') == 'flow' and not dispatched:
                try:
                    self.bridge('start', pid, missing, images_only=True) if job['options'].get('frames_only') else self.bridge('start', pid, missing)
                    dispatched = True
                except DomainError as error:
                    stage += '. ' + str(error)
            self.update(pid, progress=100*done/max(1,len(generations)), stage=stage)
            self.stop_event.wait(2)

    def ensure_references(self, pid):
        if self.service._project_profile_type(pid) != 'forgotten_cities':
            return
        self.update(pid,state='images',stage='Подготавливаю эталоны города и предметов…',progress=0)
        metadata=self.service.detail(pid)['analysis_metadata'].get('forgotten_cities',{})
        required=[anchor for anchor in metadata.get('anchors',[]) if anchor.get('reference_required')]
        # References belong to individual shots. A story can contain many more
        # objects and places than a provider accepts in one image request.
        for anchor in required:
            self.check(pid)
            kind='prop' if anchor['anchor_type']=='artifact' else 'location'
            table=kind+'s'
            target=self.db.one(f'SELECT * FROM {table} WHERE project_id=? AND name=?',(pid,anchor['canonical_name']))
            if not target:
                with self.db.transaction() as conn:
                    conn.execute(f'INSERT INTO {table}(id,project_id,name,description) VALUES(?,?,?,?)',
                        (uid(),pid,anchor['canonical_name'],str(anchor.get('description') or anchor['canonical_name'])[:4000]))
                target=self.db.one(f'SELECT * FROM {table} WHERE project_id=? AND name=?',(pid,anchor['canonical_name']))
            if target['locked']:
                references=json.loads(target['references_json'] or '{}')
                selected=references.get('item' if kind=='prop' else 'environment')
                from .review_policy import visual_reviews_enabled
                if not visual_reviews_enabled():
                    if not selected:
                        raise DomainError('Выберите и закрепите эталон «'+anchor['canonical_name']+'».',409)
                    asset=self.service.asset(selected)
                    if not (self.service.root/asset['path']).is_file():
                        raise DomainError('Файл закреплённого эталона отсутствует.',409)
                    continue
                self.update(pid,stage='Проверяю содержимое эталона «'+anchor['canonical_name']+'»')
                verdict=self.editorial.reference_verdict(pid,anchor,selected,target={**target,'kind':kind}) if selected else {'status':'fail','correction':'Нет выбранного изображения.'}
                if verdict['status']=='pass':continue
                self.recast_reference(pid,kind,target,anchor,verdict.get('correction',''))
                target=self.db.one(f'SELECT * FROM {table} WHERE id=?',(target['id'],))
            session=self.service.reference_cast(kind,target)
            if not session:
                created=(self.service.cast_prop(target['id'],2) if kind=='prop' else self.service.cast_location(target['id'],2))
                ids=created['generation_ids']
            else:ids=session['generation_ids']
            dispatched=False
            while True:
                self.check(pid)
                candidates=[self.db.one('SELECT * FROM generations WHERE id=?',(gid,)) for gid in ids]
                if any(g['state'] in ('failed','cancelled','resource_degraded') for g in candidates):
                    raise DomainError('Не удалось создать эталон «'+anchor['canonical_name']+'». Повторите его в разделе эталонов и продолжите.',409)
                if all(g['state']=='succeeded' and g['asset_id'] for g in candidates):break
                stage='Создаю два варианта эталона «'+anchor['canonical_name']+'»'
                if self.service.settings().get('image_backend')=='flow' and not dispatched:
                    try:self.bridge('start',pid,None,ids);dispatched=True
                    except DomainError as error:stage+='. '+str(error)
                self.update(pid,stage=stage)
                self.stop_event.wait(2)
            verdicts=[]
            from .review_policy import visual_reviews_enabled
            if not visual_reviews_enabled():
                raise DomainError('Варианты эталона «'+anchor['canonical_name']+'» сохранены. Выберите и закрепите изображение вручную, затем продолжите.',409)
            for candidate in candidates:
                self.check(pid)
                self.update(pid,stage='Проверяю варианты эталона «'+anchor['canonical_name']+'»')
                verdicts.append((candidate,self.editorial.reference_verdict(pid,anchor,candidate['asset_id'],target={**target,'kind':kind})))
            suitable=[(candidate,verdict) for candidate,verdict in verdicts if verdict['status']=='pass']
            if not suitable:
                self.recast_reference(pid,kind,target,anchor,' '.join(v.get('correction','') for _,v in verdicts))
                # Resume the same durable orchestration with a new reference revision.
                return self.ensure_references(pid)
            chosen=max(suitable,key=lambda item:item[1]['score'])[0]['asset_id']
            self.service.select_reference(kind,target['id'],chosen,target['revision'])
            if kind=='prop':self.service.lock_prop(target['id'],{'item':chosen},target['revision'])
            else:self.service.lock_location(target['id'],{'environment':chosen},target['revision'])
            result=self.get(pid)['result'];result.setdefault('automatic_references',{})[anchor['anchor_id']]=chosen
            self.update(pid,result_json=result)

    def recast_reference(self,pid,kind,target,anchor,correction):
        result=self.get(pid)['result'];counts=result.setdefault('reference_recasts',{})
        if counts.get(anchor['anchor_id'],0)>=1:
            raise DomainError('Не найден подходящий вариант эталона «'+anchor['canonical_name']+'» после исправления. Проверьте варианты.',409)
        if target['locked']:
            (self.service.unlock_prop if kind=='prop' else self.service.unlock_location)(target['id'])
        description=(str(anchor.get('description') or target['description'])+' CORRECTION: '+correction)[:4000]
        (self.service.edit_prop if kind=='prop' else self.service.edit_location)(target['id'],description,target['revision'])
        counts[anchor['anchor_id']]=counts.get(anchor['anchor_id'],0)+1
        self.update(pid,result_json=result,stage='Исправляю неподходящий эталон «'+anchor['canonical_name']+'»')

    def produce_frames(self,pid):
        self.editorial.review(pid)
        self.editorial.prepare_presence(pid)
        self.ensure_references(pid)
        self.wait_images(pid)
        self.update(pid,state='frames_ready',stage='Кадры подготовлены. Анимации и сборка запускаются отдельно.',progress=100)

    def produce(self, pid):
        existing_export = self.check(pid)['result'].get('export_id')
        if existing_export:
            exported = self.exports.get(pid,existing_export)
            if exported['status'] in ('queued','running','completed'):
                self.finish_export(pid,existing_export)
                return
        self.editorial.review(pid)
        self.editorial.prepare_presence(pid)
        self.ensure_references(pid)
        self.wait_images(pid)
        detail = self.service.detail(pid)
        items = ([{'scene_id':s['id'],'revision':s['current_revision'],'generation_id':s['active_generation_id']} for s in detail['scenes'] if not s.get('still_approval',{}).get('approved')]
                 if self.service._requires_still_approval(pid) else [])
        for start in range(0,len(items),8):
            self.check(pid)
            result = self.service.approve_selected_stills(pid, {'scenes':items[start:start+8]})
            if result['failed']:
                raise DomainError(result['failed'][0]['error'],409)
        self.update(pid, state='animating', progress=0, stage='Создаю анимации…')
        for index, scene in enumerate(self.service.detail(pid)['scenes']):
            self.check(pid)
            if not scene['video_asset_id']:
                self.service.start_animation_generation(pid, [scene['id']])
            if scene['data']['animation']['selected_engine'] in ('omni_flash','veo_lite'):
                dispatched = False
                while not self.service.scene(scene['id']).get('video_asset_id'):
                    self.check(pid)
                    anim = self.service.scene(scene['id'])['data'].get('animation',{})
                    if anim.get('status') == 'failed': raise DomainError(anim.get('error') or 'Ошибка анимации.',409)
                    if not dispatched:
                        try:
                            self.bridge('start',pid,[scene['id']]); dispatched=True
                        except DomainError as error:
                            self.update(pid,stage=str(error))
                    self.stop_event.wait(2)
            self.update(pid, progress=100*(index+1)/self.get(pid)['result']['frame_count'], stage=f'Анимации: {index+1} из {self.get(pid)["result"]["frame_count"]}')
        self.check(pid)
        self.update(pid,state='assembling',stage='Расставляю клипы и голос в монтаже…')
        detail = self.service.detail(pid)
        edit = self.service.editing.get(pid)
        if edit['document']['clips'] and self.get(pid)['result'].get('montage_revision') != edit['revision']:
            raise DomainError('Монтаж уже изменён вручную. Автосборка остановлена, чтобы сохранить ваши правки.',409)
        doc = copy.deepcopy(edit['document']); doc['clips']=[]; doc['transitions']=[]
        for scene in detail['scenes']:
            source = self.service.editing.media(pid,scene['video_asset_id'],'video')
            from .voice_timing import scene_voice_timing
            timing=scene_voice_timing(detail,scene)
            if not timing:raise DomainError('Для кадра не сохранено время озвучки. Обновите план.',409)
            wanted = timing['duration']*1000
            used = min(source,wanted)
            image = self.db.one('SELECT asset_id FROM generations WHERE id=?',(scene['active_generation_id'],))['asset_id']
            doc['clips'].append({'edit_clip_id':uid(),'scene_id':scene['id'],'source_scene_revision':scene['current_revision'],
                'source_video_asset_id':scene['video_asset_id'],'source_image_asset_id':image,
                'trim_in_ms':0,'trim_out_ms':used,'playback_rate':used/wanted,'muted':True,'volume':1,'excluded':False})
        expected = self.get(pid)['result']['duration']*1000
        if abs(normalize(doc)['duration_ms']-expected)>.1:
            raise DomainError('Длительность монтажа не совпала с озвучкой.',409)
        saved = self.service.editing.save(pid,{'expected_revision':edit['revision'],'document':doc})
        result = self.get(pid)['result']; result['montage_revision']=saved['revision']
        self.update(pid,result_json=result,state='exporting',stage='Собираю итоговый ролик Full HD…',progress=0)
        exported = self.exports.start(pid,{'expected_revision':saved['revision']})
        result['export_id']=exported['id']; self.update(pid,result_json=result)
        self.finish_export(pid, exported['id'])

    def finish_export(self, pid, export_id):
        self.update(pid,state='exporting',stage='Собираю итоговый ролик Full HD…')
        result = self.get(pid)['result']
        while True:
            self.check(pid)
            exported = self.exports.get(pid,export_id)
            if exported['status']=='completed': break
            if exported['status'] in ('failed','cancelled'): raise DomainError(exported['error'] or 'Экспорт прерван.',409)
            self.update(pid,progress=exported['progress'],stage=exported['stage'])
            self.stop_event.wait(2)
        result['video_asset_id']=exported['asset_id']
        self.update(pid,result_json=result,state='completed',stage='Всё готово. Ролик собран с озвучкой.',progress=100)

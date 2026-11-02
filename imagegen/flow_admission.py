"""Reserve bounded Flow frame slots before the browser spends a generation."""
import sqlite3
from contextlib import contextmanager
from .domain import DomainError, fingerprint


class FlowAdmission:
    def __init__(self,service):
        self.service=service
        path=service.root/'google_api'/'flow_admission.sqlite';path.parent.mkdir(parents=True,exist_ok=True)
        self.path=path
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS admissions(request_id TEXT PRIMARY KEY,project TEXT,scene TEXT,revision INTEGER,prompt_hash TEXT,state TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS request_documents(request_id TEXT PRIMARY KEY,prompt TEXT NOT NULL,source_contract_hash TEXT,wire_version TEXT)')

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=30);connection.row_factory=sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE');yield connection;connection.commit()
        except Exception:
            connection.rollback();raise
        finally:connection.close()

    def claim(self,scene_id,request_id,revision,prompt):
        if self.service.settings().get('google_queue_enabled') is not True:
            return {'enabled':False,'allowed':True}
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100 or type(revision) is not int or not isinstance(prompt,str) or not prompt.strip():
            raise DomainError('Некорректное задание Flow.',400)
        scene=self.service.scene(scene_id);pid=scene['project_id']
        if revision!=scene['current_revision']:
            raise DomainError('Кадр изменился. Старое задание Flow не запущено.',409)
        from .google_queue import configured_queue
        if configured_queue(self.service.root,self.service.settings()).project_paused(pid):
            return {'enabled':True,'allowed':False,'reason':'Проект на паузе.'}
        if self.service.db.one("SELECT id FROM generations WHERE scene_id=? AND scene_revision=? AND state='queued' AND cancel_requested=0 AND json_extract(snapshot,'$.plan_review_wait') IS NOT NULL LIMIT 1",(scene_id,revision)):
            return {'enabled':True,'allowed':False,'reason':'Ожидается проверка смысла сцен.'}
        if self.service.settings().get('source_contract_enabled') is True:
            project=self.service.project(pid)
            channel=self.service.db.one('SELECT profile_type FROM channel_profiles WHERE id=?',(project.get('channel_profile_id'),))
            if channel and channel['profile_type']=='forgotten_cities':
                preflight=self.service.project_preflight(pid)
                if not preflight.get('ok'):
                    return {'enabled':True,'allowed':False,'state':'blocked_preflight',
                            'reason':'; '.join(e['message'] for e in preflight.get('errors',[])[:3]),
                            'errors':preflight.get('errors',[])}
        signature=fingerprint(prompt)
        snapshot = None
        if self.service.settings().get('source_contract_enabled') is True:
            from .final_prompt_review import check
            snapshot=self.service.scene_snapshot(scene)
            result=check(self.service,snapshot,prompt)
            if not result['allowed']:
                return {'enabled':True,'allowed':False,'state':result['status'],'reason':result['reason'],
                    'job_id':result.get('job_id'),'next_retry_at':result.get('next_retry_at')}
        with self.db() as db:
            # A terminal SDK retry uses a new request id. Once that exact
            # scene/revision has uploaded, older attempts cannot still
            # consume an in-flight slot. Never expire an unknown live request.
            db.execute('''UPDATE admissions AS a SET state='superseded'
                WHERE a.project=? AND a.state='reserved' AND EXISTS (
                  SELECT 1 FROM admissions AS done WHERE done.project=a.project
                    AND done.scene=a.scene AND done.revision=a.revision
                    AND done.state='uploaded')''',(pid,))
            old=db.execute('SELECT * FROM admissions WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if (old['project'],old['scene'],old['revision'],old['prompt_hash'])!=(pid,scene_id,revision,signature):
                    raise DomainError('Идентификатор Flow уже использован для другого задания.',409)
                return {'enabled':True,'allowed':old['state']=='reserved','state':old['state']}
            pending=self.service.google_generation_capacity(pid)['pending']
            reserved=db.execute("SELECT count(*) FROM admissions WHERE project=? AND state='reserved'",(pid,)).fetchone()[0]
            if pending+reserved>=4:
                return {'enabled':True,'allowed':False,'pending':pending,'reserved':reserved,'state':'capacity_wait','reason':('Ожидается проверка предыдущих кадров.' if pending else 'Заняты разрешения незавершённых запросов Flow. Генерация не отправлена.')}
            db.execute('INSERT INTO admissions VALUES(?,?,?,?,?,?)',(request_id,pid,scene_id,revision,signature,'reserved'))
            db.execute('INSERT INTO request_documents VALUES(?,?,?,?)', (request_id, prompt,
                ((snapshot or {}).get('source_contract') or {}).get('hash'),
                ((snapshot or {}).get('flow_wire') or {}).get('version')))
        return {'enabled':True,'allowed':True,'state':'reserved'}

    def validate(self,scene_id,request_id):
        scene=self.service.scene(scene_id)
        with self.db() as db:
            row=db.execute('SELECT * FROM admissions WHERE request_id=?',(request_id,)).fetchone()
            if not row or row['scene']!=scene_id or row['project']!=scene['project_id'] or row['revision']!=scene['current_revision'] or row['state'] not in ('reserved','uploaded'):
                raise DomainError('Разрешение Flow отсутствует или устарело. Результат сохранён в мосте.',409)

    def finish(self,scene_id,request_id,outcome):
        self.validate(scene_id,request_id)
        if outcome not in ('uploaded','rejected'):
            raise DomainError('Неизвестный исход Flow.',400)
        with self.db() as db:
            db.execute("UPDATE admissions SET state=? WHERE request_id=? AND state='reserved'",(outcome,request_id))

    def fail(self,scene_id,request_id,code):
        """Close a confirmed terminal attempt, never an unknown transport result."""
        if code not in ('ERR_GEN_FAILED','COMPOSITION_REJECTED','FLOW_SERVICE_UNAVAILABLE',
                        'ERR_UNSUPPORTED_MODEL','ERR_TOO_MANY_REFS','FLOW_BUSY','PREFLIGHT_REJECTED','NOT_SENT'):
            raise DomainError('Неизвестный подтверждённый отказ Flow.',400)
        scene=self.service.scene(scene_id)
        with self.db() as db:
            row=db.execute('SELECT * FROM admissions WHERE request_id=?',(request_id,)).fetchone()
            # An attempt may have failed before reserving, or with the queue
            # disabled. Acknowledge absence without creating a reservation.
            if not row:
                return {'ok':True,'state':'not_reserved'}
            # Closing a confirmed old revision is safe; it must not strand a
            # slot merely because the user has since edited this scene.
            if row['scene']!=scene_id or row['project']!=scene['project_id']:
                raise DomainError('Разрешение Flow принадлежит другому заданию.',409)
            if row['state'] not in ('reserved','failed','rejected'):
                raise DomainError('Попытка Flow уже завершена другим результатом.',409)
            db.execute("UPDATE admissions SET state='failed' WHERE request_id=? AND state='reserved'",(request_id,))
        return {'ok':True,'state':'failed' if row['state']=='reserved' else row['state']}

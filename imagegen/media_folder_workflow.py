"""Local folder -> library -> visual analysis -> proposals -> reviewable suggestions."""
import json
import hashlib
import threading
import time
from pathlib import Path
from .db import uid, now, dump
from .domain import DomainError
from .media_library import SUPPORTED_IMAGE_EXTS, SUPPORTED_VIDEO_EXTS


class MediaFolderWorkflow:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.lock = threading.Lock()
        with self.db.transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS media_folder_jobs(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                folder_path TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0, total_files INTEGER NOT NULL DEFAULT 0,
                processed_files INTEGER NOT NULL DEFAULT 0, imported_files INTEGER NOT NULL DEFAULT 0,
                reused_files INTEGER NOT NULL DEFAULT 0, errors_json TEXT NOT NULL DEFAULT '[]',
                options_json TEXT NOT NULL, base_edit_revision INTEGER NOT NULL,
                analysis_job_id TEXT, proposal_id TEXT, applied_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            c.execute("UPDATE media_folder_jobs SET state='interrupted', message='Процесс прерван перезапуском. Запустите папку снова: готовые материалы будут использованы повторно.', updated_at=? WHERE state IN ('queued','running')", (now(),))

    def _update(self, job_id, **values):
        values['updated_at'] = now()
        with self.db.transaction() as c:
            c.execute('UPDATE media_folder_jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), job_id))

    def latest(self, project_id):
        self.service.project(project_id)
        row = self.db.one('SELECT * FROM media_folder_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (project_id,))
        return self._public(row) if row else {'state':'idle'}

    @staticmethod
    def _public(row):
        result = dict(row)
        result['errors'] = json.loads(result.pop('errors_json', '[]'))
        result['options'] = json.loads(result.pop('options_json', '{}'))
        result['options'].pop('voice_hash', None)
        return result

    def start(self, project_id, data):
        self.service.project(project_id)
        value = str(data.get('folder_path') or '').strip().strip('"')
        folder = Path(value).expanduser()
        if not value or not folder.is_absolute() or not folder.is_dir():
            raise DomainError('Укажите полный путь к существующей папке на этом компьютере.')
        folder = folder.resolve()
        from .insert_coverage import policy as coverage_policy
        options = {'placement_policy':'whole-narration-segment/1', 'recursive':bool(data.get('recursive',True)), 'auto_apply':False,
                   'coverage':coverage_policy(data.get('coverage')),
                   'voice_hash':self.service.media_matching.compute_voice_hash(project_id)}
        with self.lock:
            active = self.db.one("SELECT * FROM media_folder_jobs WHERE project_id=? AND state IN ('queued','running') LIMIT 1", (project_id,))
            if active:
                return self._public(active)
            jid = uid()
            revision = self.service.editing.get(project_id)['revision']
            with self.db.transaction() as c:
                c.execute('''INSERT INTO media_folder_jobs(id,project_id,folder_path,state,step,options_json,base_edit_revision,created_at,updated_at)
                             VALUES(?,?,?,'queued','scanning',?,?,?,?)''', (jid,project_id,str(folder),dump(options),revision,now(),now()))
            threading.Thread(target=self._run, args=(project_id,jid,folder,options,revision), daemon=True).start()
            return self._public(self.db.one('SELECT * FROM media_folder_jobs WHERE id=?',(jid,)))

    def _run(self, pid, jid, folder, options, revision):
        errors=[]; imported=reused=0
        try:
            self._update(jid,state='running',step='scanning',message='Ищу фото и видео в папке…')
            ext=SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS
            paths=folder.rglob('*') if options['recursive'] else folder.iterdir()
            files=sorted((p for p in paths if p.is_file() and not p.is_symlink() and p.suffix.lower() in ext),key=lambda p:str(p).lower())
            if not files:
                raise DomainError('В папке не найдено поддерживаемых фото или видео.')
            signature=hashlib.sha256(dump([(str(p.relative_to(folder)),p.stat().st_size,p.stat().st_mtime_ns) for p in files]).encode()).hexdigest()
            options['folder_signature']=signature
            previous=self.db.one("SELECT * FROM media_folder_jobs WHERE project_id=? AND id!=? AND state='completed' ORDER BY created_at DESC LIMIT 1",(pid,jid))
            if previous and not json.loads(previous['errors_json']):
                old=json.loads(previous['options_json'])
                same=all(old.get(k)==options.get(k) for k in ('folder_signature','voice_hash','auto_apply','recursive','placement_policy','coverage'))
                if same and previous['folder_path']==str(folder) and old.get('completed_edit_revision')==revision:
                    self._update(jid,state='completed',step='complete',progress=1,total_files=len(files),processed_files=len(files),reused_files=len(files),proposal_id=previous['proposal_id'],options_json=previous['options_json'],message='Эта папка уже обработана. Повторные вставки не добавлены.')
                    return
            self._update(jid,total_files=len(files),step='importing')
            for i,p in enumerate(files):
                self._update(jid,message=f'Добавляю {i+1}/{len(files)}: {p.name}',progress=.45*i/len(files))
                try:
                    before=self.db.one('SELECT COUNT(*) AS n FROM media_library_items WHERE project_id=?',(pid,))['n']
                    self.service.media_library.import_file(pid,p)
                    after=self.db.one('SELECT COUNT(*) AS n FROM media_library_items WHERE project_id=?',(pid,))['n']
                    if after>before: imported+=1
                    else: reused+=1
                except Exception as e:
                    errors.append({'file':p.name,'error':str(e)})
                self._update(jid,processed_files=i+1,imported_files=imported,reused_files=reused,errors_json=dump(errors))
            if not (imported+reused):
                raise DomainError('Не удалось прочитать файлы из папки. Подробности приведены в списке ошибок.')
            job=self.service.media_analysis.start_job(pid)
            self._update(jid,step='analyzing',analysis_job_id=job['id'],progress=.45,message='Анализирую материалы локальной Qwen3-VL…')
            while job['state'] in ('queued','running'):
                time.sleep(.5)
                job=self.service.media_analysis.get_job_status(pid,job['id'])
                self._update(jid,progress=.45+.3*job.get('progress',0),message=job.get('current_step') or 'Анализ материалов…')
            if job['state']!='completed':
                raise DomainError(job.get('error') or 'Анализ материалов прерван.')
            if job.get('error'): errors.append({'file':'Анализ','error':job['error']})
            self._update(jid,step='matching',progress=.8,message='Подбираю вставки к фразам сценария…',errors_json=dump(errors))
            proposal=self.service.media_matching.propose_inserts(pid,{'policy':'balanced','coverage':options['coverage']})
            proposal_id=proposal.get('proposal_id')
            self._update(jid,proposal_id=proposal_id)
            accepted=[p['proposal_id'] for p in proposal.get('proposals',[]) if p.get('match_status')=='matched']
            message=f"Подобрано вставок: {len(accepted)}."
            if not accepted:
                message+=' Подходящего места не найдено. Материалы оставлены в медиатеке.'
            if errors:
                message+=' Некоторые файлы не удалось обработать; подробности в списке ошибок.'
            options['completed_edit_revision']=self.service.editing.get(pid)['revision']
            self._update(jid,state='completed',step='complete',progress=1,applied_count=0,message=message,errors_json=dump(errors),options_json=dump(options))
        except Exception as e:
            self._update(jid,state='failed',message=str(e),errors_json=dump(errors))

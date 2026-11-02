"""Recoverable project removal and an explicit allowlist of derived caches."""
import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from .db import now
from .domain import DomainError

CACHES=(
    ('semantic','Смысловые расчёты','montage-voice-fit/semantic-cache'),
    ('montage_asr','Распознавание для монтажа','montage-voice-fit/asr'),
    ('voice_asr','Кэш распознавания речи','voice/asr'),
    ('analysis','Кэш анализатора сценария','nvidia_analysis_cache'),
    ('detector','Временные изображения проверок','detector-inputs'),
)
GENERATION_STATES=('queued','preparing','submitted','running','saving','reconciling','waiting_prompt','blocked_prompt')
VOICE_STATES=('queued','recognizing','analyzing','aligning','images','animating','assembling','exporting')

def reparse(path):
    info=path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',1024))

class ProjectMaintenance:
    def __init__(self,app):
        self.app=app;self.service=app.service;self.db=self.service.db;self.root=self.service.root.resolve()

    def busy(self,pid=None):
        suffix=' AND project_id=?' if pid else ''
        args=(pid,) if pid else ()
        # In Flow mode, an idle external image queue is not a running cache
        # writer. The live bridge check below still blocks real generation.
        idle_flow=" AND NOT (state='queued' AND purpose IN ('scene','character','location','prop'))" if pid is None and self.service.settings().get('image_backend')=='flow' else ''
        if self.db.one('SELECT id FROM generations WHERE state IN ('+','.join('?' for _ in GENERATION_STATES)+')'+suffix+idle_flow+' LIMIT 1',(*GENERATION_STATES,*args)):
            return 'Есть незавершённая генерация. Дождитесь её завершения или отмените очередь.'
        if self.db.one("SELECT id FROM edit_exports WHERE status IN ('queued','running')"+suffix+' LIMIT 1',args):return 'Идёт экспорт видео. Дождитесь его завершения или отмените экспорт.'
        if self.db.one('SELECT project_id FROM voice_workflows WHERE state IN ('+','.join('?' for _ in VOICE_STATES)+')'+suffix+' LIMIT 1',(*VOICE_STATES,*args)):
            return 'Выполняется обработка озвучки. Дождитесь завершения или остановите её.'
        with self.app.montage_voice_fit.lock:
            if any(pid is None or project==pid for project,event in self.app.montage_voice_fit.running.values()):return 'Сейчас рассчитывается подгонка монтажа. Дождитесь результата или отмените расчёт.'
        bridge=self.app.flow_bridge_v1
        if bridge.get('running') and time.time()-bridge.get('last_seen',0)<90 and (pid is None or bridge.get('project_id')==pid):return 'В Flow выполняется задание. Сначала остановите очередь и дождитесь сохранения результата.'
        return None

    def list_projects(self):
        rows=self.db.all('''SELECT p.id,p.title,p.created_at,
            (SELECT COUNT(*) FROM scenes s WHERE s.project_id=p.id) AS scene_count,
            (SELECT COUNT(*) FROM assets a WHERE a.project_id=p.id) AS media_count
            FROM projects p
            ORDER BY p.created_at DESC''')
        return {'projects':rows,'trash':[]}

    def delete_project(self,pid,data=None):
        data = data or {}
        row=self.db.one('SELECT id,title FROM projects WHERE id=?',(pid,))
        if not row:
            return {'project_id':pid,'deleted':True,'freed_bytes':0,'deleted_files':0}

        # Forceful deletion: abort any active jobs, unblock bridges and threads
        try:
            with self.app.montage_voice_fit.lock:
                for k in list(self.app.montage_voice_fit.running.keys()):
                    if self.app.montage_voice_fit.running.get(k) == pid:
                        del self.app.montage_voice_fit.running[k]
        except Exception:
            pass

        if getattr(self.service,'active_project_id',None)==pid:
            self.service.active_project_id=None
        with self.app.flow_bridge_v1_lock:
            if self.app.flow_bridge_v1.get('project_id')==pid:
                self.app.flow_bridge_v1.update(project_id=None,command=None,running=False)

        freed_bytes=0
        deleted_files=0

        # 1. Delete individual asset files
        try:
            asset_rows=self.db.all('SELECT path FROM assets WHERE project_id=?',(pid,))
            for a in asset_rows:
                rel=a.get('path')
                if rel:
                    p=(self.root/rel).resolve()
                    if p.is_file() and p.is_relative_to(self.root):
                        try:
                            freed_bytes+=p.stat().st_size
                            p.unlink(missing_ok=True)
                            deleted_files+=1
                        except Exception:
                            pass
        except Exception:
            pass

        # 2. Delete project directory: projects/{pid}
        project_dir=(self.root/'projects'/pid).resolve()
        if project_dir.is_dir() and project_dir.is_relative_to(self.root):
            try:
                for p in project_dir.rglob('*'):
                    if p.is_file():
                        try:
                            freed_bytes+=p.stat().st_size
                            deleted_files+=1
                        except Exception:
                            pass
                shutil.rmtree(project_dir,ignore_errors=True)
            except Exception:
                pass

        # 3. Clean up all DB records with foreign_keys OFF before transaction
        conn = self.db.connect()
        try:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM project_trash WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM reviews WHERE generation_id IN (SELECT id FROM generations WHERE project_id=?)',(pid,))
            conn.execute('DELETE FROM assets WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM veo_jobs WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM generations WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM batches WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM reference_casts WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM editorial_scene_applications WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)',(pid,))
            conn.execute('DELETE FROM editorial_scene_plans WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM scene_revisions WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)',(pid,))
            conn.execute('DELETE FROM scene_fidelity_runs WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)',(pid,))
            conn.execute('DELETE FROM scenes WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM characters WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM locations WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM props WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM voice_plan_applications WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM voice_plan_versions WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM voice_workflows WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM project_edits WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM edit_exports WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM story_audits WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM events WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM projects WHERE id=?',(pid,))
            conn.commit()
            conn.execute('PRAGMA foreign_keys=ON')
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {'project_id':pid,'deleted':True,'freed_bytes':freed_bytes,'deleted_files':deleted_files}

    def delete_all_projects(self,data=None):
        data = data or {}
        rows = self.db.all('SELECT id FROM projects')
        total_freed = 0
        total_files = 0
        deleted_count = 0
        for r in rows:
            res = self.delete_project(r['id'], {'confirm': True})
            total_freed += res.get('freed_bytes', 0)
            total_files += res.get('deleted_files', 0)
            deleted_count += 1
        return {'deleted_count': deleted_count, 'freed_bytes': total_freed, 'deleted_files': total_files}

    def trash(self,pid,data=None):
        return self.delete_project(pid,data or {'confirm':True})

    def restore(self,pid):
        return {'project_id':pid,'restored':False,'message':'Корзина отключена. Проекты удаляются навсегда.'}

    def _inventory(self):
        groups=[];files=[]
        protected={(self.root/row['path']).resolve() for row in self.db.all('SELECT path FROM assets WHERE path IS NOT NULL')}
        for key,label,relative in CACHES:
            directory=self.root/relative
            group={'id':key,'label':label,'bytes':0,'files':0,'skipped':0};groups.append(group)
            if not directory.exists():continue
            if reparse(directory) or not directory.resolve().is_relative_to(self.root):group['skipped']+=1;continue
            for parent,dirs,names in os.walk(directory,topdown=True,followlinks=False):
                safe=[]
                for name in dirs:
                    child=Path(parent)/name
                    if reparse(child) or not child.resolve().is_relative_to(directory.resolve()):group['skipped']+=1
                    else:safe.append(name)
                dirs[:]=safe
                for name in names:
                    path=Path(parent)/name
                    if reparse(path) or not path.resolve().is_relative_to(directory.resolve()):group['skipped']+=1;continue
                    if path.resolve() in protected:group['skipped']+=1;continue
                    info=path.stat()
                    if not stat.S_ISREG(info.st_mode):group['skipped']+=1;continue
                    row={'group':key,'relative':path.relative_to(self.root).as_posix(),'bytes':info.st_size,'mtime_ns':info.st_mtime_ns}
                    files.append(row);group['bytes']+=info.st_size;group['files']+=1
        files.sort(key=lambda row:row['relative'])
        token=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return groups,files,token

    def cache_info(self):
        groups,files,token=self._inventory()
        return {'groups':groups,'bytes':sum(g['bytes'] for g in groups),'files':len(files),'token':token,'busy':self.busy()}

    def clear_cache(self,data):
        if data.get('confirm') is not True:raise DomainError('Подтвердите очистку выбранного кэша.')
        selected=data.get('groups')
        allowed={key for key,label,path in CACHES}
        if not isinstance(selected,list) or not selected or any(not isinstance(k,str) or k not in allowed for k in selected):raise DomainError('Выберите доступные категории кэша.')
        reason=self.busy()
        if reason:raise DomainError(reason,409)
        groups,files,token=self._inventory()
        if data.get('expected_token')!=token:raise DomainError('Состав кэша изменился. Обновите оценку перед очисткой.',409)
        removed=0;size=0;failed=0
        roots={key:(self.root/path).resolve() for key,label,path in CACHES}
        for row in files:
            if row['group'] not in selected:continue
            path=self.root/row['relative'];boundary=roots[row['group']]
            # Check containment and reparse status immediately before unlink.
            # No project directory is recursively deleted or moved.
            try:
                if not path.resolve().is_relative_to(boundary) or not boundary.is_relative_to(self.root):raise DomainError('Недопустимый путь кэша.')
                cursor=path
                while cursor!=self.root:
                    if reparse(cursor):raise DomainError('Путь кэша заменён ссылкой. Обновите оценку.',409)
                    cursor=cursor.parent
                info=path.stat()
                if info.st_size!=row['bytes'] or info.st_mtime_ns!=row['mtime_ns']:raise DomainError('Файл кэша изменился. Повторите очистку.',409)
                path.unlink();removed+=1;size+=row['bytes']
            except FileNotFoundError:continue
            except (PermissionError,OSError):failed+=1
        self.service.editing._media_cache.clear();self.service.editing._image_cache.clear()
        return {'removed_files':removed,'removed_bytes':size,'failed_files':failed,'remaining':self.cache_info()}

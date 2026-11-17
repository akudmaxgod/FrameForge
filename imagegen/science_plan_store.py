"""Retain the old automatic scientific plan as a restorable project before replacing it."""
import json
from .db import uid,now
from .domain import DomainError

def require_replaceable(service,connection,pid,keep_generation_id=None):
    service._require_unreviewed_analysis(connection,pid)
    if connection.execute('SELECT 1 FROM project_edits WHERE project_id=?',(pid,)).fetchone():
        raise DomainError('В научпоп-проекте уже есть монтаж. Пересборку выполните в копии проекта, чтобы сохранить привязки.',409)
    for row in connection.execute('SELECT id,purpose,snapshot,state FROM generations WHERE project_id=?',(pid,)).fetchall():
        if row['id']==keep_generation_id or row['purpose']=='analysis':continue
        snapshot=json.loads(row['snapshot'])
        if row['purpose']!='scene' or not snapshot.get('science_auto') or row['state']!='succeeded':
            raise DomainError('В проекте есть созданные пользователем материалы. Автоматическая замена сцен недоступна; сохраните копию проекта.',409)

def archive_auto_plan(service,connection,pid,keep_generation_id):
    require_replaceable(service,connection,pid,keep_generation_id)
    original=dict(connection.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
    archive_id=uid();original.update(id=archive_id,title=original['title']+' · предыдущий научный план',created_at=now())
    columns=list(original)
    connection.execute('INSERT INTO projects('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(original[k] for k in columns))
    for table in ('characters','locations','props','scenes','batches','assets'):
        connection.execute('UPDATE '+table+' SET project_id=? WHERE project_id=?',(archive_id,pid))
    connection.execute('UPDATE generations SET project_id=? WHERE project_id=? AND id!=?',(archive_id,pid,keep_generation_id))
    connection.execute('INSERT INTO project_trash(project_id,deleted_at) VALUES(?,?)',(archive_id,now()))
    service.db.event(connection,pid,'science_plan_archived',{'archive_project_id':archive_id})
    return archive_id

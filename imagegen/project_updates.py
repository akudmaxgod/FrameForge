"""Transactional project cursors; polling never reconstructs a full storyboard."""
import json

ACTIVE=('queued','preparing','submitted','running','saving','reconciling')

def install(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS project_update_versions(project_id TEXT PRIMARY KEY, content INTEGER NOT NULL DEFAULT 0, progress INTEGER NOT NULL DEFAULT 0)')
    connection.execute('DELETE FROM project_update_versions WHERE project_id NOT IN (SELECT id FROM projects)')
    connection.execute('INSERT OR IGNORE INTO project_update_versions(project_id) SELECT id FROM projects')
    connection.execute('CREATE INDEX IF NOT EXISTS generations_project_created ON generations(project_id,created_at)')
    connection.execute("CREATE INDEX IF NOT EXISTS generations_video_asset ON generations(asset_id) WHERE purpose='scene_video'")
    direct=('characters','locations','props','scenes','assets','reference_casts','story_audits','batches','project_edits','edit_exports','editorial_scene_plans','editorial_scene_applications','voice_plan_versions','voice_plan_applications')
    tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    mapping={table:lambda ref:f'{ref}.project_id' for table in direct if table in tables and 'project_id' in {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}}
    mapping['editorial_scene_applications']=lambda ref:f'(SELECT project_id FROM scenes WHERE id={ref}.scene_id)'
    mapping['projects']=lambda ref:f'{ref}.id'
    mapping['scene_revisions']=lambda ref:f'(SELECT project_id FROM scenes WHERE id={ref}.scene_id)'
    mapping['reviews']=lambda ref:f'(SELECT project_id FROM generations WHERE id={ref}.generation_id)'
    for table in ('generations','voice_workflows'):mapping[table]=lambda ref:f'{ref}.project_id'
    for table,project in mapping.items():
        for action in ('INSERT','UPDATE','DELETE'):
            if table=='projects' and action=='DELETE':
                connection.execute('''CREATE TRIGGER IF NOT EXISTS project_cursor_projects_delete AFTER DELETE ON projects BEGIN DELETE FROM project_update_versions WHERE project_id=OLD.id; END''')
                continue
            ref='OLD' if action=='DELETE' else 'NEW';pid=project(ref)
            content='1'
            if action=='UPDATE' and table=='generations':
                content="CASE WHEN NEW.state NOT IN ('queued','preparing','submitted','running','saving','reconciling') OR NEW.snapshot IS NOT OLD.snapshot OR NEW.asset_id IS NOT OLD.asset_id THEN 1 ELSE 0 END"
            if action=='UPDATE' and table=='voice_workflows':
                content="CASE WHEN NEW.result_json IS NOT OLD.result_json OR NEW.options_json IS NOT OLD.options_json OR NEW.state IN ('completed','failed','cancelled','ready','paused') THEN 1 ELSE 0 END"
            # No-op worker UPDATEs must not invalidate an otherwise idle client.
            columns=[r[1] for r in connection.execute(f'PRAGMA table_info({table})')]
            changed=' OR '.join(f'NEW."{c}" IS NOT OLD."{c}"' for c in columns) if action=='UPDATE' else '1'
            sql=f'''CREATE TRIGGER IF NOT EXISTS project_cursor_{table}_{action.lower()} AFTER {action} ON {table}
                WHEN ({pid}) IS NOT NULL AND ({changed}) BEGIN
                INSERT INTO project_update_versions(project_id,content,progress) VALUES({pid},{content},1)
                ON CONFLICT(project_id) DO UPDATE SET content=content+({content}),progress=progress+1;
                END'''
            connection.execute(sql)
    # Shared channel data is part of the project payload too.
    for table,channel in {
        'channel_profiles':lambda r:f'{r}.id',
        'channel_profile_versions':lambda r:f'{r}.channel_id',
        'channel_characters':lambda r:f'{r}.channel_id',
        'channel_reference_assets':lambda r:f'{r}.channel_id',
        'channel_character_versions':lambda r:f'(SELECT channel_id FROM channel_characters WHERE id={r}.character_id)',
    }.items():
        if table not in tables:continue
        columns=[r[1] for r in connection.execute(f'PRAGMA table_info({table})')]
        for action in ('INSERT','UPDATE','DELETE'):
            ref='OLD' if action=='DELETE' else 'NEW'
            changed=' OR '.join(f'NEW."{c}" IS NOT OLD."{c}"' for c in columns) if action=='UPDATE' else '1'
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS project_cursor_{table}_{action.lower()} AFTER {action} ON {table} WHEN {changed} BEGIN
                UPDATE project_update_versions SET content=content+1,progress=progress+1
                WHERE project_id IN (SELECT id FROM projects WHERE channel_profile_id={channel(ref)});
                END''')

def versions(service,pid):
    return service.db.one('SELECT content,progress FROM project_update_versions WHERE project_id=?',(pid,)) or {'content':0,'progress':0}

def poll(service,pid,content=None,progress=None):
    current=versions(service,pid)
    if str(content)!=str(current['content']) or str(progress)==str(current['progress']):return current
    fields='id,state,progress,error,attempts,started_at,finished_at,cancel_requested,provider_id'
    rows=service.db.all(f"SELECT {fields} FROM generations WHERE project_id=? AND state IN ('queued','preparing','submitted','running','saving','reconciling') ORDER BY created_at",(pid,))
    # Terminal states change content and cause a full refresh. Only active jobs need patches.
    rows=[{**r,'progress':json.loads(r['progress'] or '{}')} for r in rows if r['state'] in ACTIVE]
    voice=service.db.one('SELECT state,stage,progress,pause_requested,error,updated_at FROM voice_workflows WHERE project_id=?',(pid,))
    return {**current,'generations':rows,'voice_workflow':voice}

"""Bounded continuation of a user-started image batch, preserving old media."""
import json
from .db import dump
from .forgotten_cities.retries import RetryStore


def schedule(service,generation_id):
    if service.settings().get('source_contract_enabled') is not True:return None
    generation=service.db.one('SELECT * FROM generations WHERE id=?',(generation_id,))
    if not generation or not generation['batch_id']:return None
    if service.editing.get(generation['project_id'])['document']['clips']:return None
    snapshot=json.loads(generation['snapshot']);review=snapshot.get('source_fidelity_review') or {}
    if review.get('status')!='fail':return None
    scene=service.scene(generation['scene_id'])
    root=snapshot.get('auto_source_retry_of') or generation_id
    if scene['active_generation_id']!=root or scene.get('video_asset_id') or scene['current_revision']!=generation['scene_revision']:return None
    manual=service.db.one('SELECT status FROM reviews WHERE generation_id=?',(root,))
    if manual and manual['status'] in ('approved','rejected'):return None
    batch=service.db.one('SELECT state FROM batches WHERE id=?',(generation['batch_id'],))
    if not batch or batch['state'] not in ('running','completed','completed_with_errors'):return None
    retry=RetryStore(service.db).reject({**snapshot,'fidelity_candidate_id':generation_id},review)
    if retry['exhausted']:return None
    fresh=service.scene_snapshot(scene);fresh['source_fidelity_retry']=retry
    fresh['auto_source_retry_of']=root;fresh['auto_source_retry_parent']=generation_id
    with service.db.transaction() as connection:
        live=connection.execute('SELECT active_generation_id,current_revision,video_asset_id FROM scenes WHERE id=?',(scene['id'],)).fetchone()
        if live['active_generation_id']!=root or live['current_revision']!=scene['current_revision'] or live['video_asset_id']:return None
        exists=connection.execute("SELECT id FROM generations WHERE json_extract(snapshot,'$.auto_source_retry_parent')=? LIMIT 1",(generation_id,)).fetchone()
        if exists:return exists['id']
        current_batch=connection.execute('SELECT state FROM batches WHERE id=?',(generation['batch_id'],)).fetchone()
        if current_batch['state'] not in ('running','completed','completed_with_errors'):return None
        gid=service.insert_generation(connection,generation['project_id'],'scene',fresh,batch_id=generation['batch_id'],scene_id=scene['id'],scene_revision=scene['current_revision'])
        connection.execute("UPDATE batches SET state='running' WHERE id=?",(generation['batch_id'],))
        return gid


def select_checked(service,generation_id):
    if service.settings().get('source_contract_enabled') is not True:return False
    generation=service.db.one('SELECT * FROM generations WHERE id=?',(generation_id,))
    if not generation or generation['state']!='succeeded' or not generation['asset_id']:return False
    if service.editing.get(generation['project_id'])['document']['clips']:return False
    snapshot=json.loads(generation['snapshot']);root=snapshot.get('auto_source_retry_of')
    if not root or (snapshot.get('source_fidelity_review') or {}).get('status')!='pass':return False
    from .google_image_reviews import current_approval
    if not current_approval(service,generation,snapshot):return False
    with service.db.transaction() as connection:
        manual=connection.execute('SELECT status FROM reviews WHERE generation_id=?',(root,)).fetchone()
        if manual and manual['status'] in ('approved','rejected'):return False
        changed=connection.execute('UPDATE scenes SET active_generation_id=? WHERE id=? AND current_revision=? AND active_generation_id=? AND video_asset_id IS NULL',
            (generation_id,generation['scene_id'],generation['scene_revision'],root)).rowcount
        return bool(changed)

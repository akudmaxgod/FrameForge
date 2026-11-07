"""Explicit resolution of uncertain cloud attempts, without starting generation."""
import copy
import json
from .db import dump, now, uid
from .domain import DomainError


def manifest(service, project_id):
    service.project(project_id)
    rows=service.db.all('''SELECT s.id,s.position,s.current_revision,s.video_asset_id,r.data
        FROM scenes s JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision
        WHERE s.project_id=? ORDER BY s.position''',(project_id,))
    return {'scenes':[{'scene_id':r['id'],'position':r['position'],'current_revision':r['current_revision'],
        'video_asset_id':r['video_asset_id'],
        'resolved_attempt_keys':list((json.loads(r['data']).get('animation') or {}).get('resolved_attempts') or {})}
        for r in rows]}


def resolve_no_result(service, scene_id, payload):
    rejected=payload.get('confirmed_provider_rejection') is True
    message=str(payload.get('provider_message') or '').strip()
    if rejected and (not message or len(message)>2000):
        raise DomainError('Нужен точный текст отказа Flow (до 2000 символов).',400)
    if not rejected and payload.get('confirmed_no_result') is not True:
        raise DomainError('Сначала проверьте библиотеку Flow и подтвердите отсутствие результата.',400)
    scene=service.scene(scene_id); data=copy.deepcopy(scene['data']);anim=data.get('animation') or {}
    key=payload.get('idempotency_key');rev=payload.get('expected_scene_revision')
    if not isinstance(key,str) or not key or type(rev) is not int:
        raise DomainError('Не указана точная попытка и ревизия кадра.',400)
    decision='provider_rejected' if rejected else 'no_result'
    if key in (anim.get('resolved_attempts') or {}):
        if anim['resolved_attempts'][key].get('decision')!=decision:
            raise DomainError('Для этой попытки уже сохранено другое решение.',409)
        return {'resolved':True,'already_resolved':True,'scene':scene}
    if (scene.get('video_asset_id') or rev!=scene['current_revision'] or key!=anim.get('idempotency_key')
            or anim.get('selected_engine') not in ('omni_flash','veo_lite')):
        raise DomainError('Кадр или видеопопытка изменились. Обновите страницу.',409)
    job=service.db.one('SELECT * FROM veo_jobs WHERE project_id=? AND scene_id=? AND idempotency_key=?',
                       (scene['project_id'],scene_id,key))
    source=service.db.one('SELECT asset_id FROM generations WHERE id=?',(scene.get('active_generation_id'),))
    asset=service.db.one('SELECT sha256 FROM assets WHERE id=? AND project_id=?',
                         ((source or {}).get('asset_id'),scene['project_id']))
    if (not job or job['status']=='completed' or not source or not asset
            or job['source_image_asset_id']!=source['asset_id'] or job['source_image_sha256']!=asset['sha256']):
        raise DomainError('Исходник или состояние попытки изменились. Старый результат сохранён.',409)
    record={'decision':decision,'at':now()}
    if rejected:
        record.update(message=message,evidence_source='operator_flow_ui')
    anim.setdefault('resolved_attempts',{})[key]=record
    if rejected:
        # A confirmed refusal is terminal for the existing attempt, not a
        # command to retry it. Preserve the exact request/source identity.
        anim.update(status='failed',error='Flow отклонил запрос: '+message,
                    provider_rejection={'message':message,'evidence_source':'operator_flow_ui'})
    else:
        anim.update(status='planned_omni' if anim['selected_engine']=='omni_flash' else 'planned_veo',
                    request_nonce=uid(),prompt_contract='image-motion/3',error=None)
        anim.pop('idempotency_key',None)
    data['animation']=anim
    with service.db.transaction() as conn:
        live=conn.execute('''SELECT s.current_revision,s.active_generation_id,s.video_asset_id,r.data FROM scenes s JOIN scene_revisions r
            ON r.scene_id=s.id AND r.revision=s.current_revision WHERE s.id=?''',(scene_id,)).fetchone()
        current=(json.loads(live['data']).get('animation') or {}) if live else {}
        if (not live or live['video_asset_id'] or live['current_revision']!=rev
                or live['active_generation_id']!=scene.get('active_generation_id') or current.get('idempotency_key')!=key):
            raise DomainError('Кадр изменился во время подтверждения. Новая попытка не создана.',409)
        conn.execute("UPDATE veo_jobs SET status='failed',completed_at=?,error=? WHERE id=? AND status!='completed'",
                     (now(),('Подтверждённый отказ Flow: '+message) if rejected else 'Отсутствие результата подтверждено после проверки Flow.',job['id']))
        conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(scene_id,rev+1,dump(data),now()))
        conn.execute('UPDATE scenes SET current_revision=? WHERE id=?',(rev+1,scene_id))
        service.db.event(conn,scene['project_id'],'animation_attempt_resolved',{'scene_id':scene_id,
            'idempotency_key':key,'decision':decision})
    return {'resolved':True,'already_resolved':False,'scene':service.scene(scene_id)}

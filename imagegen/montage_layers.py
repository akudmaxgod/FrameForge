"""A generated segment remains the base; selected footage is an optional cover."""
import copy
import json
from .domain import DomainError

def duration_ms(clip):
    return (clip['trim_out_ms']-clip['trim_in_ms'])/clip.get('playback_rate',1)+clip.get('hold_last_ms',0)

def effective_clip(base):
    layer=base.get('footage_overlay')
    if not layer or not layer.get('enabled',True):return copy.deepcopy(base)
    clip=copy.deepcopy(layer)
    clip['edit_clip_id']=base['edit_clip_id'];clip['excluded']=base.get('excluded',False)
    clip['narration_origin_clip_id']=base.get('narration_origin_clip_id',base['edit_clip_id'])
    clip['trim_out_ms']=clip['trim_in_ms']+(duration_ms(base)-clip.get('hold_last_ms',0))*clip.get('playback_rate',1)
    clip['footage_overlay']=copy.deepcopy(layer)
    return clip

def generated_base(service,pid,clips,cover):
    """Recover the saved original, including its old animation and framing."""
    origin=cover.get('narration_origin_clip_id',clips[0].get('narration_origin_clip_id',clips[0]['edit_clip_id']))
    base=next((copy.deepcopy(c) for c in clips if c.get('source_kind','scene')=='scene' and len(clips)==1),None)
    if base is None:
        rows=service.db.all('SELECT target_document_json FROM media_match_proposals WHERE project_id=? ORDER BY created_at',(pid,))
        for row in rows:
            metadata=json.loads(row['target_document_json'] or '{}')
            candidates=[r['orig_clip'] for r in metadata.get('split_records',[]) if r.get('orig_clip')]
            candidates+=metadata.get('backup_doc',{}).get('clips',[])
            base=next((copy.deepcopy(c) for c in candidates if c.get('source_kind','scene')=='scene' and c.get('narration_origin_clip_id',c['edit_clip_id'])==origin),None)
            if base is not None:break
    if base is None:return None
    base.pop('footage_overlay',None)
    span=sum(duration_ms(c) for c in clips)
    if abs(duration_ms(base)-span)>.01:
        if base.get('media_type')!='image':return None
        base['trim_out_ms']=base['trim_in_ms']+span;base['hold_last_ms']=0;base['playback_rate']=1
    base['edit_clip_id']=cover['edit_clip_id'];base['narration_origin_clip_id']=origin
    overlay=copy.deepcopy(cover);overlay.pop('footage_overlay',None);overlay['enabled']=True
    overlay['origin_scene_id']=base['scene_id'];overlay['edit_clip_id']=base['edit_clip_id']
    base['footage_overlay']=overlay
    return base

def validate_layers(editing,pid,document):
    for base in document['clips']:
        layer=base.get('footage_overlay')
        if layer is None:continue
        if not isinstance(layer,dict) or 'footage_overlay' in layer or base.get('source_kind','scene')!='scene' or layer.get('source_kind')!='footage':
            raise DomainError('Недопустимый слой футажа.')
        if type(layer.get('enabled',True)) is not bool:raise DomainError('Недопустимое состояние слоя футажа.')
        enabled=layer.get('enabled',True);checked=copy.deepcopy(layer);checked.pop('enabled',None)
        checked['edit_clip_id']=base['edit_clip_id'];checked['excluded']=False
        checked['trim_out_ms']=checked['trim_in_ms']+(duration_ms(base)-checked.get('hold_last_ms',0))*checked.get('playback_rate',1)
        probe=editing.validate(pid,{'schema_version':1,'clips':[checked],'transitions':[],'audio_tracks':[],'source_audio':False})
        base['footage_overlay']={**probe['clips'][0],'enabled':enabled}

def migrate_layers(service,pid,document):
    result=copy.deepcopy(document);count=0
    for i,clip in enumerate(result['clips']):
        if clip.get('source_kind')!='footage' or not clip.get('origin_scene_id') or clip.get('footage_overlay'):continue
        restored=generated_base(service,pid,[clip],clip)
        if restored is not None:result['clips'][i]=restored;count+=1
    if count and result.get('voice_fit'):
        from .montage_voice_fit import source_signature
        result['voice_fit']['source_hash']=source_signature(result)
    return result,count

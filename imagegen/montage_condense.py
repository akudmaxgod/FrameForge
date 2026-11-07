"""Conservative reverse fit: propose exclusions, never delete media."""
import copy
import re
from .domain import DomainError
from .editing import normalize


def prepare(snap,keep_ids=None):
    from .montage_semantic import clip_texts
    active=[c for c in snap['document']['clips'] if not c.get('excluded')]
    voice=snap['audio_binding']
    if not voice:raise DomainError('Добавьте включённую озвучку.')
    target=voice['offset_ms']+voice['trim_out_ms']-voice['trim_in_ms']
    if target>=normalize(snap['document'])['duration_ms']-.01:
        raise DomainError('Этот режим сокращает видеоряд. Озвучка должна быть короче текущего монтажа.')
    texts={x['clip_id']:x for x in clip_texts(snap)}
    scenes={s['id']:s for s in snap['scenes']}
    def locked(c):return bool(c.get('timing_locked') or c.get('manual_timing'))
    def animated(c):
        a=scenes.get(c.get('scene_id'),{}).get('data',{}).get('animation',{})
        return c.get('media_type','video')=='video' and a.get('selected_engine') in ('omni_flash','veo_lite')
    keys={c['edit_clip_id']:re.sub(r'\s+',' ',texts[c['edit_clip_id']]['embedding_text']).strip().casefold() for c in active}
    for c in active:
        quote=scenes.get(c.get('scene_id'),{}).get('data',{}).get('source_quote')
        if isinstance(quote,str) and quote.strip():keys[c['edit_clip_id']]+='\nQUOTE:'+quote.strip()
    if keep_ids is not None:
        if not isinstance(keep_ids,list) or not all(isinstance(x,str) for x in keep_ids) or len(set(keep_ids))!=len(keep_ids):raise DomainError('Некорректный выбор кадров.')
        keep=set(keep_ids)
        if not keep or not keep<=set(keys):raise DomainError('Выберите хотя бы один существующий кадр.')
        if any(locked(c) and c['edit_clip_id'] not in keep for c in active):raise DomainError('Закреплённый клип нельзя исключить. Сначала снимите фиксацию.')
    else:
        keep=set(keys);groups={}
        for i,c in enumerate(active):groups.setdefault(keys[c['edit_clip_id']],[]).append((i,c))
        for group in groups.values():
            if len(group)<2:continue
            protected=[c for i,c in group if i in (0,len(active)-1) or locked(c) or animated(c)]
            winner=(protected or [group[0][1]])[0]['edit_clip_id']
            for i,c in group:
                if c['edit_clip_id']!=winner and c not in protected:keep.discard(c['edit_clip_id'])
    reduced=copy.deepcopy(snap)
    for c in reduced['document']['clips']:
        if not c.get('excluded') and c['edit_clip_id'] not in keep:c['excluded']=True
    rows=[{'clip_id':c['edit_clip_id'],'scene_id':c.get('scene_id'),'label':'Кадр '+str(scenes.get(c.get('scene_id'),{}).get('position',i+1)),
           'text':texts[c['edit_clip_id']]['text'],'included':c['edit_clip_id'] in keep,'animated':animated(c),'locked':locked(c),
           'reason':('Оставлен: анимация Flow' if animated(c) else 'Оставлен') if c['edit_clip_id'] in keep else ('Исключён вручную' if keep_ids is not None else 'Повтор описания: оставлен другой кадр с тем же содержанием')}
          for i,c in enumerate(active)]
    return reduced,rows


def finish(snap,proposal,rows):
    from .montage_voice_fit import source_signature
    from .montage_media import camera_plan
    proposal=copy.deepcopy(proposal);doc=proposal['document'];scenes={s['id']:s for s in snap['scenes']}
    for c in doc['clips']:
        if c.get('excluded'):continue
        scene=scenes.get(c.get('scene_id'),{});anim=scene.get('data',{}).get('animation',{})
        if anim.get('selected_engine')=='ken_burns' and scene.get('image_asset_id')==c.get('source_image_asset_id') and scene.get('image_width') and scene.get('image_height'):
            duration=(c['trim_out_ms']-c['trim_in_ms'])/c.get('playback_rate',1)+c.get('hold_last_ms',0)
            c.update(original_video_asset_id=c.get('source_video_asset_id') or c.get('original_video_asset_id'),media_type='image',source_video_asset_id=None,
                     source_duration_ms=86400000,trim_in_ms=0,trim_out_ms=duration,playback_rate=1,hold_last_ms=0,muted=True,
                     camera_motion=camera_plan(scene['image_width'],scene['image_height'],anim.get('ken_burns_preset') or 'gentle_push_in'))
    doc['voice_fit'].update(method='condense',source_hash=source_signature(doc))
    proposal.update(method='condense',timeline=normalize(doc),selection=rows,
        reduction={'before_ms':normalize(snap['document'])['duration_ms'],'after_ms':normalize(doc)['duration_ms'],
                   'excluded_count':sum(not r['included'] for r in rows),'kept_count':sum(r['included'] for r in rows)})
    proposal['warnings'].append({'code':'conservative_reduction','message':'Автоматически предложены только повторы одинаковых описаний. Уникальные события не исключаются автоматически. Проверьте список; любой кадр можно вернуть до применения.'})
    return proposal

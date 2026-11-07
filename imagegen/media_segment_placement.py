"""One continuous source per narration segment, without image/video sandwiches."""
import copy
import hashlib
import json
from pathlib import Path
from .db import uid,now,dump
from .domain import DomainError
from .editing import normalize

POLICY='whole-narration-segment/1'

def logical_segments(document,timeline=None):
    timeline=timeline or normalize(document)
    result=[]
    for clip in timeline['clips']:
        origin=clip.get('narration_origin_clip_id',clip['edit_clip_id'])
        if result and result[-1]['narration_origin_clip_id']==origin and abs(result[-1]['end_ms']-clip['start_ms'])<.01:
            group=result[-1];group['replace_clip_ids'].append(clip['edit_clip_id']);group['end_ms']=clip['end_ms'];group['duration_ms']=group['end_ms']-group['start_ms']
            group['scene_id']=group.get('scene_id') or clip.get('scene_id') or clip.get('origin_scene_id')
            if clip.get('source_kind')!='footage':group['source_kind']='scene'
        else:
            group=copy.deepcopy(clip)
            group.update(narration_origin_clip_id=origin,replace_clip_ids=[clip['edit_clip_id']])
            group['scene_id']=clip.get('scene_id') or clip.get('origin_scene_id')
            result.append(group)
    return result

def source_window(service,candidate,duration_ms,inspect=True):
    """An analysis chunk is not a montage duration limit; verify the actual interval."""
    result=copy.deepcopy(candidate)
    if candidate.get('media_type')=='image':return result
    total=candidate.get('media_duration_ms')
    if total is None:
        item=service.db.one('SELECT * FROM media_library_items WHERE id=?',(candidate['item_id'],))
        if not item:return None
        total=item.get('duration_ms',0);result.setdefault('file_path',item['file_path'])
    if float(total)+.01<duration_ms:return None
    start=min(max(0,float(candidate.get('seg_start_ms',candidate.get('start_ms',0)))),float(total)-duration_ms)
    end=start+duration_ms
    result.update(seg_start_ms=start,seg_end_ms=end,start_ms=start,end_ms=end,duration_ms=duration_ms,media_duration_ms=total)
    if not inspect:return result
    source=service.root/result['file_path']
    if not source.is_file():return None
    stat=source.stat()
    key=hashlib.sha256(f'{POLICY}:{source}:{stat.st_size}:{stat.st_mtime_ns}:{start:.3f}:{end:.3f}'.encode()).hexdigest()
    directory=service.root/'outputs'/'media_library'/'windows';directory.mkdir(parents=True,exist_ok=True)
    manifest=directory/(key+'.json')
    if manifest.is_file():
        cached=json.loads(manifest.read_text(encoding='utf-8'))
        if not cached['continuous']:return None
        result['keyframe_paths_json']=dump(cached['frames']);return result
    import av
    import cv2
    frames=[];previous=None;last=-1e9;continuous=True
    with av.open(str(source)) as media:
        stream=media.streams.video[0];media.seek(max(0,int(start/1000/float(stream.time_base))),stream=stream)
        for frame in media.decode(stream):
            t=float(frame.pts*stream.time_base)*1000 if frame.pts is not None else 0
            if t<start:continue
            if t>end:break
            if t-last<450:continue
            image=frame.to_ndarray(format='bgr24');small=cv2.resize(image,(160,90));hsv=cv2.cvtColor(small,cv2.COLOR_BGR2HSV)
            hist=cv2.calcHist([hsv],[0,1],None,[16,16],[0,180,0,256]);cv2.normalize(hist,hist,alpha=0,beta=1,norm_type=cv2.NORM_MINMAX)
            if previous is not None and cv2.compareHist(previous,hist,cv2.HISTCMP_CORREL)<.55:
                continuous=False;break
            previous=hist;last=t
    if continuous:
        from .media_library import extract_keyframe
        for i,fraction in enumerate((.1,.5,.9)):
            path=directory/f'{key}-{i}.jpg'
            if extract_keyframe(source,start+duration_ms*fraction,path):frames.append(str(path.relative_to(service.root)).replace('\\','/'))
    manifest.write_text(dump({'continuous':continuous,'frames':frames}),encoding='utf-8')
    if not continuous or not frames:return None
    result['keyframe_paths_json']=dump(frames)
    return result

def apply_full_segments(service,pid,proposal_id,proposals,current,coverage=None):
    before=copy.deepcopy(current['document']);doc=copy.deepcopy(before)
    if before.get('voice_fit',{}).get('stale'):raise DomainError('Подгонка к озвучке устарела. Обновите её перед вставкой.',409)
    originals={c['edit_clip_id']:c for c in before['clips']};order=list(originals);replacements={};consumed=set();records=[];edges={}
    from .insert_coverage import ensure_unanimated, assert_budget
    from .insert_coverage import policy as coverage_policy
    from .insert_photo_motion import animate_photo
    photo_motion = coverage_policy(coverage)['photo_motion']
    target_ids = {cid for p in proposals for cid in (p.get('replace_clip_ids') or [p['edit_clip_id']])}
    target_clips = [c for c in before['clips'] if c['edit_clip_id'] in target_ids]
    ensure_unanimated(service, pid, target_clips, coverage)
    for p in proposals:
        ids=p.get('replace_clip_ids') or [p['edit_clip_id']]
        if any(i not in originals or i in consumed for i in ids):raise DomainError('Сегменты монтажа изменились. Пересчитайте подбор.',409)
        index=order.index(ids[0])
        if order[index:index+len(ids)]!=ids:raise DomainError('Части сегмента были перемещены. Пересчитайте подбор.',409)
        old=[copy.deepcopy(originals[i]) for i in ids]
        duration=normalize({'clips':old,'transitions':[]})['duration_ms']
        if p.get('replacement_clip'):
            replacement=copy.deepcopy(p['replacement_clip']);replacement['edit_clip_id']=uid()
        else:
            cand=source_window(service,p['candidate_segment'],duration,inspect=False)
            if cand is None:raise DomainError('Футаж слишком короткий для целого сегмента. Монтаж не изменён.',409)
            item=service.db.one('SELECT * FROM media_library_items WHERE id=? AND project_id=?',(cand['item_id'],pid))
            if not item:raise DomainError('Материал больше не доступен в этом проекте.',409)
            asset=service.db.one('SELECT id,generation_id FROM assets WHERE path=? AND project_id=?',(item['file_path'],pid))
            if asset:
                generation=service.db.one('SELECT purpose FROM generations WHERE id=?',(asset['generation_id'],))
                if not generation or generation['purpose'] not in ('footage_insert','edit_video','edit_image','edit_footage','edit_thumbnail'):
                    raise DomainError('Материал имеет другое назначение. Импортируйте его в медиатеку отдельно.',409)
                aid=asset['id']
            else:
                aid,gid=uid(),uid()
                with service.db.transaction() as c:
                    c.execute("INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at) VALUES(?,?,'footage_insert','succeeded','{}',0,?,?)",(gid,pid,aid,now()))
                    c.execute('INSERT INTO assets(id,project_id,generation_id,path,sha256,width,height,bytes) VALUES(?,?,?,?,?,?,?,?)',(aid,pid,gid,item['file_path'],item['sha256'],item['width'],item['height'],item['size_bytes']))
            image=cand['media_type']=='image';start=0 if image else cand['seg_start_ms']
            replacement={'edit_clip_id':uid(),'title':'Футаж: '+cand['original_name'],'source_kind':'footage','scene_id':None,
                         'origin_scene_id':p.get('scene_id') or next((c.get('scene_id') for c in old if c.get('scene_id')),None),
                         'media_type':'image' if image else 'video','source_image_asset_id':aid if image else None,
                         'source_video_asset_id':None if image else aid,'source_scene_revision':0,'trim_in_ms':start,'trim_out_ms':start+duration,
                         'playback_rate':1,'hold_last_ms':0,'muted':True,'illustrative':True,'framing':p.get('framing','fit')}
        replacement['placement_policy']=POLICY
        replacement['placement_mode']='automatic'
        animate_photo(service, pid, replacement, photo_motion, len(records))
        replacement['narration_origin_clip_id']=p.get('narration_origin_clip_id',old[0].get('narration_origin_clip_id',ids[0]))
        if replacement.get('source_kind')=='footage':
            from .montage_layers import generated_base
            layered=generated_base(service,pid,old,replacement)
            if layered is None:raise DomainError('Не удалось сохранить исходный кадр под футажом.',409)
            replacement=layered
        replacements[ids[0]]=replacement;consumed.update(ids)
        records.append({'insert_clip_id':replacement['edit_clip_id'],'orig_clips':old})
        for old_id in ids:edges[old_id]=replacement['edit_clip_id']
    doc['clips']=[replacements.get(c['edit_clip_id'],c) for c in before['clips'] if c['edit_clip_id'] not in consumed or c['edit_clip_id'] in replacements]
    transitions=[]
    for t in doc.get('transitions',[]):
        t['left']=edges.get(t['left'],t['left']);t['right']=edges.get(t['right'],t['right'])
        if t['left']!=t['right']:transitions.append(t)
    doc['transitions']=transitions
    if abs(normalize(doc)['duration_ms']-normalize(before)['duration_ms'])>.01:raise DomainError('Замена меняет длительность стыков. Монтаж не изменён.',409)
    if doc.get('voice_fit'):
        from .montage_voice_fit import source_signature
        doc['voice_fit']['source_hash']=source_signature(doc)
    assert_budget(before, doc, coverage)
    saved=service.editing.save(pid,{'document':doc,'expected_revision':current['revision']},
                              guard=lambda: ensure_unanimated(service, pid, target_clips, coverage))
    applied_clips={c['edit_clip_id']:c for c in saved['document']['clips']}
    for record in records:record['applied_clip']=applied_clips[record['insert_clip_id']]
    metadata={'placement_policy':POLICY,'backup_doc':before,'applied_revision':saved['revision'],'group_records':records}
    with service.db.transaction() as c:c.execute("UPDATE media_match_proposals SET status='applied',target_document_json=?,updated_at=? WHERE id=?",(dump(metadata),now(),proposal_id))
    return {'status':'applied','applied_count':len(records),'new_revision':saved['revision'],'document':saved['document']}

def rollback_full_segments(service,pid,proposal_id,metadata):
    current=service.editing.get(pid)
    if current['revision']==metadata['applied_revision']:doc=metadata['backup_doc']
    else:
        current_clips={c['edit_clip_id']:c for c in current['document']['clips']}
        if any(r.get('applied_clip')!=current_clips.get(r['insert_clip_id']) for r in metadata['group_records']):
            raise DomainError('Вставки изменены вручную после расстановки. Откат сохранит правки: используйте историю монтажа.',409)
        doc=copy.deepcopy(current['document']);records={r['insert_clip_id']:r['orig_clips'] for r in metadata['group_records']};clips=[]
        for c in doc['clips']:clips.extend(copy.deepcopy(records.get(c['edit_clip_id'],[c])))
        doc['clips']=clips
        for t in doc.get('transitions',[]):
            if t['left'] in records:t['left']=records[t['left']][-1]['edit_clip_id']
            if t['right'] in records:t['right']=records[t['right']][0]['edit_clip_id']
        if doc.get('voice_fit') and not doc['voice_fit'].get('stale'):
            from .montage_voice_fit import source_signature
            doc['voice_fit']['source_hash']=source_signature(doc)
    saved=service.editing.save(pid,{'document':doc,'expected_revision':current['revision']})
    with service.db.transaction() as c:c.execute("UPDATE media_match_proposals SET status='rolled_back',updated_at=? WHERE id=?",(now(),proposal_id))
    return {'status':'rolled_back','new_revision':saved['revision'],'document':saved['document']}

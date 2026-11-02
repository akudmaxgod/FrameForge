"""Draft voice timing outside the production analyzer and generation code."""
import math
import re
import copy
import json
from .voice_alignment import summarize_range

def scene_voice_timing(project,scene):
    """Applied narration timelines outrank timing kept on preserved old scenes."""
    from .domain import DomainError
    metadata=project.get('analysis_metadata') or {}
    if isinstance(metadata,str):metadata=json.loads(metadata)
    timeline=metadata.get('narration_timeline')
    if timeline:
        matches=[f for f in timeline.get('frames',[]) if f.get('scene_id')==scene['id']]
        if len(matches)!=1:raise DomainError('Кадр не имеет единственной привязки к озвучке. Обновите план.',409)
        timing=matches[0].get('timing')
    else:timing=scene.get('data',{}).get('voice_timing')
    if timing is None:return None
    if not isinstance(timing,dict) or any(type(timing.get(k)) not in (int,float) or not math.isfinite(timing[k]) for k in ('start','end','duration')):
        raise DomainError('Некорректная длительность кадра по озвучке.',409)
    if timing['start']<0 or timing['end']<=timing['start'] or abs(timing['end']-timing['start']-timing['duration'])>1e-6:
        raise DomainError('Длительность кадра не соответствует временной шкале озвучки.',409)
    return copy.deepcopy(timing)

MOTION=[('gentle_push_in','Мягкое приближение'),('gentle_pull_back','Мягкое отдаление'),('gentle_pan_right','Плавный проход вправо'),('gentle_pan_left','Плавный проход влево')]

def timed_frames(story,words,scenes,duration,limit=12):
    frames=[]
    for parent,scene in enumerate(scenes):
        a,b=scene['source_start'],scene['source_end']
        assert story[a:b]==scene['source_quote'],'Source text mismatch'
        subset=[w for w in words if a<=w['char_start']<b and w['start'] is not None]
        if not subset:raise ValueError('No speech support for scene '+str(parent+1))
        seconds=max(.01,subset[-1]['end']-subset[0]['start'])
        count=max(1,math.ceil(seconds/limit));cuts=[0]
        for piece in range(1,count):
            target=subset[0]['start']+seconds*piece/count
            candidates=range(cuts[-1]+2,len(subset)-2*(count-piece))
            def score(i):
                text=story[subset[i-1]['char_end']:subset[i]['char_start']]
                return abs(subset[i]['start']-target)-(0.8 if any(c in text for c in ',;:—') else 0)
            if candidates:cuts.append(min(candidates,key=score))
        cuts.append(len(subset))
        for piece,(left,right) in enumerate(zip(cuts,cuts[1:])):
            start=a if piece==0 else subset[left]['char_start']
            end=b if piece==len(cuts)-2 else subset[right]['char_start']
            # Keep punctuation; trim only whitespace at a proposed shot boundary.
            while start<end and story[start].isspace():start+=1
            while end>start and story[end-1].isspace():end-=1
            quote=story[start:end]
            timing=summarize_range(story,words,start,end)
            preset,label=MOTION[len(frames)%len(MOTION)]
            frames.append({'position':len(frames)+1,'parent_position':parent+1,'parent_scene_id':scene.get('id'),'part':piece+1,'parts':len(cuts)-1,'source_start':start,'source_end':end,'source_quote':quote,'speech_start':timing['start'],'speech_end':timing['end'],'timing_needs_review':timing['needs_review'],'timing_reasons':timing['reasons'],'visual_description':scene.get('beat',''),'time_layer':scene.get('time_layer',''),'scene_draft':scene,'engine':'ken_burns','camera_preset':preset,'camera_label':label,'omni_selected':False})
    for i,f in enumerate(frames):
        f['start']=0 if i==0 else f['speech_start']
        f['end']=frames[i+1]['speech_start'] if i+1<len(frames) else duration
        f['duration']=f['end']-f['start']
        assert f['duration']>0,('Nonpositive timing',i+1)
        assert story[f['source_start']:f['source_end']]==f['source_quote']
    assert abs(sum(f['duration'] for f in frames)-duration)<1e-6
    return frames

def verify_plan(frames,story,duration):
    assert frames and frames[0]['start']==0 and abs(frames[-1]['end']-duration)<1e-6
    for i,f in enumerate(frames):
        assert f['end']>f['start']
        assert story[f['source_start']:f['source_end']]==f['source_quote']
        if i:assert abs(frames[i-1]['end']-f['start'])<1e-6
    return True


def semantic_timed_frames(story,words,scenes,duration,limit=12):
    """Split long events only at supported sentence boundaries, never mid-clause."""
    drafts=[]
    dependent=re.compile(r'^(?:он|она|они|оно|его|её|ее|это|поэтому|потому|he|she|they|it|therefore|because)\b',re.I)
    for scene in scenes:
        a,b=scene['source_start'],scene['source_end']
        subset=[w for w in words if a<=w['char_start']<b and w.get('start') is not None]
        cuts=[a];last=subset[0]['start'] if subset else 0
        if subset and subset[-1]['end']-last>limit:
            for left,right in zip(subset,subset[1:]):
                between=story[left['char_end']:right['char_start']]
                word=story[left['char_start']:left['char_end']]
                following=story[right['char_start']:b].lstrip()
                sentence_end=bool(re.search(r'[.!?]',between)) and word.casefold() not in ('г','гг','им','т','д','тд','mr','dr')
                if sentence_end and not dependent.match(following) and right['start']-last>=limit*.7:
                    cuts.append(right['char_start']);last=right['start']
        cuts.append(b)
        for start,end in zip(cuts,cuts[1:]):
            while start<end and story[start].isspace():start+=1
            while end>start and story[end-1].isspace():end-=1
            drafts.append({**scene,'source_start':start,'source_end':end,'source_quote':story[start:end],
                'semantic_parent_source':{'start':a,'end':b,'quote':story[a:b]}})
    # Disable the legacy duration-based word splitter after semantic selection.
    frames=timed_frames(story,words,drafts,duration,max(duration,limit)+1)
    for frame in frames:
        frame['boundary_method']='source_event_or_sentence'
        if frame['duration']>limit:
            frame['timing_needs_review']=True
            frame['timing_reasons']=list(frame['timing_reasons'])+['Длинное смысловое событие: нужна проверка возможного разделения.']
    return frames

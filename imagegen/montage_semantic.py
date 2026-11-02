"""Local semantic scene/narration binding; never creates source quotations."""
import copy
import hashlib
import math
import re
from collections import Counter
import numpy as np
from .domain import DomainError
from .editing import normalize

VERSION='montage-semantic/1'
MODEL='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MODEL_REVISION='e8f8c211226b894fcb81acc59f3b34ba3efd5f42'

def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def clip_texts(snap):
    scenes={s['id']:s for s in snap['scenes']}
    result=[]
    for c in snap['document']['clips']:
        if c.get('excluded'):continue
        data=scenes.get(c.get('scene_id'),{}).get('data',{})
        text=data.get('manual_prompt') or data.get('source_quote') or data.get('beat') or data.get('text') or c.get('title')
        if not isinstance(text,str) or not text.strip():raise DomainError('У клипа нет описания для смысловой привязки: '+c['edit_clip_id'])
        hint=data.get('prompt_narration_hint') or ''
        if not isinstance(hint,str):raise DomainError('Некорректная подсказка озвучки для клипа.')
        result.append({'clip_id':c['edit_clip_id'],'text':text.strip(),'narration_hint':hint,
            'text_sha256':digest(text.strip()+'\n\0'+hint)})
    if not result:raise DomainError('Добавьте готовые клипы в монтаж.')
    sentences=[re.split(r'(?<=[.!?])\s+',x['text']) for x in result]
    common=Counter(sentence.strip() for parts in sentences for sentence in set(parts[1:]) if sentence.strip())
    threshold=max(3,math.ceil(len(result)*.6))
    for item,parts in zip(result,sentences):
        # Repeated style-only suffixes drown out the scene description. Keep
        # the first descriptive sentence, and keep the original prompt intact.
        item['embedding_text']=item['narration_hint'] or ' '.join([parts[0]]+[p for p in parts[1:] if common[p.strip()]<threshold])
    return result

def speech_units(asr,voice,count):
    """Atomic phrases contain actual ASR words and measured audio timestamps."""
    words=[];previous=-1
    for segment in asr.get('segments',[]):
        for word in segment.get('words',[]):
            a,b=word.get('start'),word.get('end')
            if (type(a) not in (int,float) or type(b) not in (int,float) or
                not math.isfinite(a+b) or not 0<=a<b):continue
            if a+0.03<previous:raise DomainError('Распознанные слова идут в обратном порядке; нужна проверка записи.')
            previous=a
            if b*1000<=voice['trim_in_ms'] or a*1000>=voice['trim_out_ms']:continue
            text=str(word.get('word','')).strip()
            if not text:continue
            words.append({'text':text,'start_ms':max(voice['offset_ms'],voice['offset_ms']+a*1000-voice['trim_in_ms']),
                'end_ms':min(voice['offset_ms']+voice['trim_out_ms']-voice['trim_in_ms'],voice['offset_ms']+b*1000-voice['trim_in_ms']),
                'probability':float(word.get('probability',0))})
    if len(words)<count:raise DomainError('Произносимых слов меньше, чем клипов. Сократите число клипов или задайте границы вручную.')
    # Enough anchors even for many five-second shots. Punctuation and pauses
    # are preferred, without making a sentence an indivisible unit.
    max_words=max(1,min(6,len(words)//max(1,count*2)))
    units=[];pending=[]
    for i,w in enumerate(words):
        pending.append(w)
        gap=i+1==len(words) or words[i+1]['start_ms']-w['end_ms']>=350
        dangling=re.sub(r'\W','',w['text'].casefold()) in {'и','а','но','или','в','во','на','с','со','к','ко','по','для','от','до','из','что','чтобы','не','and','or','the','of','to','with'}
        if (not dangling and len(pending)>=max_words) or len(pending)>=max_words+3 or w['end_ms']-pending[0]['start_ms']>=3500 or gap or re.search(r'[.!?;:]$',w['text']):
            units.append({'text':' '.join(x['text'] for x in pending),'start_ms':pending[0]['start_ms'],'end_ms':pending[-1]['end_ms'],
                'confidence':sum(x['probability'] for x in pending)/len(pending),'words':pending});pending=[]
    if pending:units.append({'text':' '.join(x['text'] for x in pending),'start_ms':pending[0]['start_ms'],'end_ms':pending[-1]['end_ms'],
        'confidence':sum(x['probability'] for x in pending)/len(pending),'words':pending})
    if len(units)<count:raise DomainError('Не хватает временных опор для всех клипов.')
    return units

def partition(scene_vectors,unit_vectors,units,target,offset=0):
    """Globally optimal contiguous partition. No scene reordering or gaps."""
    s=np.asarray(scene_vectors,dtype=np.float64);u=np.asarray(unit_vectors,dtype=np.float64)
    n,m=len(s),len(u)
    if s.ndim!=2 or u.ndim!=2 or s.shape[1]!=u.shape[1] or n<1 or m<n or len(units)!=m or not np.isfinite(s).all() or not np.isfinite(u).all():
        raise DomainError('Некорректные смысловые векторы.')
    if np.any(np.linalg.norm(s,axis=1)<1e-8) or np.any(np.linalg.norm(u,axis=1)<1e-8):raise DomainError('Пустые смысловые векторы.')
    if n>2000 or m>30000:raise DomainError('Слишком много опор для одного расчёта.')
    s=s/np.maximum(np.linalg.norm(s,axis=1,keepdims=True),1e-9)
    u=u/np.maximum(np.linalg.norm(u,axis=1,keepdims=True),1e-9)
    prefix=np.vstack([np.zeros((1,u.shape[1])),np.cumsum(u,axis=0)])
    edges=np.array([0]+[x['start_ms'] for x in units[1:]]+[target],dtype=np.float64)
    if np.any(np.diff(edges)<=0):raise DomainError('Временные опоры перекрываются; нужна проверка распознавания.')
    ratio=m/n;limit=max(8,math.ceil(ratio*4));expected=max(1,(target-offset)/n)
    bonuses=np.zeros(m+1)
    for j in range(1,m):
        last=units[j-1]['text']
        bonuses[j]=.09 if re.search(r'[.!?;:]$',last) else .025 if last.endswith(',') else 0
        if units[j]['start_ms']-units[j-1]['end_ms']>=250:bonuses[j]+=.04
    previous=np.full(m+1,-np.inf);previous[0]=0;back=np.full((n,m+1),-1,dtype=np.int32)
    for i in range(n):
        current=np.full(m+1,-np.inf)
        # Vectorize all possible ends for each interval length.
        for length in range(1,min(limit,m)+1):
            ends=np.arange(max(i+1,length),min(m,m-(n-i-1))+1)
            starts=ends-length
            valid=(starts>=i)&np.isfinite(previous[starts])
            ends=ends[valid];starts=starts[valid]
            if not len(ends):continue
            vectors=prefix[ends]-prefix[starts]
            similarity=(vectors@s[i])/np.maximum(np.linalg.norm(vectors,axis=1),1e-9)
            duration=edges[ends]-edges[starts]
            # Weak duration regularizer avoids giving an unrelated scene all
            # narration; actual scene semantics determine the boundaries.
            score=similarity*np.sqrt(length/ratio)-.16*np.square(np.log(np.maximum(duration,1)/expected))+bonuses[ends]
            candidates=previous[starts]+score
            better=candidates>current[ends]
            current[ends[better]]=candidates[better];back[i,ends[better]]=starts[better]
        previous=current
    if not np.isfinite(previous[m]):raise DomainError('Не удалось построить непрерывный порядок привязок.')
    spans=[];end=m
    for i in range(n-1,-1,-1):
        start=int(back[i,end]);spans.append((start,end));end=start
    spans.reverse()
    return spans,edges,s,u

def match(snap,asr,scene_vectors,unit_vectors,units):
    voice=snap['audio_binding'];texts=clip_texts(snap)
    if len(scene_vectors)!=len(texts):raise DomainError('Векторы относятся к другому набору сцен.')
    if abs(asr.get('audio_duration',0)*1000-next(a for a in snap['document']['audio_tracks'] if a['role']=='voice')['source_duration_ms'])>100:
        raise DomainError('Распознавание относится к записи другой длительности.')
    target=voice['offset_ms']+voice['trim_out_ms']-voice['trim_in_ms']
    spans,edges,s,u=partition(scene_vectors,unit_vectors,units,target,voice['offset_ms'])
    bindings=[]
    for i,((a,b),source) in enumerate(zip(spans,texts)):
        v=np.mean(u[a:b],axis=0);v=v/max(float(np.linalg.norm(v)),1e-9)
        score=float(s[i]@v)
        others=[float(s[j]@v) for j in range(max(0,i-2),min(len(s),i+3)) if j!=i]
        margin=score-max(others) if others else 1
        confidence=float(np.mean([x['confidence'] for x in units[a:b]]))
        reasons=[]
        if score<.38:reasons.append('Слабое смысловое совпадение')
        if margin<.025:reasons.append('Похожие соседние сцены')
        if confidence<.65 or min(x['confidence'] for x in units[a:b])<.5:reasons.append('Неуверенное распознавание речи')
        if edges[b]-edges[a]>20000:reasons.append('Длинный интервал сцены')
        bindings.append({**source,'unit_start':a,'unit_end':b,'start_ms':float(edges[a]),'end_ms':float(edges[b]),
            'narration':' '.join(x['text'] for x in units[a:b]),'similarity':round(score,4),'margin':round(margin,4),
            'asr_confidence':round(confidence,4),'needs_review':bool(reasons),'reasons':reasons,'reviewed':False})
    return {'version':VERSION,'model':MODEL,'model_revision':MODEL_REVISION,'audio_binding':copy.deepcopy(voice),
        'bindings':bindings,'units':units,'boundaries_ms':[float(edges[a]) for a,b in spans]+[target],
        'review_revision':0,'similarity_is_probability':False}

def propose(snap,mapping,decisions=None):
    """Bindings live in montage, not generated prompts or forged story quotes."""
    from .montage_voice_fit import source_signature
    mapping=copy.deepcopy(mapping);doc=copy.deepcopy(snap['document']);voice=snap['audio_binding']
    if mapping['audio_binding']!=voice:raise DomainError('Привязки относятся к другой озвучке.',409)
    texts=clip_texts(snap);bindings=mapping['bindings'];active=[c for c in doc['clips'] if not c.get('excluded')]
    if [c['edit_clip_id'] for c in active]!=[b['clip_id'] for b in bindings]:raise DomainError('Порядок клипов изменился.',409)
    if [t['text_sha256'] for t in texts]!=[b['text_sha256'] for b in bindings]:raise DomainError('Промпты изменились после сопоставления.',409)
    decisions=decisions or []
    if not isinstance(decisions,list) or not all(isinstance(d,dict) for d in decisions):raise DomainError('Некорректный список подтверждений.')
    by_id={b['clip_id']:b for b in bindings}
    changed_boundaries=set()
    if len({d.get('clip_id') for d in decisions})!=len(decisions):raise DomainError('Повторное решение для одного клипа.')
    for d in decisions:
        b=by_id.get(d.get('clip_id'))
        if not b or type(d.get('accepted')) is not bool:raise DomainError('Некорректное подтверждение привязки.')
        if 'start_ms' in d:
            value=d['start_ms']
            if type(value) not in (int,float) or not math.isfinite(value):raise DomainError('Некорректная ручная граница.')
            if b is bindings[0] and value!=0:raise DomainError('Первый кадр должен начинаться с нуля.')
            if abs(b['start_ms']-value)>.01:
                changed_boundaries.add(bindings.index(b))
                b['start_ms']=value;b['manual_boundary']=True
        b['reviewed']=d['accepted']
    # Moving a join changes both phrases. The old phrase text must not be
    # approved by a checkbox that was ticked before the new text was shown.
    for index in changed_boundaries:
        for affected in [index-1,index]:
            if affected<0:continue
            b=bindings[affected];b['reviewed']=False;b['needs_review']=True
            reason='Граница изменена вручную: проверьте обновлённую фразу'
            if reason not in b['reasons']:b['reasons'].append(reason)
    target=voice['offset_ms']+voice['trim_out_ms']-voice['trim_in_ms']
    boundaries=[b['start_ms'] for b in bindings]+[target]
    if boundaries[0]!=0 or any(b<a+1000/24 for a,b in zip(boundaries,boundaries[1:])):
        raise DomainError('Границы должны идти по порядку и оставлять каждому клипу хотя бы один видеокадр.')
    if any(x<voice['offset_ms'] for x in boundaries[1:-1]) or boundaries[-2]>=target:
        raise DomainError('Ручная граница находится за пределами озвучки.')
    old={c['edit_clip_id']:c for c in normalize(doc)['clips']};scenes={s['id']:s for s in snap['scenes']}
    conflicts=[];warnings=[];changes=[]
    for i,(c,b) in enumerate(zip(active,bindings)):
        start,end=boundaries[i:i+2];b['end_ms']=end
        chosen=[u for u in mapping['units'] if start<=u['start_ms']<end]
        b['narration']=' '.join(u['text'] for u in chosen)
        if b['needs_review'] and not b['reviewed']:
            conflicts.append({'code':'semantic_review','clip_id':b['clip_id'],'message':'Подтвердите смысловую привязку клипа '+str(i+1)+': '+', '.join(b['reasons'])})
        scene=scenes.get(c.get('scene_id'));previous=old[c['edit_clip_id']]
        if scene and c.get('source_scene_revision')!=scene['current_revision']:
            conflicts.append({'code':'stale_scene','clip_id':b['clip_id'],'message':'Обновите источник изменённой сцены.'});continue
        if (c.get('timing_locked') or c.get('manual_timing')) and (abs(previous['start_ms']-start)>.01 or abs(previous.get('base_end_ms',previous['end_ms'])-end)>.01):
            conflicts.append({'code':'manual_boundary','clip_id':b['clip_id'],'message':'Клип закреплён вручную. Снимите фиксацию для подгонки.'});continue
        rate=c.get('playback_rate',1);moving=(c['trim_out_ms']-c['trim_in_ms'])/rate;duration=end-start
        c['hold_last_ms']=max(0,duration-moving)
        if duration<moving:c['trim_out_ms']=c['trim_in_ms']+duration*rate
        if c['hold_last_ms']:c['muted']=True
        c['voice_fit_auto']=True;c.pop('voice_source_binding',None)
        c['semantic_source_binding']={k:copy.deepcopy(b[k]) for k in ('text_sha256','narration','start_ms','end_ms','similarity','reviewed','needs_review')}
        c['semantic_source_binding'].update(method='semantic_inferred',audio_sha256=voice['sha256'])
        changes.append({'clip_id':b['clip_id'],'scene_id':c.get('scene_id'),'label':'Клип '+str(i+1),'quote':'',
            'old_start_ms':previous['start_ms'],'old_end_ms':previous.get('base_end_ms',previous['end_ms']),
            'new_start_ms':start,'new_end_ms':end,'hold_last_ms':c['hold_last_ms'],'playback_rate':rate})
    mapping['boundaries_ms']=boundaries
    doc['timing_mode']='voice_fit'
    doc['voice_fit']={'version':'montage-voice-fit/1','method':'semantic','phrase_alignment':True,
        'audio_binding':copy.deepcopy(voice),'story_sha256':digest(snap['story']),'source_hash':source_signature(doc),
        'stale':False,'cues':[],'semantic_mapping':{k:copy.deepcopy(v) for k,v in mapping.items() if k!='units'}}
    timeline=normalize(doc)
    if abs(timeline['duration_ms']-target)>.01:conflicts.append({'code':'duration_mismatch','message':'Расчёт не покрывает озвучку целиком.'})
    return {'version':VERSION,'method':'semantic','document':doc,'changes':changes,'conflicts':conflicts,'warnings':warnings,
        'can_apply':not conflicts,'timeline':timeline,'boundaries_ms':boundaries,'mapping':mapping,
        'timing_accuracy_measured':False,'short_video_policy':'hold'}

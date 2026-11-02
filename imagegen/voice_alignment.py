"""Local speech/reference alignment with explicit approximate timings."""
import json,re,difflib,math,hashlib,bisect
from numbers import Real
from pathlib import Path
from .voice_source import speech_projection, link_destinations

TOKEN=re.compile(r'\d+(?:[.,]\d+)?|[\w]+(?:[-‑–][\w]+)*',re.UNICODE)
def norm(text):
    if re.fullmatch(r'\d+(?:[.,]\d+)?',text):return '#'+text.replace(',','.')
    return re.sub(r'[^\w]','',text.casefold().replace('ё','е'))
def reference_words(story):
    return [{'text':m.group(),'char_start':m.start(),'char_end':m.end(),'key':norm(m.group()),'method':'unmatched','start':None,'end':None,'confidence':0} for m in TOKEN.finditer(speech_projection(story))]
def recognized_words(segments):
    words=[]
    for s in segments:
        for w in s['words']:
            # A zero-duration ASR token cannot be an acoustic anchor. Keeping
            # it in the matcher can shadow a correctly timed retry duplicate.
            if (not all(isinstance(w.get(k),Real) and not isinstance(w[k],bool) and math.isfinite(w[k]) for k in ('start','end'))
                    or w['start']<0 or w['end']<=w['start']):
                continue
            # Some ASR tokenizers emit a decimal comma as the next word.
            decimal=re.fullmatch(r'\s*[.,](\d+)\s*',w['word'])
            if decimal and words and re.fullmatch(r'\d+',words[-1]['text']) and 0<=w['start']-words[-1]['end']<=.15:
                previous=words[-1];previous['text']+='.'+decimal.group(1);previous['key']=norm(previous['text'])
                previous['end']=w['end'];previous['confidence']=min(previous['confidence'],w['probability']);continue
            parts=list(TOKEN.finditer(w['word']));weight=sum(len(m.group()) for m in parts) or 1;cursor=float(w['start'])
            for part in parts:
                length=(float(w['end'])-float(w['start']))*len(part.group())/weight
                words.append({'text':part.group(),'key':norm(part.group()),'start':cursor,'end':cursor+length,'confidence':float(w['probability']),'compound':len(parts)>1});cursor+=length
    return words

def number_units(words,heard=False):
    """Match whole number phrases before character-level fuzzy edits."""
    from .voice_numbers import value
    result=[];i=0
    while i<len(words):
        roman=False
        if heard:
            for k in range(i+1,min(i+9,len(words))):
                if re.match(r'^(?:век|тысячелет|столет)',words[k]['key']):
                    roman=all(w['key'] in ('и','или','по','до','в','начале','начала','началу','началом','конце','конца','концу','концом','середине','середины','середину') or re.fullmatch(r'[IVXLCDM]+|\d+',w['text']) for w in words[i+1:k]);break
        best=None
        for end in range(i+1,min(i+9,len(words))+1):
            number=value([w['text'] for w in words[i:end]],allow_roman=roman)
            if number is not None:best=(end,number)
        if best:
            end,number=best;result.append({'key':'number:'+str(number.normalize()),'words':words[i:end],'number':number});i=end
        else:result.append({'key':words[i]['key'],'words':[words[i]],'number':None});i+=1
    return result

def assign_number(ref,heard,number):
    start,end=heard[0]['start'],heard[-1]['end']
    if end>start and all(h['end']>h['start'] for h in heard):
        group={'char_start':ref[0]['char_start'],'char_end':ref[-1]['char_end'],
               'start':start,'end':end,'recognized_text':' '.join(w['text'] for w in heard),'value':str(number)}
        for w in ref:
            assign(w,{'start':start,'end':end,'confidence':min(h['confidence'] for h in heard)},'normalized_group')
            w['timing_group']=group.copy()
def assign(ref,heard,method):
    if not all(type(heard[k]) in (int,float) and math.isfinite(heard[k]) for k in ('start','end')) or heard['start']<0 or heard['end']<=heard['start']:
        ref['timing_error']='Ненадёжная длительность распознанного слова'
        return
    ref.update(start=heard['start'],end=heard['end'],confidence=heard['confidence'],method=method)
def replacements(ref,heard,allow_roman=False):
    if not ref or not heard:return
    from .voice_numbers import value
    number=value([w['text'] for w in ref])
    spoken=value([w['text'] for w in heard],allow_roman=allow_roman)
    if number is not None and spoken is not None and number!=spoken:
        for w in ref:w['number_mismatch']={'expected':str(number),'recognized':str(spoken)}
        return
    if number is not None and number==spoken:
        assign_number(ref,heard,number)
        return
    # Written compound names can be split differently by the speech model.
    joined_r=''.join(w['key'] for w in ref);joined_h=''.join(w['key'] for w in heard)
    numeric_tokens=any(value([w['text']],allow_roman=allow_roman) is not None for w in ref+heard)
    if not numeric_tokens and min(len(ref),len(heard))==1 and max(len(ref),len(heard))<=4 and difflib.SequenceMatcher(None,joined_r,joined_h).ratio()>=.72:
        duration=heard[-1]['end']-heard[0]['start'];weight=sum(len(w['key']) for w in ref);cursor=heard[0]['start']
        for w in ref:
            end=cursor+duration*len(w['key'])/weight;assign(w,{'start':cursor,'end':end,'confidence':min(h['confidence'] for h in heard)},'compound');cursor=end
        return
    if len(ref)>40 or len(heard)>40:return
    # Exact neighbouring blocks anchor this small, monotonic local edit distance.
    rows,cols=len(ref),len(heard);dp=[[0.0]*(cols+1) for _ in range(rows+1)];steps={}
    for i in range(1,rows+1):dp[i][0]=i*.8;steps[i,0]='delete'
    for j in range(1,cols+1):dp[0][j]=j*.8;steps[0,j]='insert'
    for i in range(1,rows+1):
        for j in range(1,cols+1):
            similarity=difflib.SequenceMatcher(None,ref[i-1]['key'],heard[j-1]['key']).ratio()
            score,step=min((dp[i-1][j-1]+(1-similarity),'pair'),(dp[i-1][j]+.8,'delete'),(dp[i][j-1]+.8,'insert'))
            dp[i][j]=score;steps[i,j]=step
    i,j=rows,cols
    while i or j:
        step=steps[i,j]
        if step=='pair':
            from .voice_numbers import FORMS
            rn=value([ref[i-1]['text']])
            # Whisper writes royal ordinals as Roman digits. Only a proven
            # ordinal in this aligned reference position permits that form.
            ordinal=FORMS.get(ref[i-1]['key'],(None,False))[1]
            hn=value([heard[j-1]['text']],allow_roman=allow_roman or ordinal)
            if rn is not None and hn is not None and rn!=hn:
                ref[i-1]['number_mismatch']={'expected':str(rn),'recognized':str(hn)}
            elif rn is not None and rn==hn:
                assign_number([ref[i-1]],[heard[j-1]],rn)
            elif rn is None and hn is None and difflib.SequenceMatcher(None,ref[i-1]['key'],heard[j-1]['key']).ratio()>=.68:
                assign(ref[i-1],heard[j-1],'fuzzy')
            i-=1;j-=1
        elif step=='delete':i-=1
        else:j-=1
def align_words(story,asr):
    ref=reference_words(story);heard=recognized_words(asr['segments'])
    ref_units=number_units(ref);heard_units=number_units(heard,heard=True)
    matcher=difflib.SequenceMatcher(None,[w['key'] for w in ref_units],[w['key'] for w in heard_units],autojunk=False)
    for tag,a,b,c,d in matcher.get_opcodes():
        if tag=='equal':
            for r,h in zip(ref_units[a:b],heard_units[c:d]):
                if r['number'] is not None:assign_number(r['words'],h['words'],r['number'])
                else:assign(r['words'][0],h['words'][0],'exact' if not h['words'][0]['compound'] else 'compound')
        elif tag=='replace':
            following=ref_units[b]['key'] if b<len(ref_units) else ''
            replacements([w for u in ref_units[a:b] for w in u['words']],[w for u in heard_units[c:d] for w in u['words']],allow_roman=bool(re.match(r'^(?:век|тысячелет|столет)',following)))
    # Small holes get reviewable suggestions, never acoustic-confidence claims.
    from .voice_numbers import value
    for word in ref:
        if word['start'] is None and value([word['text']]) is not None:word['numeric_unresolved']=True
    i=0
    while i<len(ref):
        if ref[i]['start'] is not None:i+=1;continue
        j=i
        while j<len(ref) and ref[j]['start'] is None:j+=1
        left=ref[i-1]['end'] if i else 0
        right=ref[j]['start'] if j<len(ref) else asr['audio_duration']
        if j-i<=12 and not any(w.get('number_mismatch') or w.get('numeric_unresolved') for w in ref[i:j]) and left is not None and right is not None and 0<right-left<=12:
            weight=sum(max(1,len(w['key'])) for w in ref[i:j]);cursor=left
            for w in ref[i:j]:
                end=cursor+(right-left)*max(1,len(w['key']))/weight;w.update(start=cursor,end=end,method='interpolated',confidence=0);cursor=end
        i=j
    return ref,heard
def summarize_range(story,words,start,end):
    subset=[w for w in words if w['char_start']<end and w['char_end']>start]
    matched=[w for w in subset if w['start'] is not None]
    if not matched:return {'text':story[start:end],'start':None,'end':None,'needs_review':True,'reasons':['Нет акустических опор'],'words':len(subset)}
    reasons=[]
    if len(matched)!=len(subset):reasons.append('Часть слов не сопоставлена')
    if any(w.get('number_mismatch') for w in subset):reasons.append('Число в озвучке отличается от числа в сценарии')
    if subset and subset[0]['method']=='interpolated':reasons.append('Начало рассчитано между опорными словами')
    if subset and subset[-1]['method']=='interpolated':reasons.append('Конец рассчитан между опорными словами')
    if any(w['method']=='interpolated' for w in subset):reasons.append('Есть слова с приблизительным временем')
    if any(w.get('timing_group') and (start>w['timing_group']['char_start'] or end<w['timing_group']['char_end']) for w in subset):
        reasons.append('Граница проходит внутри числительного с общим временем группы')
    if any(w['confidence']<.55 for w in matched if w['method']!='interpolated'):reasons.append('Низкая уверенность распознавания')
    if subset and any(w['start'] is not None and w['end']-w['start']<.02 for w in (subset[0],subset[-1])):reasons.append('Опорное слово на границе имеет слишком короткую длительность')
    return {'text':story[start:end],'start':matched[0]['start'],'end':matched[-1]['end'],'needs_review':bool(reasons),'reasons':reasons,'words':len(subset),'acoustic_words':sum(w['method'] in ('exact','fuzzy','compound','normalized_group') for w in subset),'exact_words':sum(w['method']=='exact' for w in subset),'char_start':start,'char_end':end,'timing_accuracy_measured':False}

def unsupported_passages(story, words):
    """A good global percentage cannot certify an entire missing phrase."""
    result=[];group=[]
    for word in [*words, {'method':'sentinel'}]:
        if word['method'] in ('unmatched','interpolated'):
            group.append(word)
        else:
            if len(group)>=4:
                a,b=group[0]['char_start'],group[-1]['char_end']
                result.append({'start':a,'end':b,'quote':story[a:b],'word_count':len(group)})
            group=[]
    return result
def build_report(snapshot,asr):
    story=snapshot['project']['story'];words,heard=align_words(story,asr)
    sentences=[]
    for m in re.finditer(r'\S[\s\S]*?(?:[.!?]+(?=\s|$)|$)',speech_projection(story)):
        row=summarize_range(story,words,m.start(),m.end())
        if row['start'] is not None:sentences.append(row)
    scenes=[]
    for scene in snapshot['scenes']:
        data=scene['data'];start=data['source_start'];end=data['source_end']
        assert story[start:end]==data['source_quote'],'Scene/source mismatch'
        row=summarize_range(story,words,start,end);row.update(scene_id=scene['id'],position=scene['position'],scene_revision=scene['current_revision'])
        if row['start'] is not None:scenes.append(row)
    # Proposed coverage includes pauses until the next scene. Keep actual speech
    # endpoints separately so users can review both before adopting a schedule.
    for i,s in enumerate(scenes):
        s['proposed_start']=0 if i==0 else s['start']
        s['proposed_end']=scenes[i+1]['start'] if i+1<len(scenes) else asr['audio_duration']
        s['proposed_duration']=s['proposed_end']-s['proposed_start']
        if s['proposed_duration']<=0:s['needs_review']=True;s['reasons'].append('Границы сцен пересекаются')
    count={method:sum(w['method']==method for w in words) for method in ['exact','fuzzy','compound','normalized_group','interpolated','unmatched']}
    acoustic=count['exact']+count['fuzzy']+count['compound']+count['normalized_group']
    relevant=[w for w in words if w['start'] is not None]
    reverse=[i for i,(a,b) in enumerate(zip(relevant,relevant[1:])) if a['start']>b['start']+.001]
    return {'schema_version':1,'prototype':True,'project_id':snapshot['project']['id'],'story_sha256':hashlib.sha256(story.encode()).hexdigest(),'nonspoken_spans':link_destinations(story),'unsupported_passages':unsupported_passages(story,words),'audio_duration':asr['audio_duration'],'model':asr['model'],'metrics':{'reference_words':len(words),'recognized_words':len(heard),'methods':count,'acoustic_coverage':acoustic/max(1,len(words)),'covered_words':len(relevant),'covered_acoustic_coverage':acoustic/max(1,len(relevant)),'scene_count':len(scenes),'scene_review_count':sum(s['needs_review'] for s in scenes),'sentence_count':len(sentences),'sentence_review_count':sum(s['needs_review'] for s in sentences),'backwards_word_starts':len(reverse)},'scenes':scenes,'sentences':sentences,'words':words}

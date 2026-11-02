"""Source-bound text frames after the two-pass global entity/event analysis.

The registry/cache describe the story, not a requested editing density. This
projection is deliberately local, repeatable for different targets, and makes
no provider calls. It does not certify the visual interpretation of narration.
"""
import bisect
import copy
import hashlib
import heapq
import json
import re
from functools import lru_cache

from razdel import sentenize

VERSION='text-storyboard/2'
WORDS=re.compile(r"[^\W_]+(?:[-’'][^\W_]+)*",re.UNICODE)

@lru_cache(maxsize=16000)
def lemma(word):
    from ..named_event_coverage import _morph
    morph=_morph()
    return morph.parse(word)[0].normal_form if morph else word.casefold()

def tokens(text):
    return tuple(lemma(m.group().casefold()) for m in WORDS.finditer(text))

def _labels(characters):
    result={}
    for index,c in enumerate(characters):
        aliases=c.get('aliases') or []
        if isinstance(aliases,str):aliases=json.loads(aliases)
        for label in [c.get('name'),c.get('canonical_name'),*aliases]:
            key=tokens(label or '')
            if key:result.setdefault(key,set()).add(index)
    return {k:next(iter(v)) for k,v in result.items() if len(v)==1}

def _mentions(text,labels):
    words=tokens(text);found={}
    for label,index in labels.items():
        for at in range(len(words)-len(label)+1):
            if words[at:at+len(label)]==label:
                found.setdefault(index,at)
                break
    return sorted(found,key=lambda i:found[i])

def _cuts(story,target):
    words=list(WORDS.finditer(story))
    if not words:raise ValueError('Для раскадровки нужен текст с содержательными словами.')
    # Never manufacture empty/repeated frames to satisfy an impossible budget.
    count=min(target,len(words))
    starts=[m.start() for m in words]
    candidates={m.start():1 for m in words[1:]}
    sentences=list(sentenize(story))
    for s in sentences[1:]:candidates[s.start]=4
    for m in re.finditer(r'[,;:—–](?:[»”\"])?\s+(?=\S)',story):
        at=m.end()
        if at in candidates:candidates[at]=3
    paragraphs=[m.end() for m in re.finditer(r'\n\s*\n\s*(?=\S)',story)]
    for at in paragraphs:
        if at in candidates:candidates[at]=5
    boundaries=[0,*paragraphs,len(story)] if len(paragraphs)+1<=count else [0,len(story)]
    boundaries=sorted(set(boundaries))
    pool=sorted(candidates)
    def best(a,b):
        ai,bi=bisect.bisect_left(starts,a),bisect.bisect_left(starts,b)
        total=bi-ai
        if total<2:return None
        ranked=[]
        for cut in pool[bisect.bisect_right(pool,a):bisect.bisect_left(pool,b)]:
            left=bisect.bisect_left(starts,cut)-ai;right=total-left
            if min(left,right)<1:continue
            # Prefer punctuation, while avoiding a pathological one-word tail
            # when a more balanced source boundary is available.
            rank=2*left*right*(1+.16*(candidates[cut]-1))
            ranked.append((rank,-abs(left-right),-cut,cut))
        if not ranked:return None
        rank,_,_,cut=max(ranked)
        return (-rank,a,b,cut)
    heap=[]
    for a,b in zip(boundaries,boundaries[1:]):
        item=best(a,b)
        if item:heapq.heappush(heap,item)
    while len(boundaries)-1<count and heap:
        _,a,b,cut=heapq.heappop(heap)
        bisect.insort(boundaries,cut)
        for left,right in [(a,cut),(cut,b)]:
            item=best(left,right)
            if item:heapq.heappush(heap,item)
    spans=[]
    for a,b in zip(boundaries,boundaries[1:]):
        while a<b and story[a].isspace():a+=1
        while b>a and story[b-1].isspace():b-=1
        spans.append((a,b))
    return spans,sentences

def plan(analysis,story,target):
    if target is None:return analysis
    from ..domain import MAX_SCENES
    if type(target) is not int or not 1<=target<=MAX_SCENES:
        raise ValueError('Некорректное выбранное количество кадров.')
    source_hash=hashlib.sha256(story.encode()).hexdigest()
    previous=(analysis.get('diagnostics') or {}).get('storyboard') or {}
    if (previous.get('version')==VERSION and previous.get('requested_frames')==target
            and previous.get('story_sha256')==source_hash
            and len(analysis.get('scenes',[]))==previous.get('actual_frames')):
        from ..source_contract import require_complete_coverage
        require_complete_coverage(story,analysis['scenes'])
        return analysis
    result=copy.deepcopy(analysis)
    # A second density request must not treat already generated frames as new
    # semantic episodes. Retain the original lightweight event description.
    episodes=copy.deepcopy(previous.get('semantic_episodes') or result.get('scenes') or [])
    spans,sentences=_cuts(story,target)
    from ..subject_policy import enrich, normalize_scene
    characters=enrich(result.get('characters',[]), result.get('cast_bible'));labels=_labels(characters)
    sentence_starts=[s.start for s in sentences]
    sentence_context=[];last_actors=[];previous_end=0
    for s in sentences:
        if '\n\n' in story[previous_end:s.start]:last_actors=[]
        named=_mentions(s.text,labels)
        physical=[];mentioned=[];depicted=[]
        for index in named:
            c=characters[index]
            # Representation and attribution are distinct from a living actor.
            terms=[c.get('name',''),c.get('canonical_name','')]
            # Limit this check to the grammatical prefix of this named mention.
            matches=[re.search(re.escape(term.split()[0][:max(3,len(term.split()[0])-2)]),s.text,re.I) for term in terms if term]
            hit=next((m for m in matches if m),None)
            prefix=s.text[max(0,hit.start()-65):hit.start()] if hit else ''
            if re.search(r'(?:портрет\w*|стату\w*|бюст\w*|изображени\w*|portrait of|statue of)\s+(?:\w+\s+){0,3}$',prefix,re.I):depicted.append(index)
            elif re.search(r'(?:по словам|как писал\w*|как отмечал\w*|цитат\w*|автор\w*|according to|as written by)\s+(?:\w+\s+){0,2}$',prefix,re.I):mentioned.append(index)
            else:physical.append(index)
        pronoun=re.match(r'^\s*(?:(?:потом|затем|но|и|тогда|then|but|and)\s+)?(он|она|he|she)\b',s.text,re.I)
        if pronoun:
            from ..named_event_coverage import _morph
            morph=_morph();gender='femn' if pronoun[1].casefold() in ('она','she') else 'masc'
            antecedents=[]
            for index in last_actors:
                given=WORDS.search(characters[index]['name'])
                parsed=morph.parse(given.group())[0] if morph and given else None
                if parsed and parsed.tag.gender==gender:antecedents.append(index)
            if len(antecedents)==1:physical=list(dict.fromkeys([antecedents[0],*physical]))
        if physical:last_actors=list(physical)
        sentence_context.append((physical,mentioned,depicted))
        previous_end=s.stop
    frames=[]
    for number,(a,b) in enumerate(spans,1):
        si=max(0,bisect.bisect_right(sentence_starts,a)-1)
        selected=[i for i in range(si,len(sentences)) if sentences[i].start<b and sentences[i].stop>a]
        physical=list(dict.fromkeys(i for n in selected for i in sentence_context[n][0]))
        mentioned=list(dict.fromkeys(i for n in selected for i in sentence_context[n][1]))
        depicted=list(dict.fromkeys(i for n in selected for i in sentence_context[n][2]))
        quote=story[a:b]
        complete_a=min([a,*[sentences[i].start for i in selected]])
        complete_b=max([b,*[sentences[i].stop for i in selected]])
        # Select original episode only for provenance, never copy its summary,
        # participant list, medical state, location or guessed era into a frame.
        parent=max(enumerate(episodes),key=lambda pair:max(0,min(b,pair[1].get('source_end',0))-max(a,pair[1].get('source_start',0))),default=(-1,{}))
        names=[characters[i]['name'] for i in physical]
        cids=[characters[i].get('cast_id') or characters[i].get('id') or characters[i]['name'] for i in physical]
        loc='';lid=''
        for location in result.get('locations',[]):
            label=tokens(location['name']);context=tokens(story[complete_a:complete_b])
            if label and any(context[n:n+len(label)]==label for n in range(len(context)-len(label)+1)):
                loc=location['name'];lid=location.get('id','');break
        frame={
            'beat':quote,'heading':'','source_start':a,'source_end':b,'source_quote':quote,
            'source_alignment':'exact_text_frame','analysis_authority':'deepseek-v2',
            'characters':names,'character_ids':cids,'visible_people':len(names),'raw_visible_people':len(names),
            'population_policy':'scene_participants' if names else 'source_unspecified','episodic_people':[],
            'mentioned_characters':[characters[i]['name'] for i in mentioned],
            'depicted_people':[characters[i]['name'] for i in depicted],
            'offscreen_characters':[],'voice_only_characters':[],
            'location':loc or 'Обстановка по исходному фрагменту','location_ref':loc,'location_id':lid,'time':'',
            'shot':['wide shot','medium shot','close-up'][(number-1)%3],
            'emotion':'','wardrobe':{n:'default' for n in names},'props':[],'negative':'',
            'continuity_before':'','continuity_after':'',
            'duration_sec':round(max(1,len(re.sub(r'\s+',' ',quote))/15),3),
            'storyboard_frame':{'version':VERSION,'position':number,'semantic_episode':parent[0]+1,
                'sentence_source':{'start':complete_a,'end':complete_b,'quote':story[complete_a:complete_b]},
                'interpretation_status':'source_bound_draft','location_unresolved':not bool(loc),'semantic_accuracy_measured':False},
        }
        frames.append(normalize_scene(frame, characters))
    result['scenes']=frames
    info={'version':VERSION,'requested_frames':target,'actual_frames':len(frames),
          'semantic_episodes_count':len(episodes),'semantic_episodes':episodes,
          'story_sha256':source_hash,'method':'local_source_partition_after_global_analysis',
          'source_coverage_complete':True,'semantic_accuracy_measured':False,'additional_model_requests':0}
    result.setdefault('diagnostics',{}).update(storyboard=info,scenes_count=len(frames))
    result.setdefault('warnings',[]).append('Кадры разделены по точным фрагментам сценария; локальная раскадровка не подтверждает смысловую точность изображения.')
    if len(frames)!=target:
        result['warnings'].append(f'Выбрано {target} кадров; текст допускает {len(frames)} непустых фрагментов. Повторяющиеся пустые кадры не добавлялись.')
    result['diagnostics']['warnings']=list(result['warnings'])
    result['diagnostics']['unresolved_warnings_count']=len(result['warnings'])
    from ..source_contract import require_complete_coverage
    require_complete_coverage(story,frames)
    return result

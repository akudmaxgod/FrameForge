"""Source-backed guard for named actors lost by a compressed scene plan.

Not a certificate of full semantic coverage. No additional model requests,
name lists, invented places, experimental details or historical assumptions.
"""
import copy
import hashlib
import json
import re
from functools import lru_cache

VERSION = 'named-event-coverage/1'
_WORDS = re.compile(r'[А-ЯЁа-яёA-Za-z]+(?:[-‑–][А-ЯЁа-яёA-Za-z]+)*')
_CONTEXT = re.compile(r'по\s+(?:словам|мнению|гипотезе)|согласно|цитат|портрет|стату|бюст|именем|назван|авторств|в\s+(?:книге|труде|работе)\b', re.I)
_NON_ASSERTED = re.compile(r'\b(?:не|никогда|если|возможно|вероятно|мог|могла|могли|будет|будут|предположим)\b|[?«»"]',re.I)
_MENTAL = {'считать','полагать','думать','верить','предполагать','упоминать','упоминаться','называться','называть','существовать','означать','быть'}

@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except (ImportError,OSError):
        return None

def _list(value):
    return json.loads(value) if isinstance(value,str) else value or []

def _pattern(label):
    words=_WORDS.findall(label)
    return r'(?<!\w)'+r'[\s\-‑–—]+'.join(re.escape(w) for w in words)+r'(?!\w)' if words else r'(?!)'

def audit(story, characters, scenes):
    """Find an explicit named grammatical subject and asserted finite action.

    Missing/ambiguous morphology stays unresolved, never becomes a person
    merely because the name appears somewhere in a large source_quote.
    """
    used=set()
    for row in scenes:
        data=row.get('data',row)
        used.update(map(str,data.get('character_ids') or []))
        used.update(map(str,data.get('characters') or []))
    morph=_morph();events=[];unresolved=[]
    all_aliases={}
    for c in characters:
        for label in [c.get('name'),c.get('canonical_name'),*_list(c.get('aliases'))]:
            if label:all_aliases.setdefault(label.casefold(),set()).add(c.get('cast_id') or c.get('id') or c['name'])
    sentences=list(re.finditer(r'[^.!?\n]+(?:[.!?]+|$)',story))
    for c in characters:
        ids={str(v) for v in (c.get('id'),c.get('cast_id'),c.get('name'),c.get('canonical_name')) if v}
        if ids & used:continue
        cid=c.get('cast_id') or c.get('id') or c['name']
        labels=list(dict.fromkeys(v for v in [c.get('name'),c.get('canonical_name'),*_list(c.get('aliases'))] if v))
        candidates=[]
        for span in sentences:
            text=span.group();hits=[]
            for label in labels:
                if len(all_aliases[label.casefold()])!=1:continue
                hits.extend(re.finditer(_pattern(label),text,re.I))
            if not hits:continue
            hit=min(hits,key=lambda h:h.start())
            if _CONTEXT.search(text) or _NON_ASSERTED.search(text):continue
            # Do not steal another actor's action through an object, possessive
            # phrase, co-ordination or subordinate attribution clause.
            before=text[:hit.start()]
            if ',' in before or any(p.tag.POS=='VERB' for w in _WORDS.findall(before) for p in (morph.parse(w)[:1] if morph else [])):continue
            name_words=_WORDS.findall(hit.group())
            if morph and name_words:
                parses=morph.parse(name_words[0])
                if any('Name' in p.tag for p in parses) and not any(p.tag.case in ('nomn',None) for p in parses):continue
            after=text[hit.end():]
            words=_WORDS.findall(after)
            action=None
            for index,word in enumerate(words[:10]):
                parses=morph.parse(word)[:1] if morph else []
                if parses and parses[0].tag.POS=='VERB':
                    parse=parses[0]
                    attributed = parse.normal_form in {'писать','говорить','утверждать','рассказывать','отмечать','сообщать'} and any(w.casefold() in {'что','о','об','будто','как'} for w in words[index+1:index+3])
                    if parse.normal_form not in _MENTAL and parse.tag.tense!='futr' and not attributed:action=parse.normal_form
                    break
            if not action:continue
            a=span.start()+len(text)-len(text.lstrip());b=span.end()
            quote=story[a:b]
            evidence_id=hashlib.sha256((str(cid)+':'+str(a)+':'+str(b)+':'+quote).encode()).hexdigest()[:20]
            candidates.append({'event_id':evidence_id,'character_id':cid,'name':c['name'],'source_start':a,'source_end':b,'source_quote':quote,'predicate':action})
        # One minimally supported episode recovers the actor; do not expand a
        # whole biography or treat this narrow guard as an exhaustive planner.
        if candidates:events.append(candidates[0])
        elif any(re.search(_pattern(label),story,re.I) for label in labels):
            unresolved.append({'character_id':cid,'name':c['name'],'status':'mention_or_unresolved','automatic_scene_added':False})
    return {'version':VERSION,'method':'unrepresented_named_subject_action','semantic_completeness_measured':False,
            'events':sorted(events,key=lambda e:e['source_start']),'unresolved':unresolved}

def repair(analysis,story,*,max_scenes=100):
    result=copy.deepcopy(analysis)
    report=audit(story,result.get('characters',[]),result.get('scenes',[]))
    report['added']=[]
    for event in report['events']:
        if len(result['scenes'])>=max_scenes:
            raise ValueError('План достиг лимита сцен, но именной эпизод не показан: '+event['name'])
        quote=event['source_quote'];name=event['name']
        # Use exact source wording only. No nearby modern place, co-actor,
        # props or technical procedure may leak into this recovered episode.
        scene={'beat':quote,'characters':[name],'character_ids':[event['character_id']],
               'visible_people':1,'raw_visible_people':1,'population_policy':'scene_participants',
               'source_quote':quote,'source_start':event['source_start'],'source_end':event['source_end'],
               'source_alignment':'exact_named_event','event_id':'coverage-'+event['event_id'],
               'event_coverage_recovery':{'version':VERSION,'evidence':event},
               'analysis_authority':'source_named_event_recovery','location':'','location_ref':'','location_id':'',
               'time':'','shot':'medium shot','duration_sec':8,'wardrobe':{name:'default'},'props':[],
               'mentioned_characters':[],'offscreen_characters':[],'voice_only_characters':[],
               'depicted_people':[],'episodic_people':[],'negative':'',
               'continuity_before':'','continuity_after':''}
        parent=next((row for row in result['scenes'] if type(row.get('source_start')) is int and row['source_start']<=event['source_start'] and event['source_end']<=row.get('source_end',0)),None)
        if parent:
            # This is an extra visual shot inside an already narrated block,
            # not a second reading of that block. Retain explicit provenance
            # rather than falsifying or discarding old source boundaries.
            scene['intentional_visual_reuse']=True
            scene['reuse_reason']='Дополнительный визуальный эпизод по точной цитате внутри ранее сгруппированного блока; без повторной озвучки.'
            scene['narration_policy']='reuse_existing_audio'
            scene['parent_source_span']={'start':parent['source_start'],'end':parent['source_end']}
        position=next((i+1 for i,row in enumerate(result['scenes']) if row is parent),len(result['scenes']))
        result['scenes'].insert(position,scene)
        report['added'].append(event)
        result.setdefault('warnings',[]).append('Добавлен пропущенный именной эпизод по точной цитате: '+name)
    result.setdefault('diagnostics',{})['named_event_coverage']=report
    return result

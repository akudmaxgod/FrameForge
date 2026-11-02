"""Export settings from source-grounded event bindings, never from NER alone."""
import hashlib,re
from functools import lru_cache

@lru_cache(maxsize=1)
def morphology():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()

def name_supported(name,quote):
    def forms(word):return {p.normal_form for p in morphology().parse(word)}
    words=re.findall(r'[^\W\d_]+|\d+',str(quote).casefold())
    source=set().union(*(forms(w) for w in words)) if words else set()
    labels=re.findall(r'[^\W\d_]+|\d+',str(name).casefold())
    return bool(labels) and all(forms(w)&source for w in labels)

def name_key(value):
    return re.sub(r'\s+',' ',str(value).casefold().replace('ё','е')).strip()

def literal(record,story,end):
    a,b=record.get('start'),record.get('end')
    return (type(a) is int and type(b) is int and 0<=a<b<=end
            and story[a:b]==record.get('quote') and name_supported(record.get('name',''),record.get('quote','')))

def build(doc,story,selected_event_ids):
    spatial=doc.metadata.get('spatial_grounding',{})
    records=spatial.get('evidence',[]);assignments=spatial.get('event_locations',{})
    candidates=list(dict.fromkeys(str(r.get('name')) for r in records if r.get('name')))
    candidates+=list(dict.fromkeys(e.canonical_name for e in doc.entities
        if getattr(e.entity_type,'value',e.entity_type)=='location' and e.canonical_name not in candidates))
    aliases={name_key(n):n for n in candidates}
    # An alias is identity evidence, not a claim that both places are on screen.
    for n,name in enumerate(candidates):
        for other in candidates[:n]:
            match=re.search(re.escape(other)+r'\s*,?\s+(?:или|он\s+же|также\s+называемый)\s+'+re.escape(name)+r'(?!\w)',story,re.I)
            naming=bool(match and (re.search(r'назван\w*|называ\w*|извест\w*|имену\w*',story[max(0,match.start()-80):match.start()],re.I)
                or re.search(r'он\s+же|также\s+называемый',match.group(),re.I)))
            if naming:
                aliases[name_key(name)]=aliases[name_key(other)];break
    cards={};history=[];bindings={};rejected=[]
    for event in sorted(doc.events,key=lambda e:e.span.start):
        for record in records:
            if record.get('event_id')!=event.event_id or record.get('kind')!='physical':continue
            if literal(record,story,event.span.end):history.append(record)
            else:rejected.append({'event_id':event.event_id,'name':record.get('name'),'reason':'invalid_or_future_source'})
        assigned=assignments.get(event.event_id)
        canonical=aliases.get(name_key(assigned),assigned) if assigned else None
        proof=next((r for r in reversed(history) if name_key(aliases.get(name_key(r['name']),r['name']))==name_key(canonical)),None) if canonical else None
        if not proof:
            bindings[event.event_id]={'place_id':None,'name':None,'reason':'unknown' if not assigned else 'no_physical_source'}
            continue
        ident='place_'+hashlib.sha256(name_key(canonical).encode()).hexdigest()[:16]
        bindings[event.event_id]={'place_id':ident,'name':canonical,'evidence':{k:proof[k] for k in ('start','end','quote')}}
        if event.event_id in selected_event_ids and ident not in cards:
            cards[ident]={'name':canonical,'setting_id':ident,
                'description':'Подтверждённое место действия. Источник: '+proof['quote']}
    used={name_key(v['name']) for v in bindings.values() if v['name']}
    context=[dict(r) for r in records if r.get('kind')!='physical']
    for entity in doc.entities:
        if getattr(entity.entity_type,'value',entity.entity_type)=='location' and name_key(entity.canonical_name) not in used:
            context.append({'name':entity.canonical_name,'kind':'entity_mention','entity_id':entity.entity_id})
    return list(cards.values()),{'revision':'event-settings/2','bindings':bindings,'context_mentions':context,
        'rejected_evidence':rejected,'aliases':{n:c for n,c in aliases.items() if name_key(c)!=n}}

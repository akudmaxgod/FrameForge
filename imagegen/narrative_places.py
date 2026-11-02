"""Retain named place identities independently of the primary city reference."""
import hashlib
import re

def enrich(metadata, doc, story, units):
    raw=doc.to_dict() if hasattr(doc,'to_dict') else doc
    if not isinstance(raw,dict):return metadata
    from .forgotten_cities.source_inventory import is_same_place
    grouped=[]
    for entity in raw.get('entities',[]):
        item=next((x for x in grouped if is_same_place(entity.get('canonical_name',''),x.get('canonical_name',''))),None)
        if item:
            item['aliases']=list(dict.fromkeys([*item.get('aliases',[]),entity.get('canonical_name',''),*entity.get('aliases',[])]))
            item['evidence_spans']=[*item.get('evidence_spans',[]),*entity.get('evidence_spans',[])]
        else:grouped.append({**entity})
    places=[]
    for entity in grouped:
        name=entity.get('canonical_name','')
        aliases=list(dict.fromkeys([name,*entity.get('aliases',[])]))
        # Literal apposition is stronger than an erroneous NER label:
        # "Bayon, a Buddhist temple" is a place, not a living character.
        literal=any(re.search(re.escape(alias)+r'\s*[,—–]\s*[^.!?]{0,55}\b(?:храм\w*|водо[её]м\w*|столичн\w*\s+центр)',story,re.I) for alias in aliases)
        if ((entity.get('entity_type')!='location' and not literal) or not name[:1].isupper() or
            len(name)>60 or any(c in name for c in '.!?') or len(name.split())>5):continue
        spans=[]
        for span in entity.get('evidence_spans',[]):
            a,b=span.get('start'),span.get('end')
            if type(a) is int and type(b) is int and story[a:b]==span.get('text'):
                spans.append({'start':a,'end':b,'quote':story[a:b]})
        if not spans:continue
        for alias in aliases:
            if not alias or len(alias)>60:continue
            for match in re.finditer(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',story,re.I):
                item={'start':match.start(),'end':match.end(),'quote':match.group()}
                if item not in spans:spans.append(item)
        own=[u for u in units if any(u['start']<=s['start']<u['end'] for s in spans)]
        definitions=[u for u in own if re.search(r'храм|барай|водо[её]м|столичн\w*\s+центр|башн',u['text'],re.I)
            and not re.search(r'модель|исследован|отложен',u['text'],re.I)]
        places.append({'place_id':entity['entity_id'],'canonical_name':name,'aliases':aliases,
            'evidence':spans,'definition_source_ids':[u['id'] for u in definitions]})
        # A mentioned country/opponent is catalogued, not an architectural
        # reference. Only explicitly described recurring sites get cards.
        if not literal or not definitions or len(own)<2:continue
        for layer,suffix in [('historical_reconstruction','исторический вид'),('modern_ruins','нынешнее состояние')]:
            canonical=name+' — '+suffix
            if any(a['canonical_name']==canonical for a in metadata['anchors']):continue
            evidence=[{'start':u['start'],'end':u['end'],'text':u['text']} for u in definitions]
            description=('Сохранять идентичность именно '+name+'. '+
                ' '.join(u['text'] for u in definitions[:3])+
                ' Эпоха видимого кадра: '+layer+'. Не переносить состояние руин в древний вид. '
                'Не добавлять материалы, объекты, находки или людей, которых источник не подтверждает.')
            metadata['anchors'].append({'anchor_id':'named_'+hashlib.sha256(canonical.encode()).hexdigest()[:12],
                'anchor_type':'landscape','canonical_name':canonical,'description':description,
                'evidence_spans':evidence,'identity_scope':'specific_object','importance':.8,
                'locked':False,'match_spans':[],'recurrence_count':len(own),'reference_asset_id':None,
                'reference_required':False,'source_kind':None,'source_properties':{},
                'temporal_context':None,'time_layer':layer,'place_id':entity['entity_id']})
    metadata['named_places']=places
    return metadata

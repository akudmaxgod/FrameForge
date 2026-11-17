"""Migrate existing semantic test examples to the source-ID response protocol."""
import copy
import json


def response_for(body, value):
    payload=json.loads(body['messages'][1]['content'])
    menu=payload.get('source_options',[])
    def ids(quote):
        if not quote:return []
        exact=[r for r in menu if r['text']==quote]
        candidates=exact or [r for r in menu if isinstance(quote,str) and quote in r['text']]
        return [min(candidates,key=lambda r:len(r['text']))['id']] if candidates else ['unavailable_source']
    value=copy.deepcopy(value)
    if 'catalog' in value:
        def convert(row):row['quote_ids']=ids(row.pop('quote',None))
        for entity in value['catalog']['entities']:
            convert(entity)
            for alias in entity.get('aliases',[]):convert(alias)
            if entity.get('species'):convert(entity['species'])
        for key in ('relations','states'):
            for row in value['catalog'][key]:convert(row)
        for fact in value.get('setting_facts',[]):convert(fact)
        value.pop('characters',None)
    elif 'events' in value:
        for entity in value.get('entities',[]):
            entity['evidence_ids']=[i for q in entity.pop('evidence_quotes',[]) for i in ids(q)]
        for event in value['events']:
            event['location_source_ids']=ids(event.pop('location_quote',None))
            for presence in event.get('presences',[]):
                for target in [presence,*presence.get('states',[])]:
                    target['source_id']=ids(target.pop('quote',None))[0]
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}

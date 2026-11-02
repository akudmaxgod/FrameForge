"""A complete source-validated overview can resume independently of later stages."""
import hashlib
import json
from pathlib import Path
from .story_catalog import build,SCHEMA,RULES
from .source_review_status import signature,write

VERSION='validated-story-overview/1'


def identity(story, model, assisted):
    return {'version':VERSION,'story_sha256':hashlib.sha256(story.encode()).hexdigest(),
            'model':model,'assisted':bool(assisted),'contract_hash':signature([SCHEMA,RULES])}


def path(folder, story, model, assisted):
    return Path(folder)/'validated-overviews'/(signature(identity(story,model,assisted))+'.json')


def validate(story, overview):
    if not isinstance(overview,dict):raise ValueError('Неполный общий обзор истории.')
    if any(not isinstance(overview.get(k),str) for k in ('timeline','causal_links','uncertainties')):
        raise ValueError('Неполный общий обзор истории.')
    if not isinstance(overview.get('setting_facts'),list):raise ValueError('Неполные сведения об обстановке.')
    if not isinstance(overview.get('catalog'),dict):raise ValueError('Отсутствует общий каталог.')
    for fact in overview['setting_facts']:
        if (not isinstance(fact,dict) or not isinstance(fact.get('quote'),str)
                or not fact['quote'] or fact['quote'] not in story):
            raise ValueError('Обстановка не подтверждена источником.')
    for row in overview.get('characters',[]):
        if not isinstance(row,dict):raise ValueError('Неполное описание персонажа.')
        quotes=row.get('quotes')
        if not isinstance(quotes,list) or not quotes or any(not isinstance(q,str) or not q or q not in story for q in quotes):
            raise ValueError('Описание персонажа не подтверждено источником.')
    return build(story,overview)


def save(folder, story, model, assisted, overview, provenance):
    validate(story,overview)
    packet={**identity(story,model,assisted),'overview':overview,'provenance':provenance}
    packet['content_hash']=signature(packet)
    target=path(folder,story,model,assisted)
    write(target,packet)
    return target


def load(folder, story, model, assisted):
    if not folder:return None
    target=path(folder,story,model,assisted)
    if not target.is_file():return None
    try:
        packet=json.loads(target.read_text(encoding='utf-8'))
        if any(packet.get(k)!=v for k,v in identity(story,model,assisted).items()):return None
        if packet.get('content_hash')!=signature({k:v for k,v in packet.items() if k!='content_hash'}):return None
        validate(story,packet['overview'])
        return packet
    except (ValueError,KeyError,TypeError,AttributeError):return None

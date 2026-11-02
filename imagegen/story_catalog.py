"""One source-grounded, typed identity catalogue shared by all story blocks.

Membership never implies visibility. Pronouns are occurrence-level decisions,
not global aliases. Uncertain identity is left unbound rather than guessed.
"""
import copy
import hashlib
import json
import re

VERSION = 'story-catalog/1'
KINDS = {'person', 'animal', 'creature', 'group', 'location', 'object', 'vehicle', 'era', 'organization'}
PRONOUNS = {'он','она','оно','они','его','её','ее','их','ему','ей','им','онa','he','she','it','they','him','her','them'}

RULES = '''The shared story_catalog fixes identity, kind and species across blocks.
Reuse its id as semantic_id for a supported actor. Never merge distinct people
just because their profession matches. A group is not its named member. A place
is not a person; an animal cannot change species or owner without exact source.
Registry membership/mention/ownership does not make anyone physically visible.
Resolve pronouns only in their occurrence context, never as permanent aliases.
Future registry facts do not authorize current actions, state or presence.'''

SCHEMA = '''"catalog":{"entities":[{"key":"c1","name":string,
"kind":"person|animal|creature|group|location|object|vehicle|era|organization",
"quote":exact unique source sentence,"aliases":[{"name":string,"quote":exact identity evidence}],
"species":null or {"name":string,"quote":exact source evidence}}],
"relations":[{"subject":"c1","predicate":"owns|member_of|located_in","object":"c2","source_status":"fact|hypothesis|reported|negated|comparison|conditional","quote":exact source evidence}],
"states":[{"entity":"c1","value":string,"source_status":"fact|hypothesis|reported|negated|comparison|conditional","quote":exact source evidence}] }'''


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def quote_span(story, quote):
    from .source_quotes import locate
    try:
        value=locate(quote,story)
    except ValueError as error:
        raise ValueError('Общий реестр: '+str(error)) from None
    return {'start':value['start'],'end':value['end'],'quote':value['text']}


def supported(name, quote):
    from .narrative_frame_focus import subject_supported
    return subject_supported(name, quote)


def alias_evidence(story, name, alias):
    """Keep the same strict guard for initial and repaired identity claims."""
    if not isinstance(alias, dict) or not isinstance(alias.get('name'), str):
        raise ValueError('Псевдоним должен иметь собственное доказательство тождества.')
    text = alias['name'].strip()
    proof = quote_span(story, alias.get('quote'))
    if text.casefold() in PRONOUNS or not supported(text, proof['quote']) or not supported(name, proof['quote']):
        raise ValueError('Псевдоним не подтверждает тождество: '+text)
    return {'name': text, 'evidence': proof}


def seal(packet):
    packet['content_hash'] = signature({k:v for k,v in packet.items() if k != 'content_hash'})
    return packet


def validate(story, packet):
    if (packet.get('version') != VERSION or packet.get('story_sha256') != hashlib.sha256(story.encode()).hexdigest()
            or packet.get('content_hash') != signature({k:v for k,v in packet.items() if k != 'content_hash'})):
        raise ValueError('Общий реестр изменён или принадлежит другому сценарию.')
    ids = [e['id'] for e in packet['entities']]
    if len(ids) != len(set(ids)):
        raise ValueError('Повторный идентификатор общего реестра.')
    for entity in packet['entities']:
        if entity['kind'] not in KINDS:
            raise ValueError('Неизвестный тип сущности.')
        for evidence in [entity['evidence'], *[a['evidence'] for a in entity['aliases']],
                         *([entity['species']['evidence']] if entity.get('species') else [])]:
            if story[evidence['start']:evidence['end']] != evidence['quote']:
                raise ValueError('Изменена цитата общего реестра.')
    for row in [*packet['relations'],*packet['states']]:
        evidence=row['evidence']
        if story[evidence['start']:evidence['end']] != evidence['quote']:
            raise ValueError('Изменено основание связи или состояния.')
    return packet


def build(story, overview, doc=None):
    raw = overview.get('catalog')
    # Older cached overviews remain readable, but are explicitly labelled.
    compatibility = raw is None
    if compatibility:
        raw = {'entities': [], 'relations': [], 'states': []}
        for n, item in enumerate(overview.get('characters', [])):
            raw['entities'].append({'key': 'legacy_'+str(n), 'name': item['name'], 'kind': 'person',
                                    'quote': item['quotes'][0], 'aliases': []})
    if not isinstance(raw, dict) or any(not isinstance(raw.get(k), list) for k in ('entities','relations','states')):
        raise ValueError('Неполная структура общего реестра истории.')
    entities = []; keys = {}
    for item in raw['entities']:
        key = item.get('key'); name = item.get('name'); kind = item.get('kind')
        if not isinstance(key, str) or not key or key in keys or kind not in KINDS or not isinstance(name, str) or not name.strip():
            raise ValueError('Не определены имя, тип или ключ сущности общего реестра.')
        evidence = quote_span(story, item.get('quote'))
        if not supported(name, evidence['quote']):
            raise ValueError('Имя сущности не подтверждено её цитатой: '+name)
        # This is occurrence-scoped: another character named Petra elsewhere
        # remains legal; the literal city apposition cannot become an actor.
        place = re.search(r'\b(?:город|городе|города|city|town)\s+[«"\']?'+re.escape(name)+r'\b', evidence['quote'], re.I)
        if kind in ('person','animal','creature') and place:
            raise ValueError('Название города нельзя превратить в участника: '+name)
        aliases = []
        for alias in item.get('aliases', []):
            aliases.append(alias_evidence(story, name, alias))
        anchor = evidence['quote'].casefold().find(name.casefold())
        origin = evidence['start'] + max(0, anchor)
        eid = 'identity_'+signature([hashlib.sha256(story.encode()).hexdigest(), kind, origin, name.casefold()])[:24]
        if any(e['id'] == eid for e in entities):
            raise ValueError('Повторная сущность: объедините её доказанные псевдонимы.')
        species = None
        if item.get('species'):
            species = {'name': item['species']['name'], 'evidence': quote_span(story, item['species'].get('quote'))}
            if kind != 'animal' or not supported(species['name'], species['evidence']['quote']) or not supported(name, species['evidence']['quote']):
                raise ValueError('Вид животного не подтверждён источником.')
        entities.append({'id': eid, 'name': name, 'kind': kind, 'evidence': evidence, 'aliases': aliases,
                         'species': species, 'visibility_policy': 'per_event_evidence_required'})
        keys[key] = eid
    by_id = {e['id']: e for e in entities}
    relations = []; states = []
    for item in raw['relations']:
        s, o = keys.get(item.get('subject')), keys.get(item.get('object'))
        proof = quote_span(story, item.get('quote'))
        if not s or not o or item.get('predicate') not in ('owns','member_of','located_in'):
            raise ValueError('Отношение ссылается на неизвестную сущность или тип связи.')
        if not all(supported(by_id[i]['name'], proof['quote']) for i in (s,o)):
            raise ValueError('Связь сущностей не подтверждена общей цитатой.')
        status=item.get('source_status','fact')
        if status not in ('fact','hypothesis','reported','negated','comparison','conditional'):
            raise ValueError('Не определён статус связи в источнике.')
        relations.append({'subject_id': s, 'predicate': item['predicate'], 'object_id': o, 'evidence': proof,'source_status':status})
    for item in raw['states']:
        eid = keys.get(item.get('entity')); proof = quote_span(story, item.get('quote'))
        if not eid or not isinstance(item.get('value'), str) or not item['value'].strip() or not supported(by_id[eid]['name'], proof['quote']):
            raise ValueError('Состояние не связано с подтверждённой сущностью.')
        status=item.get('source_status','fact')
        if status not in ('fact','hypothesis','reported','negated','comparison','conditional'):
            raise ValueError('Не определён статус состояния в источнике.')
        states.append({'entity_id': eid, 'value': item['value'], 'evidence': proof,'source_status':status})
    return seal({'version': VERSION, 'story_sha256': hashlib.sha256(story.encode()).hexdigest(),
                 'entities': sorted(entities, key=lambda e:e['id']), 'relations': relations, 'states': states,
                 'status': 'legacy_overview' if compatibility else 'source_grounded',
                 'identity_policy': 'explicit_evidence_required', 'semantic_accuracy_measured': False})


def slice_for(packet, end, start=0):
    """Never leak later names, ownership or temporary states into a frame."""
    entities = []
    for entity in packet.get('entities', []):
        candidates = [{'name': entity['name'], 'evidence': entity['evidence']}, *entity['aliases']]
        candidates = [a for a in candidates if a['evidence']['end'] <= end]
        if not candidates:
            continue
        row = {k:copy.deepcopy(entity[k]) for k in ('id','kind','visibility_policy')}
        row.update(name=candidates[0]['name'], evidence=candidates[0]['evidence'], aliases=candidates[1:])
        if entity.get('species') and entity['species']['evidence']['end'] <= end:
            row['species'] = copy.deepcopy(entity['species'])
        entities.append(row)
    ids = {e['id'] for e in entities}
    return {'version': VERSION, 'entities': entities,
            'relations': [copy.deepcopy(r) for r in packet.get('relations', []) if r['subject_id'] in ids and r['object_id'] in ids and r['evidence']['end'] <= end],
            'states': [copy.deepcopy(s) for s in packet.get('states', []) if s['entity_id'] in ids and s['evidence']['start'] < end and start < s['evidence']['end']],
            'coreferences': [copy.deepcopy(c) for c in packet.get('coreferences', []) if c['source']['end'] <= end and start < c['source']['end']],
            'rules': RULES}


def match(packet, name, kind=None, end=None):
    catalog = slice_for(packet, end) if end is not None else packet
    names = lambda e:[e['name'], *[a['name'] for a in e.get('aliases', [])]]
    matches = [e for e in catalog.get('entities', []) if any(name.casefold() == n.casefold() for n in names(e))
               and (kind is None or e['kind'] == kind or kind == 'person' and e['kind'] == 'group')]
    return matches[0] if len(matches) == 1 else None


def reference_ids(packet, doc):
    value=export(packet,doc)
    return {row['analysis_entity_id'] for row in value['entities'] if row['reference_eligible']}


def canonicalize(data, doc):
    """Use app IDs across independently parsed blocks; never merge by profession."""
    packet = doc.metadata.get('story_catalog')
    if not packet:
        return {}
    bindings = doc.metadata.setdefault('story_catalog_bindings', {})
    actual = {e.entity_id for e in doc.entities}; chosen = {}; claimed = set()
    for entity in data['entities']:
        kind = getattr(entity['entity_type'], 'value', entity['entity_type'])
        row = match(packet, entity['canonical_name'], kind)
        if not row:
            continue
        prior = bindings.get(row['id'])
        if prior in actual:
            if prior in claimed:
                raise ValueError('Один участник реестра продублирован в блоке.')
            entity['existing_entity_ids'] = list(dict.fromkeys([prior, *entity['existing_entity_ids']]))
            claimed.add(prior)
        entity['canonical_name'] = row['name']
        # Species belongs to the identity, never the previous block's wording.
        chosen[entity['semantic_id']] = row['id']
    return chosen


def bind_applied(data, doc, chosen):
    bindings = doc.metadata.setdefault('story_catalog_bindings', {})
    for entity in data['entities']:
        eid = chosen.get(entity['semantic_id'])
        if not eid:
            continue
        candidates = [e for e in doc.entities if e.canonical_name == entity['canonical_name']
                      and any(s in e.evidence_spans for s in entity['evidence_spans'])]
        if len(candidates) == 1:
            bindings[eid] = candidates[0].entity_id


def export(packet, doc):
    """Reference eligibility comes from repeated physical events, not mentions."""
    value = copy.deepcopy(packet); bindings = doc.metadata.get('story_catalog_bindings', {})
    actual_entities={e.entity_id:e for e in doc.entities}
    for row in value['entities']:
        actual = bindings.get(row['id']); physical = []
        if actual and actual not in actual_entities:
            # An identity audit may have merged local IR IDs. Recover only an
            # unambiguous same-name entity carrying the original source proof.
            names={row['name'].casefold(),*[a['name'].casefold() for a in row['aliases']]}
            candidates=[e for e in doc.entities if e.canonical_name.casefold() in names
                and any(s.start<=row['evidence']['start']<row['evidence']['end']<=s.end
                        for s in e.evidence_spans)]
            actual=candidates[0].entity_id if len(candidates)==1 else None
        for event in doc.events:
            if any(p.entity_id == actual and p.physical_presence for p in event.presences):
                physical.append(event.event_id)
        row['analysis_entity_id'] = actual
        row['physical_event_ids'] = physical
        row['reference_eligible'] = row['kind'] in ('person','animal','creature') and len(set(physical)) > 1
    reverse={row['analysis_entity_id']:row['id'] for row in value['entities'] if row.get('analysis_entity_id')}
    mentions={m.mention_id:m for m in doc.mentions}
    value['coreferences']=[]
    for decision in doc.coreferences:
        mention=mentions.get(decision.mention_id)
        if not mention:
            continue
        status=getattr(decision.status,'value',decision.status)
        resolved=[reverse[i] for i in decision.resolved_entity_ids if i in reverse]
        value['coreferences'].append({'source':{'start':mention.span.start,'end':mention.span.end,'quote':mention.span.text},
                                     'entity_ids':resolved,'status':status,
                                     'identity_status':'bound' if resolved and status in ('resolved_local','resolved_cloud','group_resolved') else 'unresolved'})
    return seal(value)

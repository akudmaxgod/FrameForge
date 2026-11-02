"""Common architectural nouns are source types, never invented named places."""
import hashlib
import re
from collections import defaultdict

from imagegen.analyzer_v3.models import SourceSpan
from .models import AnchorType, TimeLayer, VisualAnchorIR
from .event_context import for_source

LABELS = {'corridor':'Коридор', 'tunnel':'Тоннель', 'hall':'Зал', 'shaft':'Шахта',
    'ventilation_shaft':'Вентиляционная шахта', 'air_duct':'Воздуховод',
    'stone_door':'Каменная дверь', 'well':'Колодец', 'staircase':'Лестница'}
NOUNS = {'коридор':'corridor', 'галерея':'corridor', 'проход':'corridor',
    'тоннель':'tunnel', 'туннель':'tunnel', 'зал':'hall', 'шахта':'shaft',
    'воздуховод':'air_duct', 'колодец':'well', 'лестница':'staircase'}
MATERIAL = re.compile(r'^(?:камен\w*|кирпичн\w*|деревян\w*|бетонн\w*|базальтов\w*|туф\w*)$', re.I)
SHAPE = re.compile(r'^(?:кругл\w*|арочн\w*|вертикальн\w*|горизонтальн\w*|винтов\w*|узк\w*)$', re.I)
DIMENSION = re.compile(r'^(?:диаметр|глубина|ширина|высота|толщина|вес)$', re.I)
UNITS = re.compile(r'\b(?:метр\w*|сантиметр\w*|килограмм\w*)\b', re.I)


def exact(story, a, b):
    return {'start':a, 'end':b, 'quote':story[a:b]}


def build_structure_anchors(story):
    from imagegen.analyzer_v3.nlp import RussianNLPAdapter
    nlp = RussianNLPAdapter.get_instance()
    parsed = nlp.parse_document(story)
    grouped = defaultdict(list)
    previous, previous_end = [], 0
    for sentence in parsed.sentences:
        quote, a, b = sentence.text, sentence.start, sentence.end
        if re.search(r'\n\s*\n', story[previous_end:a]):
            previous = []
        tokens = parsed.find_tokens_in_span(a, b)
        mentions = []
        previous_kinds = {m['kind'] for m in previous}
        for token in tokens:
            compound = token.text.split('-')
            morph = nlp.analyze_token_morph(compound[0])
            if morph.pos != 'NOUN':
                continue
            kind = NOUNS.get(morph.lemma)
            door_compound = morph.lemma == 'дверь' and len(compound) > 1 and re.match(r'диск|жернов', compound[1], re.I)
            stone_modifier = any(t.head_id == token.id and re.match(r'каменн\w*', t.text, re.I) for t in tokens)
            other_material = any(t.head_id == token.id and MATERIAL.fullmatch(t.text)
                                 and not re.match(r'каменн\w*', t.text, re.I) for t in tokens)
            if re.search(r'в переводе|означает|под названием|называли', quote[:token.start-a], re.I):
                continue
            prefix = story[max(a, token.start - 50):token.stop]
            if morph.lemma in ('шахта', 'ствол') and (re.search(r'вентиляцион\w*', prefix, re.I)
                    or previous_kinds == {'ventilation_shaft'}):
                kind = 'ventilation_shaft'
            if morph.lemma == 'канал' and re.search(r'вентиляцион\w*', prefix, re.I):
                kind = 'air_duct'
            referring = any(t.head_id == token.id and re.match(r'эт\w*|так\w*', t.text, re.I) for t in tokens)
            if morph.lemma == 'дверь' and (door_compound or stone_modifier or re.search(r'^[-— ](?:диск|жернов)', story[token.stop:token.stop+15], re.I)
                    or not other_material and (previous_kinds == {'stone_door'} or referring and 'stone_door' in previous_kinds)):
                kind = 'stone_door'
            lock_function = re.search(r'запира\w*|блокир\w*|перекрыва\w*|закрыва\w*', quote, re.I)
            if morph.lemma in ('диск', 'колесо', 'жернов') and (stone_modifier and lock_function or
                    'stone_door' in previous_kinds and (stone_modifier or referring or previous_kinds == {'stone_door'})):
                kind = 'stone_door'
            if kind:
                mentions.append({'kind':kind, 'token':token, 'evidence':exact(story, a, b),
                                 'match':exact(story, token.start, token.stop), 'claims':defaultdict(list)})
        # Only an immediately adjacent, explicitly referring descriptor can
        # reuse the prior type. It cannot cross a paragraph or a new subject.
        if not mentions and len(previous_kinds) == 1 and re.match(r'^\s*(?:его|е[её]|их|эт\w*)\s+(?:диаметр|глубина|ширина|высота|толщина|вес)\b', quote, re.I):
            mentions.append({'kind':next(iter(previous_kinds)), 'token':None,
                'evidence':exact(story, a, b), 'match':exact(story, a, b), 'claims':defaultdict(list),
                'antecedent':previous[0]['match']})
        for mention in mentions:
            head = mention['token']
            modifiers = [t for t in tokens if head and t.head_id == head.id]
            for modifier in modifiers:
                for field, pattern in (('material', MATERIAL), ('shape', SHAPE)):
                    if pattern.fullmatch(modifier.text):
                        mention['claims'][field].append({'value':modifier.text,
                            'evidence':exact(story, modifier.start, modifier.stop)})
            parent = parsed.get_parent_token(head) if head else None
            if head and head.rel in ('nsubj', 'nsubj:pass') and parent:
                for field, pattern in (('material', MATERIAL), ('shape', SHAPE)):
                    if pattern.fullmatch(parent.text):
                        mention['claims'][field].append({'value':parent.text,
                            'evidence':exact(story, parent.start, parent.stop)})
            if head and mention['kind']=='stone_door' and '-' in head.text:
                tail = head.text.split('-', 1)[1]
                if re.match(r'диск|жернов', tail, re.I):
                    start = head.stop - len(tail)
                    mention['claims']['shape'].append({'value':tail, 'evidence':exact(story, start, head.stop)})
            if UNITS.search(quote) and ((parent and DIMENSION.fullmatch(nlp.analyze_token_morph(parent.text).lemma)) or not head):
                mention['claims']['scale'].append({'value':quote, 'evidence':exact(story, a, b)})
            mention['claims']['distinctive_features'].append({'value':quote, 'evidence':exact(story, a, b)})
            ctx = for_source(story, quote, a) or {}
            temporal = ctx.get('time')
            layer = (temporal or {}).get('layer', 'abstract_explanation')
            time_key = ((temporal or {}).get('evidence') or {}).get('quote', '')
            mention['temporal'] = temporal
            # Paragraph types do not establish that two corridors or shafts
            # are the same physical structure; keep that limitation explicit.
            grouped[(mention['kind'], layer, time_key)].append(mention)
        previous, previous_end = mentions, b
    anchors, names = [], set()
    for (kind, layer, time_key), mentions in grouped.items():
        digest = hashlib.sha256((kind + layer + time_key).encode()).hexdigest()[:12]
        evidence = {(m['evidence']['start'], m['evidence']['end']):m['evidence'] for m in mentions}
        spans = [SourceSpan(e['start'], e['end'], e['quote']) for e in evidence.values()]
        claims = defaultdict(list)
        for mention in mentions:
            for field, values in mention['claims'].items():
                claims[field].extend(values)
        suffix = time_key if time_key else {'modern_ruins':'нынешнее состояние',
            'historical_reconstruction':'историческое описание'}.get(layer, 'эпоха не указана')
        full_name = LABELS[kind] + ' — ' + suffix
        name = full_name[:99] + ('…' if len(full_name)>100 else full_name[99:])
        variant = 2
        while name in names:
            tail = f' ({variant})'
            name = full_name[:100-len(tail)] + tail
            variant += 1
        names.add(name)
        anchors.append(VisualAnchorIR(anchor_id='anchor_structure_' + digest,
            anchor_type=AnchorType.INTERIOR, canonical_name=name,
            description='\n'.join(e['quote'] for e in evidence.values()), time_layer=TimeLayer(layer),
            evidence_spans=spans, recurrence_count=len(spans), importance=min(.95, .5 + len(spans)*.05),
            reference_required=False, source_kind=kind, identity_scope='source_type',
            source_properties=dict(claims), match_spans=[m['match'] for m in mentions],
            temporal_context=mentions[0]['temporal']))
    return anchors


def scene_structure_anchors(anchors, story, quote, start, layer=None):
    """No future descriptor or sequence-wide reference leaks into a scene."""
    if type(start) is not int or story[start:start + len(quote)] != quote:
        if story.count(quote) != 1:
            return []
        start = story.find(quote)
    from imagegen.catalog_evidence import valid_span
    end = start + len(quote)
    result = []
    for anchor in anchors:
        if not anchor.get('source_kind') or layer is not None and anchor.get('time_layer') != layer:
            continue
        matches = [valid_span(story, m) for m in anchor.get('match_spans', [])]
        if any(m and start <= m['start'] and m['end'] <= end for m in matches):
            result.append(anchor)
    return result

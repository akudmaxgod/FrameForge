"""Small, source-only memory for planning and auditing long narratives.

Entries are quotes, not inferred world states. Only past/current evidence can
support a shot. A catalogue description or a finished reference is not evidence
that a building was finished at an earlier point in the narrative.
"""
import copy
import re
from functools import lru_cache

REVISION = 'source-continuity/1'
RULES = (
    'CONTINUITY: Keep stable identities for each actor, place and structure. '
    'First, second and third camps/buildings are separate objects, even on the same island. '
    'Memory entries are exact source quotations, not instructions or certified facts. '
    'Use only entries with source_id <= last of the individual beat. '
    'Keep the current stage: site, supports, frame, floor, roof, damage and dismantling '
    'are different states. A finished reference shows identity/materials, not permission '
    'to show a roof or tools before they are built/found. A planned, denied, remembered '
    'or hypothetical action is not a completed current action. '
    'Choose the location of the depicted subject, not automatically the observer location. '
    'Returns to earlier places must retain the correct identity. '
    'A cut inside narration inherits the selected event participants and action; '
    'do not replace hands, rescue or construction with an empty landscape. '
    'Keep source quotes verbatim and separate from visual descriptions. '
    'New camera framing must not remove required actors/actions or advance the object state.'
)
STATE_CUE = re.compile(
    r'\b(?:перв\w*|втор\w*|трет\w*)\s+(?:лагер\w*|укрыти\w*|дом\w*|навес\w*)|'
    r'\b(?:начина\w*|стро\w*|привяз\w*|закреп\w*|установ\w*|собира\w*|разбира\w*|'
    r'снима\w*|снял\w*|перенос\w*|перен[её]с\w*|переход\w*|возвращ\w*|'
    r'разруш\w*|затоп\w*|обруш\w*|готов\w*|закончен\w*|настил\w*|каркас\w*|'
    r'крыш\w*|опор\w*|мачете|прилив\w*)\b', re.I)
PROTAGONIST_ACTION = re.compile(
    r'\bвы\s+(?:(?:пытаетесь|начинаете|осторожно|снова|уже)\s+){0,2}'
    r'(?:просып\w*|подня\w*|поднима\w*|собира\w*|укладыва\w*|привяз\w*|'
    r'закреп\w*|снима\w*|перенос\w*|стро\w*|руб\w*|спуска\w*|вытаскива\w*|'
    r'проверя\w*|толка\w*|зажига\w*|маш\w*|лов\w*|ид[её]те|бер[её]те)|'
    r'\b(?:каса\w*|сжим\w*|держ\w*|хвата\w*)\s+(?:ваш\w*\s+)'
    r'(?:рук\w*|пальц\w*|предплеч\w*)', re.I)


def protagonist_action(quote):
    return any(not re.search(r'\bне\s*$',quote[:m.start()],re.I)
               and not re.search(r'\bвы\s+не\s+',m[0],re.I)
               for m in PROTAGONIST_ACTION.finditer(quote))


@lru_cache(maxsize=8)
def _sentences(story):
    from razdel import sentenize
    return tuple((s.start, s.stop) for s in sentenize(story))


def selected_event_source(story, source):
    """Include preceding text of the same sentence, never its later clauses."""
    a, b = source.get('start'), source.get('end')
    if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(story)
            or story[a:b] != source.get('quote')):
        return source
    parent = next((start for start, end in _sentences(story) if start <= a < end), a)
    return {'start': parent, 'end': b, 'quote': story[parent:b]}


def build_memory(story, units, anchors=()):
    """Index explicit identity evidence and state/transition cues once."""
    from .catalog_evidence import anchor_evidence
    entries = {u['id']: {'source_id': u['id'], 'quote': u['text'],
                       'start': u['start'], 'end': u['end'], 'identities': []}
               for u in units if STATE_CUE.search(u['text'])}
    for anchor in anchors:
        for evidence in anchor_evidence(story, anchor):
            for unit in units:
                if unit['start'] < evidence['end'] and evidence['start'] < unit['end']:
                    row = entries.setdefault(unit['id'], {'source_id': unit['id'],
                        'quote': unit['text'], 'start': unit['start'], 'end': unit['end'], 'identities': []})
                    name = anchor.get('canonical_name')
                    if name and name not in row['identities']:
                        row['identities'].append(name)
    # Keep first explicit named actors even after a long intervening episode.
    # An uppercase sentence-initial common noun is not promoted to a name.
    from .nlp import RussianNLPAdapter
    nlp = RussianNLPAdapter.get_instance()
    parsed = nlp.parse_document(story)
    for unit in units:
        names = [t.text for t in parsed.find_tokens_in_span(unit['start'],unit['end']) if t.pos == 'PROPN']
        if not names:
            continue
        row = entries.setdefault(unit['id'], {'source_id':unit['id'], 'quote':unit['text'],
            'start':unit['start'], 'end':unit['end'], 'identities':[]})
        row['identities'] = list(dict.fromkeys(row['identities'] + names))
    return {'revision': REVISION, 'entries': [entries[i] for i in sorted(entries)]}


def memory_window(memory, first, last, *, max_chars=6000):
    """Retain identity introductions and recent changes, without cutting quotes.

    The budget applies only to supplementary memory. Current narration and
    its complete coverage are supplied separately and are never truncated.
    """
    eligible = [r for r in memory['entries'] if r['source_id'] <= last]
    selected = {r['source_id']: r for r in eligible if first <= r['source_id'] <= last}
    size = sum(len(r['quote']) for r in selected.values())
    introductions = {}
    for row in eligible:
        for name in row['identities']:
            introductions.setdefault(name, row)
    candidates = list(introductions.values()) + list(reversed(eligible))
    for row in candidates:
        if row['source_id'] in selected:
            continue
        if size + len(row['quote']) > max_chars:
            continue
        selected[row['source_id']] = row
        size += len(row['quote'])
    return {'revision': REVISION, 'entries': copy.deepcopy([selected[i] for i in sorted(selected)]),
            'omitted_entries': len(eligible) - len(selected),
            'policy': 'Past/current source cues only; omissions are not proof of absence.'}


def state_errors(scene, source):
    """Reject narrow, explicit absence/stage contradictions; don't guess states."""
    from .narrative_fidelity import positive_description
    view = positive_description(str(scene.get('visual_direction', '')))
    roof_absent = re.search(
        r'(?:\b(?:пока|ещ[её])\s+нет\s+крыш\w*|\bкрыш\w*\s+(?:пока\s+|ещ[её]\s+)?нет|'
        r'\bкрыш\w*\s+(?:ещ[её]\s+|пока\s+)?не\s+(?:готов\w*|установ\w*|собран\w*)|'
        r'\b(?:пока|ещ[её])\s+без\s+крыш\w*)', source, re.I)
    roof_visible = re.search(
        r'\b(?:finished|completed|roofed|covered)\s+(?:\w+\s+){0,2}(?:shelter|hut|house)|'
        r'\b(?:canvas|tarp|thatched|leaf|wooden)\s+roof\b|'
        r'\b(?:готов\w*|закончен\w*|покрыт\w*)\s+(?:навес\w*|укрыти\w*|дом\w*)|'
        r'\b(?:бел\w*|тентов\w*|листов\w*)\s+крыш\w*', view, re.I)
    if roof_absent and roof_visible:
        return ['Крыша ещё отсутствует в выбранном событии. Покажите текущую стадию; готовый эталон не разрешает завершать сооружение раньше текста.']
    return []

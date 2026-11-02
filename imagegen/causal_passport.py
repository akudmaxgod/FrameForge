"""Evidence-bound narrative ledger. Explicit language is never historical proof.

Sentence adjacency is chronology, not causality. Only explicit connective
phrases create relationship candidates, and no candidate resolves a scene.
"""
import re
from .domain import fingerprint
from .scene_semantics import signals
from .forgotten_cities.event_context import source_events

VERSION = 'causal-source-ledger/1'
CONNECTIVE = re.compile(r'\b(?P<because>потому\s+что|так\s+как|поскольку)|'
                        r'\b(?P<consequence>поэтому|из-за\s+этого|вследствие\s+этого)|'
                        r'\b(?P<purpose>для\s+того,?\s+чтобы|чтобы)|'
                        r'\b(?P<condition>если)|\b(?P<until>пока)\b', re.I)


def exact(story, start, end):
    while start < end and story[start] in ' \t\r\n,;:': start += 1
    while end > start and story[end-1] in ' \t\r\n,;:': end -= 1
    return {'start': start, 'end': end, 'quote': story[start:end]}


def evidence_valid(story, item, end):
    return (isinstance(item, dict) and type(item.get('start')) is int and
            type(item.get('end')) is int and 0 <= item['start'] < item['end'] <= end and
            story[item['start']:item['end']] == item.get('quote'))


def build(story, scene_spans):
    events, links, previous = [], [], None
    for context in source_events(story):
        source = context['source']; a, b = source['start'], source['end']
        identifier = 'event-' + fingerprint(source)[:24]
        qualifications = signals(source['quote'], a)
        issues = list(context.get('review_reasons') or [])
        # Parser output has the same forward-only evidence requirements as a shot.
        for role in ('subjects', 'participants', 'animals'):
            for item in context.get(role, []):
                for field in ('evidence', 'link'):
                    if item.get(field) and not evidence_valid(story, item[field], b):
                        issues.append('Участник или субъект не имеет точного предшествующего основания.')
        event = {'id': identifier, 'source': source, 'subjects': context['subjects'],
                 'participants': context['participants'], 'animals': context['animals'],
                 'actions': context['predicates'], 'time': context['time'], 'place': context['place'],
                 'scope': context['scope'], 'qualifications': qualifications,
                 'implicit_actor': context.get('implicit_actor', False),
                 'time_layers': [context['time']['layer']] if context['time'] else [],
                 'source_claim_status': 'qualified' if qualifications else 'unqualified',
                 'historical_accuracy_verified': False,
                 'needs_review': bool(issues) or bool(qualifications),
                 'review_reasons': list(dict.fromkeys(issues))}
        if event['implicit_actor']:
            event['needs_review'] = True
            event['review_reasons'].append('Действующее лицо не названо явно.')
        for match in CONNECTIVE.finditer(source['quote']):
            kind = match.lastgroup
            marker = exact(story, a+match.start(), a+match.end())
            before = exact(story, a, marker['start'])
            after = exact(story, marker['end'], b)
            origin = identifier; endpoint = identifier; resolved = True; fronted = not before['quote']
            # A fronted cause/condition/purpose needs a comma delimiting its clause.
            if not before['quote'] and kind in ('because', 'condition', 'purpose'):
                comma = story.find(',', marker['end'], b)
                if comma < 0:
                    resolved = False
                else:
                    before = exact(story, marker['end'], comma)
                    after = exact(story, comma+1, b)
                    if kind in ('because', 'purpose'): before, after = after, before
            if kind == 'consequence' and not before['quote']:
                contiguous = (previous and previous['scope']['start'] == context['scope']['start'] and
                              not re.search(r'\n\s*\n', story[previous['source']['end']:a]))
                if contiguous:
                    before = previous['source']; origin = previous['id']
                else:
                    resolved = False
            if not before['quote'] or not after['quote']: resolved = False
            relationship = {'because': 'explicit_cause', 'consequence': 'explicit_consequence',
                            'purpose': 'purpose', 'condition': 'condition', 'until': 'temporal_limit'}[kind]
            # "because" puts the effect before the cause. Purpose/condition are
            # separate relationships, never recorded as accomplished outcomes.
            left, right = (after, before) if kind == 'because' or (kind == 'condition' and not fronted) else (before, after)
            polarity = 'denied' if re.search(r'\bне\s*$', story[max(a,marker['start']-12):marker['start']], re.I) else 'source_claim'
            link_signals = list(qualifications)
            if previous and origin == previous['id']:
                link_signals = previous['qualifications'] + link_signals
            link = {'kind': relationship, 'from_event_id': origin, 'to_event_id': endpoint,
                    'from_source': left, 'to_source': right, 'marker': marker,
                    'polarity': polarity, 'qualifications': link_signals,
                    'state': 'candidate' if resolved else 'unresolved',
                    'historical_accuracy_verified': False, 'automatic_resolution': False}
            link['id'] = 'link-' + fingerprint(link)[:24]; links.append(link)
        events.append(event); previous = event
    mapping = []
    for scene in scene_spans:
        rows = [event for event in events if event['source']['start'] < scene['end'] and event['source']['end'] > scene['start']]
        mapping.append({'scene_id': scene.get('scene_id'), 'source': scene,
                        'event_ids': [row['id'] for row in rows],
                        'partial_events': [row['id'] for row in rows if row['source']['start'] < scene['start'] or row['source']['end'] > scene['end']]})
    result = {'version': VERSION, 'story_hash': fingerprint(story), 'events': events,
              'relationships': links, 'scene_mapping': mapping,
              'semantic_completeness_measured': False, 'automatic_application': False,
              'limitations': ['Локальный разбор требует проверки смысла; отсутствие связки не доказывает отсутствие причины.',
                             'Порядок рассказа не устанавливает причину. Утверждение сценария не подтверждает исторический факт.']}
    return {**result, 'hash': fingerprint(result)}

"""Local source interpretation guard; never an independent truth/quality verdict."""
import re
from .domain import fingerprint

VERSION = 'scene-semantics/1'
SIGNALS = {
    'reported': re.compile(r'\b(?:утверждал\w*|заявлял\w*|говорили\s+о|по\s+их\s+поверь\w*|якобы|псевдонауч\w*|легенд\w*)\b', re.I),
    'hypothesis': re.compile(r'\b(?:полага\w*|предполага\w*|возможно|вероятно|гипотез\w*)\b|\bбыл[аои]?\s+бы\b', re.I),
    'conditional': re.compile(r'\bесли\b|\bв\s+случае\b', re.I),
    'purpose': re.compile(r'\b(?:для\s+того,?\s+чтобы|чтобы\s+избежать)\b', re.I),
    'capability': re.compile(r'\b(?:могли|мог|могла|требовалось|позволяла|позволяло|позволяли)\b', re.I),
    'denied_event': re.compile(r'\bне\s+(?:был[аои]?\s+)?(?:взят\w*|произош\w*|разрушен\w*|погиб\w*|следствием\b)|\bни\s+разу\b', re.I),
}
REASONS = {
    'reported': 'Передано чужое утверждение: нельзя показывать его содержание как установленное событие.',
    'hypothesis': 'Гипотеза или неопределённость требует выбора допустимой иллюстрации.',
    'conditional': 'Описано условие, а не подтверждённый отдельный эпизод.',
    'purpose': 'Описана цель или предотвращение события: нельзя превращать угрозу в случившееся действие.',
    'capability': 'Описана возможность или функция: отдельное действие не установлено этой фразой.',
    'denied_event': 'Сценарий отрицает событие: требуется иллюстрация, сохраняющая отрицание.',
}


def signals(quote, start=0):
    return [{'kind': kind, 'evidence': {'start': start+m.start(), 'end': start+m.end(), 'quote': m.group()}}
            for kind, pattern in SIGNALS.items() for m in pattern.finditer(quote)]


def direction_rules(items):
    kinds = {item['kind'] for item in items}
    rules = []
    if kinds & {'reported', 'hypothesis'}:
        rules.append('The source reports a claim or hypothesis. Do not portray its embedded event as established fact; preserve attribution and uncertainty.')
    if 'conditional' in kinds:
        rules.append('Preserve the conditional nature of this explanation; it does not establish that this particular episode happened.')
    if 'capability' in kinds:
        rules.append('This describes a capability or function. An illustration of how it could work is not a witnessed individual action.')
    if 'denied_event' in kinds:
        rules.append('Do not enact the denied event. The absence or rejection of an event must remain visible in the interpretation.')
    if 'purpose' in kinds:
        rules.append('Preserve the stated purpose or prevention; a feared or prevented event is not established as having happened.')
    return rules


def review(story, source, packet):
    quote, start, end = source['quote'], source['start'], source['end']
    items = signals(quote, start)
    findings = [{'code': item['kind'], 'reason': REASONS[item['kind']], 'evidence': item['evidence']} for item in items]
    context = packet.get('narrative_context') or {}
    for reason in context.get('review_reasons', []):
        findings.append({'code': 'event_context', 'reason': reason, 'evidence': source})
    # Validate evidence independently of the parser that selected the subject.
    for role in ('participants', 'subjects', 'animals'):
        for item in context.get(role, []):
            for field in ('evidence', 'link'):
                evidence = item.get(field)
                if evidence and (type(evidence.get('start')) is not int or type(evidence.get('end')) is not int or
                    evidence['start'] < 0 or evidence['end'] > end or evidence['end'] <= evidence['start'] or
                    story[evidence['start']:evidence['end']] != evidence.get('quote')):
                    findings.append({'code': 'invalid_evidence', 'reason': 'Участник или субъект не имеет точного предшествующего основания.', 'evidence': source})
    if packet.get('source_binding') == 'narrated_event':
        if not str(packet.get('primary_subject') or '').strip():
            findings.append({'code': 'unresolved_subject', 'reason': 'Главный предмет изображения не определён; точная цитата сама по себе не выбирает смысловой центр кадра.', 'evidence': source})
        if context.get('implicit_actor'):
            findings.append({'code': 'implicit_actor', 'reason': 'Действие выполняют неназванные участники; нужно определить допустимое изображение людей.', 'evidence': source})
        layers = context.get('time_layers') or []
        if len(layers) > 1:
            findings.append({'code': 'mixed_time', 'reason': 'В кадре объединены действия разных временных состояний.', 'evidence': source})
    result = {'version': VERSION, 'source_hash': fingerprint(source),
              'method': 'local_source_interpretation_rules', 'checked': True,
              'findings': findings, 'signals': items, 'direction_rules': direction_rules(items),
              'status': 'needs_editorial_review' if findings else 'no_local_flags',
              'semantic_completeness_measured': False, 'historical_accuracy_verified': False,
              'image_quality_measured': False}
    return {**result, 'hash': fingerprint(result)}

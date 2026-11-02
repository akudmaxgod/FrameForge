"""Source-bound camera choices, independent of the recurring location image."""
import hashlib
import re
from .domain import DomainError
from .narrative_storyboard import digest

VERSION = 'narrative-framing/1'
VIEWS = {
    'high_oblique': 'Elevated diagonal establishing view; reveal the spatial arrangement with foreground, middle distance and background.',
    'overhead': 'Near-vertical overhead view of the selected subject; emphasize its geometry and material boundaries.',
    'water_level': 'Camera close to water level, looking along the selected stone boundary; reflections and depth lead through the frame.',
    'ground_wide': 'Wide ground-level view from a distinctly different side, with clear depth and the selected subject in its surroundings.',
    'low_angle': 'Low upward camera angle to convey the true scale of the selected masonry, without exaggerating its height.',
    'side_detail': 'Tight diagonal side view of the selected physical subject, emphasizing its edges, depth and construction details.',
    'texture_macro': 'Extreme close view of the selected material; fill the frame with its real texture and a small contextual edge.',
    'leading_lines': 'Ground-level view looking lengthwise along the selected structure, with strong perspective and deep focus.',
    'reverse_view': 'View from the opposite side of the selected subject, with a fresh foreground and different background alignment.',
    'specimen_macro': 'Close specimen photograph on a plain neutral research surface; fill the frame with the specimen, no exterior site background.',
}


def geography_context(story, metadata):
    """Carry literal geography even for shots that correctly have no city image.

    These quotations do not authorize additional objects or transfer modern
    water levels / ruin condition into an ancient reconstruction.
    """
    document = metadata.get('narrative_storyboard') or {}
    quotes = []
    for unit in document.get('source_units', []):
        text = unit['text']
        if re.search(r'тропическ|расположен\w*.*(?:побереж|остров)|вулканическ\w*\s+остров|\b(?:tropical|located.*(?:coast|island))\b', text, re.I):
            if len(' '.join(quotes)) + len(text) > 450:
                continue
            if story[unit['start']:unit['end']] == text:
                quotes.append(text)
        if len(quotes) == 3:
            break
    return ' '.join(quotes)


def framing_packet(story, metadata, data, beat):
    choice = data.get('narrative_framing')
    if not choice:
        return None
    span = [data.get('source_start'), data.get('source_end')]
    if (not isinstance(choice, dict) or choice.get('version') != VERSION or
            choice.get('story_sha256') != hashlib.sha256(story.encode()).hexdigest() or
            choice.get('beat_hash') != digest(beat) or choice.get('source_span') != span or
            choice.get('view') not in VIEWS):
        raise DomainError('Ракурс устарел или не относится к этому смысловому кадру.', 409)
    environment = choice.get('environment', '')
    if not isinstance(environment, str) or len(environment) > 500:
        raise DomainError('Некорректное описание окружения ракурса.', 409)
    evidence = choice.get('environment_evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise DomainError('Некорректные цитаты окружения ракурса.', 409)
    if environment and not evidence:
        raise DomainError('Окружение ракурса не привязано к тексту.', 409)
    for item in evidence:
        if not isinstance(item, dict):
            raise DomainError('Неверная цитата окружения.', 409)
        a, b = item.get('start'), item.get('end')
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(story)
                or story[a:b] != item.get('quote')):
            raise DomainError('Цитата окружения не совпадает со сценарием.', 409)
    return {'camera': VIEWS[choice['view']], 'environment': environment}


def camera_choice(story, beat, data, view, environment='', evidence=()):
    return {'version': VERSION, 'story_sha256': hashlib.sha256(story.encode()).hexdigest(),
            'beat_hash': digest(beat), 'source_span': [data['source_start'], data['source_end']],
            'view': view, 'environment': environment, 'environment_evidence': list(evidence)}


def default_view(beat, part):
    if part < 2:
        return None
    if beat['expected_people']:
        sequence = ('ground_wide', 'reverse_view', 'side_detail')
    elif beat['subject_state'] in ('artifact', 'buried_remains') or beat['function'] == 'detail':
        sequence = ('side_detail', 'texture_macro', 'overhead')
    else:
        sequence = ('ground_wide', 'side_detail', 'high_oblique', 'reverse_view')
    return sequence[(part - 2) % len(sequence)]

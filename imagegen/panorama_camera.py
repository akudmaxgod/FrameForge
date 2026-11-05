"""Deterministic contextual coverage for the Panorama camera profile.

This is a framing planner, not a semantic rewrite or a visual quality evaluator.
It reads existing source/approved-action cues and previous coverage, never adds
story facts, participants, objects, symptoms, or locations. Plans live only in
generation snapshots; source scene revisions remain immutable.
"""
import hashlib
import json
import re

VERSION = 'panorama-camera/2'
SHOTS = {
    'extreme_wide': 'extreme wide establishing shot',
    'wide': 'wide environmental shot',
    'medium': 'contextual medium shot',
    'over_shoulder': 'over-the-shoulder two-person shot',
    'close': 'close-up reaction shot',
    'detail': 'close-up of the source-described action detail',
}
ANGLES = {
    'eye_three_quarter': 'eye-level three-quarter view',
    'side': 'side-on eye-level view',
    'high': 'elevated oblique view',
    'low': 'restrained low oblique view',
}
PLACEMENTS = {'depth': 'foreground depth and layered setting',
              'thirds': 'off-centre, space toward action',
              'relationship': 'clear existing participant positions'}
WIDE = frozenset({'extreme_wide', 'wide'})
PATTERNS = {
    'detail': re.compile(r'\b(?:сып\w*|припухл\w*|от[её]к\w*|шрам\w*|кольц\w*|рукоят\w*|'
                         r'rash|swelling|scar|hilt|ring)\b|(?:переда\w*|вруча\w*|дарит|показыва\w*|'
                         r'читает|осматрива\w*|hands?\s+over|gives?|examines?|reads?)\b.{0,70}'
                         r'\b(?:меч\w*|письм\w*|книг\w*|кинжал\w*|рук\w*|sword|letter|book|dagger|hand)\b', re.I),
    'action': re.compile(r'\b(?:беж\w*|броса\w*|атаку\w*|сража\w*|спас\w*|отступ\w*|'
                         r'плыв\w*|летит|поднима\w*|спуска\w*|едет|ид[её]т|входит|выходит|'
                         r'схватк\w*|бой|битв\w*|runs?|running|attack\w*|fight\w*|escap\w*|'
                         r'ride\w*|riding|climb\w*|sail\w*|flies|flying|walk\w*|enter\w*|leaves)\b', re.I),
    'dialogue': re.compile(r'\b(?:говор\w*|рассказ\w*|объясня\w*|убежда\w*|предлага\w*|'
                           r'обсужда\w*|спрашива\w*|отвеча\w*|разговор\w*|talk\w*|speak\w*|'
                           r'tells?|asks?|answers?|discuss\w*|offers?|persuad\w*)\b', re.I),
    'reaction': re.compile(r'\b(?:удив\w*|испуга\w*|плачет|улыба\w*|осозна\w*|'
                           r'потряс\w*|смотрит|surpris\w*|frighten\w*|cries|smil\w*|'
                           r'realiz\w*|shocked|watches)\b', re.I),
    'landscape': re.compile(r'\b(?:пейзаж\w*|ландшафт\w*|долин\w*|побереж\w*|горизонт\w*|'
                            r'флот\w*|город\w*|крепост\w*|замок|замка|горы|водопад\w*|гейзер\w*|'
                            r'landscape|valley|coast\w*|horizon|fleet|city|castle|mountains?|waterfall|geyser)\b', re.I),
    'crowd': re.compile(r'\b(?:арм\w*|войск\w*|толп\w*|стад\w*|отряд\w*|'
                        r'army|armies|troops|crowd|herd|formation)\b', re.I),
}


def source_key(scene):
    values = [scene.get(k) for k in ('source_quote', 'source_start', 'source_end')]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def human_count(project, scene):
    """An animal or an unbound observer must not manufacture an OTS partner."""
    from .subject_policy import enrich
    from .domain import is_nonhuman_character
    registry = enrich(project.get('characters') or [], project.get('cast_bible'))
    chosen = set(scene.get('characters') or []) | set(scene.get('character_ids') or [])
    selected = [c for c in registry if any(c.get(k) in chosen for k in ('id', 'cast_id', 'name', 'canonical_name'))]
    if selected:
        return sum(not is_nonhuman_character(c) and c.get('identity_scope') != 'collective'
                   for c in selected) + len(scene.get('episodic_people') or [])
    # Without registry evidence do not infer two people from two arbitrary names.
    participants = (scene.get('visual_content') or {}).get('participants') or []
    if participants:
        return sum(isinstance(p, dict) and p.get('kind') == 'human' for p in participants)
    return len(scene.get('episodic_people') or [])


def cues(project, scene):
    approved = scene.get('visual_content') or scene.get('frame_focus') or {}
    text = ' '.join(str(approved.get(k) or '') for k in ('visual_direction', 'action', 'state')).strip()
    if not text:
        text = str(scene.get('source_quote') or scene.get('beat') or '')
    humans = human_count(project, scene)
    if PATTERNS['detail'].search(text): kind = 'detail'
    elif PATTERNS['action'].search(text): kind = 'action'
    elif PATTERNS['dialogue'].search(text) and humans >= 2: kind = 'dialogue'
    elif PATTERNS['reaction'].search(text) and humans: kind = 'reaction'
    elif scene.get('source_groups') or PATTERNS['crowd'].search(text): kind = 'crowd'
    elif PATTERNS['landscape'].search(text): kind = 'landscape'
    else: kind = 'context'
    place = str(approved.get('place') or scene.get('location_ref') or scene.get('location') or '').strip()
    if place in ('Обстановка по исходному фрагменту', 'Исходная обстановка'): place = ''
    episode = (scene.get('storyboard_frame') or {}).get('semantic_episode')
    return kind, humans, place, episode


def build_plan(project, scenes):
    """Choose coverage as a sequence, including frames outside a scoped request."""
    weights = {
        'context': {'wide': 7, 'extreme_wide': 5, 'medium': 4},
        'landscape': {'extreme_wide': 9, 'wide': 8, 'medium': 1},
        'crowd': {'wide': 8, 'extreme_wide': 7, 'medium': 3},
        'action': {'wide': 8, 'medium': 6, 'extreme_wide': 3},
        'dialogue': {'over_shoulder': 7, 'medium': 6, 'wide': 5},
        'reaction': {'close': 7, 'medium': 6, 'wide': 4},
        'detail': {'detail': 12, 'medium': 5, 'wide': 1},
    }
    target = {'slow': .72, 'steady': .62, 'fast': .53}.get(project.get('camera_pacing'), .62)
    result, history, wide_count = {}, [], 0
    previous_place, previous_episode = '', None
    ordered = sorted(scenes, key=lambda row: row.get('position', 0))
    for ordinal, row in enumerate(ordered):
        scene = row.get('data', row)
        key = row.get('id', row.get('scene_id', ordinal))
        if scene.get('frame_composition'):
            # Preserve approved camera wording, including an explicit Preserve.
            result[key] = None
            history.append(None)
            continue
        kind, humans, place, episode = cues(project, scene)
        new_space = bool(place and previous_place and place != previous_place)
        new_episode = episode is not None and previous_episode is not None and episode != previous_episode
        establishing = (not history or new_space or new_episode) and kind not in ('detail', 'reaction')
        scores = dict(weights[kind])
        if establishing:
            scores.update(extreme_wide=12 if kind in ('landscape', 'crowd', 'context') else 6,
                          wide=11 if kind in ('action', 'dialogue') else 9)
        last = history[-1] if history else None
        prior = history[-2] if len(history) > 1 else None
        for shot in scores:
            if last and shot in WIDE and last['shot'] in WIDE:
                scores[shot] -= 2
                if prior and prior['shot'] in WIDE: scores[shot] -= 4
            if last and shot == last['shot']:
                scores[shot] -= 4
                if prior and prior['shot'] == shot: scores[shot] -= 6
            if shot in WIDE and wide_count < target * (len(history) + 1):
                scores[shot] += 3
        shot = max(scores, key=scores.get)
        if shot == 'over_shoulder' and humans < 2:
            shot = 'medium'
        if shot in WIDE and kind in ('landscape', 'crowd'):
            angles = ['high', 'eye_three_quarter', 'side']
        elif shot in WIDE and kind == 'action':
            angles = ['side', 'low', 'eye_three_quarter']
        else:
            angles = ['eye_three_quarter', 'side']
        angle = next((a for a in angles if not last or a != last['angle']), angles[0])
        placement = 'relationship' if kind == 'dialogue' else ('depth' if shot in WIDE else 'thirds')
        plan = {'version': VERSION, 'source_key': source_key(scene), 'shot': shot,
                'angle': angle, 'placement': placement, 'category': kind,
                'human_count': humans, 'establishing': establishing,
                'reason': ('Новое место/эпизод: показать обстановку.' if establishing else {
                    'detail': 'В источнике есть важная предметная или телесная деталь.',
                    'action': 'Показать действие и его пространственное направление.',
                    'dialogue': 'Связать уже присутствующих собеседников.',
                    'reaction': 'Сделать описанную реакцию читаемой.',
                    'landscape': 'Показать масштаб и устройство места.',
                    'crowd': 'Сохранить масштаб описанной группы.',
                    'context': 'Контекстный план с отличием от предыдущей постановки.'}[kind])}
        result[key] = plan
        history.append(plan)
        wide_count += shot in WIDE
        if place: previous_place = place
        if episode is not None: previous_episode = episode
    return result


def selection(project, scene):
    if project.get('camera_mode') != 'panorama' or scene.get('frame_composition'):
        return None
    value = scene.get('panorama_camera')
    if (isinstance(value, dict) and value.get('version') == VERSION
            and value.get('source_key') == source_key(scene)
            and value.get('shot') in SHOTS and value.get('angle') in ANGLES
            and value.get('placement') in PLACEMENTS
            and (value['shot'] != 'over_shoulder' or human_count(project, scene) >= 2)):
        return value
    return build_plan(project, [{'id': 'current', 'data': scene}])['current']


def instruction(project, scene):
    plan = selection(project, scene)
    if plan is None:
        return ''
    subject = {
        'extreme_wide': 'Environment dominates; human height 10-20% of frame.',
        'wide': 'Setting/action dominate; human height 25-45% of frame.',
        'medium': 'Keep setting around the source action.',
        'over_shoulder': 'Past one existing participant toward the other; no extra observer.',
        'close': 'Source-described reaction, with setting visible.',
        'detail': 'Source-required detail only; intentional body cropping allowed.',
    }[plan['shot']]
    if plan.get('human_count', 0) == 0 and plan['shot'] in WIDE:
        subject = 'Environment dominates; true subject scale; no added human scale figure.'
    return (f"CAMERA: {SHOTS[plan['shot']]}; {ANGLES[plan['angle']]}; {PLACEMENTS[plan['placement']]}. "
            + subject + ' Refs: identity, not cropping. Keep action/place/cast/detail and screen direction/sides. '
            'Plausible vantage; no extra people/props/events.')


def owns_framing(project, scene):
    if project.get('camera_mode') != 'panorama':
        return False
    composition = scene.get('frame_composition')
    return not composition or composition.get('shot_size') != 'preserve'


def wide_framing(project, scene):
    if scene.get('frame_composition'):
        return (scene['frame_composition'] or {}).get('shot_size') == 'wide'
    plan = selection(project, scene)
    return bool(plan and plan['shot'] in WIDE)

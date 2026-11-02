"""An explicitly pinned fictional participant, scoped to exact narration spans.

Environment references never identify people. Repeated unnamed source roles can
share this independent visual reference without turning other residents into the
same person. No automatic coreference is inferred from the word 'man'.
"""
import hashlib
import json

from .domain import DomainError
from .db import dump, now


def bind_identity(service, project, scene, expected_people):
    identity = scene.get('participant_identity')
    if identity is None:
        return None
    if not isinstance(identity, dict):
        raise DomainError('Эталон героя должен содержать привязку к сцене.', 409)
    evidence = identity.get('source') or {}
    if not isinstance(evidence, dict):
        raise DomainError('Эталон героя должен ссылаться на точный фрагмент сценария.', 409)
    start, end, quote = evidence.get('start'), evidence.get('end'), evidence.get('quote')
    if (type(start) is not int or type(end) is not int or not isinstance(quote, str)
            or not quote or not 0 <= start < end <= len(project['story'])
            or project['story'][start:end] != quote
            or scene.get('source_quote') != quote
            or scene.get('source_start') != start or scene.get('source_end') != end):
        raise DomainError('Текст сцены изменился: обновите привязку эталона героя.', 409)
    binding = (scene.get('forgotten_cities_prompt_contract') or {}).get('source_binding')
    if expected_people != 1 or scene.get('population_policy') in ('inhabited_city', 'source_people', 'source_group') or binding in ('buried_remains', 'artifact_detail'):
        raise DomainError('Этот эталон героя применяется только к сцене с одним живым участником.', 409)
    name, appearance = identity.get('name'), identity.get('appearance')
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or not isinstance(appearance, str) or len(appearance) > 240:
        raise DomainError('Укажите короткое имя и описание внешности героя.', 409)
    asset = service.db.one('SELECT * FROM assets WHERE id=? AND project_id=?', (identity.get('asset_id'), project['id']))
    if not asset:
        raise DomainError('Эталон героя не найден в этом проекте.', 409)
    generation = service.db.one('SELECT * FROM generations WHERE id=?', (asset['generation_id'],))
    snapshot = json.loads(generation['snapshot']) if generation and isinstance(generation.get('snapshot'), str) else (generation or {}).get('snapshot') or {}
    if not generation or generation['state'] != 'succeeded' or generation['purpose'] != 'scene' or snapshot.get('expected_people') != 1:
        raise DomainError('Для эталона героя нужен готовый кадр с одним человеком.', 409)
    path = (service.root / asset['path']).resolve()
    if not path.is_relative_to(service.root.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
        raise DomainError('Файл эталона героя отсутствует или изменился.', 409)
    origin = service.db.one('SELECT position FROM scenes WHERE id=? AND project_id=?', (generation['scene_id'], project['id']))
    return {**identity, 'id': 'participant:' + asset['id'], 'sha256': asset['sha256'],
            'reference_position': origin['position'] if origin else None}


def identity_instruction(scene):
    identity = scene.get('participant_identity')
    if not isinstance(identity, dict) or scene.get('visible_people') != 1:
        return ''
    return (f"FIXED PARTICIPANT: Character @{identity['name']} in the attached identity image is the SAME person in this scene. "
            'Preserve his face, age, build, hair and clothing; no new face or headwear. '
            + identity.get('appearance', '') + '. '
            'Use this image for the person only. Do not copy its room, pose or tools; the narration defines the action and equipment. ')


def identity_reference(identity):
    return {'asset_id': identity['asset_id'], 'participant_id': identity['id'],
            'name': identity['name'], 'role': 'portrait', 'scope': 'participant_identity',
            'sha256': identity['sha256']}


def pin_participant(service, project_id, request):
    """Pin a chosen person to explicitly selected scenes, in one transaction."""
    project = service.project(project_id)
    generation = service.db.one('SELECT * FROM generations WHERE id=? AND project_id=?',
                                (request.get('generation_id'), project_id))
    if not generation or not generation.get('scene_id') or not generation.get('asset_id'):
        raise DomainError('Выберите готовое изображение героя.', 409)
    origin = service.scene(generation['scene_id'])
    if origin['active_generation_id'] != generation['id']:
        raise DomainError('Выбранный эталон уже заменён другим вариантом.', 409)
    targets = request.get('scene_ids')
    revisions = request.get('revisions') or {}
    if not isinstance(revisions, dict) or not isinstance(targets, list) or not targets or any(not isinstance(s, str) for s in targets):
        raise DomainError('Выберите сцены с этим же героем.', 409)
    changes = []
    for scene_id in dict.fromkeys(targets):
        scene = service.scene(scene_id)
        if scene['project_id'] != project_id:
            raise DomainError('Все сцены героя должны принадлежать этому проекту.', 409)
        if scene['current_revision'] != revisions.get(scene_id):
            raise DomainError('Сцены изменились. Обновите страницу.', 409)
        if service.db.one("SELECT id FROM generations WHERE scene_id=? AND state IN ('queued','preparing','submitted','running','saving','reconciling') AND cancel_requested=0", (scene_id,)):
            raise DomainError('Сначала завершите или отмените генерацию выбранных сцен.', 409)
        data = scene['data']
        grounded = service._ground_forgotten_scene(scene, project)['data']
        identity = {'asset_id': generation['asset_id'], 'name': request.get('name') or 'Герой',
                    'appearance': request.get('appearance') or '', 'reference_position': origin['position'],
                    'source': {'start': data.get('source_start'), 'end': data.get('source_end'), 'quote': data.get('source_quote')}}
        identity = bind_identity(service, project, {**grounded, 'participant_identity': identity}, grounded.get('visible_people'))
        # The selected origin keeps its revision and approval; its exact image
        # already defines this appearance. Other scenes carry the persistent pin.
        if scene_id != origin['id'] and data.get('participant_identity') != identity:
            changes.append((scene, {**data, 'participant_identity': identity}))
    with service.db.transaction() as connection:
        current = connection.execute('SELECT active_generation_id FROM scenes WHERE id=?', (origin['id'],)).fetchone()[0]
        if current != generation['id']:
            raise DomainError('Эталон изменился. Обновите страницу.', 409)
        if connection.execute('SELECT story FROM projects WHERE id=?', (project_id,)).fetchone()[0] != project['story']:
            raise DomainError('Сценарий изменился. Обновите привязку героя.', 409)
        for scene, updated in changes:
            revision = connection.execute('SELECT current_revision FROM scenes WHERE id=?', (scene['id'],)).fetchone()[0]
            if revision != scene['current_revision']:
                raise DomainError('Сцены изменились. Обновите страницу.', 409)
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene['id'], revision + 1, dump(updated), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (revision + 1, scene['id']))
        service.db.event(connection, project_id, 'participant_identity_pinned',
                         {'asset_id': generation['asset_id'], 'origin_scene_id': origin['id'], 'scene_ids': targets})
    return {'asset_id': generation['asset_id'], 'origin_position': origin['position'],
            'scene_ids': targets, 'changed_scene_ids': [scene['id'] for scene, _ in changes], 'locked': True}

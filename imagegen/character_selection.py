"""Derived reference selection; the complete character registry stays intact."""
import copy
import json


def _aliases(character):
    value = character.get('aliases') or []
    return json.loads(value) if isinstance(value, str) else value


def resolve_links(scene, characters, *, names_only=False):
    """Resolve IDs before labels, rejecting ambiguous labels instead of guessing."""
    identities, labels = {}, {}
    for index, character in enumerate(characters):
        for token in (character.get('id'), character.get('cast_id')):
            if token:
                identities.setdefault(str(token), set()).add(index)
        for token in (character.get('name'), character.get('canonical_name'), *_aliases(character)):
            if token:
                labels.setdefault(str(token).strip().casefold(), set()).add(index)
    selected, issues = [], []
    tokens = ([] if names_only else list(scene.get('character_ids') or [])) + list(scene.get('characters') or [])
    for token in tokens:
        matches = identities.get(str(token)) or labels.get(str(token).strip().casefold(), set())
        if len(matches) == 1:
            index = next(iter(matches))
            if index not in selected:
                selected.append(index)
        else:
            issues.append({'code': 'ambiguous_character_link' if matches else 'unknown_character_link',
                           'message': f'Неоднозначная или неизвестная ссылка на персонажа: {token}',
                           'details': {'token': str(token)}})
    result = dict(scene)
    result['characters'] = [characters[i].get('name') or characters[i].get('canonical_name') for i in selected]
    result['character_ids'] = [characters[i].get('cast_id') or characters[i].get('id') for i in selected]
    return result, issues


def project_selection(project):
    from .domain import resolve_scene_presence_contract, DomainError
    from .generation_scene import normalize
    from .mode_policy import allows
    from .subject_policy import enrich, requires_portrait
    characters = enrich(project.get('characters', []), project.get('cast_bible'))
    # The resolver accepts both persistence representations. Its outputs use cast IDs.
    usage = {c['id']: {'scene_ids': [], 'scene_revisions': []} for c in characters}
    previous, events, issues = None, {}, []
    for scene in project.get('scenes', []):
        try:
            data = normalize(project, copy.deepcopy(scene.get('data', scene)), project.get('locations', []))
        except DomainError as error:
            # Invalid source content must remain inspectable in detail/preflight.
            issues.append({'code': 'invalid_character_source', 'message': str(error), 'scene_id': scene.get('id')})
            continue
        event = data.get('event_id')
        if event:
            events.setdefault(event, data)
        contract = resolve_scene_presence_contract(data, characters, project.get('locations', []),
            story=project.get('story', ''), previous_contract=previous,
            parent_event=events.get(event), scene_index=scene.get('position'))
        previous = contract
        issues.extend({**e, 'scene_id': scene.get('id')} for e in contract['semantic_errors'])
        visible = set(contract['visible_character_ids'])
        for c in characters:
            if (c.get('cast_id') or c['id']) in visible or c['id'] in visible:
                usage[c['id']]['scene_ids'].append(scene.get('id'))
                usage[c['id']]['scene_revisions'].append(scene.get('current_revision'))
    selected = [c['id'] for c in characters if usage[c['id']]['scene_ids'] and requires_portrait(c) and allows(project, 'character')]
    requirements = []
    for kind, resources in [('character', characters), ('location', project.get('locations', [])), ('prop', project.get('props', []))]:
        if not allows(project, kind):
            continue
        for resource in resources:
            if kind == 'character' and resource['id'] not in selected:
                continue
            if kind == 'prop':
                from .domain import prop_is_story
                if not prop_is_story(resource):
                    continue
            key = {'character': 'portrait', 'location': 'environment', 'prop': 'prop'}[kind]
            refs = resource.get('references') or json.loads(resource.get('references_json') or '{}')
            requirements.append({'id': resource['id'], 'kind': kind, 'name': resource['name'],
                                 'ready': bool(resource.get('locked') and refs.get(key))})
    return {'character_reference_ids': selected, 'character_usage': usage,
            'character_selection_issues': issues, 'reference_requirements': requirements}

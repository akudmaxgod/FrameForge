"""Separate a visible subject from an individual requiring a persistent identity.

Structured registry evidence wins. Legacy collective heads and anonymous-animal
descriptions are reconciled without deleting registry rows or changing source text.
"""
import copy
import json
import re

VERSION = 'subject-reference-policy/1'
FIELDS = ('entity_type', 'identity_scope')
ANIMAL_GROUP = re.compile(r'^(?:(?:больш\w*|мал\w*|огромн\w*|небольш\w*)\s+)*(?:стад\w*|ста[яиюе]\w*|табун\w*|отар\w*|косяк\w*|рой|ро[яюем]\w*|herd|flock|swarm|pack|shoal)\b', re.I)
GROUP = re.compile(r'^(?:(?:больш\w*|мал\w*|огромн\w*|небольш\w*)\s+)*(?:групп\w*|толп\w*|населени\w*|общин\w*|команд\w*|отряд\w*|арми\w*|войск\w*|семь[яиюе]\w*|люди|зрители|прохожие|туристы|охранники|солдаты|group|crowd|population|team|army|family|people|tourists|guards|soldiers)\b', re.I)
ANIMAL = re.compile(r'\b(?:animal|creature|non-?human|mammal|bird|fish|reptile|amphibian|insect|arachnid|mollusc|mollusk|cephalopod|животн\w*|существ\w*|млекопитающ\w*|птиц\w*|рыб\w*|пресмыкающ\w*|земноводн\w*|насеком\w*|паукообразн\w*|моллюск\w*|головоног\w*)\b', re.I)
ANONYMOUS = re.compile(r'^(?:одинок\w*|дикий|дикая|дикое|дикие|обычн\w*|случайн\w*|безымянн\w*|неизвестн\w*|один|одна|одно|несколько|generic|anonymous|unnamed|wild|lone|a|an)\b', re.I)


def policy(character):
    c = character if isinstance(character, dict) else {'name': str(character)}
    name = str(c.get('canonical_name') or c.get('name') or '').strip()
    description = str(c.get('description') or '').strip()
    declared = str(c.get('entity_type') or '').casefold()
    scope = str(c.get('identity_scope') or '').casefold()
    animal_group = bool(ANIMAL_GROUP.match(name) or ANIMAL_GROUP.match(description))
    collective = animal_group or scope == 'collective' or declared == 'group' or bool(GROUP.match(name))
    kind = {'person':'human', 'nonhuman':'animal', 'non-human':'animal'}.get(declared, declared)
    if kind not in ('human', 'animal', 'creature', 'object'):
        kind = 'animal' if animal_group or ANIMAL.search(name + ' ' + description) else 'human'
    if collective:
        scope = 'collective'
    elif kind == 'object':
        scope = 'generic'
    elif scope not in ('individual', 'generic'):
        # A named animal remains referenceable; generic wildlife does not become
        # a recurring hero merely because the model put it in the registry.
        scope = 'generic' if kind in ('animal', 'creature') and ANONYMOUS.match(name) else 'individual'
    return {'subject_policy_version': VERSION, 'entity_type': kind, 'identity_scope': scope,
            'portrait_required': scope == 'individual' and kind != 'object'}


def enrich(characters, bible=None):
    if isinstance(bible, str):
        bible = json.loads(bible or '{}')
    bible = bible or {}
    rows = bible.get('characters', []) if isinstance(bible, dict) else bible
    if isinstance(bible, dict) and not isinstance(rows, list): rows = []
    if isinstance(bible, dict) and not rows:
        rows = [v for v in bible.values() if isinstance(v, dict)]
    by_id = {str(c.get('cast_id') or c.get('id')): c for c in rows if isinstance(c, dict)}
    result = []
    for c in characters:
        evidence = by_id.get(str(c.get('cast_id') or c.get('id')), {})
        value = {**c, **{k: evidence[k] for k in FIELDS if evidence.get(k) not in (None, '', 'unknown', 'unspecified')}}
        result.append({**value, **policy(value)})
    return result


def requires_portrait(character):
    return policy(character)['portrait_required']


def source_description(scene):
    rows = scene.get('source_subjects') or []
    if not rows: return ''
    descriptions = [f"{s['name']} ({s['entity_type']}, {s['identity_scope']}): {s.get('description', '')}" for s in rows]
    return ('Source-described subjects: ' + '; '.join(descriptions) +
            '. Preserve their source activity and group/single multiplicity. These are source subjects, not individual portrait identities; no fixed portrait is required for them.')


def normalize_scene(scene, characters):
    """Move non-identity actors to source subjects, retaining their depiction.

    This is a derived rendering view; saved source quotes, IDs and revisions are
    never rewritten. Repeated projection is idempotent.
    """
    chars = enrich(characters)
    rejected = [c for c in chars if not requires_portrait(c)]
    if not rejected:
        return scene
    tokens = {str(t).strip().casefold() for t in [*(scene.get('characters') or []), *(scene.get('character_ids') or [])]}
    selected = []
    for c in rejected:
        aliases = c.get('aliases') or []
        if isinstance(aliases, str): aliases = json.loads(aliases)
        keys = {str(t).strip().casefold() for t in (c.get('id'), c.get('cast_id'), c.get('name'), c.get('canonical_name'), *aliases) if t}
        if tokens & keys: selected.append((c, keys))
    if not selected: return scene
    result = copy.deepcopy(scene)
    excluded = set().union(*(keys for _, keys in selected))
    for key in ('characters', 'character_ids'):
        result[key] = [v for v in result.get(key, []) if str(v).strip().casefold() not in excluded]
    result['cast'] = [c for c in result.get('cast', []) if str(c.get('character_id') or c.get('name') or '').casefold() not in excluded]
    if isinstance(result.get('wardrobe'), dict):
        result['wardrobe'] = {k:v for k,v in result['wardrobe'].items() if k.casefold() not in excluded}
    subjects = list(result.get('source_subjects') or [])
    animals = list(result.get('source_animals') or [])
    groups = list(result.get('source_groups') or [])
    episodic = list(result.get('episodic_people') or [])
    for c, _ in selected:
        name = c.get('name') or c.get('canonical_name')
        subject = {'name':name, 'description':c.get('description', ''), 'entity_type':c['entity_type'],
                   'identity_scope':c['identity_scope'], 'portrait_required':False}
        if subject not in subjects: subjects.append(subject)
        if c['entity_type'] in ('animal', 'creature'):
            if name not in animals: animals.append(name)
        elif c['identity_scope'] == 'collective':
            if name not in groups: groups.append(name)
        elif c['entity_type'] == 'human' and name not in episodic:
            episodic.append(name)
    result.update(source_subjects=subjects, source_animals=animals, source_groups=groups, episodic_people=episodic)
    # Legacy analysis counted registry rows as people, including animal groups.
    for key in ('visible_people', 'raw_visible_people'):
        if type(result.get(key)) is int: result[key] = max(0, result[key] - len(selected))
    if groups:
        result['population_policy'] = 'source_group'
    elif animals and not result['characters'] and not episodic:
        result['population_policy'] = 'source_animals'
    return result

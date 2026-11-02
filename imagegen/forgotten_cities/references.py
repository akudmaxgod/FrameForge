"""Bind visual references without replacing a source-specific setting or event."""
import json
import re

from .grounding import ARCHITECTURE


def scene_locations(project, scene, locations):
    metadata = project.get('analysis_metadata') or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    document = metadata.get('forgotten_cities') or {}
    anchors = document.get('anchors') or []
    packet = scene.get('forgotten_cities_prompt_contract') or {}
    quote = str(packet.get('source_quote') or scene.get('source_quote') or '')
    layer = packet.get('visual_time_layer') or scene.get('time_layer')
    primary = str((document.get('city_bible') or {}).get('canonical_name') or '')
    by_name = {item['name'].casefold(): item for item in locations}
    by_anchor = {item['anchor_id']: item for item in anchors}
    if packet.get('source_binding') in ('artifact_detail', 'artifact_action', 'buried_remains') or layer in ('map', 'museum_display'):
        return []

    def current(anchor, scope):
        resource = by_name.get(str(anchor.get('canonical_name', '')).casefold())
        if not resource:
            return None
        refs = resource.get('references') or resource.get('references_json') or {}
        if isinstance(refs, str):
            refs = json.loads(refs)
        if not resource.get('locked') or not refs.get('environment'):
            return None
        return {**resource, 'reference_scope': scope, 'anchor_id': anchor['anchor_id']}

    if packet.get('sequence_reviewed'):
        # Empty means deliberately no city reference, especially research,
        # samples and geological views. Never fall back to the city master.
        chosen=next((a for a in anchors if a['canonical_name']==scene.get('location_ref')),None)
        item=current(chosen,'exact_location') if chosen else None
        return [item] if item else []

    # A source-specific anchor wins over the city-wide context. Structural
    # type descriptions do not establish one immutable physical room layout.
    for anchor_id in packet.get('anchor_ids') or []:
        anchor = by_anchor.get(anchor_id, {})
        if anchor.get('anchor_type') == 'artifact' or anchor.get('time_layer') != layer:
            continue
        if anchor.get('anchor_type') in ('city_master', 'landscape'):
            continue
        scope = 'material_context' if anchor.get('identity_scope') == 'source_type' else 'exact_location'
        resource = current(anchor, scope)
        if resource:
            return [resource]

    if packet.get('source_topic') and packet['source_topic'] not in ('historical_fieldwork',):
        return []
    narrative = packet.get('narrative_context') or {}
    place = str((narrative.get('place') or {}).get('name') or '')
    linked_structure = any(by_anchor.get(key, {}).get('source_kind') for key in packet.get('anchor_ids') or [])
    city_named = bool(primary and primary.casefold() in quote.casefold())
    master_bound = any(a.get('anchor_type') == 'city_master' and quote and
                       quote in str(a.get('description') or '') and quote in project.get('story', '')
                       for a in anchors)
    context_quote = str((narrative.get('scope') or {}).get('quote') or '')
    city_in_context = bool(primary and context_quote and context_quote in project.get('story', '')
                           and primary.casefold() in context_quote.casefold())
    physical_setting = bool(ARCHITECTURE.search(quote) or linked_structure or
                            re.search(r'дом|помещени|коридор|проход|зал|галере|шахт|тоннел|пещер|город', place, re.I))
    if not physical_setting and not city_named and not city_in_context:
        return []

    # Do not import the main city's appearance into a different named place.
    # Geography mentions carry evidence spans; compare only the selected quote.
    for mention in document.get('geography_mentions') or []:
        name = str(mention.get('text') or '')
        kind = str(mention.get('geography_type') or '').casefold()
        if name and name.casefold() in quote.casefold() and not city_named and not master_bound and kind in (
                'secondary_city', 'comparison_location', 'origin_region', 'physical_sublocation'):
            if primary.casefold() != name.casefold():
                return []

    target_type = 'landscape' if layer in ('modern_ruins', 'excavation') else 'city_master'
    target_layer = 'modern_ruins' if target_type == 'landscape' else 'historical_reconstruction'
    # City-wide images provide material context, never an exact substitute for
    # the narrated room, participant, epoch, geometry, or archaeological fact.
    for anchor in anchors:
        if anchor.get('anchor_type') == target_type and anchor.get('time_layer') == target_layer:
            resource = current(anchor, 'material_context')
            if resource:
                return [resource]
    return []


def context_instruction(name):
    return (f'Attached environment context: {name}. Use only compatible architectural material texture '
            'and visual consistency. The selected source defines the actual place, structure, layout and epoch. '
            'Do not copy people, faces, clothing, tools, props, modern damage, lighting equipment or the camera '
            'composition from this image. Do not replace the source-described corridor, shaft, doorway or '
            'action with the whole reference view. Do not assume different rooms are the same physical room. '
            'The image is a generated illustration, not evidence for undocumented historical details.')


def isolate_material_context(scene, locations, participant_identity=None):
    """Keep optional setting pictures from supplying an unrelated human cast.

    A written negative instruction cannot reliably remove a person from an
    attached illustration. New narration shots opt into structural isolation;
    already completed legacy shots retain their original generation contract.
    Exact location references remain mandatory. A separately bound participant
    identity remains authoritative for shots of the recurring story person.
    """
    if scene.get('environment_reference_policy') != 'isolate_unbound_participants/1' or participant_identity:
        return locations, []
    omitted = [item for item in locations if item.get('reference_scope') == 'material_context']
    return [item for item in locations if item.get('reference_scope') != 'material_context'], omitted

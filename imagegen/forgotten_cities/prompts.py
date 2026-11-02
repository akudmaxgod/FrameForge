"""Five-block generation prompt contract for Forgotten Cities shots."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, List
from .grounding import bind_source, PERSON, ARCHITECTURE, DETAIL
from .lifecycle import apply_city_lifecycle


def _visual_direction(event_id: str, beat: str, time_layer: str, artifact_names: List[str]) -> str:
    """Give each documentary beat a distinct, source-compatible viewpoint."""
    text = f"{beat} {' '.join(artifact_names)}".casefold()
    if any(word in text for word in ("переул", "улиц", "маршрут", "сосед")):
        return "Eye-level street view from inside a narrow adobe lane; walls lead the eye forward and the street itself is the subject."
    if any(word in text for word in ("двор", "помещен", "порог", "очаг", "дом")):
        return "Eye-level view inside a courtyard or domestic room; show thresholds, openings and the relationship between connected spaces."
    if any(word in text for word in ("мастерск", "работ", "формовал", "глин", "строил", "поддерж")):
        return "Close working view at human height: hands or tools only when the people policy permits them, with materials and the work surface clearly visible."
    if any(word in text for word in ("глаз", "печать", "оттиск", "доска", "кость", "лазурит", "предмет")):
        return "Tight archaeological object shot at table height; the named artifact fills the frame and its material details are legible."
    if time_layer == "historical_reconstruction":
        return "Wide but inhabited reconstruction from a low roof or gate height; show complete buildings, streets and courtyards, never modern ruins."
    if time_layer in ("modern_ruins", "excavation"):
        return "Documentary archaeological view with a distinct ground-level or elevated survey angle; show excavated walls and their spatial layout, not a generic skyline."
    variants = (
        "Low street-level architectural view through an adobe passage, with depth and a clear route through the city.",
        "Human-height courtyard view focused on one doorway, threshold and domestic work area.",
        "Close material study of adobe brick, stone and weathered surfaces, with shallow depth of field.",
        "Elevated plan-reading view that clearly reveals rooms, lanes and their relationships.",
        "Wide horizon view of the city and surrounding dry landscape, reserved for a transition or conclusion.",
    )
    number = sum(ord(char) for char in event_id) % len(variants)
    return variants[number]


def documentary_treatment(
    event_id: str,
    ordinal: int,
    total: int,
    context: Dict[str, object],
    artifact_names: List[str],
    source_quote: str = "",
    burial: bool = False,
) -> Dict[str, str]:
    """Choose a varied, evidence-safe visual treatment for a documentary beat.

    Long explanatory scripts often classify most sentences as
    ``abstract_explanation``.  Passing that label directly to the generic
    framing engine previously turned almost every selected beat into an empty
    atmospheric frame.  This deterministic treatment cycle preserves the
    source's semantic layer while choosing a concrete documentary view of the
    already established city, ruins, architecture or artefact.
    """
    source_layer = str(context.get("time_layer") or "abstract_explanation")
    people_policy = str(context.get("people_policy") or "none")
    has_people = people_policy in {"anonymous_action", "anonymous_background"}
    phase = max(0, ordinal - 1) % 6

    # Literal source content outranks diversity, inferred time layers and the
    # generic planner's framing. A detail of an object cannot become a city.
    if burial:
        return {"shot_type": "prop_detail", "visual_time_layer": "excavation",
                "treatment": "source_backed_burial", "source_binding": "buried_remains",
                "direction": "Documentary close view into the source-described grave. Show the source-described person's deceased remains lying in the burial, never a living portrait. Preserve the stated sex and the named artifact's position relative to the remains when specified. The deceased person is not sitting, standing, smiling or looking at the camera. Preserve the burial context; no invented violence, blood or graphic injury."}
    if artifact_names:
        if any('череп' in name.casefold() for name in artifact_names):
            return {"shot_type": "prop_detail", "visual_time_layer": "museum_display",
                    "treatment": "source_backed_artifact_detail", "source_binding": "artifact_detail",
                    "direction": "Archaeological study of the same skull and its source-described triangular opening. Use a lateral view making the opening legible, or a close detail of its bone margins when described. Preserve the reference anatomy and angular contour in every view. No artificial eye, no circular drilled hole, no invented surgery."}
        human_action = bool(PERSON.search(source_quote) or any(w in source_quote.casefold() for w in ('мастер', 'ремеслен', 'изготов', 'делал', 'craftsperson')))
        if human_action:
            return {"shot_type": "main_action", "visual_time_layer": source_layer if source_layer in {'excavation', 'museum_display'} else 'historical_reconstruction',
                    "treatment": "source_backed_artifact_action", "source_binding": "artifact_action",
                    "direction": "Close human-height view of the exact source-described interaction with " + artifact_names[0] + ". Depict only the stated action and the stated living/deceased condition. The artifact remains readable and matches its attached reference; do not substitute a generic city view."}
        details = ("show the stated pupil, fine radial surface lines or edge attachment holes only when described. Radial lines are surface markings, never streets, canals, walls, a well or city layout. "
                   if any('глаз' in name.casefold() for name in artifact_names) else
                   "show only this object's source-described features, not markings borrowed from a different find. ")
        return {"shot_type": "prop_detail", "visual_time_layer": "museum_display",
                "treatment": "source_backed_artifact_detail", "source_binding": "artifact_detail",
                "direction": "Macro archaeological object study of " + artifact_names[0] + ". The source-described markings belong to the surface of this same small artifact. Preserve its reference shape, material and scale; " + details + "Keep architecture out of the main subject."}
    if source_quote and ARCHITECTURE.search(source_quote):
        return {"shot_type": "establishing", "visual_time_layer": source_layer if source_layer != 'abstract_explanation' else 'historical_reconstruction',
                "treatment": "source_backed_architecture", "source_binding": "architecture",
                "direction": _visual_direction(event_id, source_quote, source_layer, [])}

    street_views = (
        "Eye-level street view through an inhabited adobe lane, with doorways, thresholds and a clear route through the city.",
        "Low street-level view from a city gate toward branching adobe lanes; show the full depth of the neighbourhood.",
        "Courtyard-to-street view through an open doorway, showing connected homes, lanes and public space.",
    )
    reconstruction_views = (
        street_views[phase % len(street_views)],
        "Elevated roof-height view across dense adobe homes, courtyards, lanes and the surrounding dry plain.",
        "Human-height courtyard view of domestic architecture, thresholds, hearth area and connected rooms.",
    )
    ruins_views = (
        "Ground-level archaeological survey along excavated adobe walls and lanes, with room layout clearly readable.",
        "Elevated documentary survey of modern ruins showing foundations, courtyards and the route between sectors.",
    )

    # Preserve explicit source time layers first.  They already have stronger
    # evidence than the explanatory treatment cycle.
    if source_layer == "historical_reconstruction":
        if has_people:
            return {"shot_type": "main_action", "visual_time_layer": source_layer,
                    "treatment": "source_backed_historical_activity",
                    "direction": reconstruction_views[phase % len(reconstruction_views)]}
        return {"shot_type": "establishing" if phase in {0, 3} else "atmosphere",
                "visual_time_layer": source_layer, "treatment": "source_backed_historical_architecture",
                "direction": reconstruction_views[phase % len(reconstruction_views)]}
    if source_layer in {"modern_ruins", "excavation"}:
        return {"shot_type": "establishing" if phase % 2 == 0 else "atmosphere",
                "visual_time_layer": source_layer, "treatment": "source_backed_archaeological_survey",
                "direction": ruins_views[phase % len(ruins_views)]}

    if has_people:
        return {"shot_type": "main_action", "visual_time_layer": "historical_reconstruction",
                "treatment": "illustrative_city_activity",
                "direction": reconstruction_views[phase % len(reconstruction_views)]}
    if phase in {1, 4}:
        return {"shot_type": "establishing", "visual_time_layer": "historical_reconstruction",
                "treatment": "illustrative_city_architecture",
                "direction": reconstruction_views[phase % len(reconstruction_views)]}
    if phase == 2:
        return {"shot_type": "establishing", "visual_time_layer": "modern_ruins",
                "treatment": "illustrative_ruins_context",
                "direction": ruins_views[ordinal % len(ruins_views)]}
    if phase == 3:
        return {"shot_type": "prop_detail", "visual_time_layer": "historical_reconstruction",
                "treatment": "illustrative_architectural_material_detail",
                "direction": "Close material study of adobe brick, stone, timber joints and weathered thresholds; use only architecture and excavated material already established for the city."}
    return {"shot_type": "atmosphere", "visual_time_layer": "historical_reconstruction",
            "treatment": "illustrative_city_context",
            "direction": street_views[ordinal % len(street_views)]}


def build_prompt_packet(
    metadata: Dict[str, object],
    event_id: str,
    beat: str,
    location: str,
    *,
    ordinal: int = 1,
    total: int = 1,
    story_text: str = "",
    source_quote: str = "",
    source_start: int = None,
) -> Dict[str, object]:
    city = metadata.get("city_bible") or {}
    anchors = metadata.get("anchors") or []
    contexts = {item.get("event_id"): item for item in metadata.get("event_contexts", [])}
    sequences = metadata.get("sequences") or []
    context = contexts.get(event_id, {})
    sequence = next((item for item in sequences if event_id in item.get("event_ids", [])), {})
    anchor_ids: List[str] = []
    if sequence.get("location_anchor_id"):
        anchor_ids.append(sequence["location_anchor_id"])
    anchor_ids.extend(sequence.get("artifact_anchor_ids") or [])
    quote = source_quote or beat.removeprefix('Visual enactment of: ').strip()
    binding = bind_source(metadata, event_id, quote, story=story_text, source_start=source_start)
    # Sequence anchors are episode-wide context, not necessarily the subject
    # of this individual sentence. Resolve the subject again for every shot.
    selected = [anchor for anchor in anchors if anchor.get("anchor_id") in anchor_ids and anchor.get('anchor_type') != 'artifact']
    selected.extend(binding['artifacts'])
    anchor_ids = [a['anchor_id'] for a in selected]
    artifact_names = [a.get("canonical_name") for a in selected if a.get("anchor_type") == "artifact"]
    subjects = artifact_names or ([binding['literal_subject']] if binding['literal_subject'] else [])
    treatment = documentary_treatment(event_id, ordinal, total, context, subjects, quote, binding['burial'])
    from .topics import topic_treatment
    topic_view = topic_treatment(binding['topic'], quote)
    if topic_view:
        treatment = topic_view
        subjects = [binding['topic']]
    if binding['architecture_detail'] and not topic_view:
        treatment = {'shot_type': 'prop_detail', 'visual_time_layer': str(context.get('time_layer') or 'modern_ruins'),
                     'treatment': 'source_backed_architectural_detail', 'source_binding': 'architecture',
                     'direction': 'Close view of the source-described architectural surface or material. Depict the stated physical details on that surface; do not invent a different object or a new city layout.'}
    source_roles = []
    if binding['source_action']:
        noticing = bool(re.search(r'заметил\w*', quote, re.I))
        source_roles = ['женщина', 'мастер'] if noticing else ['мастер']
        direction = ('Illustrative historical reconstruction of an anonymous woman and a helper noticing her eye problem. '
                     'Show their interaction, not a grave, excavation or unrelated city view.' if noticing else
                     'Illustrative historical reconstruction of an anonymous craftsperson performing the selected source action: ' + quote +
                     ' Focus on selecting material or shaping the named small artifact as stated, not unrelated pottery or excavation tools.')
        treatment = {'shot_type': 'main_action', 'visual_time_layer': 'historical_reconstruction',
                     'treatment': 'source_backed_artifact_action', 'source_binding': 'artifact_action',
                     'direction': direction + ' The artifact identity and small anatomical eye-socket scale (never palm-sized) must match its reference; exact undocumented details remain unspecified.'}
    narrative = None
    if story_text:
        from .event_context import for_source, narrative_treatment
        narrative = for_source(story_text, quote, source_start)
        if (narrative and not artifact_names
                and (not binding['literal_subject'] or not DETAIL.search(quote))
                and (not topic_view or binding['topic'] == 'historical_fieldwork' or
                     topic_view.get('treatment') == 'illustrative_reported_claim') and not binding['burial']):
            treatment = narrative_treatment(narrative, str(context.get('time_layer') or 'abstract_explanation'))
            from imagegen.narrative_obligations import participant_slots
            source_roles = participant_slots(narrative)
            subjects = source_roles or [s['name'] for s in narrative['subjects'] if not s.get('unresolved')]
            context = {**context, 'time_layer': treatment['visual_time_layer'],
                       'people_policy': 'anonymous_action' if source_roles else 'none'}
            # Episode-wide city anchors must not replace a narrated interior,
            # route or participant action. Attach only an exact setting match.
            selected = [a for a in selected if narrative.get('place') and
                        str(a.get('canonical_name', '')).casefold() == narrative['place']['name'].casefold()]
            from .structures import scene_structure_anchors
            selected.extend(scene_structure_anchors(anchors, story_text, quote,
                narrative['source']['start'], treatment['visual_time_layer']))
            selected = list({a['anchor_id']:a for a in selected}.values())
            anchor_ids = [a['anchor_id'] for a in selected]
    treatment = apply_city_lifecycle(treatment, quote, story_text, source_start)
    visual_direction = treatment["direction"]
    reference_hashes = {
        anchor.get("anchor_id"): hashlib.sha256(
            json.dumps(anchor, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for anchor in selected if anchor.get("anchor_id")
    }
    style = "Historically grounded archaeological documentary imagery; stable visual language across the episode."
    city_contract = (
        f"City: {city.get('canonical_name', '')}. Region: {city.get('region', '')}. "
        f"Period: {city.get('period_start', '')}. Architecture: {city.get('architecture_profile', '')}. "
        f"Materials: {', '.join(city.get('material_palette') or [])}. "
        f"Environment: {city.get('environment_profile', '')}."
    )
    time_contract = (
        f"Time layer: {treatment['visual_time_layer']}. Source semantic layer: {context.get('time_layer', 'abstract_explanation')}. "
        f"Evidence: {context.get('evidence_level', 'established')}."
    )
    if treatment.get('source_binding') == 'narrated_event' and narrative:
        city_contract = 'Narrated setting: ' + (narrative.get('place') or {}).get('evidence', {}).get('quote', 'Only the setting stated in the source') + '. Do not infer architecture or material from an unrelated city anchor.'
        location = (narrative.get('place') or {}).get('evidence', {}).get('quote', 'Only the setting stated in the source')
    shot_contract = (
        f"Source event (authoritative): {quote}. Location context: {location}. People policy: {'none; deceased remains only' if binding['burial'] else context.get('people_policy', 'none')}. "
        f"Primary artifact: {artifact_names[0] if artifact_names else 'none'}. Visual direction: {visual_direction} "
        "Do not substitute this direction with a generic aerial view of ruins."
    )
    if treatment["treatment"].startswith("illustrative_"):
        shot_contract += " This is an illustrative archaeological context, not a claim that a specific undocumented event occurred."
    negatives = ([] if treatment.get('source_binding') == 'narrated_event' else
                 list(city.get("forbidden_anachronisms") or []))
    negatives.extend(["mixed historical periods", "invented inscriptions", "text overlays", "logos", "watermarks"])
    if context.get("evidence_level") == "illustrative_hypothesis":
        negatives.append("do not present the hypothetical circumstances as an established documented event")
        shot_contract += " Illustrative reconstruction; exact circumstances unknown."
    negative_contract = "Forbidden: " + ", ".join(dict.fromkeys(negatives)) + "."
    blocks = {
        "style_contract": style,
        "city_contract": city_contract,
        "time_layer_contract": time_contract,
        "shot_contract": shot_contract,
        "negative_contract": negative_contract,
    }
    return {
        **blocks,
        "anchor_ids": anchor_ids,
        "reference_hashes": reference_hashes,
        "visual_direction": visual_direction,
        "shot_type": treatment["shot_type"],
        "visual_time_layer": treatment["visual_time_layer"],
        "documentary_treatment": treatment["treatment"],
        "source_binding": treatment.get('source_binding', 'context'),
        "source_quote": quote,
        "source_topic": binding['topic'],
        "source_action": quote if treatment.get('source_binding') == 'narrated_event' else binding['source_action'],
        "narrative_context": narrative if treatment.get('source_binding') == 'narrated_event' else None,
        "source_roles": source_roles,
        "city_population": treatment.get('city_population'),
        "required_artifacts": artifact_names,
        "primary_subject": subjects[0] if subjects else '',
        "binding_errors": binding['errors'],
        "compiled_prompt": "\n".join((
            f"STYLE CONTRACT: {style}",
            f"CITY CONTRACT: {city_contract}",
            f"TIME LAYER: {time_contract}",
            f"SHOT CONTRACT: {shot_contract}",
            f"NEGATIVE CONTRACT: {negative_contract}",
        )),
    }

"""Visual Safety Framing Engine for Stage 6 Visual Event Planner.

Generates safe framing policies, shot types, and composition descriptions,
strictly preventing non-physical entities (VOICE_ONLY, DEPICTED, MENTIONED, PERCEIVED_VOICE,
DECEASED_MENTION) from appearing as physical scene participants.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.models import (
    DocumentIR,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    PresenceRole,
)
from imagegen.analyzer_v3.planner_models import (
    CameraAngle,
    EventNode,
    EventNodeType,
    VisualFramingPolicy,
    VisualShotType,
)

TRANSMISSION_KEYWORDS = {"телефон", "трубка", "рация", "динамик", "интерком", "радио", "громкоговоритель", "связь", "сообщение"}
DEPICTION_KEYWORDS = {"фотография", "фото", "портрет", "картина", "рисунок", "экран", "монитор", "статуя", "холст"}
PERCEPTION_KEYWORDS = {"показалось", "почудилось", "галлюцинация", "в мыслях", "в голове", "привиделось"}
DECEASED_KEYWORDS = {"могила", "память", "покойный", "умерший", "погибший"}


class VisualSafetyFramingEngine:
    """Computes framing policies and safe visual guidelines for an event."""

    def compute_framing(
        self,
        node: EventNode,
        event: EventIR,
        doc_ir: DocumentIR,
    ) -> Tuple[VisualFramingPolicy, VisualShotType, CameraAngle, str, str, str, str]:
        """Returns:
        (framing_policy, shot_type, camera_angle, safe_framing_desc, visual_action, mood, composition_focus)
        """
        text_lower = event.span.text.lower()
        entity_map = {e.entity_id: e for e in doc_ir.entities}

        phys_entities = [entity_map[eid] for eid in node.physical_entity_ids if eid in entity_map]
        non_phys_entities = [entity_map[eid] for eid in node.non_physical_entity_ids if eid in entity_map]

        physical_objects = [e for e in phys_entities if e.entity_type in (EntityType.OBJECT, EntityType.VEHICLE)]
        phys_entities = [e for e in phys_entities if e.entity_type in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE)]
        has_physical_actors = len(phys_entities) > 0
        has_non_physical = len(non_phys_entities) > 0

        # Determine framing policy & safe framing description
        if not has_physical_actors:
            # Strictly safe non-physical framing
            if physical_objects:
                framing_policy = VisualFramingPolicy.PROP_DETAIL
                shot_type = VisualShotType.PROP_DETAIL
                camera_angle = CameraAngle.EYE_LEVEL
                safe_desc = "Show the source-described physical object; do not invent visible people."
                focus = ", ".join(e.canonical_name for e in physical_objects)
            elif any(re.search(rf"\b{w}\w*\b", text_lower) for w in TRANSMISSION_KEYWORDS) or any(e.modality == EntityModalityV3.VOICE_ONLY for e in non_phys_entities):
                framing_policy = VisualFramingPolicy.PROP_DETAIL
                shot_type = VisualShotType.PROP_DETAIL
                camera_angle = CameraAngle.EYE_LEVEL
                voice_names = ", ".join(
                    e.canonical_name for e in non_phys_entities
                    if e.modality in (EntityModalityV3.VOICE_ONLY, EntityModalityV3.PERCEIVED_VOICE)
                ) or "неуказанный собеседник"
                safe_desc = (
                    f"Показать только средство аудиосвязи; голос {voice_names} остаётся за кадром. "
                    "Не изображать удалённого собеседника как физически присутствующего персонажа."
                )
                focus = "Communication device / audio interface"
            elif any(re.search(rf"\b{w}\w*\b", text_lower) for w in DEPICTION_KEYWORDS) or any(e.modality == EntityModalityV3.DEPICTED for e in non_phys_entities):
                framing_policy = VisualFramingPolicy.DEPICTED_MEDIUM
                shot_type = VisualShotType.CLOSEUP_FOCUS
                camera_angle = CameraAngle.EYE_LEVEL
                safe_desc = (
                    "Framing focused on physical medium (framed photograph/screen/artwork). "
                    "Depicted individual is represented solely within the image medium, not standing in physical room."
                )
                focus = "Depiction surface / medium artifact"
            elif any(re.search(rf"\b{w}\w*\b", text_lower) for w in PERCEPTION_KEYWORDS) or any(e.modality == EntityModalityV3.PERCEIVED_VOICE for e in non_phys_entities):
                framing_policy = VisualFramingPolicy.SILHOUETTE_WITHOUT_IDENTITY
                shot_type = VisualShotType.ENVIRONMENT_ATMOSPHERIC
                camera_angle = CameraAngle.DUTCH_ANGLE
                safe_desc = (
                    "Atmospheric psychological framing with vague ambient shadows. "
                    "Imagined voice must NOT produce a defined character portrait."
                )
                focus = "Atmospheric shadows / psychological ambiance"
            else:
                framing_policy = VisualFramingPolicy.EMPTY_LOCATION
                shot_type = VisualShotType.ENVIRONMENT_ATMOSPHERIC
                camera_angle = CameraAngle.EYE_LEVEL
                safe_desc = "Depict location setting and environment without phantom character figures."
                focus = "Setting / Environment"
        else:
            # Physical character present
            if has_non_physical and any(e.modality in (EntityModalityV3.VOICE_ONLY, EntityModalityV3.PERCEIVED_VOICE) for e in non_phys_entities):
                framing_policy = VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER
                shot_type = VisualShotType.REACTION_SHOT
                camera_angle = CameraAngle.EYE_LEVEL
                safe_desc = (
                    f"Frame confirmed physical character {[e.canonical_name for e in phys_entities]} listening/reacting. "
                    f"Voice interlocutor {[e.canonical_name for e in non_phys_entities]} is off-screen/audio-only."
                )
                focus = f"Reaction of {phys_entities[0].canonical_name}"
            elif has_non_physical and any(e.modality == EntityModalityV3.DEPICTED for e in non_phys_entities):
                framing_policy = VisualFramingPolicy.PHYSICAL_ACTOR
                shot_type = VisualShotType.OVER_SHOULDER
                camera_angle = CameraAngle.EYE_LEVEL
                safe_desc = (
                    f"Physical character {phys_entities[0].canonical_name} viewing photo/screen. "
                    f"Depicted figure stays strictly inside medium boundaries."
                )
                focus = f"{phys_entities[0].canonical_name} inspecting depiction"
            else:
                framing_policy = VisualFramingPolicy.PHYSICAL_ACTOR
                safe_desc = f"Direct visual staging of confirmed physical actors: {[e.canonical_name for e in phys_entities]}."
                focus = ", ".join(e.canonical_name for e in phys_entities)

                # Shot selection based on node type
                if node.node_type == EventNodeType.EXPOSITION:
                    shot_type = VisualShotType.ESTABLISHING_WIDE
                    camera_angle = CameraAngle.EYE_LEVEL
                elif node.node_type == EventNodeType.CLIMAX_MOMENT:
                    shot_type = VisualShotType.MEDIUM_ACTION
                    camera_angle = CameraAngle.LOW_ANGLE
                elif node.node_type == EventNodeType.DIALOGUE_EXCHANGE:
                    shot_type = VisualShotType.MEDIUM_ACTION
                    camera_angle = CameraAngle.EYE_LEVEL
                elif node.node_type == EventNodeType.SETTING_TRANSITION:
                    shot_type = VisualShotType.ESTABLISHING_WIDE
                    camera_angle = CameraAngle.EYE_LEVEL
                else:
                    shot_type = VisualShotType.MEDIUM_ACTION
                    camera_angle = CameraAngle.EYE_LEVEL

        # Action description & mood
        action_desc = f"Visual enactment of: {event.span.text.strip()}"
        mood = "Dramatic / cinematic" if node.is_climax else "Narrative realism"

        return framing_policy, shot_type, camera_angle, safe_desc, action_desc, mood, focus

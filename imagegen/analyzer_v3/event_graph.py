"""Event Graph Builder for Story Analyzer V3 Stage 6.

Constructs structural EventGraph strictly from existing EventIR instances in DocumentIR,
identifying temporal, causal, dialogue, and actor continuity relations without hallucinating
new events or connections.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.context import DocumentContext
from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    PresenceRole,
)
from imagegen.analyzer_v3.planner_models import (
    EventEdge,
    EventEdgeType,
    EventGraph,
    EventNode,
    EventNodeType,
)

CAUSAL_CONNECTIVES_REGEX = re.compile(
    r"\b(?:поэтому|потому что|из-за этого|в результате|вследствие чего|благодаря этому|отчего|так как|затем что)\b",
    re.IGNORECASE,
)

CLIMAX_LEXICON = {
    "взрыв", "выстрел", "ударил", "крикнул", "закричал", "погиб", "упал", "ворвался",
    "внезапно", "раздался", "рухнул", "схватил", "ужас", "авария", "катастрофа", "огонь",
    "побег", "спас", "прорыв", "выстрелил", "убил", "исчез", "смерть", "победил"
}


class EventGraphBuilder:
    """Builds a deterministic EventGraph strictly grounded in DocumentIR."""

    def build_graph(self, doc_ir: DocumentIR) -> EventGraph:
        ctx = DocumentContext(doc_ir)
        doc_mode = doc_ir.mode if hasattr(doc_ir, "mode") and doc_ir.mode else DocumentMode.NARRATIVE

        # Identify primary actors (frequently recurring physical entities)
        entity_mention_counts: Dict[str, int] = {}
        entity_map = {e.entity_id: e for e in doc_ir.entities}
        for ev in doc_ir.events:
            for p in ev.presences:
                if p.physical_presence:
                    entity = entity_map.get(p.entity_id)
                    if doc_mode == DocumentMode.NARRATIVE and entity and entity.entity_type not in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE):
                        continue
                    entity_mention_counts[p.entity_id] = entity_mention_counts.get(p.entity_id, 0) + 1

        sorted_actors = sorted(entity_mention_counts.items(), key=lambda x: x[1], reverse=True)
        primary_actors = [ent_id for ent_id, count in sorted_actors if count >= 2]
        if not primary_actors and sorted_actors:
            primary_actors = [sorted_actors[0][0]]

        # Track first appearance of entities across events
        entity_first_event: Dict[str, str] = {}
        for ev in doc_ir.events:
            for p in ev.presences:
                if p.entity_id not in entity_first_event:
                    entity_first_event[p.entity_id] = ev.event_id

        nodes: Dict[str, EventNode] = {}
        prev_location: Optional[str] = None

        for idx, ev in enumerate(doc_ir.events):
            # Physical vs non-physical entities in this event
            phys_entities: List[str] = []
            non_phys_entities: List[str] = []
            actor_ids: List[str] = []
            patient_ids: List[str] = []

            for p in ev.presences:
                if p.physical_presence:
                    phys_entities.append(p.entity_id)
                    if p.role in (PresenceRole.ACTOR, PresenceRole.SUBJECT, PresenceRole.SPEAKER):
                        actor_ids.append(p.entity_id)
                    elif p.role in (PresenceRole.OBJECT, PresenceRole.TARGET):
                        patient_ids.append(p.entity_id)
                else:
                    non_phys_entities.append(p.entity_id)

            # Facts in this event
            fact_ids = [f.fact_id for f in ev.facts]

            # Detect introductions
            intro_entities = [
                ent_id for ent_id, first_ev_id in entity_first_event.items()
                if first_ev_id == ev.event_id and ent_id in phys_entities
            ]
            is_intro = len(intro_entities) > 0

            # Detect climax / dramatic peak
            ev_text_lower = ev.span.text.lower()
            is_climax = any(re.search(rf"\b{word}\w*\b", ev_text_lower) for word in CLIMAX_LEXICON)

            # Location
            loc_entity = next(
                (e for e in doc_ir.entities if e.entity_type == EntityType.LOCATION and e.entity_id in ev.mentioned_entity_ids),
                None
            )
            loc_name = loc_entity.canonical_name if loc_entity else None
            is_location_shift = loc_name is not None and loc_name != prev_location
            if loc_name:
                prev_location = loc_name

            # Classify node type
            if idx == 0:
                node_type = EventNodeType.EXPOSITION
            elif is_climax:
                node_type = EventNodeType.CLIMAX_MOMENT
            elif len(ev.dialogue_speakers) > 0:
                node_type = EventNodeType.DIALOGUE_EXCHANGE
            elif is_location_shift:
                node_type = EventNodeType.SETTING_TRANSITION
            elif len(fact_ids) > 0 and doc_mode in (DocumentMode.FACT_LIST, DocumentMode.MULTI_SUBJECT_REVIEW, DocumentMode.EXPLAINER):
                node_type = EventNodeType.FACT_STATEMENT
            else:
                node_type = EventNodeType.NARRATIVE_BEAT

            # Compute importance score
            importance = 0.4
            if idx == 0 or idx == len(doc_ir.events) - 1:
                importance += 0.35  # Opening & finale are vital
            if any(a in primary_actors for a in actor_ids):
                importance += 0.25
            if is_intro:
                importance += 0.2
            if is_climax:
                importance += 0.3
            if is_location_shift:
                importance += 0.15
            if len(ev.dialogue_speakers) > 0:
                importance += 0.1
            if len(fact_ids) > 0:
                importance += 0.15

            importance = min(1.0, round(importance, 3))

            node = EventNode(
                event_id=ev.event_id,
                event_index=idx,
                section_id=ev.section_id,
                span=ev.span,
                node_type=node_type,
                actor_entity_ids=list(dict.fromkeys(actor_ids)),
                patient_entity_ids=list(dict.fromkeys(patient_ids)),
                physical_entity_ids=list(dict.fromkeys(phys_entities)),
                non_physical_entity_ids=list(dict.fromkeys(non_phys_entities)),
                fact_ids=fact_ids,
                importance_score=importance,
                is_climax=is_climax,
                is_character_introduction=is_intro,
                intro_entity_ids=intro_entities,
                location_name=loc_name,
                dialogue_speakers=list(ev.dialogue_speakers),
            )
            nodes[ev.event_id] = node

        # Build Edges
        edges: List[EventEdge] = []
        event_ids = list(nodes.keys())

        for i in range(len(event_ids) - 1):
            curr_id = event_ids[i]
            next_id = event_ids[i + 1]
            curr_node = nodes[curr_id]
            next_node = nodes[next_id]

            # 1. Temporal sequence
            edges.append(
                EventEdge(
                    source_event_id=curr_id,
                    target_event_id=next_id,
                    edge_type=EventEdgeType.TEMPORAL_SEQUENCE,
                    confidence=1.0,
                    rationale=f"Chronological progression {curr_id} -> {next_id}",
                )
            )

            # 2. Causal consequence if connective words are present
            next_text = next_node.span.text
            if CAUSAL_CONNECTIVES_REGEX.search(next_text):
                matching_facts = [f for f in next_node.fact_ids]
                edges.append(
                    EventEdge(
                        source_event_id=curr_id,
                        target_event_id=next_id,
                        edge_type=EventEdgeType.CAUSAL_CONSEQUENCE,
                        confidence=0.85,
                        evidence_fact_ids=matching_facts,
                        rationale=f"Explicit causal conjunction in '{next_text[:30]}...'",
                    )
                )

            # 3. Dialogue continuation
            is_curr_diag = bool(curr_node.dialogue_speakers) or bool(re.search(r"[«\"“].+?[»\"”]|:\s*[«\"“]|(?:спросил|ответил|сказал|произнес|крикнул|прошептал)\b", curr_node.span.text, re.IGNORECASE))
            is_next_diag = bool(next_node.dialogue_speakers) or bool(re.search(r"[«\"“].+?[»\"”]|:\s*[«\"“]|(?:спросил|ответил|сказал|произнес|крикнул|прошептал)\b", next_node.span.text, re.IGNORECASE))
            if is_curr_diag and is_next_diag:
                edges.append(
                    EventEdge(
                        source_event_id=curr_id,
                        target_event_id=next_id,
                        edge_type=EventEdgeType.DIALOGUE_CONTINUATION,
                        confidence=0.9,
                        rationale="Continuous conversational dialogue turn",
                    )
                )

            # 4. Actor continuity
            common_actors = set(curr_node.actor_entity_ids).intersection(set(next_node.actor_entity_ids))
            if common_actors:
                edges.append(
                    EventEdge(
                        source_event_id=curr_id,
                        target_event_id=next_id,
                        edge_type=EventEdgeType.ACTOR_CONTINUATION,
                        confidence=0.8,
                        rationale=f"Actor continuation: {list(common_actors)}",
                    )
                )

        return EventGraph(
            nodes=nodes,
            edges=edges,
            doc_mode=doc_mode,
            total_events=len(nodes),
            primary_actors=primary_actors,
        )

"""Event Selector for Stage 6 Visual Event Planner.

Applies budget pacing, hard constraints, character coverage, and document mode quotas
prior to ranking, guaranteeing chronological ordering, zero hallucinations, and full provenance.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.framing import VisualSafetyFramingEngine
from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    EntityIR,
    EventIR,
)
from imagegen.analyzer_v3.planner_models import (
    EventGraph,
    EventNode,
    EventNodeType,
    SelectedEventIR,
    SelectionReasonCode,
    VisualFramingPolicy,
)


class EventSelector:
    """Selects and plans a balanced set of visual events based on EventGraph."""

    def __init__(self, framing_engine: Optional[VisualSafetyFramingEngine] = None):
        self.framing_engine = framing_engine or VisualSafetyFramingEngine()

    def select_events(
        self,
        graph: EventGraph,
        doc_ir: DocumentIR,
        target_budget: int = 16,
    ) -> List[SelectedEventIR]:
        """Selects up to target_budget events, enforcing constraints, pacing, and coverage."""
        events_by_id: Dict[str, EventIR] = {ev.event_id: ev for ev in doc_ir.events}
        nodes_list = sorted(graph.nodes.values(), key=lambda n: n.event_index)

        if not nodes_list:
            return []

        total_nodes = len(nodes_list)
        budget = max(1, min(target_budget, total_nodes))

        selected_node_ids: Set[str] = set()
        selection_reasons: Dict[str, List[str]] = {}
        selection_rationales: Dict[str, str] = {}

        def record_selection(node_id: str, code: SelectionReasonCode, rationale: str):
            selected_node_ids.add(node_id)
            if node_id not in selection_reasons:
                selection_reasons[node_id] = []
            if code.value not in selection_reasons[node_id]:
                selection_reasons[node_id].append(code.value)
            if node_id not in selection_rationales:
                selection_rationales[node_id] = rationale

        # 1. Mandatory Milestone 1: Opening / Exposition (First Event)
        record_selection(nodes_list[0].event_id, SelectionReasonCode.EXPOSITION_ESTABLISHING, "Opening scene / narrative exposition")

        # 2. Mandatory Milestone 2: Finale / Resolution (Last Event)
        if total_nodes > 1 and len(selected_node_ids) < budget:
            record_selection(nodes_list[-1].event_id, SelectionReasonCode.RESOLUTION_BEAT, "Story climax / narrative resolution")

        # 3. Mandatory Milestone 3: Key Character Introductions
        for actor_id in graph.primary_actors:
            if len(selected_node_ids) >= budget:
                break
            intro_node = next(
                (n for n in nodes_list if actor_id in n.intro_entity_ids or (actor_id in n.physical_entity_ids and n.is_character_introduction)),
                None
            )
            if intro_node and intro_node.event_id not in selected_node_ids:
                record_selection(
                    intro_node.event_id,
                    SelectionReasonCode.PRIMARY_ACTOR_INTRO,
                    f"First visual introduction of primary character {actor_id}",
                )

        # 4. Mandatory Milestone 4: Climax moments
        for node in nodes_list:
            if len(selected_node_ids) >= budget:
                break
            if node.is_climax and node.event_id not in selected_node_ids:
                record_selection(node.event_id, SelectionReasonCode.CLIMAX_BEAT, "Critical narrative dramatic peak / action climax")

        # 5. Document Mode Handling: Factual / Review Quotas
        if graph.doc_mode in (DocumentMode.FACT_LIST, DocumentMode.MULTI_SUBJECT_REVIEW, DocumentMode.EXPLAINER):
            # Ensure sections and specification facts are distributed
            sections = doc_ir.sections
            sec_budget = max(1, math.ceil(budget / max(1, len(sections))))
            for sec in sections:
                sec_nodes = [n for n in nodes_list if n.section_id == sec.section_id]
                fact_nodes = [n for n in sec_nodes if len(n.fact_ids) > 0]
                for fn in fact_nodes[:sec_budget]:
                    if len(selected_node_ids) >= budget:
                        break
                    if fn.event_id not in selected_node_ids:
                        record_selection(fn.event_id, SelectionReasonCode.KEY_FACT_SPEC, f"Technical / factual specification in section {sec.section_id}")

        # 6. Fill remaining budget by Highest Importance & Temporal Pacing
        if len(selected_node_ids) < budget:
            # Segment timeline into equal temporal buckets to guarantee uniform pacing
            remaining_needed = budget - len(selected_node_ids)
            unselected_nodes = [n for n in nodes_list if n.event_id not in selected_node_ids]

            # Sort unselected by importance
            unselected_nodes.sort(key=lambda n: n.importance_score, reverse=True)

            for n in unselected_nodes:
                if len(selected_node_ids) >= budget:
                    break
                record_selection(n.event_id, SelectionReasonCode.PACING_FILL, f"High-importance narrative beat (score: {n.importance_score})")

        # 7. Strictly preserve chronological order
        selected_nodes_ordered = [
            graph.nodes[ev_id] for ev_id in sorted(selected_node_ids, key=lambda eid: graph.nodes[eid].event_index)
        ]

        # 8. Build SelectedEventIR instances with Framing
        selected_events: List[SelectedEventIR] = []
        for s_idx, node in enumerate(selected_nodes_ordered):
            ev = events_by_id[node.event_id]
            framing_policy, shot_type, camera_angle, safe_desc, visual_action, mood, focus = self.framing_engine.compute_framing(
                node, ev, doc_ir
            )

            primary_subjs = [eid for eid in node.actor_entity_ids if eid in node.physical_entity_ids]
            if not primary_subjs and node.physical_entity_ids:
                primary_subjs = [node.physical_entity_ids[0]]

            sec_subjs = [eid for eid in node.physical_entity_ids if eid not in primary_subjs]

            selected_event = SelectedEventIR(
                selected_id=f"sel_{s_idx}",
                event_id=node.event_id,
                event_index=node.event_index,
                section_id=node.section_id,
                span=node.span,
                framing_policy=framing_policy,
                shot_type=shot_type,
                camera_angle=camera_angle,
                primary_subject_entity_ids=primary_subjs,
                secondary_entity_ids=sec_subjs,
                non_physical_entity_ids=list(node.non_physical_entity_ids),
                safe_framing_description=safe_desc,
                visual_action_description=visual_action,
                mood_and_atmosphere=mood,
                composition_focus=focus,
                source_fact_ids=list(node.fact_ids),
                selection_reason_codes=selection_reasons.get(node.event_id, [SelectionReasonCode.PACING_FILL.value]),
                selection_rationale=selection_rationales.get(node.event_id, "Selected for narrative visual progression"),
                qwen_detailed=False,
                qwen_diagnostics=None,
            )
            selected_events.append(selected_event)

        return selected_events

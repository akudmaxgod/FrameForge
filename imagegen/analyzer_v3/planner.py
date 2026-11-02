"""Visual Event Planner coordinating graph building, event selection, safety framing, and detailing."""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from imagegen.analyzer_v3.detailer import QwenVisualDetailer, VisualPlannerLLMDetailer
from imagegen.analyzer_v3.event_graph import EventGraphBuilder
from imagegen.analyzer_v3.framing import VisualSafetyFramingEngine
from imagegen.analyzer_v3.models import DocumentIR, DocumentMode
from imagegen.analyzer_v3.planner_models import (
    EventGraph,
    SelectedEventIR,
    VisualEventPlan,
    VisualFramingPolicy,
)
from imagegen.analyzer_v3.selector import EventSelector


def complete_source_plan(doc_ir, story_text, plan):
    """Restore omitted events for a full-story export, retaining refined shots.

    A ranked shortlist is useful for a preview, but cannot stand in for a
    complete narration. This deterministic pass reuses the saved structural
    analysis and makes no model calls. It does not certify visual semantics.
    """
    from imagegen.source_contract import coverage, require_complete_coverage

    def scenes(shots):
        return [{'source_start':s.span.start, 'source_end':s.span.end,
                 'source_quote':s.span.text} for s in shots]

    if coverage(story_text, scenes(plan.selected_events))['ok']:
        return plan
    expanded = VisualEventPlanner().plan(doc_ir, story_text, len(doc_ir.events))
    previous = {s.event_id:s for s in plan.selected_events}
    expanded.selected_events = [previous.get(s.event_id, s) for s in expanded.selected_events]
    for index, shot in enumerate(expanded.selected_events):
        # Retained shortlist IDs would otherwise collide with inserted shots.
        from dataclasses import replace
        expanded.selected_events[index] = replace(shot, selected_id=f'sel_{index}')
    require_complete_coverage(story_text, scenes(expanded.selected_events))
    expanded.metadata.update(requested_target_budget=plan.target_budget,
        source_coverage_expanded=True, original_selected_count=len(plan.selected_events),
        coverage_method='all_source_events', semantic_completeness_measured=False)
    return expanded


class VisualEventPlanner:
    """Orchestrates Stage 6 Visual Event Planning over an immutable DocumentIR."""

    def __init__(
        self,
        graph_builder: Optional[EventGraphBuilder] = None,
        framing_engine: Optional[VisualSafetyFramingEngine] = None,
        selector: Optional[EventSelector] = None,
        detailer: Optional[VisualPlannerLLMDetailer] = None,
    ):
        self.graph_builder = graph_builder or EventGraphBuilder()
        self.framing_engine = framing_engine or VisualSafetyFramingEngine()
        self.selector = selector or EventSelector(self.framing_engine)
        self.qwen_detailer = QwenVisualDetailer(detailer)

    def plan(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        target_budget: int = 16,
    ) -> VisualEventPlan:
        """Constructs an immutable VisualEventPlan without mutating DocumentIR."""
        t0 = time.time()
        story_hash = hashlib.sha256(story_text.encode("utf-8")).hexdigest()
        doc_mode = doc_ir.mode if hasattr(doc_ir, "mode") and doc_ir.mode else DocumentMode.NARRATIVE

        # Step 1: Build Event Graph strictly from DocumentIR
        graph = self.graph_builder.build_graph(doc_ir)

        # Step 2: Deterministic event selection applying budget and coverage
        selected_events = self.selector.select_events(
            graph=graph,
            doc_ir=doc_ir,
            target_budget=target_budget,
        )

        # Step 3: Optional LLM refinement (fail-closed with deterministic fallback)
        selected_events = self.qwen_detailer.refine_plan(
            selected_events=selected_events,
            doc_ir=doc_ir,
            story_text=story_text,
        )

        # Step 4: Actor coverage metrics
        actor_coverage: Dict[str, int] = {}
        for actor_id in graph.primary_actors:
            count = sum(
                1 for s in selected_events
                if actor_id in s.primary_subject_entity_ids or actor_id in s.secondary_entity_ids
            )
            actor_coverage[actor_id] = count

        # Step 5: Validate VisualEventPlan invariants
        self._validate_plan_invariants(selected_events, doc_ir)

        elapsed = round(time.time() - t0, 4)
        plan_id = f"plan_{story_hash[:12]}_{int(time.time())}"

        metadata = {
            "planning_sec": elapsed,
            "target_budget": target_budget,
            "total_candidate_events": len(doc_ir.events),
            "selected_count": len(selected_events),
            "graph_nodes_count": len(graph.nodes),
            "graph_edges_count": len(graph.edges),
            "primary_actors_count": len(graph.primary_actors),
            "qwen_detailed_count": sum(1 for s in selected_events if s.qwen_detailed),
        }

        provenance = {
            "generator": "StoryAnalyzerV3.VisualEventPlanner",
            "stage": "Stage 6: Visual Event Planner",
            "schema_version": "3.0",
            "story_hash": story_hash,
        }

        return VisualEventPlan(
            plan_id=plan_id,
            story_hash=story_hash,
            doc_mode=doc_mode,
            target_budget=target_budget,
            selected_events=selected_events,
            event_graph=graph,
            total_candidate_events=len(doc_ir.events),
            selected_count=len(selected_events),
            actor_coverage=actor_coverage,
            metadata=metadata,
            provenance=provenance,
        )

    def _validate_plan_invariants(
        self,
        selected_events: List[SelectedEventIR],
        doc_ir: DocumentIR,
    ) -> None:
        """Enforces hard structural invariants on the selected visual event plan."""
        event_map = {ev.event_id: ev for ev in doc_ir.events}
        entity_map = {e.entity_id: e for e in doc_ir.entities}

        last_event_index = -1
        for idx, sel in enumerate(selected_events):
            # 1. Event existence
            if sel.event_id not in event_map:
                raise ValueError(f"Selected event '{sel.event_id}' does not exist in DocumentIR.events")

            # 2. Chronological non-decreasing order
            if sel.event_index < last_event_index:
                raise ValueError(
                    f"Selected events violate chronological ordering at index {idx}: "
                    f"event_index {sel.event_index} < previous {last_event_index}"
                )
            last_event_index = sel.event_index

            # 3. SourceSpan match
            ev = event_map[sel.event_id]
            if sel.span.start != ev.span.start or sel.span.end != ev.span.end:
                raise ValueError(f"Selected event span [{sel.span.start}:{sel.span.end}] does not match EventIR span")

            # 4. Physical actors validation (no phantom entities)
            for eid in sel.primary_subject_entity_ids + sel.secondary_entity_ids:
                if eid not in entity_map:
                    raise ValueError(f"Entity '{eid}' in selected event '{sel.selected_id}' does not exist in DocumentIR.entities")

            # 5. Non-physical entities must NOT be assigned PHYSICAL_ACTOR if no physical presence exists
            ev_presences = [p for p in ev.presences if p.physical_presence]
            if not ev_presences and sel.framing_policy == VisualFramingPolicy.PHYSICAL_ACTOR:
                raise ValueError(
                    f"Selected event '{sel.selected_id}' has no physical presence in event '{sel.event_id}', "
                    f"cannot assign PHYSICAL_ACTOR framing policy"
                )

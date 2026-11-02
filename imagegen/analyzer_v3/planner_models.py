"""Data models and data structures for Stage 6 Visual Event Planner."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.models import (
    DocumentMode,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    PresenceRole,
    SourceSpan,
    VisualPolicy,
)


class VisualShotType(str, Enum):
    ESTABLISHING_WIDE = "establishing_wide"
    MEDIUM_ACTION = "medium_action"
    CLOSEUP_FOCUS = "closeup_focus"
    PROP_DETAIL = "prop_detail"
    ENVIRONMENT_ATMOSPHERIC = "environment_atmospheric"
    OVER_SHOULDER = "over_shoulder"
    REACTION_SHOT = "reaction_shot"
    UNKNOWN = "unknown"


class CameraAngle(str, Enum):
    EYE_LEVEL = "eye_level"
    LOW_ANGLE = "low_angle"
    HIGH_ANGLE = "high_angle"
    DUTCH_ANGLE = "dutch_angle"
    TOP_DOWN = "top_down"
    UNKNOWN = "unknown"


class VisualFramingPolicy(str, Enum):
    PHYSICAL_ACTOR = "PHYSICAL_ACTOR"
    PROP_DETAIL = "PROP_DETAIL"
    DEPICTED_MEDIUM = "DEPICTED_MEDIUM"
    REACTION_OF_CONFIRMED_CHARACTER = "REACTION_OF_CONFIRMED_CHARACTER"
    EMPTY_LOCATION = "EMPTY_LOCATION"
    AUDIO_ONLY_INDICATOR = "AUDIO_ONLY_INDICATOR"
    SILHOUETTE_WITHOUT_IDENTITY = "SILHOUETTE_WITHOUT_IDENTITY"
    SAFE_NEUTRAL = "SAFE_NEUTRAL"


class EventNodeType(str, Enum):
    NARRATIVE_BEAT = "narrative_beat"
    DIALOGUE_EXCHANGE = "dialogue_exchange"
    FACT_STATEMENT = "fact_statement"
    SETTING_TRANSITION = "setting_transition"
    CLIMAX_MOMENT = "climax_moment"
    EXPOSITION = "exposition"
    UNKNOWN = "unknown"


class EventEdgeType(str, Enum):
    TEMPORAL_SEQUENCE = "temporal_sequence"
    CAUSAL_CONSEQUENCE = "causal_consequence"
    DIALOGUE_CONTINUATION = "dialogue_continuation"
    ACTOR_CONTINUATION = "actor_continuation"
    LOCATION_CONTINUATION = "location_continuation"
    UNKNOWN = "unknown"


class SelectionReasonCode(str, Enum):
    EXPOSITION_ESTABLISHING = "EXPOSITION_ESTABLISHING"
    PRIMARY_ACTOR_INTRO = "PRIMARY_ACTOR_INTRO"
    CLIMAX_BEAT = "CLIMAX_BEAT"
    RESOLUTION_BEAT = "RESOLUTION_BEAT"
    CAUSAL_TRANSITION = "CAUSAL_TRANSITION"
    LOCATION_SHIFT = "LOCATION_SHIFT"
    KEY_FACT_SPEC = "KEY_FACT_SPEC"
    PACING_FILL = "PACING_FILL"
    DIALOGUE_MOMENT = "DIALOGUE_MOMENT"
    ACTION_CONTINUITY = "ACTION_CONTINUITY"


@dataclass
class EventEdge:
    source_event_id: str
    target_event_id: str
    edge_type: EventEdgeType = EventEdgeType.TEMPORAL_SEQUENCE
    confidence: float = 1.0
    evidence_fact_ids: List[str] = field(default_factory=list)
    rationale: str = ""

    def __post_init__(self):
        if isinstance(self.edge_type, str):
            try:
                object.__setattr__(self, "edge_type", EventEdgeType(self.edge_type))
            except ValueError:
                object.__setattr__(self, "edge_type", EventEdgeType.UNKNOWN)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "target_event_id": self.target_event_id,
            "edge_type": self.edge_type.value if hasattr(self.edge_type, "value") else str(self.edge_type),
            "confidence": self.confidence,
            "evidence_fact_ids": list(self.evidence_fact_ids),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventEdge:
        raw_et = data.get("edge_type", "temporal_sequence")
        try:
            et = EventEdgeType(raw_et)
        except ValueError:
            et = EventEdgeType.UNKNOWN

        return cls(
            source_event_id=str(data["source_event_id"]),
            target_event_id=str(data["target_event_id"]),
            edge_type=et,
            confidence=float(data.get("confidence", 1.0)),
            evidence_fact_ids=list(data.get("evidence_fact_ids", [])),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class EventNode:
    event_id: str
    event_index: int
    section_id: str
    span: SourceSpan
    node_type: EventNodeType = EventNodeType.NARRATIVE_BEAT
    actor_entity_ids: List[str] = field(default_factory=list)
    patient_entity_ids: List[str] = field(default_factory=list)
    physical_entity_ids: List[str] = field(default_factory=list)
    non_physical_entity_ids: List[str] = field(default_factory=list)
    fact_ids: List[str] = field(default_factory=list)
    importance_score: float = 0.5
    is_climax: bool = False
    is_character_introduction: bool = False
    intro_entity_ids: List[str] = field(default_factory=list)
    location_name: Optional[str] = None
    dialogue_speakers: List[str] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.node_type, str):
            try:
                object.__setattr__(self, "node_type", EventNodeType(self.node_type))
            except ValueError:
                object.__setattr__(self, "node_type", EventNodeType.UNKNOWN)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_index": self.event_index,
            "section_id": self.section_id,
            "span": self.span.to_dict(),
            "node_type": self.node_type.value if hasattr(self.node_type, "value") else str(self.node_type),
            "actor_entity_ids": list(self.actor_entity_ids),
            "patient_entity_ids": list(self.patient_entity_ids),
            "physical_entity_ids": list(self.physical_entity_ids),
            "non_physical_entity_ids": list(self.non_physical_entity_ids),
            "fact_ids": list(self.fact_ids),
            "importance_score": self.importance_score,
            "is_climax": self.is_climax,
            "is_character_introduction": self.is_character_introduction,
            "intro_entity_ids": list(self.intro_entity_ids),
            "location_name": self.location_name,
            "dialogue_speakers": list(self.dialogue_speakers),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventNode:
        raw_nt = data.get("node_type", "narrative_beat")
        try:
            nt = EventNodeType(raw_nt)
        except ValueError:
            nt = EventNodeType.UNKNOWN

        return cls(
            event_id=str(data["event_id"]),
            event_index=int(data.get("event_index", 0)),
            section_id=str(data.get("section_id", "sec_0")),
            span=SourceSpan.from_dict(data["span"]),
            node_type=nt,
            actor_entity_ids=list(data.get("actor_entity_ids", [])),
            patient_entity_ids=list(data.get("patient_entity_ids", [])),
            physical_entity_ids=list(data.get("physical_entity_ids", [])),
            non_physical_entity_ids=list(data.get("non_physical_entity_ids", [])),
            fact_ids=list(data.get("fact_ids", [])),
            importance_score=float(data.get("importance_score", 0.5)),
            is_climax=bool(data.get("is_climax", False)),
            is_character_introduction=bool(data.get("is_character_introduction", False)),
            intro_entity_ids=list(data.get("intro_entity_ids", [])),
            location_name=data.get("location_name"),
            dialogue_speakers=list(data.get("dialogue_speakers", [])),
        )


@dataclass
class EventGraph:
    nodes: Dict[str, EventNode] = field(default_factory=dict)
    edges: List[EventEdge] = field(default_factory=list)
    doc_mode: DocumentMode = DocumentMode.NARRATIVE
    total_events: int = 0
    primary_actors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "doc_mode": self.doc_mode.value if hasattr(self.doc_mode, "value") else str(self.doc_mode),
            "total_events": self.total_events,
            "primary_actors": list(self.primary_actors),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventGraph:
        raw_mode = data.get("doc_mode", "narrative")
        try:
            dmode = DocumentMode(raw_mode)
        except ValueError:
            dmode = DocumentMode.NARRATIVE

        return cls(
            nodes={k: EventNode.from_dict(v) for k, v in data.get("nodes", {}).items()},
            edges=[EventEdge.from_dict(e) for e in data.get("edges", [])],
            doc_mode=dmode,
            total_events=int(data.get("total_events", len(data.get("nodes", {})))),
            primary_actors=list(data.get("primary_actors", [])),
        )


@dataclass
class SelectedEventIR:
    """Represents a planned visual shot selected from an immutable EventIR."""
    selected_id: str
    event_id: str
    event_index: int
    section_id: str
    span: SourceSpan
    framing_policy: VisualFramingPolicy = VisualFramingPolicy.PHYSICAL_ACTOR
    shot_type: VisualShotType = VisualShotType.MEDIUM_ACTION
    camera_angle: CameraAngle = CameraAngle.EYE_LEVEL
    primary_subject_entity_ids: List[str] = field(default_factory=list)
    secondary_entity_ids: List[str] = field(default_factory=list)
    non_physical_entity_ids: List[str] = field(default_factory=list)
    safe_framing_description: str = ""
    visual_action_description: str = ""
    mood_and_atmosphere: str = ""
    composition_focus: str = ""
    source_fact_ids: List[str] = field(default_factory=list)
    selection_reason_codes: List[str] = field(default_factory=list)
    selection_rationale: str = ""
    qwen_detailed: bool = False
    qwen_diagnostics: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.framing_policy, str):
            try:
                object.__setattr__(self, "framing_policy", VisualFramingPolicy(self.framing_policy))
            except ValueError:
                object.__setattr__(self, "framing_policy", VisualFramingPolicy.SAFE_NEUTRAL)
        if isinstance(self.shot_type, str):
            try:
                object.__setattr__(self, "shot_type", VisualShotType(self.shot_type))
            except ValueError:
                object.__setattr__(self, "shot_type", VisualShotType.MEDIUM_ACTION)
        if isinstance(self.camera_angle, str):
            try:
                object.__setattr__(self, "camera_angle", CameraAngle(self.camera_angle))
            except ValueError:
                object.__setattr__(self, "camera_angle", CameraAngle.EYE_LEVEL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_id": self.selected_id,
            "event_id": self.event_id,
            "event_index": self.event_index,
            "section_id": self.section_id,
            "span": self.span.to_dict(),
            "framing_policy": self.framing_policy.value if hasattr(self.framing_policy, "value") else str(self.framing_policy),
            "shot_type": self.shot_type.value if hasattr(self.shot_type, "value") else str(self.shot_type),
            "camera_angle": self.camera_angle.value if hasattr(self.camera_angle, "value") else str(self.camera_angle),
            "primary_subject_entity_ids": list(self.primary_subject_entity_ids),
            "secondary_entity_ids": list(self.secondary_entity_ids),
            "non_physical_entity_ids": list(self.non_physical_entity_ids),
            "safe_framing_description": self.safe_framing_description,
            "visual_action_description": self.visual_action_description,
            "mood_and_atmosphere": self.mood_and_atmosphere,
            "composition_focus": self.composition_focus,
            "source_fact_ids": list(self.source_fact_ids),
            "selection_reason_codes": list(self.selection_reason_codes),
            "selection_rationale": self.selection_rationale,
            "qwen_detailed": self.qwen_detailed,
            "qwen_diagnostics": self.qwen_diagnostics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SelectedEventIR:
        raw_fp = data.get("framing_policy", "PHYSICAL_ACTOR")
        try:
            fp = VisualFramingPolicy(raw_fp)
        except ValueError:
            fp = VisualFramingPolicy.SAFE_NEUTRAL

        raw_st = data.get("shot_type", "medium_action")
        try:
            st = VisualShotType(raw_st)
        except ValueError:
            st = VisualShotType.MEDIUM_ACTION

        raw_ca = data.get("camera_angle", "eye_level")
        try:
            ca = CameraAngle(raw_ca)
        except ValueError:
            ca = CameraAngle.EYE_LEVEL

        return cls(
            selected_id=str(data["selected_id"]),
            event_id=str(data["event_id"]),
            event_index=int(data.get("event_index", 0)),
            section_id=str(data.get("section_id", "sec_0")),
            span=SourceSpan.from_dict(data["span"]),
            framing_policy=fp,
            shot_type=st,
            camera_angle=ca,
            primary_subject_entity_ids=list(data.get("primary_subject_entity_ids", [])),
            secondary_entity_ids=list(data.get("secondary_entity_ids", [])),
            non_physical_entity_ids=list(data.get("non_physical_entity_ids", [])),
            safe_framing_description=str(data.get("safe_framing_description", "")),
            visual_action_description=str(data.get("visual_action_description", "")),
            mood_and_atmosphere=str(data.get("mood_and_atmosphere", "")),
            composition_focus=str(data.get("composition_focus", "")),
            source_fact_ids=list(data.get("source_fact_ids", [])),
            selection_reason_codes=list(data.get("selection_reason_codes", [])),
            selection_rationale=str(data.get("selection_rationale", "")),
            qwen_detailed=bool(data.get("qwen_detailed", False)),
            qwen_diagnostics=data.get("qwen_diagnostics"),
        )


@dataclass
class VisualEventPlan:
    """Immutable Visual Event Plan for storyboard generation."""
    plan_id: str
    story_hash: str
    doc_mode: DocumentMode
    target_budget: int
    selected_events: List[SelectedEventIR] = field(default_factory=list)
    event_graph: Optional[EventGraph] = None
    total_candidate_events: int = 0
    selected_count: int = 0
    actor_coverage: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "story_hash": self.story_hash,
            "doc_mode": self.doc_mode.value if hasattr(self.doc_mode, "value") else str(self.doc_mode),
            "target_budget": self.target_budget,
            "selected_events": [e.to_dict() for e in self.selected_events],
            "event_graph": self.event_graph.to_dict() if self.event_graph else None,
            "total_candidate_events": self.total_candidate_events,
            "selected_count": self.selected_count,
            "actor_coverage": dict(self.actor_coverage),
            "metadata": dict(self.metadata),
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VisualEventPlan:
        raw_mode = data.get("doc_mode", "narrative")
        try:
            dmode = DocumentMode(raw_mode)
        except ValueError:
            dmode = DocumentMode.NARRATIVE

        eg = EventGraph.from_dict(data["event_graph"]) if data.get("event_graph") else None

        return cls(
            plan_id=str(data["plan_id"]),
            story_hash=str(data.get("story_hash", "")),
            doc_mode=dmode,
            target_budget=int(data.get("target_budget", len(data.get("selected_events", [])))),
            selected_events=[SelectedEventIR.from_dict(e) for e in data.get("selected_events", [])],
            event_graph=eg,
            total_candidate_events=int(data.get("total_candidate_events", 0)),
            selected_count=int(data.get("selected_count", len(data.get("selected_events", [])))),
            actor_coverage=dict(data.get("actor_coverage", {})),
            metadata=dict(data.get("metadata", {})),
            provenance=dict(data.get("provenance", {})),
        )

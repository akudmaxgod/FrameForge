"""Data models and intermediate representations for Story Analyzer V3."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class DocumentMode(str, Enum):
    NARRATIVE = "narrative"
    FACT_LIST = "fact_list"
    MULTI_SUBJECT_REVIEW = "multi_subject_review"
    EXPLAINER = "explainer"
    INSTRUCTIONAL = "instructional"
    MIXED = "mixed"


class EntityModalityV3(str, Enum):
    PHYSICAL = "physical"
    VOICE_ONLY = "voice_only"
    PERCEIVED_VOICE = "perceived_voice"
    DEPICTED = "depicted"
    MENTIONED = "mentioned"
    DECEASED_MENTION = "deceased_mention"
    OBJECT = "object"
    LOCATION = "location"
    ORGANIZATION = "organization"
    UNKNOWN = "unknown"


class CoreferenceStatus(str, Enum):
    RESOLVED_LOCAL = "resolved_local"
    RESOLVED_CLOUD = "resolved_cloud"
    GROUP_RESOLVED = "group_resolved"
    UNRESOLVED_SAFE = "unresolved_safe"
    UNRESOLVED_BLOCKING = "unresolved_blocking"
    NOT_APPLICABLE = "not_applicable"


class AnalysisOutcome(str, Enum):
    SUCCESS = "success"
    SUCCESS_WITH_WARNINGS = "success_with_warnings"
    PARTIAL_SUCCESS = "partial_success"
    BLOCKED = "blocked"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"


class SyntacticRole(str, Enum):
    SUBJECT = "subject"
    OBJECT = "object"
    OBLIQUE = "oblique"
    ATTRIBUTE = "attribute"
    LOCATION = "location"
    UNKNOWN = "unknown"


class EntityType(str, Enum):
    PERSON = "person"
    VEHICLE = "vehicle"
    OBJECT = "object"
    ANIMAL = "animal"
    CREATURE = "creature"
    LOCATION = "location"
    ORGANIZATION = "organization"
    CONCEPT = "concept"
    UNKNOWN = "unknown"


class PresenceRole(str, Enum):
    ACTOR = "actor"
    MENTIONED = "mentioned"
    SPEAKER = "speaker"
    OBSERVER = "observer"
    SUBJECT = "subject"
    OBJECT = "object"
    TARGET = "target"


class VisualPolicy(str, Enum):
    REFERENT_CONFIRMED = "REFERENT_CONFIRMED"
    MENTION_ONLY = "MENTION_ONLY"
    SAFE_NEUTRAL = "SAFE_NEUTRAL"
    VOICE_ONLY = "VOICE_ONLY"
    DEPICTED_ONLY = "DEPICTED_ONLY"
    IDENTITY_REQUIRED_UNRESOLVED = "IDENTITY_REQUIRED_UNRESOLVED"


class CoreferenceReasonCode(str, Enum):
    SAME_CLAUSE_ANTECEDENT = "SAME_CLAUSE_ANTECEDENT"
    REFLEXIVE_LOCAL_SUBJECT = "REFLEXIVE_LOCAL_SUBJECT"
    DISQUALIFIED_LOCAL_SUBJECT = "DISQUALIFIED_LOCAL_SUBJECT"
    INTERLOCUTOR_COMPLEMENT = "INTERLOCUTOR_COMPLEMENT"
    REPORTING_SPEAKER = "REPORTING_SPEAKER"
    REMOTE_DEMONSTRATIVE = "REMOTE_DEMONSTRATIVE"
    QUESTION_TOPIC = "QUESTION_TOPIC"
    ANSWER_CONTINUATION = "ANSWER_CONTINUATION"
    SUBSEQUENT_ACTION = "SUBSEQUENT_ACTION"
    PREVIOUS_EVENT_SUBJECT = "PREVIOUS_EVENT_SUBJECT"
    EMBEDDED_SUBJECT = "EMBEDDED_SUBJECT"
    SYNTACTIC_PARALLELISM = "SYNTACTIC_PARALLELISM"
    ACTION_CHAIN = "ACTION_CHAIN"
    DIALOGUE_INTERLOCUTOR = "DIALOGUE_INTERLOCUTOR"
    LOCAL_RECENCY = "LOCAL_RECENCY"
    CALL_RECIPIENT_CONTINUATION = "CALL_RECIPIENT_CONTINUATION"
    COORDINATED_GROUP_ANTECEDENT = "COORDINATED_GROUP_ANTECEDENT"
    GLOBAL_PROTAGONIST = "GLOBAL_PROTAGONIST"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    SUBJECT_OBJECT_AMBIGUITY = "SUBJECT_OBJECT_AMBIGUITY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INANIMATE_OBJECT = "INANIMATE_OBJECT"
    INANIMATE_OBJECT_SAFE = "INANIMATE_OBJECT_SAFE"
    NO_CANDIDATES = "NO_CANDIDATES"


@dataclass(frozen=True)
class SourceSpan:
    """Exact character offsets in the original unmodified story text."""
    start: int
    end: int
    text: str

    def __post_init__(self):
        if self.start < 0:
            raise ValueError(f"SourceSpan start ({self.start}) must be non-negative.")
        if self.end < self.start:
            raise ValueError(f"SourceSpan end ({self.end}) cannot be less than start ({self.start}).")

    def validate_against(self, full_text: str) -> bool:
        """Validates that span text strictly matches slice of full_text."""
        if self.start < 0 or self.end > len(full_text):
            return False
        return full_text[self.start:self.end] == self.text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SourceSpan:
        return cls(
            start=int(data.get("start", 0)),
            end=int(data.get("end", 0)),
            text=str(data.get("text", "")),
        )


@dataclass
class DocumentModeDecision:
    mode: DocumentMode
    confidence: float
    evidence: List[SourceSpan] = field(default_factory=list)
    section_modes: Dict[str, DocumentMode] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "confidence": self.confidence,
            "evidence": [s.to_dict() for s in self.evidence],
            "section_modes": {k: v.value for k, v in self.section_modes.items()},
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DocumentModeDecision:
        return cls(
            mode=DocumentMode(data.get("mode", "narrative")),
            confidence=float(data.get("confidence", 1.0)),
            evidence=[SourceSpan.from_dict(s) for s in data.get("evidence", [])],
            section_modes={k: DocumentMode(v) for k, v in data.get("section_modes", {}).items()},
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class MentionIR:
    mention_id: str
    text: str
    normalized_text: str
    span: SourceSpan
    mention_type: str = "proper_noun"  # "proper_noun", "common_noun", "pronoun", "demonstrative", "ellipsis"
    grammatical_gender: Optional[str] = None  # "male", "female", "neuter", None
    grammatical_number: Optional[str] = None  # "singular", "plural", None
    section_id: str = "sec_0"
    event_id: Optional[str] = None
    syntactic_role: SyntacticRole = SyntacticRole.SUBJECT
    confidence: float = 1.0

    def __post_init__(self):
        if isinstance(self.syntactic_role, str):
            try:
                object.__setattr__(self, "syntactic_role", SyntacticRole(self.syntactic_role))
            except ValueError:
                object.__setattr__(self, "syntactic_role", SyntacticRole.UNKNOWN)

    def to_dict(self) -> Dict[str, Any]:
        role_str = self.syntactic_role.value if hasattr(self.syntactic_role, "value") else str(self.syntactic_role)
        return {
            "mention_id": self.mention_id,
            "text": self.text,
            "normalized_text": self.normalized_text,
            "span": self.span.to_dict(),
            "mention_type": self.mention_type,
            "grammatical_gender": self.grammatical_gender,
            "grammatical_number": self.grammatical_number,
            "section_id": self.section_id,
            "event_id": self.event_id,
            "syntactic_role": role_str,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MentionIR:
        raw_role = data.get("syntactic_role", "subject")
        try:
            syn_role = SyntacticRole(raw_role)
        except ValueError:
            syn_role = SyntacticRole.UNKNOWN

        return cls(
            mention_id=str(data["mention_id"]),
            text=str(data["text"]),
            normalized_text=str(data.get("normalized_text", data["text"])),
            span=SourceSpan.from_dict(data["span"]),
            mention_type=str(data.get("mention_type", "proper_noun")),
            grammatical_gender=data.get("grammatical_gender"),
            grammatical_number=data.get("grammatical_number"),
            section_id=str(data.get("section_id", "sec_0")),
            event_id=data.get("event_id"),
            syntactic_role=syn_role,
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class EntityIR:
    entity_id: str
    canonical_name: str
    entity_type: EntityType = EntityType.PERSON
    gender: Optional[str] = None  # "male", "female", "neuter", None
    aliases: List[str] = field(default_factory=list)
    first_mention_start: int = 0
    mention_ids: List[str] = field(default_factory=list)
    modality: EntityModalityV3 = EntityModalityV3.PHYSICAL
    confidence: float = 1.0
    attributes: List[str] = field(default_factory=list)
    is_recurring: bool = False
    evidence_spans: List[SourceSpan] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.entity_type, str):
            try:
                object.__setattr__(self, "entity_type", EntityType(self.entity_type))
            except ValueError:
                object.__setattr__(self, "entity_type", EntityType.UNKNOWN)
        if isinstance(self.modality, str):
            try:
                object.__setattr__(self, "modality", EntityModalityV3(self.modality))
            except ValueError:
                object.__setattr__(self, "modality", EntityModalityV3.UNKNOWN)

    def to_dict(self) -> Dict[str, Any]:
        type_str = self.entity_type.value if hasattr(self.entity_type, "value") else str(self.entity_type)
        mod_str = self.modality.value if hasattr(self.modality, "value") else str(self.modality)
        return {
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "entity_type": type_str,
            "gender": self.gender,
            "aliases": self.aliases,
            "first_mention_start": self.first_mention_start,
            "mention_ids": self.mention_ids,
            "modality": mod_str,
            "confidence": self.confidence,
            "attributes": self.attributes,
            "is_recurring": self.is_recurring,
            "evidence_spans": [s.to_dict() for s in self.evidence_spans],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EntityIR:
        raw_type = data.get("entity_type", "person")
        try:
            ent_type = EntityType(raw_type)
        except ValueError:
            ent_type = EntityType.UNKNOWN

        raw_mod = data.get("modality", "physical")
        try:
            mod = EntityModalityV3(raw_mod)
        except ValueError:
            mod = EntityModalityV3.UNKNOWN

        return cls(
            entity_id=str(data["entity_id"]),
            canonical_name=str(data["canonical_name"]),
            entity_type=ent_type,
            gender=data.get("gender"),
            aliases=list(data.get("aliases", [])),
            first_mention_start=int(data.get("first_mention_start", 0)),
            mention_ids=list(data.get("mention_ids", [])),
            modality=mod,
            confidence=float(data.get("confidence", 1.0)),
            attributes=list(data.get("attributes", [])),
            is_recurring=bool(data.get("is_recurring", False)),
            evidence_spans=[SourceSpan.from_dict(s) for s in data.get("evidence_spans", [])],
        )


@dataclass
class EventEntityPresenceIR:
    """Represents the presence, modality, and role of an entity in a specific event."""
    presence_id: str
    event_id: str
    entity_id: str
    mention_id: str
    modality: EntityModalityV3 = EntityModalityV3.PHYSICAL
    physical_presence: bool = False
    role: PresenceRole = PresenceRole.MENTIONED
    evidence_spans: List[SourceSpan] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.modality, str):
            try:
                object.__setattr__(self, "modality", EntityModalityV3(self.modality))
            except ValueError:
                object.__setattr__(self, "modality", EntityModalityV3.UNKNOWN)
        if isinstance(self.role, str):
            try:
                object.__setattr__(self, "role", PresenceRole(self.role))
            except ValueError:
                object.__setattr__(self, "role", PresenceRole.MENTIONED)

    def to_dict(self) -> Dict[str, Any]:
        mod_str = self.modality.value if hasattr(self.modality, "value") else str(self.modality)
        role_str = self.role.value if hasattr(self.role, "value") else str(self.role)
        return {
            "presence_id": self.presence_id,
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "mention_id": self.mention_id,
            "modality": mod_str,
            "physical_presence": self.physical_presence,
            "role": role_str,
            "evidence_spans": [s.to_dict() for s in self.evidence_spans],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventEntityPresenceIR:
        raw_mod = data.get("modality", "physical")
        try:
            mod = EntityModalityV3(raw_mod)
        except ValueError:
            mod = EntityModalityV3.UNKNOWN

        raw_role = data.get("role", "mentioned")
        try:
            role = PresenceRole(raw_role)
        except ValueError:
            role = PresenceRole.MENTIONED

        return cls(
            presence_id=str(data["presence_id"]),
            event_id=str(data["event_id"]),
            entity_id=str(data["entity_id"]),
            mention_id=str(data["mention_id"]),
            modality=mod,
            physical_presence=bool(data.get("physical_presence", False)),
            role=role,
            evidence_spans=[SourceSpan.from_dict(s) for s in data.get("evidence_spans", [])],
        )


@dataclass
class FactIR:
    fact_id: str
    subject_id: Optional[str]
    predicate: str
    object_value: Any
    source_span: SourceSpan
    confidence: float = 1.0
    section_id: str = "sec_0"
    event_id: Optional[str] = None
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "source_span": self.source_span.to_dict(),
            "confidence": self.confidence,
            "section_id": self.section_id,
            "event_id": self.event_id,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FactIR:
        return cls(
            fact_id=str(data["fact_id"]),
            subject_id=data.get("subject_id"),
            predicate=str(data.get("predicate", "")),
            object_value=data.get("object_value"),
            source_span=SourceSpan.from_dict(data["source_span"]),
            confidence=float(data.get("confidence", 1.0)),
            section_id=str(data.get("section_id", "sec_0")),
            event_id=data.get("event_id"),
            diagnostics=list(data.get("diagnostics", [])),
        )


@dataclass
class EventIR:
    event_id: str
    section_id: str
    span: SourceSpan
    event_type: str = "action"  # "action", "dialogue", "state", "step", "fact", "transition", "comparison"
    actor_ids: List[str] = field(default_factory=list)
    mentioned_entity_ids: List[str] = field(default_factory=list)
    subject_entity_ids: List[str] = field(default_factory=list)
    comparison_target_ids: List[str] = field(default_factory=list)
    location_id: Optional[str] = None
    time_anchor: Optional[str] = None
    facts: List[FactIR] = field(default_factory=list)
    presences: List[EventEntityPresenceIR] = field(default_factory=list)
    visualizable: bool = True
    importance_score: float = 0.5
    dialogue_speakers: List[str] = field(default_factory=list)
    props: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "section_id": self.section_id,
            "span": self.span.to_dict(),
            "event_type": self.event_type,
            "actor_ids": self.actor_ids,
            "mentioned_entity_ids": self.mentioned_entity_ids,
            "subject_entity_ids": self.subject_entity_ids,
            "comparison_target_ids": self.comparison_target_ids,
            "location_id": self.location_id,
            "time_anchor": self.time_anchor,
            "facts": [f.to_dict() for f in self.facts],
            "presences": [p.to_dict() for p in self.presences],
            "visualizable": self.visualizable,
            "importance_score": self.importance_score,
            "dialogue_speakers": self.dialogue_speakers,
            "props": self.props,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventIR:
        return cls(
            event_id=str(data["event_id"]),
            section_id=str(data.get("section_id", "sec_0")),
            span=SourceSpan.from_dict(data["span"]),
            event_type=str(data.get("event_type", "action")),
            actor_ids=list(data.get("actor_ids", [])),
            mentioned_entity_ids=list(data.get("mentioned_entity_ids", [])),
            subject_entity_ids=list(data.get("subject_entity_ids", [])),
            comparison_target_ids=list(data.get("comparison_target_ids", [])),
            location_id=data.get("location_id"),
            time_anchor=data.get("time_anchor"),
            facts=[FactIR.from_dict(f) for f in data.get("facts", [])],
            presences=[EventEntityPresenceIR.from_dict(p) for p in data.get("presences", [])],
            visualizable=bool(data.get("visualizable", True)),
            importance_score=float(data.get("importance_score", 0.5)),
            dialogue_speakers=list(data.get("dialogue_speakers", [])),
            props=list(data.get("props", [])),
        )


@dataclass
class SectionIR:
    section_id: str
    title: Optional[str]
    span: SourceSpan
    mode: DocumentMode = DocumentMode.NARRATIVE
    order_index: int = 0
    event_ids: List[str] = field(default_factory=list)
    subject_entity_id: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.mode, str):
            try:
                object.__setattr__(self, "mode", DocumentMode(self.mode))
            except ValueError:
                object.__setattr__(self, "mode", DocumentMode.NARRATIVE)

    def to_dict(self) -> Dict[str, Any]:
        mode_str = self.mode.value if hasattr(self.mode, "value") else str(self.mode)
        return {
            "section_id": self.section_id,
            "title": self.title,
            "span": self.span.to_dict(),
            "mode": mode_str,
            "order_index": self.order_index,
            "event_ids": self.event_ids,
            "subject_entity_id": self.subject_entity_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SectionIR:
        raw_mode = data.get("mode", "narrative")
        try:
            mode = DocumentMode(raw_mode)
        except ValueError:
            mode = DocumentMode.NARRATIVE

        return cls(
            section_id=str(data["section_id"]),
            title=data.get("title"),
            span=SourceSpan.from_dict(data["span"]),
            mode=mode,
            order_index=int(data.get("order_index", 0)),
            event_ids=list(data.get("event_ids", [])),
            subject_entity_id=data.get("subject_entity_id"),
        )


@dataclass
class CoreferenceDecision:
    mention_id: str
    resolved_entity_ids: List[str] = field(default_factory=list)
    status: CoreferenceStatus = CoreferenceStatus.RESOLVED_LOCAL
    confidence: float = 1.0
    margin: float = 1.0
    evidence_spans: List[SourceSpan] = field(default_factory=list)
    rejected_candidates: List[Dict[str, Any]] = field(default_factory=list)
    candidate_entity_ids: List[str] = field(default_factory=list)
    reason: str = ""
    reason_codes: List[str] = field(default_factory=list)
    visual_policy: VisualPolicy = VisualPolicy.REFERENT_CONFIRMED
    event_id: Optional[str] = None
    event_index: int = 0

    def __post_init__(self):
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", CoreferenceStatus(self.status))
            except ValueError:
                object.__setattr__(self, "status", CoreferenceStatus.UNRESOLVED_SAFE)
        if isinstance(self.visual_policy, str):
            try:
                object.__setattr__(self, "visual_policy", VisualPolicy(self.visual_policy))
            except ValueError:
                object.__setattr__(self, "visual_policy", VisualPolicy.SAFE_NEUTRAL)

    def to_dict(self) -> Dict[str, Any]:
        status_str = self.status.value if hasattr(self.status, "value") else str(self.status)
        vp_str = self.visual_policy.value if hasattr(self.visual_policy, "value") else str(self.visual_policy)
        return {
            "mention_id": self.mention_id,
            "resolved_entity_ids": self.resolved_entity_ids,
            "status": status_str,
            "confidence": self.confidence,
            "margin": self.margin,
            "evidence_spans": [s.to_dict() for s in self.evidence_spans],
            "rejected_candidates": self.rejected_candidates,
            "candidate_entity_ids": self.candidate_entity_ids,
            "reason": self.reason,
            "reason_codes": self.reason_codes,
            "visual_policy": vp_str,
            "event_id": self.event_id,
            "event_index": self.event_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CoreferenceDecision:
        raw_status = data.get("status", "resolved_local")
        try:
            status = CoreferenceStatus(raw_status)
        except ValueError:
            status = CoreferenceStatus.UNRESOLVED_SAFE

        raw_vp = data.get("visual_policy", "REFERENT_CONFIRMED")
        try:
            vp = VisualPolicy(raw_vp)
        except ValueError:
            vp = VisualPolicy.SAFE_NEUTRAL

        return cls(
            mention_id=str(data["mention_id"]),
            resolved_entity_ids=list(data.get("resolved_entity_ids", [])),
            status=status,
            confidence=float(data.get("confidence", 1.0)),
            margin=float(data.get("margin", 1.0)),
            evidence_spans=[SourceSpan.from_dict(s) for s in data.get("evidence_spans", [])],
            rejected_candidates=list(data.get("rejected_candidates", [])),
            candidate_entity_ids=list(data.get("candidate_entity_ids", [])),
            reason=str(data.get("reason", "")),
            reason_codes=list(data.get("reason_codes", [])),
            visual_policy=vp,
            event_id=data.get("event_id"),
            event_index=int(data.get("event_index", 0)),
        )


@dataclass
class VisualBeat:
    beat_id: str
    source_event_ids: List[str]
    section_id: str
    beat_type: str = "focal_action"
    importance: float = 0.5
    required_entities: List[str] = field(default_factory=list)
    forbidden_entities: List[str] = field(default_factory=list)
    location_id: Optional[str] = None
    source_quote: str = ""
    source_spans: List[SourceSpan] = field(default_factory=list)
    recommended_framing: str = "medium_shot"
    props: List[str] = field(default_factory=list)
    state_tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "source_event_ids": self.source_event_ids,
            "section_id": self.section_id,
            "beat_type": self.beat_type,
            "importance": self.importance,
            "required_entities": self.required_entities,
            "forbidden_entities": self.forbidden_entities,
            "location_id": self.location_id,
            "source_quote": self.source_quote,
            "source_spans": [s.to_dict() for s in self.source_spans],
            "recommended_framing": self.recommended_framing,
            "props": self.props,
            "state_tags": self.state_tags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VisualBeat:
        return cls(
            beat_id=str(data["beat_id"]),
            source_event_ids=list(data.get("source_event_ids", [])),
            section_id=str(data.get("section_id", "sec_0")),
            beat_type=str(data.get("beat_type", "focal_action")),
            importance=float(data.get("importance", 0.5)),
            required_entities=list(data.get("required_entities", [])),
            forbidden_entities=list(data.get("forbidden_entities", [])),
            location_id=data.get("location_id"),
            source_quote=str(data.get("source_quote", "")),
            source_spans=[SourceSpan.from_dict(s) for s in data.get("source_spans", [])],
            recommended_framing=str(data.get("recommended_framing", "medium_shot")),
            props=list(data.get("props", [])),
            state_tags=list(data.get("state_tags", [])),
        )


class DocumentIR:
    """Unified Intermediate Representation for Story Analyzer V3.
    
    EventIR.presences and EventIR.facts are the authoritative single sources of truth.
    DocumentIR.facts and DocumentIR.event_entity_presences provide synchronized aggregate views.
    """

    def __init__(
        self,
        story_hash: str,
        mode: DocumentMode = DocumentMode.NARRATIVE,
        sections: Optional[List[SectionIR]] = None,
        events: Optional[List[EventIR]] = None,
        mentions: Optional[List[MentionIR]] = None,
        entities: Optional[List[EntityIR]] = None,
        facts: Optional[List[FactIR]] = None,
        event_entity_presences: Optional[List[EventEntityPresenceIR]] = None,
        coreferences: Optional[List[CoreferenceDecision]] = None,
        visual_beats: Optional[List[VisualBeat]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.story_hash = story_hash
        self.mode = DocumentMode(mode) if isinstance(mode, str) else mode
        self.sections = sections if sections is not None else []
        self.events = events if events is not None else []
        self.mentions = mentions if mentions is not None else []
        self.entities = entities if entities is not None else []
        self.coreferences = coreferences if coreferences is not None else []
        self.visual_beats = visual_beats if visual_beats is not None else []
        self.metadata = metadata if metadata is not None else {}

        # If legacy facts / presences are provided at construction time, distribute into events
        if facts:
            self.facts = facts
        if event_entity_presences:
            self.event_entity_presences = event_entity_presences

    @property
    def facts(self) -> List[FactIR]:
        """Dynamically computed aggregate of facts from all events (single source of truth)."""
        return [f for ev in self.events for f in ev.facts]

    @facts.setter
    def facts(self, value: List[FactIR]) -> None:
        """Migration setter: distributes facts into their respective EventIR instances."""
        event_map = {ev.event_id: ev for ev in self.events}
        for ev in self.events:
            ev.facts.clear()
        if value:
            for f in value:
                if f.event_id and f.event_id in event_map:
                    event_map[f.event_id].facts.append(f)

    @property
    def event_entity_presences(self) -> List[EventEntityPresenceIR]:
        """Dynamically computed aggregate of presences from all events (single source of truth)."""
        return [p for ev in self.events for p in ev.presences]

    @event_entity_presences.setter
    def event_entity_presences(self, value: List[EventEntityPresenceIR]) -> None:
        """Migration setter: distributes presences into their respective EventIR instances."""
        event_map = {ev.event_id: ev for ev in self.events}
        for ev in self.events:
            ev.presences.clear()
        if value:
            for p in value:
                if p.event_id and p.event_id in event_map:
                    event_map[p.event_id].presences.append(p)


    def validate_invariants(self, story_text: str) -> List[str]:
        """Strictly validates all DocumentIR invariants against the raw story text."""
        from imagegen.analyzer_v3.validator import InvariantValidator
        return InvariantValidator(self, story_text).validate()

    def audit_text_coverage(self, story_text: str) -> Dict[str, Any]:
        """Audits character-level coverage across events, structural headers, and gaps."""
        from imagegen.analyzer_v3.validator import TextCoverageAuditor
        return TextCoverageAuditor.audit(self, story_text)

    def to_dict(self) -> Dict[str, Any]:
        mode_str = self.mode.value if hasattr(self.mode, "value") else str(self.mode)
        return {
            "story_hash": self.story_hash,
            "mode": mode_str,
            "sections": [s.to_dict() for s in self.sections],
            "events": [e.to_dict() for e in self.events],
            "mentions": [m.to_dict() for m in self.mentions],
            "entities": [ent.to_dict() for ent in self.entities],
            "facts": [f.to_dict() for f in self.facts],
            "event_entity_presences": [p.to_dict() for p in self.event_entity_presences],
            "coreferences": [c.to_dict() for c in self.coreferences],
            "visual_beats": [b.to_dict() for b in self.visual_beats],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DocumentIR:
        events = [EventIR.from_dict(e) for e in data.get("events", [])]
        sections = [SectionIR.from_dict(s) for s in data.get("sections", [])]
        mentions = [MentionIR.from_dict(m) for m in data.get("mentions", [])]
        entities = [EntityIR.from_dict(e) for e in data.get("entities", [])]
        coreferences = [CoreferenceDecision.from_dict(c) for c in data.get("coreferences", [])]
        visual_beats = [VisualBeat.from_dict(b) for b in data.get("visual_beats", [])]
        metadata = dict(data.get("metadata", {}))

        raw_facts = data.get("facts")
        facts = [FactIR.from_dict(f) for f in raw_facts] if raw_facts is not None else None

        raw_presences = data.get("event_entity_presences")
        presences = [EventEntityPresenceIR.from_dict(p) for p in raw_presences] if raw_presences is not None else None

        return cls(
            story_hash=str(data.get("story_hash", "")),
            mode=DocumentMode(data.get("mode", "narrative")),
            sections=sections,
            events=events,
            mentions=mentions,
            entities=entities,
            facts=facts,
            event_entity_presences=presences,
            coreferences=coreferences,
            visual_beats=visual_beats,
            metadata=metadata,
        )


"""Fail-closed intermediate representation for the Forgotten Cities profile.

The profile extends Story Analyzer V3 without weakening its source-grounding
contract. Every historical assertion and sequence keeps exact source spans,
while references between sequences, claims, anchors, and V3 events are checked
before the document may be persisted or sent to generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from imagegen.analyzer_v3.models import SourceSpan


SCHEMA_VERSION = "forgotten_cities/1.0"


class TimeLayer(str, Enum):
    DOCUMENTED_EVENT = "documented_event"
    MODERN_RUINS = "modern_ruins"
    HISTORICAL_RECONSTRUCTION = "historical_reconstruction"
    EXCAVATION = "excavation"
    MUSEUM_DISPLAY = "museum_display"
    MAP = "map"
    ABSTRACT_EXPLANATION = "abstract_explanation"


class EvidenceLevel(str, Enum):
    ESTABLISHED = "established"
    SUPPORTED_RECONSTRUCTION = "supported_reconstruction"
    ILLUSTRATIVE_HYPOTHESIS = "illustrative_hypothesis"


class AnchorType(str, Enum):
    CITY_MASTER = "city_master"
    DISTRICT = "district"
    INTERIOR = "interior"
    LANDSCAPE = "landscape"
    ARTIFACT = "artifact"
    MAP_STYLE = "map_style"


class ClaimType(str, Enum):
    FACT = "fact"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"
    ANALOGY = "analogy"
    UNKNOWN = "unknown"


class SequencePurpose(str, Enum):
    HOOK = "hook"
    ORIENTATION = "orientation"
    DAILY_LIFE = "daily_life"
    TECHNOLOGY = "technology"
    ARTIFACT = "artifact"
    HUMAN_STORY = "human_story"
    DECLINE = "decline"
    CONCLUSION = "conclusion"


class PeoplePolicy(str, Enum):
    NONE = "none"
    ANONYMOUS_BACKGROUND = "anonymous_background"
    ANONYMOUS_ACTION = "anonymous_action"
    DEPICTED_HISTORICAL_PERSON = "depicted_historical_person"
    RECURRING_HOST = "recurring_host"


class GeographyType(str, Enum):
    PRIMARY_CITY = "PRIMARY_CITY"
    PHYSICAL_SUBLOCATION = "PHYSICAL_SUBLOCATION"
    NATURAL_ENVIRONMENT = "NATURAL_ENVIRONMENT"
    MODERN_CONTEXT_REGION = "MODERN_CONTEXT_REGION"
    ORIGIN_REGION = "ORIGIN_REGION"
    COMPARISON_LOCATION = "COMPARISON_LOCATION"
    RESEARCHER_NAME = "RESEARCHER_NAME"
    MENTIONED_ONLY = "MENTIONED_ONLY"
    UNKNOWN_LOCATION = "UNKNOWN_LOCATION"


class VisualizationPolicy(str, Enum):
    DIRECT = "direct"
    MARKED_RECONSTRUCTION = "marked_reconstruction"
    ILLUSTRATIVE_ONLY = "illustrative_only"
    DO_NOT_VISUALIZE = "do_not_visualize"


@dataclass
class GeographyMentionIR:
    geography_id: str
    text: str
    source_span: SourceSpan
    geography_type: GeographyType
    entity_id: Optional[str] = None
    event_id: Optional[str] = None
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        self.geography_type = _enum(GeographyType, self.geography_type, "geography_type")
        if not self.geography_id.strip() or not self.text.strip():
            raise ValueError("GeographyMentionIR requires geography_id and text")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("GeographyMentionIR confidence must be between 0 and 1")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.__dict__,
            "source_span": self.source_span.to_dict(),
            "geography_type": self.geography_type.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GeographyMentionIR":
        values = dict(data)
        values["source_span"] = _span(values["source_span"])
        return cls(**values)


@dataclass
class EventContextIR:
    event_id: str
    time_layer: TimeLayer
    evidence_level: EvidenceLevel
    people_policy: PeoplePolicy = PeoplePolicy.NONE

    def __post_init__(self) -> None:
        self.time_layer = _enum(TimeLayer, self.time_layer, "time_layer")
        self.evidence_level = _enum(EvidenceLevel, self.evidence_level, "evidence_level")
        self.people_policy = _enum(PeoplePolicy, self.people_policy, "people_policy")
        if not self.event_id.strip():
            raise ValueError("EventContextIR requires event_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "time_layer": self.time_layer.value,
            "evidence_level": self.evidence_level.value,
            "people_policy": self.people_policy.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventContextIR":
        return cls(**data)


@dataclass
class PopulationStyleBibleIR:
    period_and_region: str = ""
    clothing_materials: List[str] = field(default_factory=list)
    color_palette: List[str] = field(default_factory=list)
    hairstyles_and_headwear: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    forbidden_details: List[str] = field(default_factory=list)
    evidence_spans: List[SourceSpan] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {**self.__dict__, "evidence_spans": [s.to_dict() for s in self.evidence_spans]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PopulationStyleBibleIR":
        values = dict(data)
        values["evidence_spans"] = _spans(values.get("evidence_spans"))
        return cls(**values)


def _enum(enum_type: type[Enum], value: Any, field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"Unknown {field_name} {value!r}; expected one of: {allowed}") from exc


def _spans(items: Any) -> List[SourceSpan]:
    return [item if isinstance(item, SourceSpan) else SourceSpan.from_dict(item) for item in (items or [])]


def _span(item: Any) -> SourceSpan:
    return item if isinstance(item, SourceSpan) else SourceSpan.from_dict(item)


@dataclass
class CityBibleIR:
    city_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    region: str = ""
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    culture: str = ""
    environment_profile: str = ""
    architecture_profile: str = ""
    material_palette: List[str] = field(default_factory=list)
    color_palette: List[str] = field(default_factory=list)
    modern_ruins_profile: str = ""
    reconstruction_rules: List[str] = field(default_factory=list)
    forbidden_anachronisms: List[str] = field(default_factory=list)
    evidence_spans: List[SourceSpan] = field(default_factory=list)
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.city_id.strip() or not self.canonical_name.strip():
            raise ValueError("CityBibleIR requires city_id and canonical_name")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("CityBibleIR confidence must be between 0 and 1")

    def to_dict(self) -> Dict[str, Any]:
        return {**self.__dict__, "evidence_spans": [s.to_dict() for s in self.evidence_spans]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CityBibleIR":
        values = dict(data)
        values["evidence_spans"] = _spans(values.get("evidence_spans"))
        return cls(**values)


@dataclass
class VisualAnchorIR:
    anchor_id: str
    anchor_type: AnchorType
    canonical_name: str
    description: str
    time_layer: TimeLayer
    recurrence_count: int = 0
    importance: float = 0.0
    evidence_spans: List[SourceSpan] = field(default_factory=list)
    reference_required: bool = False
    reference_asset_id: Optional[str] = None
    locked: bool = False
    source_kind: Optional[str] = None
    identity_scope: str = 'specific_object'
    source_properties: Dict[str, Any] = field(default_factory=dict)
    match_spans: List[Dict[str, Any]] = field(default_factory=list)
    temporal_context: Optional[Dict[str, Any]] = None
    place_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.anchor_type = _enum(AnchorType, self.anchor_type, "anchor_type")
        self.time_layer = _enum(TimeLayer, self.time_layer, "time_layer")
        if not self.anchor_id.strip() or not self.canonical_name.strip():
            raise ValueError("VisualAnchorIR requires anchor_id and canonical_name")
        if self.place_id is not None and (not isinstance(self.place_id,str) or not self.place_id.strip()):
            raise ValueError("VisualAnchorIR place_id must identify a named place")
        if self.recurrence_count < 0 or not 0.0 <= self.importance <= 1.0:
            raise ValueError("Invalid anchor recurrence_count or importance")
        if self.locked and self.reference_required and not self.reference_asset_id:
            raise ValueError("A locked required anchor must have reference_asset_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.__dict__,
            "anchor_type": self.anchor_type.value,
            "time_layer": self.time_layer.value,
            "evidence_spans": [s.to_dict() for s in self.evidence_spans],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualAnchorIR":
        values = dict(data)
        values["evidence_spans"] = _spans(values.get("evidence_spans"))
        return cls(**values)


@dataclass
class HistoricalClaimIR:
    claim_id: str
    claim_text: str
    claim_type: ClaimType
    subject_ids: List[str]
    source_span: SourceSpan
    confidence: float
    visualization_policy: VisualizationPolicy

    def __post_init__(self) -> None:
        self.claim_type = _enum(ClaimType, self.claim_type, "claim_type")
        self.visualization_policy = _enum(VisualizationPolicy, self.visualization_policy, "visualization_policy")
        if not self.claim_id.strip() or not self.claim_text.strip():
            raise ValueError("HistoricalClaimIR requires claim_id and claim_text")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("HistoricalClaimIR confidence must be between 0 and 1")
        if self.claim_type is ClaimType.HYPOTHESIS and self.visualization_policy is VisualizationPolicy.DIRECT:
            raise ValueError("A hypothesis cannot use direct visualization")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.__dict__,
            "claim_type": self.claim_type.value,
            "source_span": self.source_span.to_dict(),
            "visualization_policy": self.visualization_policy.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HistoricalClaimIR":
        values = dict(data)
        values["source_span"] = _span(values["source_span"])
        return cls(**values)


@dataclass
class DocumentarySequenceIR:
    sequence_id: str
    title: str
    purpose: SequencePurpose
    event_ids: List[str]
    source_span: SourceSpan
    location_anchor_id: Optional[str]
    time_layer: TimeLayer
    evidence_level: EvidenceLevel
    claim_ids: List[str] = field(default_factory=list)
    artifact_anchor_ids: List[str] = field(default_factory=list)
    people_policy: PeoplePolicy = PeoplePolicy.NONE
    visual_priority: float = 0.5
    suggested_shot_count: int = 1

    def __post_init__(self) -> None:
        self.purpose = _enum(SequencePurpose, self.purpose, "purpose")
        self.time_layer = _enum(TimeLayer, self.time_layer, "time_layer")
        self.evidence_level = _enum(EvidenceLevel, self.evidence_level, "evidence_level")
        self.people_policy = _enum(PeoplePolicy, self.people_policy, "people_policy")
        if not self.sequence_id.strip() or not self.title.strip():
            raise ValueError("DocumentarySequenceIR requires sequence_id and title")
        if not self.event_ids:
            raise ValueError("DocumentarySequenceIR requires at least one event_id")
        if not 0.0 <= self.visual_priority <= 1.0 or self.suggested_shot_count < 1:
            raise ValueError("Invalid visual_priority or suggested_shot_count")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.__dict__,
            "purpose": self.purpose.value,
            "source_span": self.source_span.to_dict(),
            "time_layer": self.time_layer.value,
            "evidence_level": self.evidence_level.value,
            "people_policy": self.people_policy.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DocumentarySequenceIR":
        values = dict(data)
        values["source_span"] = _span(values["source_span"])
        return cls(**values)


@dataclass
class ForgottenCitiesDocumentIR:
    city_bible: CityBibleIR
    anchors: List[VisualAnchorIR] = field(default_factory=list)
    claims: List[HistoricalClaimIR] = field(default_factory=list)
    sequences: List[DocumentarySequenceIR] = field(default_factory=list)
    geography_mentions: List[GeographyMentionIR] = field(default_factory=list)
    event_contexts: List[EventContextIR] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def validate(self, story_text: str, valid_event_ids: Optional[Set[str]] = None) -> List[str]:
        errors: List[str] = []
        if self.schema_version != SCHEMA_VERSION:
            errors.append(f"Unsupported forgotten cities schema_version: {self.schema_version}")

        anchor_ids = [a.anchor_id for a in self.anchors]
        claim_ids = [c.claim_id for c in self.claims]
        sequence_ids = [s.sequence_id for s in self.sequences]
        geography_ids = [g.geography_id for g in self.geography_mentions]
        context_event_ids = [c.event_id for c in self.event_contexts]
        for label, values in (("anchor", anchor_ids), ("claim", claim_ids), ("sequence", sequence_ids), ("geography", geography_ids), ("event context", context_event_ids)):
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                errors.append(f"Duplicate {label} ids: {', '.join(duplicates)}")

        anchor_map = {a.anchor_id: a for a in self.anchors}
        claim_set = set(claim_ids)
        city_subjects = {self.city_bible.city_id}
        known_subjects = city_subjects | set(anchor_ids)

        spans: List[tuple[str, SourceSpan]] = []
        spans.extend(("city evidence", s) for s in self.city_bible.evidence_spans)
        for anchor in self.anchors:
            spans.extend((f"anchor {anchor.anchor_id} evidence", s) for s in anchor.evidence_spans)
        spans.extend((f"claim {claim.claim_id}", claim.source_span) for claim in self.claims)
        spans.extend((f"sequence {seq.sequence_id}", seq.source_span) for seq in self.sequences)
        spans.extend((f"geography {geo.geography_id}", geo.source_span) for geo in self.geography_mentions)
        for label, span in spans:
            if not span.validate_against(story_text):
                errors.append(f"{label} span does not match source text")

        for claim in self.claims:
            unknown = sorted(set(claim.subject_ids) - known_subjects)
            if unknown:
                errors.append(f"Claim {claim.claim_id} references unknown subjects: {', '.join(unknown)}")

        for seq in self.sequences:
            if seq.location_anchor_id and seq.location_anchor_id not in anchor_map:
                errors.append(f"Sequence {seq.sequence_id} references unknown location anchor {seq.location_anchor_id}")
            elif seq.location_anchor_id and anchor_map[seq.location_anchor_id].anchor_type is AnchorType.ARTIFACT:
                errors.append(f"Sequence {seq.sequence_id} uses artifact {seq.location_anchor_id} as a location")
            unknown_artifacts = [aid for aid in seq.artifact_anchor_ids if aid not in anchor_map or anchor_map[aid].anchor_type is not AnchorType.ARTIFACT]
            if unknown_artifacts:
                errors.append(f"Sequence {seq.sequence_id} has invalid artifact anchors: {', '.join(unknown_artifacts)}")
            unknown_claims = sorted(set(seq.claim_ids) - claim_set)
            if unknown_claims:
                errors.append(f"Sequence {seq.sequence_id} references unknown claims: {', '.join(unknown_claims)}")
            if valid_event_ids is not None:
                unknown_events = sorted(set(seq.event_ids) - valid_event_ids)
                if unknown_events:
                    errors.append(f"Sequence {seq.sequence_id} references unknown V3 events: {', '.join(unknown_events)}")
            if seq.evidence_level is EvidenceLevel.ILLUSTRATIVE_HYPOTHESIS:
                linked = [claim for claim in self.claims if claim.claim_id in seq.claim_ids]
                if linked and all(claim.claim_type is ClaimType.FACT for claim in linked):
                    errors.append(f"Sequence {seq.sequence_id} is hypothetical but all linked claims are facts")

        city_masters = [a for a in self.anchors if a.anchor_type is AnchorType.CITY_MASTER]
        if len(city_masters) > 1:
            errors.append("Only one city_master anchor is allowed")
        if valid_event_ids is not None:
            unknown_context_events = sorted(set(context_event_ids) - valid_event_ids)
            if unknown_context_events:
                errors.append(f"Event contexts reference unknown V3 events: {', '.join(unknown_context_events)}")
        return errors

    def require_valid(self, story_text: str, valid_event_ids: Optional[Set[str]] = None) -> None:
        errors = self.validate(story_text, valid_event_ids)
        if errors:
            raise ValueError("Invalid ForgottenCitiesDocumentIR: " + "; ".join(errors))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "city_bible": self.city_bible.to_dict(),
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "claims": [claim.to_dict() for claim in self.claims],
            "sequences": [sequence.to_dict() for sequence in self.sequences],
            "geography_mentions": [geo.to_dict() for geo in self.geography_mentions],
            "event_contexts": [context.to_dict() for context in self.event_contexts],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ForgottenCitiesDocumentIR":
        schema_version = str(data.get("schema_version", ""))
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported forgotten cities schema_version: {schema_version!r}")
        return cls(
            schema_version=schema_version,
            city_bible=CityBibleIR.from_dict(data["city_bible"]),
            anchors=[VisualAnchorIR.from_dict(item) for item in data.get("anchors", [])],
            claims=[HistoricalClaimIR.from_dict(item) for item in data.get("claims", [])],
            sequences=[DocumentarySequenceIR.from_dict(item) for item in data.get("sequences", [])],
            geography_mentions=[GeographyMentionIR.from_dict(item) for item in data.get("geography_mentions", [])],
            event_contexts=[EventContextIR.from_dict(item) for item in data.get("event_contexts", [])],
        )

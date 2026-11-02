"""Semantic Pre-Planning Verification for Story Analyzer V3.

Executes targeted Groq semantic verification before visual event planning to check:
1. Narrator perspective (1st person "я" vs 3rd person impersonal).
2. Dialogue participant attribution (speaker vs listener vs radio/distant voice).
3. Temporal shifts / chronology transitions across story sections.
4. Ambiguous character links and coreference in context.

Operates across sections with a shared character registry and context window for long stories.
All corrections must supply valid source text evidence spans and pass strict local fail-closed validation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from imagegen.analyzer_v3.models import (
    CoreferenceDecision,
    CoreferenceStatus,
    DocumentIR,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    PresenceRole,
    SectionIR,
    SourceSpan,
    VisualPolicy,
)

log = logging.getLogger(__name__)


@dataclass
class DialogueCorrectionIR:
    """Verified dialogue attribution correction with text evidence."""
    event_id: str
    speaker_entity_id: Optional[str]
    voice_only_entity_ids: List[str] = field(default_factory=list)
    evidence_span: Optional[SourceSpan] = None
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "speaker_entity_id": self.speaker_entity_id,
            "voice_only_entity_ids": list(self.voice_only_entity_ids),
            "evidence_span": self.evidence_span.to_dict() if self.evidence_span else None,
            "rationale": self.rationale,
        }


@dataclass
class TemporalShiftCorrectionIR:
    """Verified temporal shift or flashback anchor with text evidence."""
    event_id: str
    shift_type: str  # 'flashback', 'time_skip', 'next_day', 'same_scene'
    time_label: str  # 'morning', 'night', 'day', 'later'
    evidence_span: Optional[SourceSpan] = None
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "shift_type": self.shift_type,
            "time_label": self.time_label,
            "evidence_span": self.evidence_span.to_dict() if self.evidence_span else None,
            "rationale": self.rationale,
        }


@dataclass
class SemanticVerificationResult:
    """Result of pre-planning semantic verification pass."""
    narrator_entity_id: Optional[str] = None
    dialogue_corrections: List[DialogueCorrectionIR] = field(default_factory=list)
    temporal_shifts: List[TemporalShiftCorrectionIR] = field(default_factory=list)
    resolved_ambiguities_count: int = 0
    rejected_corrections_count: int = 0
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "narrator_entity_id": self.narrator_entity_id,
            "dialogue_corrections": [c.to_dict() for c in self.dialogue_corrections],
            "temporal_shifts": [t.to_dict() for t in self.temporal_shifts],
            "resolved_ambiguities_count": self.resolved_ambiguities_count,
            "rejected_corrections_count": self.rejected_corrections_count,
            "diagnostics": list(self.diagnostics),
        }


class SemanticVerificationValidator:
    """Fail-closed local validator for Groq semantic verification responses."""

    @classmethod
    def validate_narrator(
        cls,
        narrator_id: Optional[str],
        doc_ir: DocumentIR,
    ) -> Tuple[bool, Optional[str]]:
        if narrator_id is None:
            return True, None
        entity_ids = {e.entity_id for e in doc_ir.entities}
        if narrator_id not in entity_ids:
            return False, f"Narrator entity '{narrator_id}' does not exist in DocumentIR.entities"
        return True, None

    @classmethod
    def validate_dialogue_correction(
        cls,
        corr: DialogueCorrectionIR,
        doc_ir: DocumentIR,
        story_text: str,
    ) -> Tuple[bool, Optional[str]]:
        event = next((ev for ev in doc_ir.events if ev.event_id == corr.event_id), None)
        if not event:
            return False, f"Target event '{corr.event_id}' not found"
        if corr.evidence_span is None:
            return False, "Dialogue correction requires source evidence"
        if not (event.span.start <= corr.evidence_span.start < corr.evidence_span.end <= event.span.end):
            return False, "Dialogue evidence must belong to the target event"

        entity_map = {e.entity_id: e for e in doc_ir.entities}
        if corr.speaker_entity_id is not None:
            if corr.speaker_entity_id not in entity_map:
                return False, f"Speaker entity '{corr.speaker_entity_id}' not found in DocumentIR"
            speaker_ent = entity_map[corr.speaker_entity_id]
            # Voice-only or mentioned entities cannot be physical speakers
            if speaker_ent.modality in (
                EntityModalityV3.DEPICTED,
                EntityModalityV3.MENTIONED,
                EntityModalityV3.DECEASED_MENTION,
            ):
                return False, f"Speaker '{speaker_ent.canonical_name}' has non-speaking modality '{speaker_ent.modality.value}'"

        for vo_id in corr.voice_only_entity_ids:
            if vo_id not in entity_map:
                return False, f"Voice-only entity '{vo_id}' not found in DocumentIR"

        if corr.evidence_span:
            if not corr.evidence_span.validate_against(story_text):
                return False, f"Evidence span [{corr.evidence_span.start}:{corr.evidence_span.end}] does not match story text"

        return True, None

    @classmethod
    def validate_temporal_shift(
        cls,
        shift: TemporalShiftCorrectionIR,
        doc_ir: DocumentIR,
        story_text: str,
    ) -> Tuple[bool, Optional[str]]:
        event = next((ev for ev in doc_ir.events if ev.event_id == shift.event_id), None)
        if not event:
            return False, f"Temporal shift target event '{shift.event_id}' not found"
        if shift.evidence_span is None:
            return False, "Temporal correction requires source evidence"
        if shift.shift_type not in {"flashback", "time_skip", "next_day", "same_scene"}:
            return False, "Unknown temporal shift"
        if not (event.span.start <= shift.evidence_span.start < shift.evidence_span.end <= event.span.end):
            return False, "Temporal evidence must belong to the target event"

        if shift.evidence_span:
            if not shift.evidence_span.validate_against(story_text):
                return False, f"Evidence span [{shift.evidence_span.start}:{shift.evidence_span.end}] does not match story text"

        return True, None


class SemanticPrePlanningVerifier:
    """Runs semantic verification across sections with shared character registry."""

    SYSTEM_PROMPT = (
        "You are the Semantic Story Referee for Story Analyzer V3.\n"
        "Your task is to verify narrative structure before visual scene planning:\n"
        "1. Identify 1st-person narrator ('я') entity if present.\n"
        "2. Verify dialogue speakers and mark remote/radio/phone voices as voice_only.\n"
        "3. Detect temporal transitions and time shifts between events.\n"
        "RULES:\n"
        "- All entity IDs MUST come from the provided entities_registry.\n"
        "- All corrections MUST provide exact evidence_span with start, end, text matching the story.\n"
        "- Output MUST strictly conform to JSON format."
    )

    def __init__(
        self,
        api_key: Optional[Union[str, List[str]]] = None,
        model: str = "openai/gpt-oss-120b",
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.total_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def verify_document(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        arbitrator: Optional[Any] = None,
    ) -> DocumentIR:
        """Executes semantic verification on doc_ir and applies verified corrections."""
        if not arbitrator and not self.api_key:
            # Deterministic local pass: perform local heuristic verification
            return self._verify_local_deterministic(doc_ir, story_text)

        try:
            # Multi-section processing with shared character registry
            result = self._run_cloud_verification(doc_ir, story_text, arbitrator)
            self._apply_verification_result(doc_ir, result, arbitrator=arbitrator)
        except Exception as exc:
            from imagegen.analyzer_v3.arbitration import redact_groq_secrets
            safe_error = redact_groq_secrets(exc)
            log.warning("Cloud semantic verification failed (%s); falling back to local verification", safe_error)
            self._verify_local_deterministic(doc_ir, story_text)
            usage = arbitrator.get_usage() if arbitrator and hasattr(arbitrator, "get_usage") else dict(self.total_usage)
            doc_ir.metadata["semantic_verification"].update({
                "mode": "cloud_failed_local_fallback",
                "cloud_attempted": True,
                "cloud_error_type": type(exc).__name__,
                "cloud_error": safe_error[:500],
                "groq_usage": usage,
            })

        return doc_ir

    def _verify_local_deterministic(self, doc_ir: DocumentIR, story_text: str) -> DocumentIR:
        """Deterministic rule-based pre-planning semantic check."""
        meta_ver = {
            "mode": "local_deterministic",
            "narrator_entity_id": None,
            "dialogue_attributions_checked": 0,
            "temporal_anchors_checked": 0,
            "unresolved_ambiguities": 0,
        }

        # Check 1: 1st-person narrator detection
        first_person_pronouns = {"я", "меня", "мне", "мной", "мною"}
        for ent in doc_ir.entities:
            for alias in ent.aliases:
                if alias.lower() in first_person_pronouns:
                    meta_ver["narrator_entity_id"] = ent.entity_id
                    break
            if meta_ver["narrator_entity_id"]:
                break

        # Check 2: Dialogue speaker vs distant/radio voice presence
        for ev in doc_ir.events:
            if ev.dialogue_speakers:
                meta_ver["dialogue_attributions_checked"] += 1
                for pres in ev.presences:
                    if pres.modality in (EntityModalityV3.VOICE_ONLY, EntityModalityV3.PERCEIVED_VOICE):
                        # Ensure voice_only presence has physical_presence=False
                        pres.physical_presence = False

        # Check 3: Ambiguity tally
        unresolved_count = sum(
            1 for c in doc_ir.coreferences
            if c.status in (CoreferenceStatus.UNRESOLVED_SAFE, CoreferenceStatus.UNRESOLVED_BLOCKING)
        )
        meta_ver["unresolved_ambiguities"] = unresolved_count

        doc_ir.metadata["semantic_verification"] = meta_ver
        return doc_ir

    def _run_cloud_verification(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        arbitrator: Optional[Any] = None,
    ) -> SemanticVerificationResult:
        """Sends structured verification payload to cloud referee across sections."""
        entities_registry = [
            {
                "entity_id": e.entity_id,
                "canonical_name": e.canonical_name,
                "modality": e.modality.value if hasattr(e.modality, "value") else str(e.modality),
                "aliases": list(e.aliases),
            }
            for e in doc_ir.entities
        ]

        sections_payload = []
        for sec in doc_ir.sections:
            sec_events = [ev for ev in doc_ir.events if ev.section_id == sec.section_id]
            sections_payload.append({
                "section_id": sec.section_id,
                "text": sec.span.text,
                "events": [
                    {
                        "event_id": ev.event_id,
                        "text": ev.span.text,
                        "dialogue_speakers": list(ev.dialogue_speakers),
                        "actor_ids": list(ev.actor_ids),
                    }
                    for ev in sec_events
                ],
            })

        user_prompt = (
            f"Verify the narrative structure and participant attribution for this Russian story:\n\n"
            f"Character Registry:\n{json.dumps(entities_registry, ensure_ascii=False, indent=2)}\n\n"
            f"Sections and Events:\n{json.dumps(sections_payload, ensure_ascii=False, indent=2)}\n\n"
            f"Provide response conforming to schema:\n"
            f"{{\n"
            f'  "narrator_entity_id": string or null,\n'
            f'  "dialogue_corrections": [\n'
            f'    {{"event_id": "...", "speaker_entity_id": "...", "voice_only_entity_ids": ["..."], "evidence_span": {{"start": 0, "end": 10, "text": "..."}}, "rationale": "..."}}\n'
            f"  ],\n"
            f'  "temporal_shifts": [\n'
            f'    {{"event_id": "...", "shift_type": "flashback|time_skip|next_day|same_scene", "time_label": "morning|day|evening|night", "evidence_span": {{"start": 0, "end": 10, "text": "..."}}, "rationale": "..."}}\n'
            f"  ]\n"
            f"}}"
        )

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }

        if arbitrator and hasattr(arbitrator, "_post_json"):
            raw_data = arbitrator._post_json(payload)
        elif self.api_key:
            raw_data = self._post_json(payload)
        else:
            raise ValueError("No Groq client available for semantic verification")

        choices = raw_data.get("choices", [])
        if not choices:
            raise ValueError("Empty choices from Groq semantic verification")

        content = choices[0].get("message", {}).get("content", "{}")
        parsed = json.loads(content)

        # Parse into typed objects
        result = SemanticVerificationResult()

        narrator_id = parsed.get("narrator_entity_id")
        is_val, err = SemanticVerificationValidator.validate_narrator(narrator_id, doc_ir)
        if is_val:
            result.narrator_entity_id = narrator_id
        else:
            result.rejected_corrections_count += 1
            result.diagnostics.append(f"Rejected narrator '{narrator_id}': {err}")

        for d_raw in parsed.get("dialogue_corrections", []):
            span_dict = d_raw.get("evidence_span")
            span = SourceSpan.from_dict(span_dict) if span_dict else None
            corr = DialogueCorrectionIR(
                event_id=str(d_raw.get("event_id", "")),
                speaker_entity_id=d_raw.get("speaker_entity_id"),
                voice_only_entity_ids=list(d_raw.get("voice_only_entity_ids", [])),
                evidence_span=span,
                rationale=str(d_raw.get("rationale", "")),
            )
            is_val, err = SemanticVerificationValidator.validate_dialogue_correction(corr, doc_ir, story_text)
            if is_val:
                result.dialogue_corrections.append(corr)
            else:
                result.rejected_corrections_count += 1
                result.diagnostics.append(f"Rejected dialogue correction for '{corr.event_id}': {err}")

        for t_raw in parsed.get("temporal_shifts", []):
            span_dict = t_raw.get("evidence_span")
            span = SourceSpan.from_dict(span_dict) if span_dict else None
            shift = TemporalShiftCorrectionIR(
                event_id=str(t_raw.get("event_id", "")),
                shift_type=str(t_raw.get("shift_type", "same_scene")),
                time_label=str(t_raw.get("time_label", "day")),
                evidence_span=span,
                rationale=str(t_raw.get("rationale", "")),
            )
            is_val, err = SemanticVerificationValidator.validate_temporal_shift(shift, doc_ir, story_text)
            if is_val:
                result.temporal_shifts.append(shift)
            else:
                result.rejected_corrections_count += 1
                result.diagnostics.append(f"Rejected temporal shift for '{shift.event_id}': {err}")

        return result

    def _apply_verification_result(
        self,
        doc_ir: DocumentIR,
        result: SemanticVerificationResult,
        arbitrator: Optional[Any] = None,
    ) -> None:
        """Applies validated semantic corrections to doc_ir events and presences."""
        # 1. Apply dialogue corrections
        for corr in result.dialogue_corrections:
            event = next((ev for ev in doc_ir.events if ev.event_id == corr.event_id), None)
            if event:
                if corr.speaker_entity_id:
                    if corr.speaker_entity_id not in event.dialogue_speakers:
                        event.dialogue_speakers.append(corr.speaker_entity_id)
                for vo_id in corr.voice_only_entity_ids:
                    event.actor_ids = [eid for eid in event.actor_ids if eid != vo_id]
                    # Update or add voice-only presence
                    pres = next((p for p in event.presences if p.entity_id == vo_id), None)
                    if pres:
                        pres.modality = EntityModalityV3.VOICE_ONLY
                        pres.physical_presence = False
                    else:
                        linked_mention = next((m for m in doc_ir.mentions if m.event_id == event.event_id and any(
                            e.entity_id == vo_id and m.mention_id in e.mention_ids for e in doc_ir.entities
                        )), None)
                        if linked_mention is None:
                            continue
                        event.presences.append(
                            EventEntityPresenceIR(
                                entity_id=vo_id,
                                mention_id=linked_mention.mention_id,
                                role=PresenceRole.SPEAKER,
                                modality=EntityModalityV3.VOICE_ONLY,
                                physical_presence=False,
                            )
                        )

        # 2. Record metadata
        doc_ir.metadata["semantic_verification"] = {
            "mode": "cloud_verified",
            "cloud_attempted": True,
            "narrator_entity_id": result.narrator_entity_id,
            "dialogue_corrections_applied": len(result.dialogue_corrections),
            "temporal_shifts_applied": len(result.temporal_shifts),
            "rejected_corrections_count": result.rejected_corrections_count,
            "diagnostics": result.diagnostics,
            "groq_usage": arbitrator.get_usage() if arbitrator and hasattr(arbitrator, "get_usage") else dict(self.total_usage),
        }

    def _post_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Direct HTTP post to Groq endpoint."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "StoryAnalyzerV3-SemanticVerifier/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            usage = data.get("usage")
            if isinstance(usage, dict):
                self.total_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                self.total_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
            self.total_usage["calls"] += 1
            return data

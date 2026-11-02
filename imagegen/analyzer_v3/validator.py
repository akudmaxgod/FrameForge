"""Strict Invariant Validator and Coverage Auditor for Story Analyzer V3.

Enforces structural integrity, non-negative offsets, exact substring matching,
cross-reference validity, fail-closed actor presence, and single-source-of-truth invariants.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from imagegen.analyzer_v3.models import (
    DocumentIR,
    EntityIR,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    SectionIR,
    SourceSpan,
)


class InvariantValidator:
    """Strict fail-closed validator for DocumentIR instances."""

    def __init__(self, doc_ir: DocumentIR, story_text: str):
        self.doc_ir = doc_ir
        self.story_text = story_text
        self.errors: List[str] = []

        # Lookup caches
        self.section_map: Dict[str, SectionIR] = {s.section_id: s for s in self.doc_ir.sections}
        self.event_map: Dict[str, EventIR] = {e.event_id: e for e in self.doc_ir.events}
        self.mention_map: Dict[str, MentionIR] = {m.mention_id: m for m in self.doc_ir.mentions}
        self.entity_map: Dict[str, EntityIR] = {ent.entity_id: ent for ent in self.doc_ir.entities}

    def validate(self) -> List[str]:
        """Runs complete invariant validation suite."""
        self.errors = []
        self._check_story_hash()
        self._check_id_uniqueness()
        self._check_source_spans()
        self._check_mention_references()
        self._check_entity_references()
        self._check_presences_integrity()
        self._check_event_actors_and_presences()
        self._check_facts_integrity()
        self._check_coreference_references()
        return self.errors

    def _check_story_hash(self) -> None:
        expected_hash = hashlib.sha256(self.story_text.encode("utf-8")).hexdigest()
        if self.doc_ir.story_hash != expected_hash:
            self.errors.append(f"DocumentIR story_hash mismatch: {self.doc_ir.story_hash} != {expected_hash}")

    def _check_id_uniqueness(self) -> None:
        def check_uniqueness(items: list, id_attr: str, name: str):
            seen: Set[str] = set()
            for it in items:
                val = getattr(it, id_attr)
                if val in seen:
                    self.errors.append(f"Duplicate {name} ID detected: '{val}'")
                seen.add(val)

        check_uniqueness(self.doc_ir.sections, "section_id", "Section")
        check_uniqueness(self.doc_ir.events, "event_id", "Event")
        check_uniqueness(self.doc_ir.mentions, "mention_id", "Mention")
        check_uniqueness(self.doc_ir.entities, "entity_id", "Entity")
        check_uniqueness(self.doc_ir.facts, "fact_id", "Fact")
        check_uniqueness(self.doc_ir.event_entity_presences, "presence_id", "EventEntityPresence")

    def _validate_span(self, span: SourceSpan, label: str) -> None:
        if not span.validate_against(self.story_text):
            self.errors.append(f"{label} span mismatch: '{span.text}' != story[{span.start}:{span.end}]")

    def _check_source_spans(self) -> None:
        for sec in self.doc_ir.sections:
            self._validate_span(sec.span, f"Section {sec.section_id}")
        for ev in self.doc_ir.events:
            self._validate_span(ev.span, f"Event {ev.event_id}")
        for m in self.doc_ir.mentions:
            self._validate_span(m.span, f"Mention {m.mention_id}")
        for f in self.doc_ir.facts:
            self._validate_span(f.source_span, f"Fact {f.fact_id}")
        for ent in self.doc_ir.entities:
            for s in ent.evidence_spans:
                self._validate_span(s, f"Entity {ent.entity_id} evidence")
        for pres in self.doc_ir.event_entity_presences:
            for s in pres.evidence_spans:
                self._validate_span(s, f"Presence {pres.presence_id} evidence")

    def _check_mention_references(self) -> None:
        for m in self.doc_ir.mentions:
            if m.section_id not in self.section_map:
                self.errors.append(f"Mention {m.mention_id} references non-existent section '{m.section_id}'")
            if m.event_id is not None and m.event_id not in self.event_map:
                self.errors.append(f"Mention {m.mention_id} references non-existent event '{m.event_id}'")

    def _check_entity_references(self) -> None:
        for ent in self.doc_ir.entities:
            ent_mention_starts = []
            for m_id in ent.mention_ids:
                if m_id not in self.mention_map:
                    self.errors.append(f"Entity {ent.entity_id} references non-existent mention '{m_id}'")
                else:
                    ent_mention_starts.append(self.mention_map[m_id].span.start)
            if ent_mention_starts:
                min_start = min(ent_mention_starts)
                if ent.first_mention_start != min_start:
                    self.errors.append(
                        f"Entity {ent.entity_id} first_mention_start ({ent.first_mention_start}) "
                        f"!= minimum mention start ({min_start})"
                    )

    def _check_presences_integrity(self) -> None:
        doc_pres_map: Dict[str, EventEntityPresenceIR] = {}
        for pres in self.doc_ir.event_entity_presences:
            doc_pres_map[pres.presence_id] = pres
            if pres.event_id not in self.event_map:
                self.errors.append(f"Presence {pres.presence_id} references non-existent event '{pres.event_id}'")
            if pres.entity_id not in self.entity_map:
                self.errors.append(f"Presence {pres.presence_id} references non-existent entity '{pres.entity_id}'")
            if pres.mention_id not in self.mention_map:
                self.errors.append(f"Presence {pres.presence_id} references non-existent mention '{pres.mention_id}'")

        event_pres_map: Dict[str, Tuple[str, EventEntityPresenceIR]] = {}
        for ev in self.doc_ir.events:
            ev_pres_ids: Set[str] = set()
            for p in ev.presences:
                if p.presence_id in ev_pres_ids:
                    self.errors.append(f"Event {ev.event_id} has duplicate presence ID '{p.presence_id}'")
                ev_pres_ids.add(p.presence_id)

                if p.event_id != ev.event_id:
                    self.errors.append(f"Event {ev.event_id} presences contains presence '{p.presence_id}' with mismatched event_id '{p.event_id}'")

                if p.presence_id in event_pres_map:
                    other_ev_id, _ = event_pres_map[p.presence_id]
                    self.errors.append(f"Duplicate presence ID '{p.presence_id}' across events ({other_ev_id} and {ev.event_id})")
                event_pres_map[p.presence_id] = (ev.event_id, p)

        # Check bidirectional presence existence between DocumentIR and EventIR
        doc_pres_ids = set(doc_pres_map.keys())
        ev_pres_ids = set(event_pres_map.keys())

        for missing_in_ev in sorted(doc_pres_ids - ev_pres_ids):
            p = doc_pres_map[missing_in_ev]
            self.errors.append(
                f"DocumentIR presence '{missing_in_ev}' (event: {p.event_id}, entity: {p.entity_id}) "
                f"missing in Event.presences"
            )

        for missing_in_doc in sorted(ev_pres_ids - doc_pres_ids):
            ev_id, p = event_pres_map[missing_in_doc]
            self.errors.append(
                f"Event {ev_id} presence '{missing_in_doc}' (entity: {p.entity_id}) "
                f"missing in DocumentIR.event_entity_presences"
            )

        # Parity check
        for p_id in sorted(doc_pres_ids & ev_pres_ids):
            p_doc = doc_pres_map[p_id]
            _, p_ev = event_pres_map[p_id]
            if p_doc.event_id != p_ev.event_id:
                self.errors.append(f"Presence '{p_id}' event_id mismatch: DocumentIR='{p_doc.event_id}' != Event='{p_ev.event_id}'")
            if p_doc.entity_id != p_ev.entity_id:
                self.errors.append(f"Presence '{p_id}' entity_id mismatch: DocumentIR='{p_doc.entity_id}' != Event='{p_ev.entity_id}'")
            if p_doc.mention_id != p_ev.mention_id:
                self.errors.append(f"Presence '{p_id}' mention_id mismatch: DocumentIR='{p_doc.mention_id}' != Event='{p_ev.mention_id}'")
            mod_doc = p_doc.modality.value if hasattr(p_doc.modality, "value") else str(p_doc.modality)
            mod_ev = p_ev.modality.value if hasattr(p_ev.modality, "value") else str(p_ev.modality)
            if mod_doc != mod_ev:
                self.errors.append(f"Presence '{p_id}' modality mismatch: DocumentIR='{mod_doc}' != Event='{mod_ev}'")
            role_doc = p_doc.role.value if hasattr(p_doc.role, "value") else str(p_doc.role)
            role_ev = p_ev.role.value if hasattr(p_ev.role, "value") else str(p_ev.role)
            if role_doc != role_ev:
                self.errors.append(f"Presence '{p_id}' role mismatch: DocumentIR='{role_doc}' != Event='{role_ev}'")
            if p_doc.physical_presence != p_ev.physical_presence:
                self.errors.append(f"Presence '{p_id}' physical_presence mismatch: DocumentIR={p_doc.physical_presence} != Event={p_ev.physical_presence}")

    def _check_event_actors_and_presences(self) -> None:
        presences_by_event_entity: Dict[Tuple[str, str], List[EventEntityPresenceIR]] = defaultdict(list)
        for ev in self.doc_ir.events:
            for p in ev.presences:
                presences_by_event_entity[(ev.event_id, p.entity_id)].append(p)

        for ev in self.doc_ir.events:
            if ev.section_id not in self.section_map:
                self.errors.append(f"Event {ev.event_id} references non-existent section '{ev.section_id}'")

            for ent_id in ev.actor_ids:
                if ent_id not in self.entity_map:
                    self.errors.append(f"Event {ev.event_id} actor_ids references non-existent entity '{ent_id}'")
                else:
                    pres_list = presences_by_event_entity.get((ev.event_id, ent_id), [])
                    if not pres_list:
                        self.errors.append(
                            f"Event {ev.event_id} has actor '{ent_id}' without EventEntityPresence record"
                        )
                    elif not any(p.physical_presence for p in pres_list):
                        modalities = [p.modality.value if hasattr(p.modality, "value") else str(p.modality) for p in pres_list]
                        self.errors.append(
                            f"Event {ev.event_id} has actor '{ent_id}' without physical presence (modality: {modalities})"
                        )

            for ent_id in ev.mentioned_entity_ids:
                if ent_id not in self.entity_map:
                    self.errors.append(f"Event {ev.event_id} mentioned_entity_ids references non-existent entity '{ent_id}'")

            for ent_id in ev.subject_entity_ids:
                if ent_id not in self.entity_map:
                    self.errors.append(f"Event {ev.event_id} subject_entity_ids references non-existent entity '{ent_id}'")

            for ent_id in ev.comparison_target_ids:
                if ent_id not in self.entity_map:
                    self.errors.append(f"Event {ev.event_id} comparison_target_ids references non-existent entity '{ent_id}'")

    def _check_facts_integrity(self) -> None:
        doc_fact_map: Dict[str, FactIR] = {}
        for f in self.doc_ir.facts:
            doc_fact_map[f.fact_id] = f
            if f.section_id not in self.section_map:
                self.errors.append(f"Fact {f.fact_id} references non-existent section '{f.section_id}'")
            if f.event_id is not None and f.event_id not in self.event_map:
                self.errors.append(f"Fact {f.fact_id} references non-existent event '{f.event_id}'")
            if f.subject_id is not None and f.subject_id not in self.entity_map:
                self.errors.append(f"Fact {f.fact_id} references non-existent subject entity '{f.subject_id}'")

        event_fact_map: Dict[str, Tuple[str, FactIR]] = {}
        for ev in self.doc_ir.events:
            ev_fact_ids: Set[str] = set()
            for f in ev.facts:
                if f.fact_id in ev_fact_ids:
                    self.errors.append(f"Event {ev.event_id} has duplicate fact ID '{f.fact_id}'")
                ev_fact_ids.add(f.fact_id)

                if f.event_id != ev.event_id:
                    self.errors.append(f"Event {ev.event_id} facts contains fact '{f.fact_id}' with mismatched event_id '{f.event_id}'")

                if f.fact_id in event_fact_map:
                    other_ev_id, _ = event_fact_map[f.fact_id]
                    self.errors.append(f"Duplicate fact ID '{f.fact_id}' across events ({other_ev_id} and {ev.event_id})")
                event_fact_map[f.fact_id] = (ev.event_id, f)

        # Bidirectional fact existence between DocumentIR and EventIR
        doc_fact_ids = set(doc_fact_map.keys())
        ev_fact_ids = set(event_fact_map.keys())

        for missing_in_ev in sorted(doc_fact_ids - ev_fact_ids):
            f = doc_fact_map[missing_in_ev]
            self.errors.append(
                f"DocumentIR fact '{missing_in_ev}' (event: {f.event_id}, predicate: {f.predicate}) "
                f"missing in Event.facts"
            )

        for missing_in_doc in sorted(ev_fact_ids - doc_fact_ids):
            ev_id, f = event_fact_map[missing_in_doc]
            self.errors.append(
                f"Event {ev_id} fact '{missing_in_doc}' (predicate: {f.predicate}) "
                f"missing in DocumentIR.facts"
            )

        # Parity check
        for f_id in sorted(doc_fact_ids & ev_fact_ids):
            f_doc = doc_fact_map[f_id]
            _, f_ev = event_fact_map[f_id]
            if f_doc.event_id != f_ev.event_id:
                self.errors.append(f"Fact '{f_id}' event_id mismatch: DocumentIR='{f_doc.event_id}' != Event='{f_ev.event_id}'")
            if f_doc.section_id != f_ev.section_id:
                self.errors.append(f"Fact '{f_id}' section_id mismatch: DocumentIR='{f_doc.section_id}' != Event='{f_ev.section_id}'")
            if f_doc.subject_id != f_ev.subject_id:
                self.errors.append(f"Fact '{f_id}' subject_id mismatch: DocumentIR='{f_doc.subject_id}' != Event='{f_ev.subject_id}'")
            if f_doc.predicate != f_ev.predicate:
                self.errors.append(f"Fact '{f_id}' predicate mismatch: DocumentIR='{f_doc.predicate}' != Event='{f_ev.predicate}'")
            if f_doc.object_value != f_ev.object_value:
                self.errors.append(f"Fact '{f_id}' object_value mismatch: DocumentIR='{f_doc.object_value}' != Event='{f_ev.object_value}'")


    def _check_coreference_references(self) -> None:
        coref_seen_mentions: Set[str] = set()
        for cd in self.doc_ir.coreferences:
            if cd.mention_id in coref_seen_mentions:
                self.errors.append(f"Duplicate CoreferenceDecision for mention '{cd.mention_id}'")
            coref_seen_mentions.add(cd.mention_id)

            if cd.mention_id not in self.mention_map:
                self.errors.append(f"CoreferenceDecision references non-existent mention '{cd.mention_id}'")
            if cd.event_id is not None and cd.event_id not in self.event_map:
                self.errors.append(f"CoreferenceDecision references non-existent event '{cd.event_id}'")
            for ent_id in cd.resolved_entity_ids:
                if ent_id not in self.entity_map:
                    self.errors.append(f"CoreferenceDecision references non-existent resolved entity '{ent_id}'")
            for cand_id in cd.candidate_entity_ids:
                if cand_id not in self.entity_map:
                    self.errors.append(f"CoreferenceDecision references non-existent candidate entity '{cand_id}'")
            for item in cd.rejected_candidates:
                rej_id = item.get("entity_id") if isinstance(item, dict) else getattr(item, "entity_id", None)
                if rej_id and rej_id not in self.entity_map:
                    self.errors.append(f"CoreferenceDecision references non-existent rejected entity '{rej_id}'")
            for s in cd.evidence_spans:
                self._validate_span(s, f"Coreference {cd.mention_id} evidence")


class TextCoverageAuditor:
    """Audits text coverage across events, structural headers, and formatting."""

    @staticmethod
    def audit(doc_ir: DocumentIR, story_text: str) -> Dict[str, Any]:
        n = len(story_text)
        if n == 0:
            return {
                "total_chars": 0,
                "counts": {"EVENT": 0, "STRUCTURAL_HEADER": 0, "WHITESPACE_OR_FORMATTING": 0, "UNCOVERED_GAP": 0},
                "gaps": [],
                "is_fully_covered": True,
            }

        coverage = ["UNCOVERED_GAP"] * n

        # Whitespace
        for i, ch in enumerate(story_text):
            if ch.isspace():
                coverage[i] = "WHITESPACE_OR_FORMATTING"

        # Headers
        for sec in doc_ir.sections:
            sec_text = story_text[sec.span.start:sec.span.end]
            for line in sec_text.splitlines():
                if line.strip().startswith("#"):
                    l_start = story_text.find(line, sec.span.start, sec.span.end)
                    if l_start != -1:
                        for i in range(l_start, l_start + len(line)):
                            coverage[i] = "STRUCTURAL_HEADER"

        # Events
        for ev in doc_ir.events:
            for i in range(ev.span.start, ev.span.end):
                coverage[i] = "EVENT"

        counts = {
            "EVENT": coverage.count("EVENT"),
            "STRUCTURAL_HEADER": coverage.count("STRUCTURAL_HEADER"),
            "WHITESPACE_OR_FORMATTING": coverage.count("WHITESPACE_OR_FORMATTING"),
            "UNCOVERED_GAP": coverage.count("UNCOVERED_GAP"),
        }

        gaps = []
        i = 0
        while i < n:
            if coverage[i] == "UNCOVERED_GAP":
                g_start = i
                while i < n and coverage[i] == "UNCOVERED_GAP":
                    i += 1
                gaps.append({
                    "start": g_start,
                    "end": i,
                    "text": story_text[g_start:i],
                })
            else:
                i += 1

        return {
            "total_chars": n,
            "counts": counts,
            "gaps": gaps,
            "is_fully_covered": counts["UNCOVERED_GAP"] == 0,
        }

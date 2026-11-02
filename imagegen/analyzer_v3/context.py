"""Document Context and Indexing for Story Analyzer V3.

Provides high-performance O(1) lookups across sections, events, mentions,
entities, presences, and facts within a DocumentIR instance.
"""
from __future__ import annotations

import bisect
from typing import Dict, List, Optional, Tuple

from imagegen.analyzer_v3.models import (
    DocumentIR,
    EntityIR,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    SectionIR,
)


class DocumentContext:
    """Pre-computed indexing context over DocumentIR for O(1) lookups across pipeline stages."""

    def __init__(self, doc_ir: DocumentIR, max_len: Optional[int] = None):
        self.doc_ir = doc_ir
        self.max_len = max_len
        self.rebuild()

    def rebuild(self) -> None:
        """Rebuilds all internal lookup indices and O(1) interval maps."""
        self.section_by_id: Dict[str, SectionIR] = {s.section_id: s for s in self.doc_ir.sections}
        self.event_by_id: Dict[str, EventIR] = {e.event_id: e for e in self.doc_ir.events}
        self.entity_by_id: Dict[str, EntityIR] = {e.entity_id: e for e in self.doc_ir.entities}
        self.mention_by_id: Dict[str, MentionIR] = {m.mention_id: m for m in self.doc_ir.mentions}

        self.entity_by_mention_id: Dict[str, EntityIR] = {}
        for ent in self.doc_ir.entities:
            for mid in ent.mention_ids:
                self.entity_by_mention_id[mid] = ent

        self.mentions_by_event_id: Dict[str, List[MentionIR]] = {}
        self.mentions_by_section_id: Dict[str, List[MentionIR]] = {}
        for m in self.doc_ir.mentions:
            if m.event_id:
                self.mentions_by_event_id.setdefault(m.event_id, []).append(m)
            if m.section_id:
                self.mentions_by_section_id.setdefault(m.section_id, []).append(m)

        self.presences_by_event_id: Dict[str, List[EventEntityPresenceIR]] = {}
        self.presences_by_event_entity: Dict[Tuple[str, str], EventEntityPresenceIR] = {}
        for ev in self.doc_ir.events:
            self.presences_by_event_id[ev.event_id] = list(ev.presences)
            for p in ev.presences:
                self.presences_by_event_entity[(p.event_id, p.entity_id)] = p

        self.facts_by_event_id: Dict[str, List[FactIR]] = {}
        for ev in self.doc_ir.events:
            self.facts_by_event_id[ev.event_id] = list(ev.facts)

        # O(1) character offset lookup arrays
        limit = self.max_len or 0
        for s in self.doc_ir.sections:
            if s.span.end > limit:
                limit = s.span.end
        for e in self.doc_ir.events:
            if e.span.end > limit:
                limit = e.span.end

        self._char_to_event: List[Optional[EventIR]] = [None] * (limit + 1)
        for ev in self.doc_ir.events:
            for idx in range(max(0, ev.span.start), min(len(self._char_to_event), ev.span.end)):
                self._char_to_event[idx] = ev

        self._char_to_section: List[Optional[SectionIR]] = [None] * (limit + 1)
        for sec in self.doc_ir.sections:
            for idx in range(max(0, sec.span.start), min(len(self._char_to_section), sec.span.end)):
                self._char_to_section[idx] = sec

    def find_event_at_offset(self, offset: int) -> Optional[EventIR]:
        """Finds event spanning the given character offset in O(1) time."""
        if 0 <= offset < len(self._char_to_event):
            return self._char_to_event[offset]
        return None

    def find_section_at_offset(self, offset: int) -> Optional[SectionIR]:
        """Finds section spanning the given character offset in O(1) time."""
        if 0 <= offset < len(self._char_to_section) and self._char_to_section[offset] is not None:
            return self._char_to_section[offset]
        return self.doc_ir.sections[0] if self.doc_ir.sections else None

    def get_event(self, event_id: str) -> Optional[EventIR]:
        """Looks up event by ID in O(1) time."""
        return self.event_by_id.get(event_id)

    def get_section(self, section_id: str) -> Optional[SectionIR]:
        """Looks up section by ID in O(1) time."""
        return self.section_by_id.get(section_id)

    def get_entity(self, entity_id: str) -> Optional[EntityIR]:
        """Looks up entity by ID in O(1) time."""
        return self.entity_by_id.get(entity_id)

    def get_mention(self, mention_id: str) -> Optional[MentionIR]:
        """Looks up mention by ID in O(1) time."""
        return self.mention_by_id.get(mention_id)

    def get_entity_for_mention(self, mention_id: str) -> Optional[EntityIR]:
        """Looks up owning entity for a mention in O(1) time."""
        return self.entity_by_mention_id.get(mention_id)

    def get_event_mentions(self, event_id: str) -> List[MentionIR]:
        """Gets all mentions in an event in O(1) time."""
        return self.mentions_by_event_id.get(event_id, [])

    def get_section_mentions(self, section_id: str) -> List[MentionIR]:
        """Gets all mentions in a section in O(1) time."""
        return self.mentions_by_section_id.get(section_id, [])

    def get_event_presences(self, event_id: str) -> List[EventEntityPresenceIR]:
        """Gets all presences in an event in O(1) time."""
        return self.presences_by_event_id.get(event_id, [])

    def get_event_facts(self, event_id: str) -> List[FactIR]:
        """Gets all facts in an event in O(1) time."""
        return self.facts_by_event_id.get(event_id, [])

    def get_presence(self, event_id: str, entity_id: str) -> Optional[EventEntityPresenceIR]:
        """Looks up presence for an event-entity pair in O(1) time."""
        return self.presences_by_event_entity.get((event_id, entity_id))


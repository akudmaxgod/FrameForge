"""Meaningful documentary sequence grouping for long historical scripts."""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

from imagegen.analyzer_v3.models import DocumentIR, EventIR, SourceSpan

from .models import (
    DocumentarySequenceIR,
    EvidenceLevel,
    EventContextIR,
    PeoplePolicy,
    SequencePurpose,
    TimeLayer,
)


_RHETORICAL_ONLY = re.compile(
    r"^\s*(?:подобрал\s+материал|комплект\s+дош[её]л\s+до\s+нас|накормить\s+людей|"
    r"а\s+пока\s+посмотрим|и\s+вот\s+вопрос)\s*[.!?…]*\s*$",
    re.I,
)

_PURPOSE_PATTERNS = [
    (SequencePurpose.DECLINE, re.compile(r"\b(?:упад\w*|покин\w*|исчез\w*|гибел\w*|засух\w*|разруш\w*)\b", re.I)),
    (SequencePurpose.ARTIFACT, re.compile(r"\b(?:артефакт\w*|сосуд\w*|печать\w*|игров\w*\s+доск\w*|глаз\w*|находк\w*)\b", re.I)),
    (SequencePurpose.TECHNOLOGY, re.compile(r"\b(?:технолог\w*|производ\w*|мастерск\w*|ремесл\w*|лазурит\w*|обработ\w*)\b", re.I)),
    (SequencePurpose.DAILY_LIFE, re.compile(r"\b(?:жител\w*|дом\w*|пищ\w*|земледел\w*|торгов\w*|повседнев\w*)\b", re.I)),
    (SequencePurpose.HUMAN_STORY, re.compile(r"\b(?:женщин\w*|мужчин\w*|реб[её]н\w*|археолог\w*|исследователь\w*)\b", re.I)),
    (SequencePurpose.ORIENTATION, re.compile(r"\b(?:располож\w*|карт\w*|регион\w*|город\w*|основан\w*)\b", re.I)),
]


class DocumentarySequenceBuilder:
    def build(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        event_contexts: List[EventContextIR],
        duration_seconds: Optional[int] = None,
    ) -> List[DocumentarySequenceIR]:
        events = [event for event in doc_ir.events if event.span.text.strip()]
        if not events:
            return []
        context_map = {context.event_id: context for context in event_contexts}
        word_count = len(re.findall(r"\b\w+\b", story_text, re.UNICODE))
        desired = max(1, min(20, max(12, round(word_count / 180)))) if len(events) > 12 else min(len(events), 12)
        max_group_events = max(1, math.ceil(len(events) / desired))

        groups: List[List[EventIR]] = []
        current: List[EventIR] = []
        for event in events:
            context = context_map.get(event.event_id)
            previous_context = context_map.get(current[-1].event_id) if current else None
            layer_changed = bool(current and context and previous_context and context.time_layer is not previous_context.time_layer)
            rhetorical = bool(_RHETORICAL_ONLY.match(event.span.text))
            if current and not rhetorical and (len(current) >= max_group_events or layer_changed):
                groups.append(current)
                current = []
            current.append(event)
        if current:
            groups.append(current)

        # A rhetorical bridge cannot stand alone. Attach it to the nearest episode.
        index = 0
        while index < len(groups):
            group = groups[index]
            if len(group) == 1 and _RHETORICAL_ONLY.match(group[0].span.text):
                if index > 0:
                    groups[index - 1].extend(group)
                    groups.pop(index)
                    continue
                if len(groups) > 1:
                    groups[1] = group + groups[1]
                    groups.pop(0)
                    continue
            index += 1

        while len(groups) > 20:
            merge_at = min(range(len(groups) - 1), key=lambda i: len(groups[i]) + len(groups[i + 1]))
            groups[merge_at].extend(groups.pop(merge_at + 1))

        total_shot_budget = None
        if duration_seconds and duration_seconds > 0:
            total_shot_budget = max(len(groups), round(duration_seconds / 6.5))

        sequences: List[DocumentarySequenceIR] = []
        for idx, group in enumerate(groups, 1):
            text = story_text[group[0].span.start:group[-1].span.end]
            contexts = [context_map[event.event_id] for event in group if event.event_id in context_map]
            layer = self._majority(contexts, "time_layer", TimeLayer.ABSTRACT_EXPLANATION)
            evidence = self._majority(contexts, "evidence_level", EvidenceLevel.ESTABLISHED)
            people = self._majority(contexts, "people_policy", PeoplePolicy.NONE)
            purpose = self._purpose(text, idx, len(groups))
            suggested = max(2, min(5, math.ceil(len(text) / 420)))
            if total_shot_budget:
                suggested = max(1, min(5, round(total_shot_budget / len(groups))))
            words = re.findall(r"[\wА-Яа-яЁё-]+", text)
            title = " ".join(words[:8]).strip().capitalize() or f"Эпизод {idx}"
            importance = max((event.importance_score for event in group), default=0.5)
            sequences.append(DocumentarySequenceIR(
                sequence_id=f"sequence_{idx:03d}",
                title=title,
                purpose=purpose,
                event_ids=[event.event_id for event in group],
                source_span=SourceSpan(group[0].span.start, group[-1].span.end, text),
                location_anchor_id=None,
                time_layer=layer,
                evidence_level=evidence,
                people_policy=people,
                visual_priority=max(0.0, min(1.0, importance)),
                suggested_shot_count=suggested,
            ))
        return sequences

    @staticmethod
    def _majority(contexts, attr, default):
        if not contexts:
            return default
        values = [getattr(context, attr) for context in contexts]
        return max(dict.fromkeys(values), key=values.count)

    @staticmethod
    def _purpose(text: str, index: int, total: int) -> SequencePurpose:
        if index == 1:
            return SequencePurpose.HOOK
        if index == total:
            return SequencePurpose.CONCLUSION
        for purpose, pattern in _PURPOSE_PATTERNS:
            if pattern.search(text):
                return purpose
        return SequencePurpose.ORIENTATION

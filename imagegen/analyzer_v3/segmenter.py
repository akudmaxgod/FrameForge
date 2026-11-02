"""Deterministic Document Segmenter for Story Analyzer V3.

Segments documents into sections and fine-grained visual events based on DocumentMode
while maintaining 100% exact SourceSpan offsets and zero coordinate drift.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from imagegen.analyzer_v3.classifier import DocumentModeClassifier
from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    DocumentModeDecision,
    EventIR,
    SectionIR,
    SourceSpan,
)
from imagegen.analyzer_v3.nlp import RussianNLPAdapter


RE_NUMBERED_SPLIT = re.compile(r"(?=(?:^|\n)\s*(?:\d{1,3}[\.\)]|[-*•]|Шаг\s*\d+[:\.]?)\s+)")


class DocumentSegmenter:
    """Deterministic segmenter converting raw text into DocumentIR structural skeletons."""

    def __init__(
        self,
        nlp_adapter: Optional[RussianNLPAdapter] = None,
        classifier: Optional[DocumentModeClassifier] = None,
    ):
        self.nlp = nlp_adapter or RussianNLPAdapter.get_instance()
        self.classifier = classifier or DocumentModeClassifier(self.nlp)

    def segment(self, text: str) -> DocumentIR:
        """Fully segments text into Sections and Events, returning a base DocumentIR."""
        story_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        mode_decision = self.classifier.classify(text)

        raw_sections = self.classifier._split_into_sections(text)
        section_irs: List[SectionIR] = []
        event_irs: List[EventIR] = []

        for sec_id, sec_title, sec_text, sec_span in raw_sections:
            sec_mode = mode_decision.section_modes.get(sec_id, mode_decision.mode)
            sec_event_ids: List[str] = []

            # Segment events within section based on its mode
            sec_events = self._segment_section_events(
                sec_id=sec_id,
                sec_mode=sec_mode,
                sec_text=sec_text,
                sec_offset=sec_span.start,
                start_event_idx=len(event_irs),
            )

            for ev in sec_events:
                event_irs.append(ev)
                sec_event_ids.append(ev.event_id)

            sec_ir = SectionIR(
                section_id=sec_id,
                span=sec_span,
                title=sec_title,
                mode=sec_mode,
                event_ids=sec_event_ids,
            )
            section_irs.append(sec_ir)

        doc_ir = DocumentIR(
            story_hash=story_hash,
            mode=mode_decision.mode,
            sections=section_irs,
            events=event_irs,
            mentions=[],
            entities=[],
            coreferences=[],
            visual_beats=[],
            metadata={
                "mode_decision": mode_decision.to_dict(),
                "total_sections": len(section_irs),
                "total_events": len(event_irs),
            },
        )

        # Invariant validation
        errors = doc_ir.validate_invariants(text)
        if errors:
            raise ValueError(f"DocumentIR validation failed after segmentation:\n" + "\n".join(errors))

        return doc_ir

    def _segment_section_events(
        self,
        sec_id: str,
        sec_mode: DocumentMode,
        sec_text: str,
        sec_offset: int,
        start_event_idx: int,
    ) -> List[EventIR]:
        """Segments a section's text into fine-grained visual events according to its mode."""
        events: List[EventIR] = []

        if sec_mode in (DocumentMode.FACT_LIST, DocumentMode.INSTRUCTIONAL):
            # Split by numbered / bullet / step items
            item_spans = self._split_by_items(sec_text, sec_offset)
            for i, span in enumerate(item_spans):
                ev_type = "step" if sec_mode == DocumentMode.INSTRUCTIONAL else "fact"
                ev_id = f"ev_{start_event_idx + len(events)}"
                events.append(
                    EventIR(
                        event_id=ev_id,
                        section_id=sec_id,
                        span=span,
                        event_type=ev_type,
                        visualizable=True,
                        importance_score=0.8,
                    )
                )
        else:
            # Narrative, Explainer, Multi-Subject Review, Mixed
            # Split by sentences using razdel
            sent_spans = self.nlp.split_sentences(sec_text)
            for sent_span in sent_spans:
                global_span = SourceSpan(
                    start=sec_offset + sent_span.start,
                    end=sec_offset + sent_span.end,
                    text=sent_span.text,
                )
                ev_type = "action"
                if sent_span.text.strip().startswith(("—", "-", "–")):
                    ev_type = "dialogue"
                elif sec_mode == DocumentMode.EXPLAINER:
                    ev_type = "state"
                elif sec_mode == DocumentMode.MULTI_SUBJECT_REVIEW:
                    ev_type = "comparison" if any(w in sent_span.text.lower() for w in ("отличие", "лучше", "чем", "плюс", "минус")) else "fact"

                ev_id = f"ev_{start_event_idx + len(events)}"
                events.append(
                    EventIR(
                        event_id=ev_id,
                        section_id=sec_id,
                        span=global_span,
                        event_type=ev_type,
                        visualizable=True,
                        importance_score=0.7 if ev_type in ("action", "dialogue") else 0.5,
                    )
                )

        # Fallback if no events detected (e.g. whitespace only)
        if not events and sec_text.strip():
            ev_id = f"ev_{start_event_idx}"
            events.append(
                EventIR(
                    event_id=ev_id,
                    section_id=sec_id,
                    span=SourceSpan(start=sec_offset, end=sec_offset + len(sec_text), text=sec_text),
                    event_type="action",
                    visualizable=True,
                    importance_score=0.5,
                )
            )

        return events

    def _split_by_items(self, text: str, offset: int) -> List[SourceSpan]:
        """Splits itemized lists into individual item SourceSpans."""
        # Find item match positions
        matches = list(re.finditer(r"(?:^|\n)\s*(?:\d{1,3}[\.\)]|[-*•]|Шаг\s*\d+[:\.]?)\s+", text, re.IGNORECASE))
        if not matches:
            # Fallback to sentence split
            sent_spans = self.nlp.split_sentences(text)
            return [
                SourceSpan(start=offset + s.start, end=offset + s.end, text=s.text)
                for s in sent_spans if s.text.strip()
            ]

        item_spans: List[SourceSpan] = []
        for i, match in enumerate(matches):
            item_start = match.start()
            if text[item_start] == "\n":
                item_start += 1  # Skip leading newline
            item_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            item_text = text[item_start:item_end].strip()
            if item_text:
                # Find exact start within text slice to handle leading spaces
                exact_start = text.find(item_text, item_start)
                exact_end = exact_start + len(item_text)
                item_spans.append(
                    SourceSpan(
                        start=offset + exact_start,
                        end=offset + exact_end,
                        text=item_text,
                    )
                )

        return item_spans

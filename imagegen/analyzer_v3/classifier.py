"""Document Mode Classifier for Story Analyzer V3.

Extracts deterministic structural features (headers, numbering, bullet points,
imperatives, specs, pronouns, temporal markers) to classify document mode:
- NARRATIVE
- FACT_LIST
- MULTI_SUBJECT_REVIEW
- EXPLAINER
- INSTRUCTIONAL
- MIXED
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from imagegen.analyzer_v3.models import DocumentMode, DocumentModeDecision, SourceSpan
from imagegen.analyzer_v3.nlp import RussianNLPAdapter


# Regex patterns for structural cues
RE_NUMBERED_ITEM = re.compile(r"^\s*(?:\d{1,3}[\.\)]|[-*•])\s+", re.MULTILINE)
RE_STEP_HEADER = re.compile(r"^\s*(?:шаг|этап|пункт|действие)\s*\d+[:\.]?", re.IGNORECASE | re.MULTILINE)
RE_MARKDOWN_HEADER = re.compile(r"^\s*#{1,6}\s+(.+)$", re.MULTILINE)
RE_DIALOGUE_LINE = re.compile(r"^\s*[—–]\s+.*?(?:[,!?]\s*[-—–]|\?|\!|$)", re.MULTILINE)
RE_FACT_TITLE = re.compile(r"\b\d+\s+(?:фактов|вещей|причин|мифов|советов|секретов|правил)\b", re.IGNORECASE)
RE_REVIEW_TITLE = re.compile(r"\b(?:обзор|сравнение|топ[- ]\d+|выбор|рейтинг)\b", re.IGNORECASE)

# Lexical indicators
IMPERATIVE_VERBS = {
    "возьмите", "отрежьте", "настройте", "установите", "проверьте", "нанесите",
    "убедитесь", "запустите", "создайте", "добавьте", "откройте", "нажмите",
    "выберите", "скопируйте", "вставьте", "сохраните", "введите", "выполните",
    "подключите", "удалите", "замените", "положите", "прикрепите", "соедините",
    "закрепите", "затяните", "открутите", "вкрутите", "отшлифуйте", "покройте"
}

COMPARATIVE_MARKERS = {
    "в отличие от", "по сравнению с", "лучше", "хуже", "быстрее", "мощнее",
    "преимущество", "недостаток", "плюсы", "минусы", "характеристики",
    "комплектация", "версия", "модель", "цена", "стоимость", "дизайн",
    "производительность", "автономность", "экран", "дисплей", "камера", "двигатель",
    "процессор", "аккумулятор", "батарея", "сенсор", "матрица", "объектив",
    "оптика", "апертура", "разрешение", "корпус", "габариты", "вес", "флагман",
    "оснащен", "оборудован", "обладает", "отличается", "поддерживает", "включает",
    "предлагает", "комплектуется", "доступен", "оснащается", "л.с.", "мм", "гб", "мп"
}

EXPLAINER_MARKERS = {
    "таким образом", "следовательно", "дело в том", "это означает", "причина заключается",
    "в качестве примера", "как показывает практика", "рассмотрим механизм",
    "теория гласит", "принцип работы", "с физической точки зрения"
}


SEQUENTIAL_PROCESS_MARKERS = {
    "сначала", "затем", "после этого", "в конце", "в завершение", "далее", "потом", "первым делом"
}


class DocumentModeClassifier:
    """Classifies the structural document mode using deterministic heuristic features."""

    def __init__(self, nlp_adapter: Optional[RussianNLPAdapter] = None):
        self.nlp = nlp_adapter or RussianNLPAdapter.get_instance()

    def classify(self, text: str) -> DocumentModeDecision:
        """Determines the overall document mode and section-specific modes."""
        evidence: List[SourceSpan] = []
        raw_lines = text.split("\n")
        non_empty_lines = [line.strip() for line in raw_lines if line.strip()]

        if not non_empty_lines:
            return DocumentModeDecision(
                mode=DocumentMode.NARRATIVE,
                confidence=1.0,
                evidence=[],
                rationale="Empty text defaults to narrative."
            )

        # 1. Structural features extraction
        numbered_matches = list(RE_NUMBERED_ITEM.finditer(text))
        step_matches = list(RE_STEP_HEADER.finditer(text))
        md_headers = list(RE_MARKDOWN_HEADER.finditer(text))
        dialogue_matches = list(RE_DIALOGUE_LINE.finditer(text))

        # Check title patterns
        first_line = non_empty_lines[0]
        has_fact_title = bool(RE_FACT_TITLE.search(first_line))
        has_review_title = bool(RE_REVIEW_TITLE.search(first_line))

        # Imperative / Command density
        words = [w.lower() for w in re.findall(r"\b[а-яёa-z0-9_-]+\b", text, re.IGNORECASE)]
        
        def is_imperative(w: str) -> bool:
            if w in IMPERATIVE_VERBS:
                return True
            parses = self.nlp.morph_analyzer.parse(w)
            return bool(parses and getattr(parses[0].tag, "mood", None) == "impr")

        imperative_count = sum(1 for w in words if is_imperative(w))
        comparative_count = sum(1 for marker in COMPARATIVE_MARKERS if marker in text.lower())
        explainer_count = sum(1 for marker in EXPLAINER_MARKERS if marker in text.lower())
        sequential_count = sum(1 for marker in SEQUENTIAL_PROCESS_MARKERS if marker in text.lower())

        numbered_ratio = len(numbered_matches) / max(len(non_empty_lines), 1)
        dialogue_ratio = len(dialogue_matches) / max(len(non_empty_lines), 1)

        # Record evidence spans for key matches
        for m in numbered_matches[:5]:
            evidence.append(SourceSpan(start=m.start(), end=m.end(), text=m.group(0)))
        for m in md_headers[:5]:
            evidence.append(SourceSpan(start=m.start(), end=m.end(), text=m.group(0)))

        # 2. Section segmentation to inspect multi-mode heterogeneity
        sections = self._split_into_sections(text)
        section_modes: Dict[str, DocumentMode] = {}
        for sec_id, sec_title, sec_text, sec_span in sections:
            sec_mode = self._classify_section(sec_title, sec_text)
            section_modes[sec_id] = sec_mode

        distinct_modes = set(section_modes.values())

        # 3. Decision Logic
        # Case: Mixed mode document (multiple sections with genuinely differing structural modes)
        if len(sections) > 1 and len(distinct_modes) > 1:
            mode_counts = {m: list(section_modes.values()).count(m) for m in distinct_modes}
            if len([m for m, count in mode_counts.items() if count >= 1]) >= 2:
                primary_mode = max(mode_counts, key=mode_counts.get)
                if mode_counts[primary_mode] < len(sections) * 0.75:
                    return DocumentModeDecision(
                        mode=DocumentMode.MIXED,
                        confidence=0.90,
                        evidence=evidence,
                        section_modes=section_modes,
                        rationale=f"Heterogeneous sections detected: {dict(mode_counts)}"
                    )

        # Case: Instructional (steps / recipe / procedural commands / continuous step instructions)
        if (len(step_matches) >= 2 or (numbered_ratio > 0.35 and imperative_count >= 2) or (imperative_count >= 3 and sequential_count >= 2)) and dialogue_ratio < 0.1:
            return DocumentModeDecision(
                mode=DocumentMode.INSTRUCTIONAL,
                confidence=0.95,
                evidence=evidence,
                section_modes=section_modes,
                rationale=f"High density of steps ({len(step_matches)}) and imperative commands ({imperative_count})."
            )

        # Case: Fact List (numbered or bullet list of independent statements)
        if (has_fact_title or numbered_ratio >= 0.4 or len(numbered_matches) >= 3) and len(md_headers) < 2 and dialogue_ratio < 0.1 and comparative_count < 4:
            return DocumentModeDecision(
                mode=DocumentMode.FACT_LIST,
                confidence=0.95,
                evidence=evidence,
                section_modes=section_modes,
                rationale=f"Numbered/bullet items ({len(numbered_matches)}) with independent factual assertions."
            )

        # Case: Multi-Subject Review (multiple object sections or review title with comparative features)
        if (has_review_title or (len(md_headers) >= 2 and comparative_count >= 2) or (comparative_count >= 4 and dialogue_ratio < 0.1)) and dialogue_ratio < 0.1:
            return DocumentModeDecision(
                mode=DocumentMode.MULTI_SUBJECT_REVIEW,
                confidence=0.92,
                evidence=evidence,
                section_modes=section_modes,
                rationale=f"Multiple subject review sections ({len(md_headers)}) and comparative indicators ({comparative_count})."
            )

        # Case: Explainer
        if explainer_count >= 2 and dialogue_ratio < 0.1 and numbered_ratio < 0.3:
            return DocumentModeDecision(
                mode=DocumentMode.EXPLAINER,
                confidence=0.88,
                evidence=evidence,
                section_modes=section_modes,
                rationale=f"Explanatory discourse markers ({explainer_count}) with thesis-proof structure."
            )

        # Case: Narrative (default for story/dialogue/fiction/actions)
        return DocumentModeDecision(
            mode=DocumentMode.NARRATIVE,
            confidence=0.95 if dialogue_ratio > 0.1 or len(md_headers) == 0 else 0.85,
            evidence=evidence,
            section_modes=section_modes,
            rationale="Coherent narrative timeline with character actions or dialogue."
        )

    def _split_into_sections(self, text: str) -> List[Tuple[str, Optional[str], str, SourceSpan]]:
        """Splits text into major structural sections by Markdown headers or large breaks."""
        header_spans = [(m.start(), m.end(), m.group(1).strip()) for m in RE_MARKDOWN_HEADER.finditer(text)]
        if not header_spans:
            # Fallback: single section
            return [("sec_0", None, text, SourceSpan(start=0, end=len(text), text=text))]

        sections: List[Tuple[str, Optional[str], str, SourceSpan]] = []
        # Pre-header content if any
        if header_spans[0][0] > 0:
            pre_text = text[0:header_spans[0][0]]
            if pre_text.strip():
                sections.append(("sec_0", None, pre_text, SourceSpan(start=0, end=header_spans[0][0], text=pre_text)))

        for i, (h_start, h_end, h_title) in enumerate(header_spans):
            sec_start = h_start
            sec_end = header_spans[i + 1][0] if i + 1 < len(header_spans) else len(text)
            sec_text = text[sec_start:sec_end]
            sec_id = f"sec_{len(sections)}"
            sections.append((sec_id, h_title, sec_text, SourceSpan(start=sec_start, end=sec_end, text=sec_text)))

        return sections

    def _classify_section(self, title: Optional[str], sec_text: str) -> DocumentMode:
        """Classifies a single section independently."""
        title_str = (title or "").lower()
        if "глава" in title_str or "акт" in title_str or "пролог" in title_str or "эпилог" in title_str:
            return DocumentMode.NARRATIVE
        if "факт" in title_str:
            return DocumentMode.FACT_LIST
        if "шаг" in title_str or "инструкц" in title_str or "настройк" in title_str or "руководств" in title_str or "практик" in title_str:
            return DocumentMode.INSTRUCTIONAL
        if "обзор" in title_str or "сравнен" in title_str or "модел" in title_str or "характеристик" in title_str:
            return DocumentMode.MULTI_SUBJECT_REVIEW
        if "теори" in title_str or "объяснен" in title_str or "введен" in title_str:
            return DocumentMode.EXPLAINER

        # Content heuristics
        step_matches = list(RE_STEP_HEADER.finditer(sec_text))
        num_matches = list(RE_NUMBERED_ITEM.finditer(sec_text))
        words = [w.lower() for w in re.findall(r"\b[а-яёa-z0-9_-]+\b", sec_text, re.IGNORECASE)]
        
        def is_imperative(w: str) -> bool:
            if w in IMPERATIVE_VERBS:
                return True
            parses = self.nlp.morph_analyzer.parse(w)
            return bool(parses and getattr(parses[0].tag, "mood", None) == "impr")

        imperative_count = sum(1 for w in words if is_imperative(w))

        if len(step_matches) >= 2 or (len(num_matches) >= 2 and imperative_count >= 2):
            return DocumentMode.INSTRUCTIONAL
        if len(num_matches) >= 2:
            return DocumentMode.FACT_LIST

        return DocumentMode.NARRATIVE

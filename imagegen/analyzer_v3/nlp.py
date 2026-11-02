"""Russian NLP Adapter for Story Analyzer V3.

Provides deterministic tokenization, sentence splitting, morpho-syntactic analysis,
and named entity recognition with strict character coordinate preservation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import razdel
import pymorphy3
from natasha import (
    Doc,
    NewsEmbedding,
    NewsMorphTagger,
    NewsNERTagger,
    NewsSyntaxParser,
    Segmenter,
)

from imagegen.analyzer_v3.models import SourceSpan


@dataclass(frozen=True)
class MorphAnalysis:
    lemma: str
    pos: str  # NOUN, VERB, ADJF, NPRO, PRCL, PREP, CONJ, ADV, etc.
    gender: Optional[str] = None  # male, female, neuter
    number: Optional[str] = None  # singular, plural
    case: Optional[str] = None    # nomn, gent, datv, accs, ablt, loct, etc.
    animacy: Optional[str] = None # anim, inan
    person: Optional[str] = None  # 1per, 2per, 3per
    tense: Optional[str] = None   # pres, past, futr
    raw_tag: str = ""


@dataclass(frozen=True)
class TokenInfo:
    span: SourceSpan
    morph: MorphAnalysis
    syntax_head: Optional[int] = None
    syntax_rel: Optional[str] = None


@dataclass
class SentenceInfo:
    span: SourceSpan
    tokens: List[TokenInfo]


class RussianNLPAdapter:
    """Singleton-friendly adapter for Russian NLP processing."""

    _instance: Optional[RussianNLPAdapter] = None

    def __init__(self):
        self.morph_analyzer = pymorphy3.MorphAnalyzer()
        self.segmenter = Segmenter()
        self.emb = NewsEmbedding()
        self.morph_tagger = NewsMorphTagger(self.emb)
        self.syntax_parser = NewsSyntaxParser(self.emb)
        self.ner_tagger = NewsNERTagger(self.emb)

    @classmethod
    def get_instance(cls) -> RussianNLPAdapter:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def split_sentences(self, text: str) -> List[SourceSpan]:
        """Splits raw text into sentences while preserving exact character spans."""
        spans: List[SourceSpan] = []
        for sent in razdel.sentenize(text):
            span = SourceSpan(start=sent.start, end=sent.stop, text=sent.text)
            # Invariant check
            assert text[span.start:span.end] == span.text, f"Sentence span mismatch: '{span.text}' != '{text[span.start:span.end]}'"
            spans.append(span)
        return spans

    def split_tokens(self, text: str, offset_start: int = 0) -> List[SourceSpan]:
        """Tokenizes text preserving exact character spans relative to full text."""
        spans: List[SourceSpan] = []
        for tok in razdel.tokenize(text):
            start = offset_start + tok.start
            end = offset_start + tok.stop
            spans.append(SourceSpan(start=start, end=end, text=tok.text))
        return spans

    def analyze_token_morph(self, word: str) -> MorphAnalysis:
        """Analyzes a single word using PyMorphy3."""
        parses = self.morph_analyzer.parse(word)
        if not parses:
            return MorphAnalysis(lemma=word.lower(), pos="UNKNOWN")
        best = parses[0]
        # Disambiguate loan words ending in 'мен' (e.g. стикмен, бармен, супермен) that PyMorphy misparses as femn plur
        if word.lower().endswith("мен") and any(p.tag.POS == "NOUN" and p.tag.gender == "masc" for p in parses):
            best = next(p for p in parses if p.tag.POS == "NOUN" and p.tag.gender == "masc")
        tag = best.tag

        gender_map = {"masc": "male", "femn": "female", "neut": "neuter"}
        gender = gender_map.get(tag.gender) if tag.gender else None

        number_map = {"sing": "singular", "plur": "plural"}
        number = number_map.get(tag.number) if tag.number else None

        pos_str = str(tag.POS) if tag.POS else "UNKNOWN"
        animacy_str = str(tag.animacy) if tag.animacy else None
        person_str = str(tag.person) if tag.person else None
        tense_str = str(tag.tense) if tag.tense else None
        case_str = str(tag.case) if tag.case else None

        return MorphAnalysis(
            lemma=best.normal_form,
            pos=pos_str,
            gender=gender,
            number=number,
            case=case_str,
            animacy=animacy_str,
            person=person_str,
            tense=tense_str,
            raw_tag=str(tag),
        )

    def normalize_proper_name(self, name: str) -> str:
        """Normalizes Russian proper name, patronymic, or surname across inflected case forms."""
        if not name or not name.strip():
            return name
        # Do not mutilate hyphenated foreign place names or compound titles (e.g. Шахр-э-Сухте, Эль-Пасо, Санкт-Петербург)
        if "-" in name:
            parts = name.split("-")
            return "-".join(p.capitalize() if i == 0 or len(p) > 2 else p for i, p in enumerate(parts))
        tokens = [w for w in re.findall(r"\b\w+\b", name, re.UNICODE)]
        if not tokens:
            return name
        normalized_words = []
        for i, w in enumerate(tokens):
            parses = self.morph_analyzer.parse(w)
            if not parses:
                normalized_words.append(w.capitalize() if i == 0 else w)
                continue
            # Prioritize first name (Name) tag over surname/patronymic tags
            prop_cand = next((p for p in parses if "Name" in p.tag), None) or next((p for p in parses if "Surn" in p.tag or "Patr" in p.tag), None)
            lemma = prop_cand.normal_form if prop_cand else parses[0].normal_form
            normalized_words.append(lemma.capitalize() if i == 0 else lemma)
        return " ".join(normalized_words) if normalized_words else name

    def normalize_non_person_name(self, name: str) -> str:
        """Inflect nominal names without turning unknown proper names into verbs."""
        if '-' in name:
            return name
        tokens = re.findall(r'\b\w+\b', name, re.UNICODE)
        analyses = [self.morph_analyzer.parse(token) for token in tokens]
        noun_gender = next((p[0].tag.gender for p in analyses if p and p[0].tag.POS == 'NOUN'
                            and self.morph_analyzer.word_is_known(p[0].word)), None)
        output = []
        for index, token in enumerate(tokens):
            parses = analyses[index]
            nominal = parses and parses[0].tag.POS in ('NOUN', 'ADJF', 'ADJS', 'NUMR') and (
                'Name' not in parses[0].tag or self.morph_analyzer.word_is_known(token))
            value = parses[0].normal_form if nominal else token
            if nominal and parses[0].tag.POS == 'ADJF' and noun_gender:
                agreed = parses[0].inflect({'nomn', 'sing', noun_gender})
                if agreed:
                    value = agreed.word
            output.append(value.capitalize() if index == 0 or token[0].isupper() else value)
        return ' '.join(output) if output else name

    def analyze_document_natasha(self, text: str) -> Doc:
        """Runs Natasha full pipeline (segmentation, morph, syntax, NER)."""
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_morph(self.morph_tagger)
        doc.parse_syntax(self.syntax_parser)
        doc.tag_ner(self.ner_tagger)
        for span in doc.spans:
            if span.type == "PER":
                span.normal = self.normalize_proper_name(span.text)
            else:
                span.normal = self.normalize_non_person_name(span.text)
        return doc

    def parse_document(self, text: str) -> ParsedDocument:
        """Parses a document through the full NLP pipeline once, producing an indexed ParsedDocument."""
        return ParsedDocument.from_text(text, self)


import bisect


@dataclass
class ParsedDocument:
    """Immutable parsed syntax and token context for a story document."""
    text: str
    natasha_doc: Doc
    tokens: List[Any]
    spans: List[Any]
    sentences: List[SourceSpan]

    # Pre-indexed lookups for O(1) syntax navigation
    token_by_id: Dict[str, Any]
    children_by_head_id: Dict[str, List[Any]]
    tokens_by_span: Dict[Tuple[int, int], Any]
    _char_to_token: List[Optional[Any]]
    _token_starts: List[int]
    _token_stops: List[int]

    @classmethod
    def from_text(cls, text: str, nlp: RussianNLPAdapter) -> ParsedDocument:
        natasha_doc = nlp.analyze_document_natasha(text)
        sentences = nlp.split_sentences(text)
        tokens = list(natasha_doc.tokens) if hasattr(natasha_doc, "tokens") else []
        spans = list(natasha_doc.spans) if hasattr(natasha_doc, "spans") else []

        token_by_id = {t.id: t for t in tokens}
        children_by_head_id: Dict[str, List[Any]] = {}
        tokens_by_span: Dict[Tuple[int, int], Any] = {}
        
        # O(1) character offset lookup array
        char_to_token: List[Optional[Any]] = [None] * (len(text) + 1)
        token_starts: List[int] = []
        token_stops: List[int] = []

        for t in tokens:
            tokens_by_span[(t.start, t.stop)] = t
            token_starts.append(t.start)
            token_stops.append(t.stop)
            for offset in range(max(0, t.start), min(len(char_to_token), t.stop)):
                char_to_token[offset] = t
            if t.head_id:
                children_by_head_id.setdefault(t.head_id, []).append(t)

        return cls(
            text=text,
            natasha_doc=natasha_doc,
            tokens=tokens,
            spans=spans,
            sentences=sentences,
            token_by_id=token_by_id,
            children_by_head_id=children_by_head_id,
            tokens_by_span=tokens_by_span,
            _char_to_token=char_to_token,
            _token_starts=token_starts,
            _token_stops=token_stops,
        )

    def find_token_at(self, offset: int) -> Optional[Any]:
        """Finds token covering the given character offset in O(1) time."""
        if 0 <= offset < len(self._char_to_token):
            return self._char_to_token[offset]
        return None

    def find_tokens_in_span(self, start: int, end: int) -> List[Any]:
        """Finds all tokens overlapping the given character span [start, end) via bisect search."""
        if not self.tokens or start >= end:
            return []
        # Find first token whose stop > start
        left_idx = bisect.bisect_right(self._token_stops, start)
        result: List[Any] = []
        for idx in range(left_idx, len(self.tokens)):
            t = self.tokens[idx]
            if t.start >= end:
                break
            result.append(t)
        return result

    def get_token_by_id(self, token_id: str) -> Optional[Any]:
        """Looks up token by Natasha token id in O(1) time."""
        return self.token_by_id.get(token_id)

    def get_parent_token(self, token: Any) -> Optional[Any]:
        """Gets governing syntax parent token in O(1) time."""
        if not token or not token.head_id:
            return None
        return self.token_by_id.get(token.head_id)

    def get_children_tokens(self, token: Any) -> List[Any]:
        """Gets syntax dependent children tokens in O(1) time."""
        if not token:
            return []
        return self.children_by_head_id.get(token.id, [])


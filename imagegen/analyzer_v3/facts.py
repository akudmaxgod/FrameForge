"""Fact Extraction Layer for Story Analyzer V3.

Modular Architecture:
- SpecificationFactExtractor: Extracts numeric and technical specifications (power, clearance, torque, battery, etc.)
  with syntactic dependency and nearest-noun phrase subject binding.
- ActionFactExtractor: Extracts verbal action/state facts (subject -> predicate -> object)
  ensuring subjects are never misidentified as objects.
- FactExtractorV3: Facade orchestrator integrating with ParsedDocument and DocumentContext.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from imagegen.analyzer_v3.context import DocumentContext
from imagegen.analyzer_v3.models import (
    DocumentIR,
    EntityIR,
    EntityType,
    EventIR,
    FactIR,
    MentionIR,
    SourceSpan,
    SyntacticRole,
)
from imagegen.analyzer_v3.nlp import ParsedDocument, RussianNLPAdapter


# Numerical/Technical Specs Regex
RE_SPEC_VALUE = re.compile(
    r"\b\d+[\d\s.,]*(?:л\.с\.|квт|нм|мм|см|м|км/с|км|гб|мб|тб|мп|гц|мач|бар|г/см³|%|°c|руб|долларов|евро|\$|€)(?=\s|[.,;!?]|$)",
    re.IGNORECASE
)


class SpecificationFactExtractor:
    """Extracts numerical and technical specification assertions with strict subject attribution."""

    def __init__(self, nlp: RussianNLPAdapter):
        self.nlp = nlp

    def extract_specs(
        self,
        event: EventIR,
        doc_ir: DocumentIR,
        ctx: DocumentContext,
        parsed_doc: ParsedDocument,
        entity_map: Dict[str, EntityIR],
    ) -> List[FactIR]:
        """Extracts specification FactIR instances for a single event."""
        facts: List[FactIR] = []
        ev_text = event.span.text

        spec_matches = list(RE_SPEC_VALUE.finditer(ev_text))
        for m in spec_matches:
            spec_val = m.group(0).strip()
            spec_start = event.span.start + m.start()
            spec_end = event.span.start + m.end()
            spec_span = SourceSpan(start=spec_start, end=spec_end, text=spec_val)

            clause_subj_id, property_name = self._resolve_spec_subject_and_property(
                spec_start, spec_end, spec_val, event, doc_ir, ctx, entity_map, parsed_doc
            )

            f_id = f"fact_{event.event_id}_{len(facts)}"
            diagnostics = []
            if clause_subj_id is None:
                diagnostics.append("unresolved_spec_subject")

            facts.append(
                FactIR(
                    fact_id=f_id,
                    subject_id=clause_subj_id,
                    predicate=property_name,
                    object_value=spec_val,
                    source_span=spec_span,
                    confidence=0.95 if clause_subj_id else 0.70,
                    section_id=event.section_id,
                    event_id=event.event_id,
                    diagnostics=diagnostics,
                )
            )

        return facts

    def _resolve_spec_subject_and_property(
        self,
        spec_start: int,
        spec_end: int,
        spec_val: str,
        event: EventIR,
        doc_ir: DocumentIR,
        ctx: DocumentContext,
        entity_map: Dict[str, EntityIR],
        parsed_doc: ParsedDocument,
    ) -> Tuple[Optional[str], str]:
        """Resolves property name and governing subject entity via syntax dependencies and context."""
        tok_by_id = parsed_doc.token_by_id
        natasha_doc = parsed_doc.natasha_doc

        # 1. Determine Property Name (Predicate)
        pre_toks = [t for t in parsed_doc.tokens if event.span.start <= t.start and t.stop <= spec_start]
        recent_words = [t.text.lower() for t in pre_toks[-4:]] if pre_toks else []
        recent_text = " ".join(recent_words)
        spec_lower = spec_val.lower()

        property_name = "характеристика"
        if ("крутящий" in recent_words and "момент" in recent_words) or ("крутящего" in recent_words and "момента" in recent_words):
            property_name = "крутящий момент"
        elif "момент" in recent_words or "момента" in recent_words:
            property_name = "крутящий момент"
        elif any(w in recent_words for w in ("клиренс", "клиренсом", "просвет")):
            property_name = "клиренс"
        elif any(w in recent_words for w in ("мощность", "мощностью", "мотор", "мотором", "двигатель", "двигателя")):
            property_name = "мощность"
        elif "расход" in recent_text:
            property_name = "расход"
        elif "разгон" in recent_text:
            property_name = "разгон"
        elif "скорост" in recent_text:
            property_name = "скорость"
        elif "аккумулятор" in recent_text or "батаре" in recent_text or "емкост" in recent_text:
            property_name = "аккумулятор"
        elif "экран" in recent_text or "дисплей" in recent_text or "диагональ" in recent_text:
            property_name = "экран"
        elif "частот" in recent_text or "герц" in recent_text:
            property_name = "частота обновления"
        elif "разрешен" in recent_text or "матриц" in recent_text:
            property_name = "разрешение"
        elif "цен" in recent_text or "стоимост" in recent_text:
            property_name = "цена"
        elif "объем" in recent_text or "вместимост" in recent_text:
            property_name = "объем"
        # Unit-based inference
        elif "л.с." in spec_lower or "квт" in spec_lower:
            property_name = "мощность"
        elif "нм" in spec_lower:
            property_name = "крутящий момент"
        elif "мм" in spec_lower or "см" in spec_lower:
            property_name = "клиренс"
        elif "мач" in spec_lower:
            property_name = "аккумулятор"
        elif "гц" in spec_lower:
            property_name = "частота обновления"
        elif "мп" in spec_lower:
            property_name = "разрешение"
        elif "км/ч" in spec_lower or "км/с" in spec_lower:
            property_name = "скорость"

        # 2. Determine Governing Subject Entity
        resolved_subject_id: Optional[str] = None

        cur_sec = ctx.get_section(event.section_id)
        sec_ent_id = None
        if cur_sec:
            if cur_sec.subject_entity_id:
                sec_ent_id = cur_sec.subject_entity_id
            elif cur_sec.title:
                t_lower = cur_sec.title.lower()
                matched = next(
                    (e for e in entity_map.values() if e.canonical_name.lower() in t_lower or any(a.lower() in t_lower for a in e.aliases)),
                    None
                )
                if matched and matched.entity_type in (EntityType.VEHICLE, EntityType.OBJECT):
                    sec_ent_id = matched.entity_id

        # Event-level vehicles
        ev_mentions = ctx.get_event_mentions(event.event_id)
        ev_vehicles = [
            ctx.get_entity_for_mention(m.mention_id)
            for m in ev_mentions
            if ctx.get_entity_for_mention(m.mention_id) and ctx.get_entity_for_mention(m.mention_id).entity_type in (EntityType.VEHICLE, EntityType.OBJECT)
        ]

        if sec_ent_id and len(ev_vehicles) <= 1:
            resolved_subject_id = sec_ent_id

        # Step A: Syntax Dependency Edge within same section
        if not resolved_subject_id:
            pre_tok = pre_toks[-1] if pre_toks else None
            if pre_tok:
                head_tok = tok_by_id.get(pre_tok.head_id)
                if head_tok:
                    target_m = next(
                        (m for m in doc_ir.mentions if m.span.start <= head_tok.start and head_tok.stop <= m.span.end and m.section_id == event.section_id),
                        None
                    )
                    if target_m:
                        ent = ctx.get_entity_for_mention(target_m.mention_id)
                        if ent and ent.entity_type in (EntityType.VEHICLE, EntityType.OBJECT, EntityType.PERSON):
                            resolved_subject_id = ent.entity_id

                    if not resolved_subject_id and head_tok.pos in ("VERB", "AUX"):
                        verb_children = parsed_doc.get_children_tokens(head_tok)
                        nsubj_tok = next((t for t in verb_children if t.rel in ("nsubj", "nsubj:pass")), None)
                        if nsubj_tok:
                            target_m = next(
                                (m for m in doc_ir.mentions if m.span.start <= nsubj_tok.start and nsubj_tok.stop <= m.span.end and m.section_id == event.section_id),
                                None
                            )
                            if target_m:
                                ent = ctx.get_entity_for_mention(target_m.mention_id)
                                if ent and ent.entity_type in (EntityType.VEHICLE, EntityType.OBJECT, EntityType.PERSON):
                                    resolved_subject_id = ent.entity_id

        # Step B: Nearest Preceding Subject/Object Noun Phrase
        if not resolved_subject_id:
            sec_mentions = ctx.get_section_mentions(event.section_id)
            preceding_mentions = [m for m in sec_mentions if m.span.end <= spec_start]
            for m in reversed(preceding_mentions):
                ent = ctx.get_entity_for_mention(m.mention_id)
                if ent and ent.entity_type in (EntityType.VEHICLE, EntityType.OBJECT, EntityType.PERSON):
                    resolved_subject_id = ent.entity_id
                    break

        # Step C: Fallback to section subject
        if not resolved_subject_id and sec_ent_id:
            resolved_subject_id = sec_ent_id

        return resolved_subject_id, property_name


class ActionFactExtractor:
    """Extracts structured verbal action/state facts ensuring subject non-identity with object."""

    def __init__(self, nlp: RussianNLPAdapter):
        self.nlp = nlp

    def extract_actions(
        self,
        event: EventIR,
        ctx: DocumentContext,
        start_fact_idx: int,
        parsed_doc: ParsedDocument,
    ) -> List[FactIR]:
        """Extracts action FactIR assertions for an event."""
        facts: List[FactIR] = []
        ev_text = event.span.text
        ev_mentions = ctx.get_event_mentions(event.event_id)

        # 1. Find subject entity
        subject_ent_id: Optional[str] = None
        if event.actor_ids:
            subject_ent_id = event.actor_ids[0]
        else:
            subj_m = next((m for m in ev_mentions if m.syntactic_role == SyntacticRole.SUBJECT), None)
            if subj_m:
                ent = ctx.get_entity_for_mention(subj_m.mention_id)
                if ent:
                    subject_ent_id = ent.entity_id

        # Collect subject spans to guarantee subject is never mistaken for object
        subject_spans: List[SourceSpan] = []
        for m in ev_mentions:
            ent = ctx.get_entity_for_mention(m.mention_id)
            if ent and ent.entity_id == subject_ent_id:
                subject_spans.append(m.span)
            elif m.syntactic_role == SyntacticRole.SUBJECT:
                subject_spans.append(m.span)

        # 2. Extract event tokens
        ev_toks = parsed_doc.find_tokens_in_span(event.span.start, event.span.end)

        # 3. Find governing verb token
        verb_tok = None
        if ev_toks:
            for t in ev_toks:
                if t.rel == "root" and t.pos in ("VERB", "INFN", "AUX"):
                    verb_tok = t
                    break
            if not verb_tok:
                for t in ev_toks:
                    if t.pos in ("VERB", "INFN"):
                        verb_tok = t
                        break

        main_verb_lemma = None
        if verb_tok:
            main_verb_lemma = self.nlp.analyze_token_morph(verb_tok.text).lemma
        else:
            words = re.findall(r"\b[а-яёa-z0-9_-]+\b", ev_text, re.IGNORECASE)
            for w in words:
                morph = self.nlp.analyze_token_morph(w)
                if morph.pos in ("VERB", "INFN"):
                    main_verb_lemma = morph.lemma
                    break

        if main_verb_lemma:
            direct_obj_val: Optional[str] = None

            if verb_tok and ev_toks:
                verb_children = parsed_doc.get_children_tokens(verb_tok)

                def is_valid_obj_cand(tok: Any) -> bool:
                    if any(s.start <= tok.start and tok.stop <= s.end for s in subject_spans):
                        return False
                    if tok.rel in ("nsubj", "nsubj:pass"):
                        return False
                    t_morph = self.nlp.analyze_token_morph(tok.text)
                    if t_morph.lemma in ("год", "день", "время", "раз", "минута", "секунда", "час", "затем", "потом"):
                        return False
                    return True

                cand_tok = next((t for t in verb_children if t.rel == "obj" and is_valid_obj_cand(t)), None)
                if not cand_tok:
                    cand_tok = next((t for t in verb_children if t.rel == "iobj" and is_valid_obj_cand(t)), None)
                if not cand_tok:
                    cand_tok = next((t for t in verb_children if t.rel == "obl" and is_valid_obj_cand(t)), None)
                if not cand_tok:
                    cand_tok = next((t for t in verb_children if t.rel in ("xcomp", "ccomp") and is_valid_obj_cand(t)), None)

                if cand_tok:
                    cand_m = next((m for m in ev_mentions if m.span.start <= cand_tok.start and cand_tok.stop <= m.span.end), None)
                    if cand_m:
                        direct_obj_val = cand_m.normalized_text.lower() if cand_m.normalized_text else cand_tok.text.lower()
                    else:
                        cand_morph = self.nlp.analyze_token_morph(cand_tok.text)
                        direct_obj_val = cand_morph.lemma if cand_morph.lemma else cand_tok.text.lower()

            # Fallback for post-verbal oblique/accusative nouns
            if direct_obj_val is None and ev_toks:
                post_verb_toks = [t for t in ev_toks if verb_tok and t.start > verb_tok.start]
                for t in post_verb_toks:
                    if t.pos in ("NOUN", "PROPN") and t.rel not in ("nsubj", "nsubj:pass"):
                        if not any(s.start <= t.start and t.stop <= s.end for s in subject_spans):
                            morph_res = self.nlp.analyze_token_morph(t.text)
                            if morph_res.case in ("accs", "gent", "ablt", "loct", "datv"):
                                if morph_res.lemma not in ("год", "день", "время", "раз", "минута", "секунда", "час"):
                                    direct_obj_val = morph_res.lemma
                                    break

            f_id = f"fact_{event.event_id}_{start_fact_idx + len(facts)}"
            predicate = main_verb_lemma
            obj_val = direct_obj_val

            if verb_tok:
                f_span = SourceSpan(start=verb_tok.start, end=verb_tok.stop, text=verb_tok.text)
            else:
                v_match = re.search(r"\b" + re.escape(main_verb_lemma[:4]) + r"\w*", ev_text, re.IGNORECASE)
                if v_match:
                    f_span = SourceSpan(
                        start=event.span.start + v_match.start(),
                        end=event.span.start + v_match.end(),
                        text=v_match.group(0),
                    )
                else:
                    f_span = event.span

            facts.append(
                FactIR(
                    fact_id=f_id,
                    subject_id=subject_ent_id,
                    predicate=predicate,
                    object_value=obj_val,
                    source_span=f_span,
                    confidence=0.90 if subject_ent_id else 0.70,
                    section_id=event.section_id,
                    event_id=event.event_id,
                    diagnostics=["inanimate_event_no_actor"] if subject_ent_id is None else [],
                )
            )

        return facts


class FactExtractorV3:
    """Facade for fact extraction orchestration."""

    def __init__(self, nlp_adapter: Optional[RussianNLPAdapter] = None):
        self.nlp = nlp_adapter or RussianNLPAdapter.get_instance()
        self.spec_extractor = SpecificationFactExtractor(self.nlp)
        self.action_extractor = ActionFactExtractor(self.nlp)

    def extract_facts(
        self,
        story_text: str,
        doc_ir: DocumentIR,
        parsed_doc: Optional[ParsedDocument] = None,
    ) -> List[FactIR]:
        """Extracts structured facts across all events and populates ev.facts as single source of truth."""
        if parsed_doc is None:
            parsed_doc = self.nlp.parse_document(story_text)

        ctx = DocumentContext(doc_ir, max_len=len(story_text))
        facts: List[FactIR] = []
        entity_map = {ent.entity_id: ent for ent in doc_ir.entities}

        for ev in doc_ir.events:
            ev.facts = []  # Reset authoritative event facts

            # 1. Numerical & Technical specs
            spec_facts = self.spec_extractor.extract_specs(ev, doc_ir, ctx, parsed_doc, entity_map)
            for sf in spec_facts:
                ev.facts.append(sf)
                facts.append(sf)

            # 2. Action & state facts
            action_facts = self.action_extractor.extract_actions(ev, ctx, len(ev.facts), parsed_doc)
            for af in action_facts:
                ev.facts.append(af)
                facts.append(af)

        return facts

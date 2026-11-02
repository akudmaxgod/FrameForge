"""Entity and Mention Extraction Layer for Story Analyzer V3.

Modular Architecture:
- EntityMentionExtractor: Extracts mention spans (NER, pronouns, common nouns) and derives syntactic roles.
- EntityRegistry: Canonical entity normalization, alias tracking, and mention linking.
- PresenceClassifier: Syntactic modality tagging and event presence linking.
- EntityExtractorV3: Facade orchestrator.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.context import DocumentContext
from imagegen.analyzer_v3.models import (
    DocumentIR,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventEntityPresenceIR,
    EventIR,
    MentionIR,
    PresenceRole,
    SourceSpan,
    SyntacticRole,
)
from imagegen.analyzer_v3.nlp import ParsedDocument, RussianNLPAdapter


# Contextual cues for modality determination
RE_VOICE_ONLY = re.compile(
    r"\b(?:по\s+телефону|в\s+трубке|по\s+рации|по\s+связи|голос\s+в\s+динамике|"
    r"раздался\s+голос|прозвучал\s+голос|голос\s+\w+\s+прозвучал|голос\s+\w+\s+раздался|"
    r"голос\s+из\s+рации|голос\s+из\s+трубки|по\s+интеркому|по\s+громкой\s+связи|по\s+аудиосвязи)\b",
    re.IGNORECASE
)
RE_PERCEIVED_VOICE = re.compile(
    r"\b(?:почудилось|послышалось|в\s+голове|внутренний\s+голос|галлюцинация|голос\s+в\s+мыслях|привиделось)\b",
    re.IGNORECASE
)
RE_DEPICTED = re.compile(
    r"\b(?:(?:на\s+)?фотографи\w*|(?:на\s+)?портрет\w*|(?:на\s+)?картин\w*|(?:на\s+)?сним\w*|(?:на\s+)?экран\w*|"
    r"изображен\w*|(?:на\s+)?холст\w*|(?:в\s+)?рамк\w*|(?:на\s+)?рисунк\w*|бюст\w*|стату\w*)\b",
    re.IGNORECASE
)
RE_DECEASED = re.compile(
    r"\b(?:погиб\w*|умер\w*|умерш\w*|скончал\w*|скончавш\w*|покойн\w*|"
    r"вспомнил\s+об\s+умерш\w*|вспомнила\s+об\s+умерш\w*|память\s+о|могил\w*|памятник\w*)\b",
    re.IGNORECASE
)
RE_PHYSICAL_ACTION = re.compile(
    r"\b(?:вош[её]л|вошла|вошли|выш[её]л|вышла|вышли|сел|села|сели|встал|встала|встали|"
    r"подош[её]л|подошла|подошли|взял|взяла|взяли|открыл|открыла|открыли|улыбнулся|улыбнулась|улыбнулись|"
    r"посмотрел|посмотрела|посмотрели|шагнул|шагнула|шагнули|сделал|сделала|сделали|"
    r"бежал|бежала|бежали|стоял|стояла|стояли|шёл|шла|шли|ждал|ждала|ждали|спросил|спросила|спросили|"
    r"ответил|ответила|ответили|поднял|подняла|подняли|закрутил|закрутила|нажал|нажала|"
    r"прожил|прожила|прожили)\w*\b",
    re.IGNORECASE
)

# Domain Lexicons
VEHICLE_KEYWORDS = {
    "автомобиль", "машина", "внедорожник", "седан", "кроссовер", "грузовик",
    "автобус", "мотоцикл", "поезд", "самолет", "вертолет", "корабль", "катер",
    "land cruiser", "toyota", "bmw", "porsche", "tesla", "audi", "mercedes",
    "range rover", "volvo", "volkswagen", "nissan", "honda", "hyundai", "kia"
}

DEVICE_KEYWORDS = {
    "смартфон", "телефон", "планшет", "ноутбук", "компьютер", "камера",
    "фотоаппарат", "телескоп", "монитор", "экран", "дисплей", "кофемашина",
    "iphone", "samsung", "galaxy", "sony", "canon", "nikon", "fujifilm",
    "celestron", "sky-watcher", "delonghi", "philips"
}

HUMAN_ROLE_KEYWORDS = {
    "мама", "мать", "папа", "отец", "дедушка", "бабушка", "сын", "дочь", "дочка",
    "брат", "сестра", "дядя", "тетя", "друг", "подруга", "капитан", "профессор",
    "кузнец", "ученик", "мастер", "врач", "доктор", "офицер", "солдат", "король",
    "королева", "принц", "принцесса", "девочка", "мальчик", "мужчина", "женщина",
    "парень", "девушка", "старик", "старуха", "археолог", "водитель", "пилот",
    "ученый", "инженер", "сосед", "соседка", "учитель", "студент",
    "стикмен", "человечек", "робот", "персонаж", "герой", "следователь", "свидетель",
    "сержант", "командир", "император", "государь", "свидетельница",
    "муж", "супруг", "нотариус", "ребенок", "ребёнок", "дети"
}

ANIMAL_KEYWORDS = {
    "собака", "кот", "кошка", "пес", "пёс", "щенок", "котенок", "котёнок",
    "лошадь", "конь", "волк", "медведь", "лиса", "тигр", "лев", "орел", "орёл",
    "птица", "ворон", "ворона", "змея", "кит", "дельфин", "акула", "олень",
    "заяц", "кролик", "овца", "коза", "козел", "козёл", "баран", "ягненок", "ягнёнок",
    "корова", "бык", "теленок", "телёнок", "свинья", "поросенок", "поросёнок",
    "рыба", "скот", "животное", "зверь", "гусь", "утка", "курица", "петух",
    "мышь", "крыса", "обезьяна", "слон", "верблюд", "осел", "осёл", "мул"
}

PRONOUN_WORDS = {
    "я", "меня", "мне", "мной", "мною", "мы", "нас", "нам", "нами",
    "он", "оно", "она", "они",
    "его", "него", "ее", "её", "неё", "нее", "их", "них",
    "ему", "нему", "ей", "ней", "им", "ним",
    "ею", "нею", "ими", "ними", "собой", "собою",
    "нем", "нём",
    "тот", "та", "то", "те", "того", "тому", "той", "тех", "тем",
    "себя", "себе"
}

RECURRING_RELATION_ROLES = {
    "муж", "супруг", "жена", "супруга", "мать", "мама", "отец", "папа",
    "дочь", "сын", "бабушка", "дедушка",
}


class EntityRegistry:
    """Manages canonical entity definitions, alias tracking, and mention indexing."""

    def __init__(self):
        self.entity_map: Dict[str, EntityIR] = {}

    def register_or_link(
        self,
        canonical_key: str,
        canonical_name: str,
        alias: str,
        ent_type: EntityType,
        gender: Optional[str],
        mention_id: str,
        span: SourceSpan,
        modality: EntityModalityV3,
        attributes: Optional[List[str]] = None,
        confidence: float = 0.95,
    ) -> EntityIR:
        """Registers a new EntityIR or links an additional mention to an existing entity."""
        if canonical_key not in self.entity_map:
            ent_id = f"ent_{len(self.entity_map)}"
            ent = EntityIR(
                entity_id=ent_id,
                canonical_name=canonical_name,
                aliases=[alias],
                entity_type=ent_type,
                gender=gender,
                first_mention_start=span.start,
                mention_ids=[mention_id],
                modality=modality,
                confidence=confidence,
                attributes=list(attributes or []),
                evidence_spans=[span],
            )
            self.entity_map[canonical_key] = ent
            return ent
        else:
            ent = self.entity_map[canonical_key]
            if alias not in ent.aliases:
                ent.aliases.append(alias)
            if mention_id not in ent.mention_ids:
                ent.mention_ids.append(mention_id)
            ent.evidence_spans.append(span)
            if attributes:
                for attr in attributes:
                    if attr not in ent.attributes:
                        ent.attributes.append(attr)
            return ent

    def get_entities(self, mentions: List[MentionIR]) -> List[EntityIR]:
        """Finalizes entity list and ensures first_mention_start strictly equals min(mention starts)."""
        mention_by_id = {m.mention_id: m for m in mentions}
        entities = list(self.entity_map.values())
        for ent in entities:
            if ent.mention_ids:
                ent.first_mention_start = min(
                    mention_by_id[mid].span.start for mid in ent.mention_ids if mid in mention_by_id
                )
        return entities


class EntityMentionExtractor:
    """Extracts MentionIR spans and derives syntactic roles using ParsedDocument and DocumentContext."""

    def __init__(self, nlp: RussianNLPAdapter):
        self.nlp = nlp

    def _has_geographic_governor(self, start, end, parsed_doc):
        """Use the name's governing noun, including coordinated name lists.

        A nearby word 'city' cannot turn an acting researcher into a place.
        Likewise, a PER label or dictionary guess cannot turn a named volcano
        or a bishop's geographical jurisdiction into a physical person.
        """
        token = parsed_doc.find_token_at(start)
        geographic = {'город', 'поселение', 'деревня', 'провинция', 'область',
                      'вулкан', 'гора', 'озеро', 'река', 'страна', 'остров', 'комплекс'}
        visited = set()
        for _ in range(6):
            if not token or token.id in visited:
                break
            visited.add(token.id)
            parent = parsed_doc.get_parent_token(token)
            if not parent or token.rel not in ('appos', 'conj', 'nmod', 'flat:name', 'flat'):
                break
            lemma = self.nlp.analyze_token_morph(parent.text).lemma
            if lemma in geographic:
                return True
            if lemma in {'епископ', 'архиепископ', 'митрополит'}:
                return self.nlp.analyze_token_morph(token.text).case in {'gent', 'loct'}
            if parent.pos == 'VERB':
                break
            token = parent
        return False

    def _is_function_word(self, text, start, parsed_doc):
        if len(text.split()) != 1:
            return False
        token = parsed_doc.find_token_at(start)
        if token and (token.rel == "case" or getattr(token, "pos", None) in {"ADP", "CCONJ", "SCONJ", "PART", "PRON"}):
            return True
        return self.nlp.analyze_token_morph(text.lower()).pos in {"PREP", "CONJ", "PRCL", "NPRO"}

    def _is_standalone_discourse_exclamation(self, text, start, end, parsed_doc):
        """Reject non-name exclamations that have no predicate or grounded action."""
        left = max(text.rfind(mark, 0, start) for mark in (".", "!", "?", "\n")) + 1
        right_candidates = [pos for mark in (".", "!", "?", "\n") if (pos := text.find(mark, end)) >= 0]
        right = min(right_candidates) + 1 if right_candidates else len(text)
        fragment = text[left:right].strip(" \t\r\n—–-«»\"“”„")
        if not fragment.endswith("!"):
            return False
        tokens = parsed_doc.find_tokens_in_span(left, right)
        if any(getattr(token, "pos", None) in {"VERB", "AUX"} for token in tokens):
            return False
        words = re.findall(r"[A-Za-zА-Яа-яЁё-]+", text[start:end])
        is_explicit_person_name = bool(words) and all(any(
            "Name" in parse.tag or "Surn" in parse.tag or "Patr" in parse.tag
            for parse in self.nlp.morph_analyzer.parse(word)
        ) for word in words)
        if is_explicit_person_name:
            return False
        return True

    def extract_mentions_and_entities(
        self,
        story_text: str,
        parsed_doc: ParsedDocument,
        ctx: DocumentContext,
        registry: EntityRegistry,
    ) -> List[MentionIR]:
        """Extracts named entities, pronouns, and common nouns into MentionIR and registers them."""
        mentions: List[MentionIR] = []
        token_spans = self.nlp.split_tokens(story_text)
        natasha_doc = parsed_doc.natasha_doc
        tok_syntax_map = parsed_doc.tokens_by_span
        tok_by_id = parsed_doc.token_by_id

        # Map role prefixes before PER spans (e.g. "Доктор Иван", "Врач Пётр")
        per_spans = [s for s in natasha_doc.spans if s.type == "PER"]
        role_attached_ranges: Set[Tuple[int, int]] = set()
        role_attributes_by_per: Dict[Tuple[int, int], str] = {}

        for p_span in per_spans:
            pre_slice = story_text[max(0, p_span.start - 30):p_span.start].rstrip()
            if pre_slice:
                last_word = pre_slice.split()[-1] if pre_slice.split() else ""
                clean_word = re.sub(r"[^\w]", "", last_word).lower()
                morph_w = self.nlp.analyze_token_morph(clean_word)
                if morph_w.lemma in HUMAN_ROLE_KEYWORDS:
                    w_start = story_text.rfind(last_word, 0, p_span.start)
                    w_end = w_start + len(last_word)
                    role_attached_ranges.add((w_start, w_end))
                    role_attributes_by_per[(p_span.start, p_span.stop)] = morph_w.lemma

        # 1. Named Entity mentions from Natasha NER (PER, LOC, ORG)
        loc_texts = {s.text.casefold() for s in natasha_doc.spans if s.type == "LOC"}
        loc_normals = {s.normal.casefold() for s in natasha_doc.spans if s.type == "LOC" and s.normal}

        for s in natasha_doc.spans:
            if s.type == "PER":
                if self._is_function_word(s.text, s.start, parsed_doc):
                    continue
                if self._is_standalone_discourse_exclamation(story_text, s.start, s.stop, parsed_doc):
                    continue
            mention_type = "proper_noun"
            ent_type = EntityType.PERSON
            if s.type == "LOC":
                ent_type = EntityType.LOCATION
            elif s.type == "ORG":
                if s.text.lower() in VEHICLE_KEYWORDS or (s.normal and s.normal.lower() in VEHICLE_KEYWORDS):
                    ent_type = EntityType.VEHICLE
                elif s.text.lower() in DEVICE_KEYWORDS or (s.normal and s.normal.lower() in DEVICE_KEYWORDS):
                    ent_type = EntityType.OBJECT
                else:
                    ent_type = EntityType.ORGANIZATION
            elif s.type == "PER":
                # Disambiguate foreign place names / locations tagged erroneously as PER
                text_lower = s.text.lower()
                is_loc = (
                    s.text.casefold() in loc_texts
                    or (s.normal and s.normal.casefold() in loc_normals)
                    or re.search(r"\b(?:шахр|сити|град|бург|стан|виль|абад|таун)\b", text_lower)
                    or self._has_geographic_governor(s.start, s.stop, parsed_doc)
                )
                if is_loc:
                    ent_type = EntityType.LOCATION
                    loc_texts.add(s.text.casefold())
                else:
                    ent_type = EntityType.PERSON

            span = SourceSpan(start=s.start, end=s.stop, text=s.text)
            norm_text = (self.nlp.normalize_proper_name(s.text) if ent_type == EntityType.PERSON
                         else self.nlp.normalize_non_person_name(s.text))
            morph = self.nlp.analyze_token_morph(norm_text.split()[0] if norm_text else (s.text.split()[0] if s.text else ""))

            syn_role = self.determine_syntactic_role(s.start, s.stop, tok_syntax_map, morph.case, ent_type, tok_by_id)

            sec = ctx.find_section_at_offset(span.start)
            ev = ctx.find_event_at_offset(span.start)

            m_id = f"m_{len(mentions)}"
            mention = MentionIR(
                mention_id=m_id,
                text=s.text,
                normalized_text=norm_text,
                span=span,
                mention_type=mention_type,
                grammatical_gender=morph.gender,
                grammatical_number=morph.number,
                section_id=sec.section_id if sec else "sec_0",
                event_id=ev.event_id if ev else None,
                syntactic_role=syn_role,
            )
            mentions.append(mention)

            canonical_key = norm_text.lower()
            attributes = []
            if (s.start, s.stop) in role_attributes_by_per:
                attributes.append(role_attributes_by_per[(s.start, s.stop)])

            modality = EntityModalityV3.PHYSICAL if ent_type == EntityType.PERSON else (
                EntityModalityV3.LOCATION if ent_type == EntityType.LOCATION else (
                    EntityModalityV3.OBJECT if ent_type in (EntityType.VEHICLE, EntityType.OBJECT) else EntityModalityV3.ORGANIZATION
                )
            )

            registry.register_or_link(
                canonical_key=canonical_key,
                canonical_name=norm_text,
                alias=s.text,
                ent_type=ent_type,
                gender=morph.gender,
                mention_id=m_id,
                span=span,
                modality=modality,
                attributes=attributes,
                confidence=0.95,
            )

        # 1.5. Supplementary proper names (given names / patronymics) not caught by Natasha NER spans
        covered_ranges = [(s.start, s.stop) for s in natasha_doc.spans]
        for t in natasha_doc.tokens:
            if self._is_function_word(t.text, t.start, parsed_doc):
                continue
            if t.text and len(t.text) > 1 and t.text[0].isupper() and t.text.lower() not in PRONOUN_WORDS and not any(start <= t.start and t.stop <= stop for start, stop in covered_ranges):
                if self._is_standalone_discourse_exclamation(story_text, t.start, t.stop, parsed_doc):
                    continue
                parses = self.nlp.morph_analyzer.parse(t.text)
                if not parses:
                    continue
                best_p = parses[0]
                geographic_name = self._has_geographic_governor(t.start, t.stop, parsed_doc)
                pre_slice = story_text[:t.start].rstrip()
                is_sentence_initial = not pre_slice or pre_slice[-1] in {'.', '!', '?', '…', '—', '\n', '"', '«'}
                
                # Check validity of proper name candidate
                if geographic_name:
                    pass
                elif is_sentence_initial:
                    if ('Name' not in best_p.tag and 'Patr' not in best_p.tag) or best_p.tag.animacy != 'anim':
                        continue
                    if any(p.tag.animacy == 'inan' and p.score >= 0.2 for p in parses):
                        continue
                else:
                    if ('Name' not in best_p.tag and 'Patr' not in best_p.tag):
                        continue
                    if best_p.tag.animacy != 'anim' or 'Init' in best_p.tag or 'Abbr' in best_p.tag:
                        continue

                ent_type = EntityType.LOCATION if geographic_name else EntityType.PERSON
                span = SourceSpan(start=t.start, end=t.stop, text=t.text)
                norm_text = self.nlp.normalize_non_person_name(t.text) if geographic_name else self.nlp.normalize_proper_name(t.text)
                morph = self.nlp.analyze_token_morph(norm_text)
                syn_role = self.determine_syntactic_role(t.start, t.stop, tok_syntax_map, morph.case, ent_type, tok_by_id)
                sec = ctx.find_section_at_offset(span.start)
                ev = ctx.find_event_at_offset(span.start)
                m_id = f"m_{len(mentions)}"
                mention = MentionIR(
                    mention_id=m_id,
                    text=t.text,
                    normalized_text=norm_text,
                    span=span,
                    mention_type="proper_noun",
                    grammatical_gender=morph.gender,
                    grammatical_number=morph.number,
                    section_id=sec.section_id if sec else "sec_0",
                    event_id=ev.event_id if ev else None,
                    syntactic_role=syn_role,
                )
                mentions.append(mention)
                canonical_key = norm_text.lower()
                registry.register_or_link(
                    canonical_key=canonical_key,
                    canonical_name=norm_text,
                    alias=t.text,
                    ent_type=ent_type,
                    gender=morph.gender,
                    mention_id=m_id,
                    span=span,
                    modality=EntityModalityV3.LOCATION if geographic_name else EntityModalityV3.PHYSICAL,
                    attributes=[],
                    confidence=0.95,
                )


        # 2. Pronoun mentions
        for token_span in token_spans:
            word_lower = token_span.text.lower()
            if word_lower in PRONOUN_WORDS:
                morph = self.nlp.analyze_token_morph(word_lower)
                m_id = f"m_{len(mentions)}"
                syn_role = self.determine_syntactic_role(
                    token_span.start, token_span.end, tok_syntax_map, morph.case, EntityType.PERSON, tok_by_id
                )
                sec = ctx.find_section_at_offset(token_span.start)
                ev = ctx.find_event_at_offset(token_span.start)
                mentions.append(
                    MentionIR(
                        mention_id=m_id,
                        text=token_span.text,
                        normalized_text=morph.lemma,
                        span=token_span,
                        mention_type="pronoun",
                        grammatical_gender=morph.gender,
                        grammatical_number=morph.number,
                        section_id=sec.section_id if sec else "sec_0",
                        event_id=ev.event_id if ev else None,
                        syntactic_role=syn_role,
                    )
                )

        # 3. Common Noun & Role mentions
        for token_idx, token_span in enumerate(token_spans):
            if any(r[0] <= token_span.start and token_span.end <= r[1] for r in role_attached_ranges):
                continue
            if self._is_standalone_discourse_exclamation(
                story_text, token_span.start, token_span.end, parsed_doc
            ):
                continue

            word_lower = token_span.text.lower()
            morph = self.nlp.analyze_token_morph(word_lower)

            from .identity_invariants import is_abstract_person_trait
            if is_abstract_person_trait(token_span, story_text, parsed_doc):
                continue

            if morph.lemma in HUMAN_ROLE_KEYWORDS or word_lower in HUMAN_ROLE_KEYWORDS:
                role_lemma = word_lower if word_lower in HUMAN_ROLE_KEYWORDS else morph.lemma
                determiner = ""
                if token_idx > 0:
                    prev_word = token_spans[token_idx - 1].text.lower()
                    if prev_word in ("один", "другой", "второй", "третий", "каждый", "тот", "этот"):
                        determiner = prev_word

                self._register_common(
                    token_span,
                    f"{determiner.capitalize() + ' ' if determiner else ''}{role_lemma.capitalize()}",
                    EntityType.PERSON,
                    EntityModalityV3.PHYSICAL,
                    mentions,
                    registry,
                    ctx,
                    gender=morph.gender,
                    number=morph.number,
                    tok_syntax_map=tok_syntax_map,
                    case=morph.case,
                    is_distinct_occurrence=role_lemma not in RECURRING_RELATION_ROLES,
                )
            elif morph.lemma in VEHICLE_KEYWORDS:
                self._register_common(
                    token_span,
                    morph.lemma.capitalize(),
                    EntityType.VEHICLE,
                    EntityModalityV3.OBJECT,
                    mentions,
                    registry,
                    ctx,
                    tok_syntax_map=tok_syntax_map,
                    case=morph.case,
                )
            elif morph.lemma in DEVICE_KEYWORDS:
                self._register_common(
                    token_span,
                    morph.lemma.capitalize(),
                    EntityType.OBJECT,
                    EntityModalityV3.OBJECT,
                    mentions,
                    registry,
                    ctx,
                    tok_syntax_map=tok_syntax_map,
                    case=morph.case,
                )
            elif morph.lemma in ANIMAL_KEYWORDS or word_lower in ANIMAL_KEYWORDS:
                self._register_common(
                    token_span,
                    morph.lemma.capitalize(),
                    EntityType.ANIMAL,
                    EntityModalityV3.PHYSICAL,
                    mentions,
                    registry,
                    ctx,
                    gender=morph.gender,
                    number=morph.number,
                    tok_syntax_map=tok_syntax_map,
                    case=morph.case,
                )
            elif (morph.pos == "NOUN" and morph.animacy == "anim" and morph.gender != "neuter"
                  # The syntax model sometimes marks an unambiguous human
                  # noun inanimate. Use that label to resolve dictionary
                  # homographs (such as floor/Paul), not to erase actors.
                  and not (any(t.feats.get('Animacy') == 'Inan' for t in parsed_doc.find_tokens_in_span(token_span.start, token_span.end))
                           and any(p.tag.animacy == 'inan' for p in self.nlp.morph_analyzer.parse(token_span.text)))
                  and not any(start <= token_span.start and token_span.end <= stop
                              for start, stop in covered_ranges)
                  and not self._is_markdown_heading_token(story_text, token_span.start)):
                if morph.lemma in ANIMAL_KEYWORDS or word_lower in ANIMAL_KEYWORDS:
                    self._register_common(
                        token_span,
                        morph.lemma.capitalize(),
                        EntityType.ANIMAL,
                        EntityModalityV3.PHYSICAL,
                        mentions,
                        registry,
                        ctx,
                        gender=morph.gender,
                        number=morph.number,
                        tok_syntax_map=tok_syntax_map,
                        case=morph.case,
                    )
                elif morph.lemma == "друг":
                    # Reciprocal pronoun check: "друг от друга", "друг друга", "друг другу"
                    ctx_window = story_text[max(0, token_span.start - 15):min(len(story_text), token_span.end + 15)].lower()
                    if re.search(r"\bдруг\s+(?:друг[ауеом]|от\s+друг|с\s+друг|к\s+друг|у\s+друг|за\s+друг|о\s+друг)\b", ctx_window) or re.search(r"\b(?:от|с|к|у|за|перед|о)\s+друг\s+друг\w*\b", ctx_window):
                        continue
                    self._register_common(
                        token_span,
                        "Друг",
                        EntityType.PERSON,
                        EntityModalityV3.PHYSICAL,
                        mentions,
                        registry,
                        ctx,
                        gender=morph.gender,
                        number=morph.number,
                        tok_syntax_map=tok_syntax_map,
                        case=morph.case,
                    )
                elif any(p.tag.animacy == "inan" and p.score >= 0.3 for p in self.nlp.morph_analyzer.parse(token_span.text)) and morph.lemma not in HUMAN_ROLE_KEYWORDS:
                    # Inanimate common noun with accidental anim homograph (e.g. "руин")
                    continue
                else:
                    # Open-vocabulary human role extraction.
                    self._register_common(
                        token_span,
                        (token_span.text if morph.number == "plural" else morph.lemma).capitalize(),
                        EntityType.PERSON,
                        EntityModalityV3.PHYSICAL,
                        mentions,
                        registry,
                        ctx,
                        gender=morph.gender,
                        number=morph.number,
                        tok_syntax_map=tok_syntax_map,
                        case=morph.case,
                        is_distinct_occurrence=True,
                    )

        return mentions

    @staticmethod
    def _is_markdown_heading_token(text: str, offset: int) -> bool:
        line_start = text.rfind("\n", 0, offset) + 1
        line_end = text.find("\n", offset)
        if line_end < 0:
            line_end = len(text)
        return text[line_start:line_end].lstrip().startswith("#")

    def determine_syntactic_role(
        self,
        start: int,
        end: int,
        tok_syntax_map: Dict[Tuple[int, int], Any],
        case: Optional[str],
        ent_type: EntityType,
        tok_by_id: Optional[Dict[str, Any]] = None,
    ) -> SyntacticRole:
        """Derives SyntacticRole from syntax tree or morphological case."""
        tok = tok_syntax_map.get((start, end))
        if tok and getattr(tok, "rel", None):
            rel = tok.rel
            if rel in ("nsubj", "nsubj:pass"):
                return SyntacticRole.SUBJECT
            elif rel in ("appos", "flat:name", "flat") and tok_by_id:
                head = tok_by_id.get(tok.head_id)
                if head and head.rel in ("nsubj", "nsubj:pass"):
                    return SyntacticRole.SUBJECT
                elif head and head.rel in ("obj", "iobj"):
                    return SyntacticRole.OBJECT
                return SyntacticRole.ATTRIBUTE
            elif rel in ("obj", "iobj"):
                return SyntacticRole.OBJECT
            elif rel in ("nmod", "appos"):
                return SyntacticRole.ATTRIBUTE
            elif rel == "obl":
                return SyntacticRole.LOCATION if ent_type == EntityType.LOCATION else SyntacticRole.OBLIQUE

        if case == "nomn":
            return SyntacticRole.SUBJECT
        elif case in ("accs", "datv"):
            return SyntacticRole.OBJECT
        elif case in ("gent", "ablt", "loct"):
            return SyntacticRole.LOCATION if ent_type == EntityType.LOCATION else SyntacticRole.OBLIQUE

        return SyntacticRole.SUBJECT if ent_type in (EntityType.PERSON, EntityType.ANIMAL) else SyntacticRole.OBJECT

    def _register_common(
        self,
        token_span: SourceSpan,
        canonical_name: str,
        ent_type: EntityType,
        modality: EntityModalityV3,
        mentions: List[MentionIR],
        registry: EntityRegistry,
        ctx: DocumentContext,
        gender: Optional[str] = None,
        number: Optional[str] = None,
        tok_syntax_map: Optional[Dict[Tuple[int, int], Any]] = None,
        case: Optional[str] = None,
        is_distinct_occurrence: bool = False,
    ) -> None:
        syn_role = SyntacticRole.SUBJECT
        if tok_syntax_map:
            syn_role = self.determine_syntactic_role(
                token_span.start, token_span.end, tok_syntax_map, case, ent_type
            )

        sec = ctx.find_section_at_offset(token_span.start)
        ev = ctx.find_event_at_offset(token_span.start)

        m_id = f"m_{len(mentions)}"
        mention = MentionIR(
            mention_id=m_id,
            text=token_span.text,
            normalized_text=canonical_name,
            span=token_span,
            mention_type="common_noun",
            grammatical_gender=gender,
            grammatical_number=number,
            section_id=sec.section_id if sec else "sec_0",
            event_id=ev.event_id if ev else None,
            syntactic_role=syn_role,
        )
        mentions.append(mention)

        canonical_key = f"{canonical_name.lower()}_{token_span.start}" if is_distinct_occurrence else canonical_name.lower()
        registry.register_or_link(
            canonical_key=canonical_key,
            canonical_name=canonical_name,
            alias=token_span.text,
            ent_type=ent_type,
            gender=gender,
            mention_id=m_id,
            span=token_span,
            modality=modality,
            confidence=0.90,
        )


class PresenceClassifier:
    """Classifies mention modality and assigns presence records across events."""

    def __init__(self, nlp: RussianNLPAdapter):
        self.nlp = nlp

    def classify_and_link(
        self,
        doc_ir: DocumentIR,
        full_text: str,
        mentions: List[MentionIR],
        entities: List[EntityIR],
        parsed_doc: ParsedDocument,
    ) -> List[EventEntityPresenceIR]:
        """Classifies each mention in its event and links presences into EventIR and DocumentIR."""
        mention_to_ent = {mid: ent for ent in entities for mid in ent.mention_ids}
        event_map = {ev.event_id: ev for ev in doc_ir.events}
        all_presences: List[EventEntityPresenceIR] = []
        entity_modality_history: Dict[str, List[EntityModalityV3]] = {ent.entity_id: [] for ent in entities}

        for ev in doc_ir.events:
            ev.presences = []  # Reset authoritative collection

        for m in mentions:
            if not m.event_id or m.event_id not in event_map:
                continue

            ev = event_map[m.event_id]
            ent = mention_to_ent.get(m.mention_id)
            if not ent:
                continue

            modality, physical_presence, role = self._classify_mention_modality(
                m, ent, ev, parsed_doc, full_text
            )

            pres_id = f"pres_{ev.event_id}_{m.mention_id}"
            presence = EventEntityPresenceIR(
                presence_id=pres_id,
                event_id=ev.event_id,
                entity_id=ent.entity_id,
                mention_id=m.mention_id,
                modality=modality,
                physical_presence=physical_presence,
                role=role,
                evidence_spans=[m.span],
            )
            all_presences.append(presence)
            ev.presences.append(presence)
            entity_modality_history[ent.entity_id].append(modality)

            # Link into EventIR
            if ent.entity_id not in ev.mentioned_entity_ids:
                ev.mentioned_entity_ids.append(ent.entity_id)

            if physical_presence and role == PresenceRole.ACTOR and ent.entity_type in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE):
                if ent.entity_id not in ev.actor_ids:
                    ev.actor_ids.append(ent.entity_id)

            if ev.event_type == "comparison":
                if ent.entity_id not in ev.subject_entity_ids:
                    ev.subject_entity_ids.append(ent.entity_id)
                if len(ev.subject_entity_ids) > 1 and ent.entity_id not in ev.comparison_target_ids:
                    ev.comparison_target_ids.append(ent.entity_id)

        # Update entity summary modality
        for ent in entities:
            modalities = entity_modality_history.get(ent.entity_id, [])
            if any(m == EntityModalityV3.PHYSICAL for m in modalities):
                ent.modality = EntityModalityV3.PHYSICAL
            elif modalities:
                ent.modality = modalities[0]

        return all_presences

    def _classify_mention_modality(
        self,
        mention: MentionIR,
        entity: EntityIR,
        event: EventIR,
        parsed_doc: ParsedDocument,
        full_text: str,
    ) -> Tuple[EntityModalityV3, bool, PresenceRole]:
        """Syntactically classifies mention modality, physical presence, and role."""
        if entity.entity_type in (EntityType.VEHICLE, EntityType.OBJECT):
            role = PresenceRole.SUBJECT if mention.syntactic_role == SyntacticRole.SUBJECT else PresenceRole.OBJECT
            return EntityModalityV3.OBJECT, True, role
        elif entity.entity_type == EntityType.LOCATION:
            return EntityModalityV3.LOCATION, True, PresenceRole.MENTIONED
        elif entity.entity_type == EntityType.ORGANIZATION:
            return EntityModalityV3.ORGANIZATION, True, PresenceRole.MENTIONED

        # Person, animal, creature
        tok_by_id = parsed_doc.token_by_id
        natasha_doc = parsed_doc.natasha_doc

        m_tokens = parsed_doc.find_tokens_in_span(mention.span.start, mention.span.end)
        m_tok = m_tokens[-1] if m_tokens else None
        parent = tok_by_id.get(m_tok.head_id) if m_tok and m_tok.head_id in tok_by_id else None

        parent_lemma = self.nlp.analyze_token_morph(parent.text.lower()).lemma if parent else ""

        DEPICTION_NOUNS = {
            "фотография", "фото", "снимок", "портрет", "картина", "рисунок",
            "изображение", "холст", "экран", "монитор", "статуя", "бюст", "памятник", "кадр"
        }
        DEPICTION_VERBS = {"изобразить", "нарисовать", "запечатлеть", "показать"}
        VOICE_NOUNS = {"голос", "звук", "шёпот", "шепот", "крик", "возглас"}
        TRANSMISSION_LEMMAS = {
            "телефон", "рация", "трубка", "динамик", "громкоговоритель", "интерком",
            "мегафон", "радио", "связь", "аудиосвязь", "эфир"
        }
        MEMORY_VERBS = {"вспомнить", "вспоминать", "подумать", "думать", "помнить", "забыть"}

        # A person named inside quoted speech or a written note is a discourse
        # reference. Physical presence must be established by the surrounding
        # narration, never inferred from the quote itself.
        from imagegen.analyzer_v3.grounding import inside_direct_speech
        if inside_direct_speech(full_text, mention.span.start):
            return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED
        paragraph_start = max(full_text.rfind("\n\n", 0, mention.span.start), -2) + 2
        if re.match(r"\s*[—–-]\s*", full_text[paragraph_start:mention.span.start]):
            return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED
        # Screenplay dialogue often has no quotation marks: an uppercase
        # speaker heading is followed by a blank line and then the utterance.
        # A name used inside that utterance is an addressee/reference, not a
        # newly visible copy of the person.
        before_paragraph = full_text[:paragraph_start].rstrip()
        prior_blocks = [b.strip() for b in re.split(r"\n\s*\n", before_paragraph) if b.strip()]
        if prior_blocks:
            candidate = prior_blocks[-1]
            if candidate.startswith("(") and candidate.endswith(")") and len(prior_blocks) > 1:
                candidate = prior_blocks[-2]
            if (re.fullmatch(r"[A-ZА-ЯЁ][A-ZА-ЯЁ\s.-]{1,60}", candidate)
                    and mention.text.casefold() not in candidate.casefold()):
                return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED
        if "first_person_narrator" in entity.attributes:
            return EntityModalityV3.PHYSICAL, True, PresenceRole.ACTOR
        if re.search(r"\bникогда\b[^.!?]{0,40}\bне\s+рассказ\w*", event.span.text.lower()):
            return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        # 1. Syntactic Depiction
        if parent and (parent_lemma in DEPICTION_NOUNS or parent.text.lower() in DEPICTION_NOUNS):
            return EntityModalityV3.DEPICTED, False, PresenceRole.MENTIONED
        if parent and parent_lemma in DEPICTION_VERBS:
            return EntityModalityV3.DEPICTED, False, PresenceRole.MENTIONED
        if m_tok:
            children = parsed_doc.get_children_tokens(m_tok)
            for ch in children:
                ch_lemma = self.nlp.analyze_token_morph(ch.text.lower()).lemma
                if ch_lemma in DEPICTION_NOUNS:
                    return EntityModalityV3.DEPICTED, False, PresenceRole.MENTIONED

        # 2. Syntactic Voice
        if parent and (parent_lemma in VOICE_NOUNS or parent.text.lower() in VOICE_NOUNS):
            ev_text_lower = event.span.text.lower()
            has_channel = any(ch in ev_text_lower for ch in TRANSMISSION_LEMMAS)
            has_imagination = any(im in ev_text_lower for im in ("почудилось", "в голове", "галлюцинация", "в мыслях", "внутренний голос"))

            if has_channel:
                return EntityModalityV3.VOICE_ONLY, False, PresenceRole.SPEAKER
            elif has_imagination:
                return EntityModalityV3.PERCEIVED_VOICE, False, PresenceRole.MENTIONED
            else:
                return EntityModalityV3.VOICE_ONLY, False, PresenceRole.SPEAKER

        # 2b. Syntactic Perception / Hallucination
        curr = parent
        seen_toks: Set[str] = set()
        while curr and curr.id not in seen_toks and len(seen_toks) < 15:
            seen_toks.add(curr.id)
            curr_lemma = self.nlp.analyze_token_morph(curr.text.lower()).lemma
            if curr_lemma in ("почудиться", "показаться", "привидеться", "пригрезиться", "мерещиться") or curr.text.lower() in ("почудилось", "показалось", "привиделось"):
                return EntityModalityV3.PERCEIVED_VOICE, False, PresenceRole.MENTIONED
            curr = tok_by_id.get(curr.head_id) if curr.head_id in tok_by_id and curr.head_id != curr.id else None

        # 3. Deceased Mention
        if parent and parent_lemma in ("могила", "память"):
            return EntityModalityV3.DECEASED_MENTION, False, PresenceRole.MENTIONED
        if m_tok:
            children = parsed_doc.get_children_tokens(m_tok)
            for ch in children:
                ch_lemma = self.nlp.analyze_token_morph(ch.text.lower()).lemma
                if ch_lemma in ("погибший", "умерший", "покойный", "скончавшийся"):
                    return EntityModalityV3.DECEASED_MENTION, False, PresenceRole.MENTIONED

        # 4. Syntactic Memory / Topic Mention
        TOPIC_PREPOSITIONS = ("о", "об", "обо", "про", "насчёт", "насчет")
        if m_tok:
            children = parsed_doc.get_children_tokens(m_tok)
            prep_child = next((t for t in children if t.rel == "case" and t.text.lower() in TOPIC_PREPOSITIONS), None)
            if prep_child:
                return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        if parent and parent_lemma in MEMORY_VERBS:
            if m_tok and m_tok.rel in ("obl", "obj", "nmod"):
                return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        # 4b. Mention in inquiry question clause
        ev_text = event.span.text
        if "?" in ev_text:
            q_pos = ev_text.find("?")
            m_rel_start = mention.span.start - event.span.start
            if 0 <= m_rel_start < q_pos:
                q_clause = ev_text[:q_pos].lower()
                clean_q = re.sub(r"^[—–»«\s\-]+", "", q_clause)
                if any(clean_q.startswith(w) for w in ("где ", "куда ", "что с ", "почему ", "откуда ", "как там ")):
                    return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        # 5. Dialogue quotation mention
        m_rel_start = mention.span.start - event.span.start
        if 0 <= m_rel_start < len(ev_text):
            quote_match = re.search(r"^[—–-]\s*([^—–-]+?)[—–-]", ev_text)
            if quote_match and mention.span.start < event.span.start + quote_match.end(1):
                if mention.syntactic_role in (SyntacticRole.OBJECT, SyntacticRole.OBLIQUE):
                    return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        COGNITIVE_EPISTEMIC_VERBS = {
            "различать", "изучать", "исследовать", "заметить", "представить",
            "считать", "полагать", "знать", "установить", "понять", "спросить",
            "позволить", "показать", "рассказать", "обнаружить", "сравнивать",
            "сравнить", "потребовать", "означать", "называть", "назвать"
        }

        # 6. Physical presence
        # Names inside a sender attribution are references, not bodies in the room.
        sender_context = full_text[max(0, event.span.start - 160):mention.span.end].lower()
        if re.search(r"(?:сообщени\w*|мессенджер\w*|письм\w*)", sender_context) and re.search(r"\bот\s+(?:неко\w*\s+)?[«\"\s]*$", full_text[event.span.start:mention.span.start].lower()):
            return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        if (mention.syntactic_role != SyntacticRole.SUBJECT and parent
                and parent_lemma in COGNITIVE_EPISTEMIC_VERBS and not RE_PHYSICAL_ACTION.search(ev_text)):
            return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED

        if mention.syntactic_role == SyntacticRole.SUBJECT:
            return EntityModalityV3.PHYSICAL, True, PresenceRole.ACTOR

        mention_prefix = full_text[max(event.span.start, mention.span.start - 4):mention.span.start].lower()
        if re.search(r"\bс\s*$", mention_prefix) and RE_PHYSICAL_ACTION.search(ev_text):
            return EntityModalityV3.PHYSICAL, True, PresenceRole.ACTOR

        if RE_PHYSICAL_ACTION.search(ev_text) and mention.syntactic_role not in (SyntacticRole.ATTRIBUTE, SyntacticRole.LOCATION):
            return EntityModalityV3.PHYSICAL, True, PresenceRole.ACTOR

        return EntityModalityV3.MENTIONED, False, PresenceRole.MENTIONED


class EntityExtractorV3:
    """Facade for entity and mention extraction orchestration."""

    def __init__(self, nlp_adapter: Optional[RussianNLPAdapter] = None):
        self.nlp = nlp_adapter or RussianNLPAdapter.get_instance()
        self.mention_extractor = EntityMentionExtractor(self.nlp)
        self.presence_classifier = PresenceClassifier(self.nlp)

    def extract_entities_and_mentions(
        self,
        story_text: str,
        doc_ir: DocumentIR,
        parsed_doc: Optional[ParsedDocument] = None,
    ) -> Tuple[List[MentionIR], List[EntityIR], List[EventEntityPresenceIR]]:
        """Extracts MentionIR, EntityIR, and EventEntityPresenceIR objects and links them to events."""
        if parsed_doc is None:
            parsed_doc = self.nlp.parse_document(story_text)

        ctx = DocumentContext(doc_ir, max_len=len(story_text))
        registry = EntityRegistry()

        # Step 1: Extract mentions and entities
        mentions = self.mention_extractor.extract_mentions_and_entities(
            story_text, parsed_doc, ctx, registry
        )
        entities = registry.get_entities(mentions)
        from imagegen.analyzer_v3.grounding import (
            ensure_first_person_narrator,
            link_definite_common_references,
            link_supported_aliases,
        )
        entities = ensure_first_person_narrator(entities, mentions, doc_ir, story_text, self.nlp)
        entities = link_supported_aliases(entities, mentions, story_text)
        entities = link_definite_common_references(entities, mentions, story_text)

        # Step 2: Classify presences and modalities
        presences = self.presence_classifier.classify_and_link(
            doc_ir, story_text, mentions, entities, parsed_doc
        )

        return mentions, entities, presences

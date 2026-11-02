"""Deterministic geography and time-layer classification for Forgotten Cities."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from imagegen.nlp import DocumentIR, EntityType, SourceSpan

from .models import (
    EvidenceLevel,
    EventContextIR,
    GeographyMentionIR,
    GeographyType,
    PeoplePolicy,
    TimeLayer,
)


_RESEARCHER_CUE = re.compile(r"\b(?:археолог|исследователь|автор|профессор|историк|уч[её]ный)\b", re.I)
_ORIGIN_CUE = re.compile(r"\b(?:из|родом\s+из|происход\w*\s+из|привез[её]н\w*\s+из|источник\w*\s+в)\b", re.I)
_COMPARISON_CUE = re.compile(r"\b(?:как\s+в|подобно|сравн\w*\s+с|в\s+отличие\s+от)\b", re.I)
_MODERN_CONTEXT_CUE = re.compile(r"\b(?:сегодня|современн\w*|ныне|территори\w*|расположен\w*\s+в)\b", re.I)
_NATURAL = re.compile(r"\b(?:рек[аиуойе]|озер[оаеу]|берег\w*|пустын\w*|долин\w*|гор[аыеу]|лес\w*|оазис\w*)\b", re.I)
_SUBLOCATION = re.compile(r"\b(?:квартал\w*|дом\w*|двор\w*|мастерск\w*|кладбищ\w*|некропол\w*|улиц\w*|храм\w*|дворец\w*|помещени\w*|цех\w*)\b", re.I)
_CITY_CUE = re.compile(r"\b(?:город\w*|поселени\w*|цитадел\w*)\b", re.I)

_MUSEUM = re.compile(r"\b(?:музе[йяею]|витрин\w*|экспонат\w*|хранится\s+в\s+коллекции)\b", re.I)
_EXCAVATION = re.compile(r"\b(?:раскоп\w*|археолог\w*\s+(?:исслед\w*|работ\w*|обнаруж\w*)|транше\w*|раскопан\w*)\b", re.I)
_MAP = re.compile(r"\b(?:карт[аеуы]|маршрут\w*|путь\s+из|путь\s+в|расположен\w*\s+между)\b", re.I)
_MODERN_RUINS = re.compile(r"\b(?:сегодня|ныне|современн\w*|руин\w*|развалин\w*|сохранил\w*|остатк\w*)\b", re.I)
_HISTORICAL = re.compile(r"\b(?:до\s+н\.\s*э\.|тысячелети\w*|век\w*|жител\w*|ремесленник\w*|строил\w*|жил\w*|торговал\w*)\b", re.I)
_HYPOTHESIS = re.compile(r"\b(?:возможно|вероятно|предполага\w*|мог(?:ло|ли|ла)?|по\s+одной\s+из\s+версий|неизвестно)\b", re.I)
_PERSON_ACTION = re.compile(r"\b(?:мастер\w*|ремесленник\w*|земледел\w*|археолог\w*|женщин\w*|мужчин\w*|жител\w*)\b", re.I)


class ForgottenCitiesGeographyClassifier:
    """Classifies only grounded V3 mentions and never invents location cards."""

    def classify(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        primary_city_name: Optional[str] = None,
    ) -> List[GeographyMentionIR]:
        entity_map = {entity.entity_id: entity for entity in doc_ir.entities}
        primary_key = (primary_city_name or self._infer_primary_city(doc_ir)).casefold().strip()
        from .source_inventory import normal_name
        location_forms = {normal_name(m.text) for m in doc_ir.mentions
            if any(e.entity_type is EntityType.LOCATION and m.mention_id in e.mention_ids for e in doc_ir.entities)}
        results: List[GeographyMentionIR] = []

        for mention in doc_ir.mentions:
            entity = next((e for e in doc_ir.entities if mention.mention_id in e.mention_ids), None)
            context = story_text[max(0, mention.span.start - 90):min(len(story_text), mention.span.end + 60)]
            before = story_text[max(0, mention.span.start - 65):mention.span.start]
            kind, confidence, rationale = self._classify_one(
                mention.text, context, before, entity, primary_key, location_forms
            )
            if kind is None:
                continue
            results.append(GeographyMentionIR(
                geography_id=f"geo_{mention.mention_id}",
                text=mention.text,
                source_span=mention.span,
                geography_type=kind,
                entity_id=entity.entity_id if entity else None,
                event_id=mention.event_id,
                confidence=confidence,
                rationale=rationale,
            ))
        return results

    def classify_event_contexts(self, doc_ir: DocumentIR, story_text: str = '') -> List[EventContextIR]:
        contexts: List[EventContextIR] = []
        for event in doc_ir.events:
            text = event.span.text
            if _MUSEUM.search(text):
                layer = TimeLayer.MUSEUM_DISPLAY
            elif _EXCAVATION.search(text):
                layer = TimeLayer.EXCAVATION
            elif _MAP.search(text):
                layer = TimeLayer.MAP
            elif _MODERN_RUINS.search(text):
                layer = TimeLayer.MODERN_RUINS
            elif _HISTORICAL.search(text):
                layer = TimeLayer.HISTORICAL_RECONSTRUCTION
            else:
                layer = TimeLayer.ABSTRACT_EXPLANATION

            level = EvidenceLevel.ILLUSTRATIVE_HYPOTHESIS if _HYPOTHESIS.search(text) else (
                EvidenceLevel.SUPPORTED_RECONSTRUCTION if layer is TimeLayer.HISTORICAL_RECONSTRUCTION
                else EvidenceLevel.ESTABLISHED
            )
            people = PeoplePolicy.NONE
            if _PERSON_ACTION.search(text):
                people = PeoplePolicy.ANONYMOUS_ACTION
            if story_text:
                from .event_context import for_source
                narrative = for_source(story_text, text, event.span.start)
                if narrative:
                    if narrative.get('time'):
                        layer = TimeLayer(narrative['time']['layer'])
                    people = PeoplePolicy.ANONYMOUS_ACTION if narrative['participants'] else PeoplePolicy.NONE
            contexts.append(EventContextIR(event.event_id, layer, level, people))
        return contexts

    def enrich_metadata(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        primary_city_name: Optional[str] = None,
    ) -> Dict[str, object]:
        geography = self.classify(doc_ir, story_text, primary_city_name)
        contexts = self.classify_event_contexts(doc_ir, story_text)
        from .sequences import DocumentarySequenceBuilder
        sequences = DocumentarySequenceBuilder().build(
            doc_ir,
            story_text,
            contexts,
        )
        from .bible import ForgottenCitiesBibleBuilder
        built = ForgottenCitiesBibleBuilder().build(
            doc_ir, story_text, geography, sequences,
            primary_city_name or self._infer_primary_city(doc_ir),
        )
        from .models import ForgottenCitiesDocumentIR
        profile_document = ForgottenCitiesDocumentIR(
            city_bible=built["city_bible"],
            anchors=built["anchors"],
            claims=built["claims"],
            sequences=sequences,
            geography_mentions=geography,
            event_contexts=contexts,
        )
        from .preflight import ForgottenCitiesPreflightValidator
        preflight = ForgottenCitiesPreflightValidator().validate(profile_document, story_text)
        payload = {
            "schema_version": "forgotten_cities/1.0",
            "profile_type": "forgotten_cities",
            "primary_city_name": primary_city_name or self._infer_primary_city(doc_ir),
            "geography_mentions": [item.to_dict() for item in geography],
            "event_contexts": [item.to_dict() for item in contexts],
            "sequences": [item.to_dict() for item in sequences],
            "city_bible": built["city_bible"].to_dict(),
            "anchors": [item.to_dict() for item in built["anchors"]],
            "claims": [item.to_dict() for item in built["claims"]],
            "population_style_bible": built["population_style_bible"].to_dict(),
            "preflight": preflight,
        }
        doc_ir.metadata["forgotten_cities"] = payload
        return payload

    def _infer_primary_city(self, doc_ir: DocumentIR) -> str:
        from .source_inventory import declared_city
        story = getattr(doc_ir, 'text', '') or ' '.join(e.span.text for e in doc_ir.events)
        declared = declared_city(story)
        if declared:
            return declared
        candidates = []
        mention_map = {mention.mention_id: mention for mention in doc_ir.mentions}
        for entity in doc_ir.entities:
            if entity.entity_type is not EntityType.LOCATION:
                continue
            if re.fullmatch(r'(?:нижний|верхний|древний|этот|тот|главный)?\s*(?:город|цитадель|квартал|поселение)', entity.canonical_name, re.I):
                continue
            mentions = [mention_map[mid] for mid in entity.mention_ids if mid in mention_map]
            first = min((m.span.start for m in mentions), default=10**9)
            city_mentions = sum(1 for m in mentions if any(
                e.span.start <= m.span.start < e.span.end and _CITY_CUE.search(e.span.text)
                for e in doc_ir.events))
            name = entity.canonical_name
            # A dictionary can lemmatize Petra as the personal name Pyotr.
            # Prefer an explicit source introduction of this very location,
            # never an unrelated person's name or a guessed spelling.
            for mention in mentions:
                before = story[max(0, mention.span.start-30):mention.span.start]
                after = story[mention.span.end:mention.span.end+40]
                if (re.search(r'\bЭто\s+(?:был[аои]?\s+)?$', before, re.I)
                        and re.match(r'\s*[—–-]\s*(?:место|город|поселение)\b', after, re.I)):
                    name = mention.text
                    break
            candidates.append((2 * city_mentions + len(mentions), city_mentions, -first, name))
        return max(candidates, default=(0, 0, 0, ""))[3]

    def _classify_one(self, text, context, before, entity, primary_key, location_forms=()):
        key = text.casefold().strip()
        entity_type = entity.entity_type if entity else EntityType.UNKNOWN
        from .source_inventory import is_same_place, normal_name
        if primary_key and is_same_place(key, primary_key):
            return GeographyType.PRIMARY_CITY, 0.98, "source-declared city, including inflected forms"
        if re.search(r'\b(?:богин\w*|бог[ау]?|храм\w*)\s*$', before[-35:], re.I):
            return None, 0.0, "deity or named temple complement, not a researcher or standalone location"
        if normal_name(text) in location_forms and not _RESEARCHER_CUE.search(before[-28:]):
            entity_type = EntityType.LOCATION
        if entity_type is EntityType.PERSON or _RESEARCHER_CUE.search(before[-28:]):
            return GeographyType.RESEARCHER_NAME, 0.99, "human subject or researcher-title context"
        if primary_key and (key == primary_key or key in primary_key or primary_key in key):
            return GeographyType.PRIMARY_CITY, 0.98, "matches the primary city"
        if _ORIGIN_CUE.search(before[-45:]):
            return GeographyType.ORIGIN_REGION, 0.9, "origin construction before mention"
        if _COMPARISON_CUE.search(before[-55:]):
            return GeographyType.COMPARISON_LOCATION, 0.9, "comparison construction before mention"
        if _SUBLOCATION.search(text):
            if entity_type is EntityType.LOCATION or _SUBLOCATION.search(text):
                return GeographyType.PHYSICAL_SUBLOCATION, 0.82, "physical urban sublocation"
        if _NATURAL.search(text):
            return GeographyType.NATURAL_ENVIRONMENT, 0.9, "natural environment term"
        if _MODERN_CONTEXT_CUE.search(before[-50:]):
            return GeographyType.MODERN_CONTEXT_REGION, 0.82, "modern geographic context"
        if entity_type is EntityType.LOCATION:
            return GeographyType.MENTIONED_ONLY, 0.65, "grounded location without a scene transition"
        return None, 0.0, "not a geographic mention"

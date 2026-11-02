"""Source-grounded city bible, visual anchors, claims and population rules."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from imagegen.analyzer_v3.models import DocumentIR, SourceSpan

from .models import (
    AnchorType,
    CityBibleIR,
    ClaimType,
    DocumentarySequenceIR,
    GeographyMentionIR,
    GeographyType,
    HistoricalClaimIR,
    PopulationStyleBibleIR,
    TimeLayer,
    VisualizationPolicy,
    VisualAnchorIR,
)


_ARCHITECTURE = re.compile(r"\b(?:архитектур\w*|сырцов\w*|кирпич\w*|стен\w*|квартал\w*|дом\w*|двор\w*|храм\w*|улиц\w*|планиров\w*|портов\w*|канал\w*|бассейн\w*|причал\w*)\b", re.I)
_ENVIRONMENT = re.compile(r"\b(?:пустын\w*|долин\w*|рек\w*|озер\w*|берег\w*|гор\w*|равнин\w*|оазис\w*|климат\w*)\b", re.I)
_RUINS = re.compile(r"\b(?:руин\w*|развалин\w*|остатк\w*|сохранил\w*|раскоп\w*)\b", re.I)
_PERIOD = re.compile(r"\b(?:(?:[IVXLCDM]+|\d{1,2})\s*(?:век\w*|тысячелети\w*)[^.!?]{0,25}|\d{1,4}\s*(?:год\w*)?\s*до\s+н\.\s*э\.)", re.I)
_HYPOTHESIS = re.compile(r"\b(?:возможно|вероятно|предполага\w*|мог(?:ло|ли|ла)?|по\s+одной\s+из\s+версий|неизвестно)\b", re.I)
_INTERPRETATION = re.compile(r"\b(?:счита\w*|по\s+мнению|интерпрет\w*|указывает|свидетельств\w*)\b", re.I)
_ANALOGY = re.compile(r"\b(?:подобно|сравним\w*|напомина\w*|как\s+в)\b", re.I)
# A phrase that names an excavated fragment is not a district just because it
# contains "из мастерской" or another place preposition.
_OBJECT_PHRASE = re.compile(
    r"^\s*(?:обломок|осколок|фрагмент|часть|деталь|изделие|артефакт|сосуд|глаз|доска|печать|оттиск)\b",
    re.I,
)

_MATERIALS = {
    "базальт": re.compile(r"\bбазальт\w*\b", re.I),
    "коралловый заполнитель": re.compile(r"\bкоралл\w*\b", re.I),
    "сырцовый кирпич": re.compile(r"\bсырцов\w*\s+кирпич\w*", re.I),
    "обожжённый кирпич": re.compile(r"\bобожж[её]нн\w*\s+кирпич\w*", re.I),
    "камень": re.compile(r"\bкам(?:ень|ня|нем|енные|енн\w*)\b", re.I),
    "дерево": re.compile(r"\bдерев(?:о|янн\w*)\b", re.I),
    "глина": re.compile(r"\bглин\w*\b", re.I),
    "тростник": re.compile(r"\bтростник\w*\b", re.I),
}
_COLORS = {
    "охристый": re.compile(r"\bохр\w*\b", re.I),
    "песочный": re.compile(r"\bпесочн\w*\b", re.I),
    "терракотовый": re.compile(r"\bтерракот\w*\b", re.I),
    "лазурный": re.compile(r"\bлазур\w*\b", re.I),
    "чёрный": re.compile(r"\bч[её]рн\w*\b", re.I),
    "белый": re.compile(r"\bбел\w*\b", re.I),
}
_ARTIFACTS = [
    ("trepanned_skull", "Череп девочки с треугольным отверстием", re.compile(r"\bчереп\w*", re.I)),
    ("artificial_eye", "Искусственный глаз", re.compile(r"\bискусственн\w*\s+глаз\w*", re.I)),
    ("game_board", "Игровая доска", re.compile(r"\bигров\w*\s+доск\w*|\bфишк\w*\s+и\s+кост\w*", re.I)),
    ("animated_vessel", "Сосуд с изображениями козла", re.compile(r"\bсосуд\w*[^.!?]{0,80}\bкоз[её]л\w*|\bкоз[её]л\w*[^.!?]{0,80}\bсосуд\w*", re.I)),
    ("seal_impression", "Печать и глиняный оттиск", re.compile(r"\bпечат\w*[^.!?]{0,60}\b(?:глин\w*\s+)?оттиск\w*|\bглин\w*\s+оттиск\w*", re.I)),
    ("lapis_chain", "Лазуритовая производственная цепочка", re.compile(r"\bлазурит\w*", re.I)),
    ("carved_seal", "Резная стеатитовая печать", re.compile(r"\b(?:резн\w*\s+(?:каменн\w*\s+)?печат\w*|печат\w*[^.!?]{0,90}стеатит\w*)", re.I)),
    ("priest_king", "Бюст «Царь-жрец»", re.compile(r"\bбюст\w*|царь[ -]жрец", re.I)),
    ("dancing_figure", "Бронзовая статуэтка «Танцующая девочка»", re.compile(r"танцующ\w*\s+девоч\w*|бронзов\w*\s+статуэтк\w*", re.I)),
    ("carnelian_beads", "Сердоликовые биконические бусы", re.compile(r"\bбус(?:ы|ин\w*)\b[^.!?]{0,100}сердолик\w*|биконическ\w*\s+бусин\w*|хараппск\w*\s+бус\w*", re.I)),
    ("stone_weights", "Кубические каменные гирьки", re.compile(r"\b(?:каменн\w*\s+гир[еь]\w*|гир[еь]\w*[^.!?]{0,70}кубик\w*)", re.I)),
]


class ForgottenCitiesBibleBuilder:
    def build(
        self,
        doc_ir: DocumentIR,
        story_text: str,
        geography: List[GeographyMentionIR],
        sequences: List[DocumentarySequenceIR],
        primary_city_name: Optional[str] = None,
    ) -> Dict[str, object]:
        primary = [item for item in geography if item.geography_type is GeographyType.PRIMARY_CITY]
        city_name = primary_city_name or (primary[0].text if primary else "")
        city_id = "city_" + self._slug(city_name or "unknown")
        city_spans = self._unique_spans([item.source_span for item in primary])
        relevant_events = [event for event in doc_ir.events if city_name.casefold() in event.span.text.casefold()] if city_name else []
        city_spans = self._unique_spans(city_spans + [event.span for event in relevant_events])
        def city_evidence(event):
            text = event.span.text
            if '?' in text or _HYPOTHESIS.search(text) or re.search(r'\b(?:если|не\s+(?:было|стоял\w*|существовал\w*)|отсутств\w*)',text,re.I):
                return False
            mentions=[g for g in geography if event.span.start <= g.source_span.start < event.span.end]
            if any(g.geography_type is GeographyType.COMPARISON_LOCATION for g in mentions):return False
            foreign=any(g.geography_type in (GeographyType.MENTIONED_ONLY,GeographyType.ORIGIN_REGION,GeographyType.UNKNOWN_LOCATION) for g in mentions)
            return not foreign or any(g.geography_type is GeographyType.PRIMARY_CITY for g in mentions)
        architecture_events = [event for event in doc_ir.events if _ARCHITECTURE.search(event.span.text) and city_evidence(event)]
        physical_architecture = [event for event in architecture_events if re.search(
            r'\b(?:стро\w*|стоя\w*|наход\w*|распола\w*|состо\w*|соедин\w*|связыва\w*|проход\w*|возвед\w*|выруб\w*|проруб\w*|показыва\w*|ширин\w*|высот\w*|глубин\w*|кирпич\w*|каменн\w*|деревянн\w*)',event.span.text,re.I)
            and (not re.search(r'\b(?:уч[её]т\w*|плат[её]ж\w*|поступлен\w*|полномочи\w*|налог\w*)',event.span.text,re.I)
                 or re.search(r'\b(?:дом\w*|храм\w*|канал\w*|бассейн\w*|стен\w*|порт\w*)\s+(?:был\w*\s+)?(?:стоя\w*|наход\w*|состо\w*|постро\w*|распола\w*|соедин\w*|связыва\w*)',event.span.text,re.I))]
        environment_events = [event for event in doc_ir.events if _ENVIRONMENT.search(event.span.text) and city_evidence(event)]
        ruin_events = [event for event in doc_ir.events if _RUINS.search(event.span.text) and city_evidence(event)
            and re.search(r'\b(?:город\w*|дом\w*|храм\w*|порт\w*|стен\w*|сооружени\w*)',event.span.text,re.I)]
        period_matches = list(_PERIOD.finditer(story_text))
        construction_text=' '.join(e.span.text for e in physical_architecture if not re.search(
            r'\b(?:стел\w*|стату\w*|сосуд\w*|гир[еь]\w*|корабл\w*|бус\w*|печать|печат[ьи]\w*|череп\w*|пластинк\w*)',e.span.text,re.I))
        materials = [name for name, pattern in _MATERIALS.items() if pattern.search(construction_text)]
        colors = [name for name, pattern in _COLORS.items() if pattern.search(construction_text)]
        aliases = sorted({item.text for item in primary if item.text.casefold() != city_name.casefold()}) if city_name else []
        modern_regions = [item.text for item in geography if item.geography_type is GeographyType.MODERN_CONTEXT_REGION]
        evidence = self._unique_spans(city_spans + [e.span for e in architecture_events + environment_events + ruin_events])
        confidence = min(1.0, 0.35 + 0.12 * len(evidence) + (0.15 if materials else 0)) if city_name else 0.0
        city = CityBibleIR(
            city_id=city_id,
            canonical_name=city_name or "Неопределённый главный город",
            aliases=aliases,
            region=modern_regions[0] if modern_regions else "",
            period_start=period_matches[0].group(0).strip() if period_matches else None,
            period_end=period_matches[-1].group(0).strip() if len(period_matches) > 1 else None,
            environment_profile=" ".join(e.span.text for e in environment_events[:3]),
            architecture_profile=" ".join(e.span.text for e in physical_architecture[:4]),
            material_palette=materials,
            color_palette=colors,
            modern_ruins_profile=" ".join(e.span.text for e in ruin_events[:3]),
            reconstruction_rules=[
                "Use only source-backed architecture and materials.",
                "Mark reconstructed views as historical reconstruction.",
                "Keep modern ruins visually separate from reconstructed buildings.",
            ],
            forbidden_anachronisms=[
                "modern clothing", "electric lighting", "modern vehicles and tools",
                "modern glass windows", "unsupported writing or symbols",
            ],
            evidence_spans=evidence,
            confidence=confidence,
        )
        anchors = self._build_anchors(city, geography, doc_ir, story_text)
        claims = self._build_claims(city, doc_ir)
        self._link_sequences(sequences, anchors, claims, geography)
        population = self._population_bible(city, doc_ir)
        return {
            "city_bible": city,
            "anchors": anchors,
            "claims": claims,
            "population_style_bible": population,
        }

    def _build_anchors(self, city, geography, doc_ir, story_text):
        historical_count = sum(1 for e in doc_ir.events if not _RUINS.search(e.span.text))
        master_description = city.architecture_profile or city.environment_profile or f"Главный вид города {city.canonical_name}"
        if historical_count:
            master_description = (
                "Реконструкция главного вида по подтверждённым цитатам о месте: сохранять описанную в источнике "
                "архитектуру, материалы и состояние. Не добавлять дворы, улицы или наземные "
                "постройки, если источник их не подтверждает. Разные состояния и эпохи "
                "рассматривать отдельно, а не переносить общий шаблон на любой кадр. "
                "Не показывать современные руины как состояние исторической реконструкции. "
                + master_description
            )
        anchors = [VisualAnchorIR(
            anchor_id="anchor_city_master",
            anchor_type=AnchorType.CITY_MASTER,
            canonical_name=f"{city.canonical_name} — главный вид",
            description=master_description,
            time_layer=TimeLayer.HISTORICAL_RECONSTRUCTION if historical_count else TimeLayer.MODERN_RUINS,
            recurrence_count=max(1, len(city.evidence_spans)), importance=1.0,
            evidence_spans=city.evidence_spans, reference_required=True,
        )]
        if _RUINS.search(story_text):
            ruin_events = [event for event in doc_ir.events if _RUINS.search(event.span.text)
                and re.search(r'\b(?:город\w*|дом\w*|храм\w*|порт\w*|стен\w*|сооружени\w*)',event.span.text,re.I) and '?' not in event.span.text]
            anchors.append(VisualAnchorIR(
                anchor_id="anchor_city_modern_ruins",
                anchor_type=AnchorType.LANDSCAPE,
                canonical_name=f"{city.canonical_name} — современные руины",
                description=" ".join(event.span.text for event in ruin_events[:3]),
                time_layer=TimeLayer.MODERN_RUINS,
                recurrence_count=len(ruin_events), importance=0.9,
                evidence_spans=[event.span for event in ruin_events], reference_required=True,
            ))
        grouped = defaultdict(list)
        for item in geography:
            if item.geography_type in (GeographyType.PHYSICAL_SUBLOCATION, GeographyType.NATURAL_ENVIRONMENT):
                if len(item.text.strip()) < 3 or _OBJECT_PHRASE.search(item.text):
                    continue
                grouped[(item.geography_type, item.text.casefold())].append(item)
        for (kind, _), items in grouped.items():
            if len(items) < 2 and kind is GeographyType.PHYSICAL_SUBLOCATION:
                continue
            first = items[0]
            anchors.append(VisualAnchorIR(
                anchor_id=f"anchor_{self._slug(first.text)}",
                anchor_type=AnchorType.LANDSCAPE if kind is GeographyType.NATURAL_ENVIRONMENT else AnchorType.DISTRICT,
                canonical_name=first.text,
                description="; ".join(dict.fromkeys(item.source_span.text for item in items)),
                time_layer=TimeLayer.HISTORICAL_RECONSTRUCTION,
                recurrence_count=len(items), importance=min(1.0, 0.45 + len(items) * 0.15),
                evidence_spans=self._unique_spans([item.source_span for item in items]),
                reference_required=len(items) >= 2,
            ))
        for artifact_id, name, pattern in _ARTIFACTS:
            matches = [event for event in doc_ir.events if pattern.search(event.span.text)]
            if artifact_id == 'animated_vessel' and not matches:
                # Vessel and goat are commonly introduced in separate sentences.
                goat = re.search(r'коз[её]л\w*|козла\w*', story_text, re.I)
                if goat and re.search(r'сосуд\w*', story_text[max(0, goat.start()-400):goat.start()], re.I):
                    matches = [event for event in doc_ir.events if goat.start()-400 <= event.span.start <= goat.end()+500]
            if artifact_id == 'trepanned_skull':
                from .artifacts import skull_evidence, skull_name, skull_constraints
                evidence = skull_evidence(story_text)
                if not evidence:
                    continue
                name = skull_name(evidence[2])
                matches = [event for event in matches if evidence[0] <= event.span.end and event.span.start < evidence[1]]
            if not matches:
                continue
            description=" ".join(event.span.text for event in matches[:3])
            if artifact_id in ('carved_seal','priest_king','dancing_figure','carnelian_beads','stone_weights'):
                # Physical details often follow as «на ней», «фигурка» or «эти
                # бусины». Retain their source paragraph, not just the name.
                first=matches[0].span
                start=story_text.rfind('\n\n',0,first.start)+2
                if start==1:start=0
                start=max(start,first.start-300 if artifact_id=='carnelian_beads' else first.start)
                end=story_text.find('\n\n',first.end)
                if end<0:end=len(story_text)
                description=story_text[start:end].strip()[:3000]
            anchors.append(VisualAnchorIR(
                anchor_id=f"anchor_artifact_{artifact_id}", anchor_type=AnchorType.ARTIFACT,
                canonical_name=name, description=(evidence[2] + ' ' + ' '.join(skull_constraints(story_text))) if artifact_id == 'trepanned_skull' else description,
                time_layer=TimeLayer.MUSEUM_DISPLAY, recurrence_count=len(matches),
                importance=min(1.0, 0.65 + len(matches) * 0.1),
                evidence_spans=[event.span for event in matches], reference_required=True,
            ))
        # A named architectural monument can be central even when the general
        # entity extractor never promoted its common-noun name to a location.
        bath_events=[event for event in doc_ir.events if re.search(r'\b(?:Велик\w*\s+бассейн\w*|бассейн\w*)',event.span.text,re.I)]
        if re.search(r'Велик\w*\s+бассейн\w*',story_text,re.I) and len(bath_events)>1:
            evidence=[event.span for event in bath_events]
            description=' '.join(event.span.text for event in bath_events)
            for suffix,layer in [('реконструкция',TimeLayer.HISTORICAL_RECONSTRUCTION),('современные руины',TimeLayer.MODERN_RUINS)]:
                anchors.append(VisualAnchorIR(anchor_id='anchor_great_bath_'+layer.value,
                    anchor_type=AnchorType.DISTRICT,canonical_name=f'Великий бассейн — {suffix}',
                    description=description,time_layer=layer,recurrence_count=len(bath_events),importance=.9,
                    evidence_spans=evidence,reference_required=True))
        from .structures import build_structure_anchors
        anchors.extend(build_structure_anchors(story_text))
        from .source_inventory import inventory_anchors
        anchors.extend(inventory_anchors(story_text, anchors))
        return anchors

    def _build_claims(self, city, doc_ir):
        claims = []
        for index, event in enumerate(doc_ir.events, 1):
            text = event.span.text.strip()
            if not text:
                continue
            if _HYPOTHESIS.search(text):
                kind, confidence, policy = ClaimType.HYPOTHESIS, 0.45, VisualizationPolicy.ILLUSTRATIVE_ONLY
            elif _ANALOGY.search(text):
                kind, confidence, policy = ClaimType.ANALOGY, 0.55, VisualizationPolicy.DO_NOT_VISUALIZE
            elif _INTERPRETATION.search(text):
                kind, confidence, policy = ClaimType.INTERPRETATION, 0.65, VisualizationPolicy.MARKED_RECONSTRUCTION
            else:
                kind, confidence, policy = ClaimType.FACT, 0.85, VisualizationPolicy.DIRECT
            claims.append(HistoricalClaimIR(
                claim_id=f"claim_{index:04d}", claim_text=text, claim_type=kind,
                subject_ids=[city.city_id], source_span=event.span,
                confidence=confidence, visualization_policy=policy,
            ))
        return claims

    def _link_sequences(self, sequences, anchors, claims, geography):
        claim_by_span = {claim.source_span.start: claim for claim in claims}
        geo_by_event = defaultdict(list)
        for item in geography:
            if item.event_id:
                geo_by_event[item.event_id].append(item)
        anchor_by_name = {anchor.canonical_name.casefold(): anchor for anchor in anchors}
        artifacts = [anchor for anchor in anchors if anchor.anchor_type is AnchorType.ARTIFACT]
        for sequence in sequences:
            sequence.claim_ids = [claim.claim_id for start, claim in claim_by_span.items() if sequence.source_span.start <= start < sequence.source_span.end]
            sequence.artifact_anchor_ids = [anchor.anchor_id for anchor in artifacts if any(sequence.source_span.start <= span.start < sequence.source_span.end for span in anchor.evidence_spans)][:1]
            for event_id in sequence.event_ids:
                for geo in geo_by_event.get(event_id, []):
                    anchor = anchor_by_name.get(geo.text.casefold())
                    if anchor and anchor.anchor_type is not AnchorType.ARTIFACT:
                        sequence.location_anchor_id = anchor.anchor_id
                        break
                if sequence.location_anchor_id:
                    break
            if sequence.location_anchor_id is None and sequence.time_layer not in (TimeLayer.MAP, TimeLayer.ABSTRACT_EXPLANATION):
                sequence.location_anchor_id = (
                    "anchor_city_modern_ruins"
                    if sequence.time_layer is TimeLayer.MODERN_RUINS and "anchor_city_modern_ruins" in {a.anchor_id for a in anchors}
                    else "anchor_city_master"
                )

    def _population_bible(self, city, doc_ir):
        people_events = [event for event in doc_ir.events if re.search(r"\b(?:жител\w*|мастер\w*|ремесленник\w*|земледел\w*)\b", event.span.text, re.I)]
        clothing = [value for value, pattern in {
            "лён": re.compile(r"\bльн\w*|л[её]н\b", re.I),
            "шерсть": re.compile(r"\bшерст\w*", re.I),
            "кожа": re.compile(r"\bкожан\w*|кожа\b", re.I),
        }.items() if pattern.search(" ".join(event.span.text for event in people_events))]
        return PopulationStyleBibleIR(
            period_and_region="; ".join(filter(None, [city.period_start, city.region])),
            clothing_materials=clothing,
            color_palette=city.color_palette,
            forbidden_details=["modern clothing", "synthetic fabrics", "modern hairstyles", "modern tools"],
            evidence_spans=[event.span for event in people_events[:8]],
        )

    @staticmethod
    def _slug(value):
        normalized = re.sub(r"[^a-zа-яё0-9]+", "_", value.casefold(), flags=re.I).strip("_")
        return normalized[:48] or hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _unique_spans(spans):
        seen, result = set(), []
        for span in spans:
            key = (span.start, span.end)
            if key not in seen:
                seen.add(key)
                result.append(span)
        return result

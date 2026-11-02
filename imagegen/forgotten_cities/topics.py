"""Resolve documentary subjects across sentences without leaking other finds."""
import re

TEXTILE = re.compile(r'\b(?:пряслиц\w*|прядени\w*|пряден\w*|волок\w*|нит[ьи]\w*|нитей|ткан\w*|веретен\w*)', re.I)
PRIMATE = re.compile(r'макак\w*|примат\w*', re.I)
GOAT = re.compile(r'коз[её]л\w*|козла\w*', re.I)


def source_topic(quote, story='', source_start=None):
    from imagegen.narrative_obligations import LEGEND
    if LEGEND.search(quote):return 'reported_claim'
    if re.search(r'узнать\s+шахмат\w*', quote, re.I):
        return 'modern_chess_analogy'
    if not isinstance(source_start, int) or story[source_start:source_start+len(quote)] != quote:
        source_start = story.find(quote) if quote else -1
    from imagegen.source_windows import excerpt
    prefix = excerpt(story, source_start, source_start) if source_start >= 0 else ''
    context = prefix + quote
    paragraph_start = story.rfind('\n\n', 0, max(0, source_start)) + 2
    paragraph_context = story[max(0, paragraph_start):max(0, source_start)] + quote
    from imagegen.scene_semantics import signals
    if (re.search(r'псевдонауч|якобы|по\s+их\s+поверь|легенд|мистическ', quote, re.I)
            or any(item['kind'] == 'reported' for item in signals(quote))):
        return 'reported_claim'
    if re.search(r'(?:выгравир|вырезан|изображ[её]н|образ\w*\s+быка)', quote, re.I) and re.search(r'печат\w*|стеатит\w*', context, re.I):
        return 'seal_engraving'
    dated_context = excerpt(story, source_start, source_start, before=2200) + quote if source_start >= 0 else quote
    if (re.search(r'\b(?:тысяча\s+(?:восемьсот|девятьсот)|1[89]\d\d)\b', dated_context, re.I)
            and re.search(r'археолог|раскоп|транше|исследоват', paragraph_context, re.I)):
        return 'historical_fieldwork'
    if re.search(r'битум\w*|рецептур\w*', quote, re.I) and re.search(r'искусственн\w*\s+глаз\w*', context, re.I):
        return 'eye_material'
    # A new subject terminates an earlier subject even across paragraph breaks.
    signals = []
    for name, pattern in [('textile', TEXTILE), ('primate', PRIMATE), ('animated_vessel', GOAT)]:
        signals.extend((m.start(), name) for m in pattern.finditer(context))
    signals.extend((m.start(), '') for m in re.finditer(r'искусственн\w*\s+глаз\w*|\bчереп\w*|игров\w*\s+доск\w*|следующ\w*\s+наход\w*', context, re.I))
    latest = max(signals, default=(-1, ''))[1]
    if PRIMATE.search(quote) or (latest == 'primate' and re.search(r'животн|кост|погреб|могил|захорон|регион|фауна|Инда', quote, re.I)):
        return 'primate'
    if latest == 'textile' and not re.search(r'искусственн\w*\s+глаз|город\w*', quote, re.I):
        return 'textile'
    if latest == 'animated_vessel' and re.search(r'сосуд|стенк|изображ|животн|художник|движени|положени\w*\s+тела', quote, re.I):
        return 'animated_vessel'
    # Summary references must retain their known animal identity, not a human skull.
    if PRIMATE.search(story) and re.search(r'(?:привез[её]нн\w*\s+издалека\s+животн|животн\w*[^.!?]{0,50}похорон)', quote, re.I):
        return 'primate'
    return ''


def topic_treatment(topic, quote):
    if topic == 'reported_claim':
        from imagegen.narrative_obligations import LEGEND
        if not LEGEND.search(quote):
            return dict(source_binding='artifact_detail', shot_type='prop_detail', visual_time_layer='abstract_explanation',
                treatment='reported_claim_not_literal_event', direction='Documentary insert reporting the narrated speculative claim. Preserve attribution and uncertainty. Do not depict an actual ancient nuclear explosion or a feared/prevented disaster as an event that happened. No invented readable headline, findings or identity labels.')
        return dict(source_binding='narrated_event', shot_type='main_action', visual_time_layer='historical_reconstruction',
            treatment='illustrative_reported_claim', direction='Explicit illustration of the reported story, not an established event: ' + quote + '. Preserve the narrated actors and action, including explicitly narrated supernatural content. Conventional artistic appearance, not authenticated portraits. No unmentioned magic, catastrophe, objects or readable labels.')
    if topic == 'seal_engraving':
        return dict(source_binding='artifact_detail', shot_type='prop_detail', visual_time_layer='museum_display',
            treatment='source_backed_seal_engraving', direction='Close documentary view of the source-described small engraved seal. The narrated animal and symbols are carved into the flat artifact surface, not a live animal or a separate giant sculpture. Preserve the original archaeological scale and material.')
    if topic == 'historical_fieldwork':
        return dict(source_binding='context', shot_type='main_action', visual_time_layer='excavation',
            treatment='source_backed_historical_fieldwork', direction='Historical documentary reconstruction of the archaeological exploration described in the source. Use the narrated nineteenth or twentieth-century period for clothing and hand tools. Show the actual selected action at the mound or trench and the exposed archaeological remains. No present-day machinery, no ancient inhabitants, no supernatural entities. This is past fieldwork, not ancient city daily life.')
    if topic == 'eye_material':
        return dict(source_binding='artifact_detail', shot_type='prop_detail', visual_time_layer='museum_display',
            treatment='source_backed_eye_material', direction='Close documentary view of the material surface of the small artificial eye. The possibly bitumen-based composition is uncertain, not a proven recipe. No spilled black liquid in a courtyard, invented ingredients, laboratory formula or readable material label.')
    if topic == 'modern_chess_analogy':
        return dict(source_binding='artifact_detail', shot_type='prop_detail', visual_time_layer='museum_display',
            treatment='illustrative_modern_comparison', direction='A recognisable modern chessboard and chess pieces in a clearly present-day neutral comparison insert. This is a modern analogy, never an excavated ancient find or a chessboard placed in the ancient city.')
    if topic == 'textile':
        return dict(source_binding='artifact_action', shot_type='main_action',
            visual_time_layer='historical_reconstruction', treatment='source_backed_textile_action',
            direction='Anonymous ancient textile worker preparing fibre or spinning thread with a spindle and spindle whorl, as described in the narration. Show hands, fibre, thread and woven cloth at natural scale. No artificial eye, skull, gaming board or modern machinery. Complete maintained adobe workshop, period clothing. The illustration does not invent a named worker.')
    if topic == 'primate':
        burial = bool(re.search(r'могил|погреб|захорон|положили|похорон', quote, re.I))
        return dict(source_binding='buried_remains' if burial else 'artifact_detail',
            shot_type='prop_detail', visual_time_layer='excavation' if burial else 'museum_display',
            treatment='source_backed_primate_remains',
            direction='Archaeological remains of the source-described rhesus macaque, a small nonhuman primate. Preserve a macaque muzzle, nonhuman skull proportions and tail vertebrae; never attach a human or trepanned girl skull. '+
                ('One separate animal grave with its pottery vessel, no living animal, no human burial.' if burial else 'Documentary study of the animal remains, not a living pet, invented owner or human skeleton.'))
    if topic == 'animated_vessel':
        return dict(source_binding='artifact_detail', shot_type='prop_detail', visual_time_layer='museum_display',
            treatment='source_backed_animated_vessel',
            direction='The same small pottery vessel with a goat approaching tree foliage in five successive poses around its wall. Preserve one vessel shape, base, material and painted sequence across all shots. Show a detail of the successive poses or the whole vessel; no artificial eye, macaque, different bowl or invented cinema apparatus.')
    return None

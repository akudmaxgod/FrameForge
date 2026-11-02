"""Source-first constraints shared by analysis and regeneration of saved scenes."""
from __future__ import annotations

import re


BURIAL = re.compile(r'\b(?:погреб\w*|похорон\w*|захорон\w*|buried|burial|entombed)\b', re.I)
PERSON = re.compile(r'\b(?:женщин\w*|мужчин\w*|реб[её]н\w*|тело\w*|останки|скелет\w*|woman|man|child|body|remains|skeleton)\b', re.I)
DETAIL = re.compile(r'\b(?:зрач\w*|поверхност\w*|отверсти\w*|края|кра[её]в\w*|зажив\w*|по\s+краям|от\s+центра|pupil|surface|holes?|margins?)\b', re.I)
SOURCE_ACTION = re.compile(r'\b(?:заметил\w*\s+(?:е[её]\s+|его\s+)?проблем\w*|подобрал\w*\s+материал\w*|сделал\w*\s+предмет\w*)\b', re.I)
ARCHITECTURE = re.compile(r'\b(?:город\w*|дом\w*|стен\w*|руин\w*|колод\w*|канал\w*|улиц\w*|кирпич\w*|глин\w*|коридор\w*|тоннел\w*|туннел\w*|галере\w*|шахт\w*|двер\w*|каменн\w*\s+(?:диск\w*|колес\w*)|city|walls?|well|canals?)\b', re.I)
OBJECT_NOUNS = ((r'\bбусин\w*\b', 'бусина'), (r'\bчереп\w*\b', 'череп'),
                (r'\bзаготов\w*\b', 'заготовка'), (r'\bоскол\w*\b', 'осколок'),
                (r'\bсосуд\w*\b', 'сосуд'), (r'\bигров\w*\s+доск\w*\b', 'игровая доска'))
OBJECT_NOUNS += ((r'\bстел\w*\b','стела'), (r'\bстату[яиеюй]\w*\b','статуя'),
                 (r'\bпластинк\w*\b','пластинка'), (r'\bгир[яиеюь]\w*\b','гиря'),
                 (r'\bкорабл\w*\b','корабль'))


def object_noun(text):
    matches = [(m.start(), name) for pattern, name in OBJECT_NOUNS for m in re.finditer(pattern, text, re.I)]
    return max(matches)[1] if matches else ''


def artifact_mentioned(text, anchor):
    name = str(anchor.get('canonical_name') or '').casefold()
    if name and name in text.casefold():
        return True
    if anchor.get('source_properties', {}).get('source_noun'):
        from .source_inventory import normal_name
        return any(normal_name(source_name) in normal_name(text)
                   for source_name in anchor['source_properties'].get('source_names', []))
    # Match the subject noun, not a material or an arbitrary adjective shared
    # with another anchor. These names correspond to the profile's artifact IR.
    patterns = (
        ('череп', r'\b(?:череп\w*|трепанац\w*|skull|trepanation)\b'),
        ('глаз', r'\b(?:искусственн\w*\s+глаз\w*|протез\w*|artificial\s+eye|prosthetic\s+eye)\b'),
        ('доск', r'\b(?:игров\w*\s+доск\w*|game\s+board)\b'),
        ('сосуд', r'\b(?:сосуд\w*|vessel)\b'),
        ('печат', r'\b(?:печат\w*|оттиск\w*|seal\s+impression)\b'),
        ('лазурит', r'\b(?:лазурит\w*|lapis)\b'),
    )
    return any(stem in name and re.search(pattern, text, re.I) for stem, pattern in patterns)


def bind_source(metadata, event_id, quote, *, story='', source_start=None):
    """Resolve the depicted object from the actual quote and preceding evidence.

    Never use a later mention or a sequence-wide artifact as authorization to
    put that object into an unrelated architectural shot.
    """
    quote = str(quote or '').strip()
    from .structures import scene_structure_anchors
    structure_subject = bool(story and scene_structure_anchors(metadata.get('anchors', []), story, quote, source_start))
    from .topics import source_topic
    topic = source_topic(quote, story, source_start)
    artifacts = [a for a in metadata.get('anchors', []) if a.get('anchor_type') == 'artifact']
    if story:
        from .artifacts import skull_evidence
        evidence = skull_evidence(story)
        position = source_start if isinstance(source_start, int) else story.find(quote)
        artifacts = [a for a in artifacts if 'череп' not in str(a.get('canonical_name', '')).casefold()
                     or (evidence and position >= 0 and position + len(quote) > evidence[0])]
    selected = [a for a in artifacts if artifact_mentioned(quote, a)]
    position = source_start if type(source_start) is int else story.find(quote)
    direct = [a for a in artifacts if any(position <= m.get('start',-1) and m.get('end',10**9) <= position+len(quote)
              for m in a.get('match_spans',[]))]
    if direct: selected = direct
    noun = object_noun(quote)
    if not selected and noun:
        candidates = [a for a in artifacts if noun in str(a.get('canonical_name','')).casefold()]
        if len(candidates)==1: selected=candidates
    if topic == 'primate':
        selected = []
        artifacts = []  # Animal bones never inherit a human skull reference.
    elif topic in ('textile', 'modern_chess_analogy'):
        selected = []
        artifacts = []
    elif topic == 'animated_vessel':
        selected = [a for a in artifacts if 'сосуд' in str(a.get('canonical_name', '')).casefold()]
    errors = []
    architecture_detail = False
    literal_subject = object_noun(quote)
    source_action = bool(SOURCE_ACTION.search(quote))
    burial = bool(BURIAL.search(quote) and PERSON.search(quote))
    burial_with_object = burial and bool(re.search(r'\b(?:с\s+ним|с\s+не[йю]|with\s+it)\b', quote, re.I))
    topic_transition = bool(re.search(r'следующ\w*\s+наход|друг\w*\s+наход|верн[её]мся\s+к\s+городу', quote, re.I))
    # Lookback is for a dependent detail, not a default subject assignment.
    # A new standalone explanation must not inherit an unrelated old find.
    dependent_detail = bool(source_action or burial_with_object or DETAIL.search(quote) or re.match(r'^\s*(?:он[аои]?|его|е[её]|их|этот\s+предмет|эта\s+находка)\b', quote, re.I))
    if dependent_detail and not selected and not literal_subject and not topic_transition and (not burial or burial_with_object) and not ARCHITECTURE.search(quote) and not structure_subject:
        if story and (not isinstance(source_start, int) or story[source_start:source_start + len(quote)] != quote):
            source_start = story.find(quote)
        candidates = []
        if story and isinstance(source_start, int) and source_start >= 0:
            # Each sentence is grounded in its nearest explicit subject. This
            # also works on legacy anchors whose evidence spans are incomplete.
            paragraph_start = story.rfind('\n\n', 0, source_start) + 2
            prefix = story[max(0, source_start - 1600, paragraph_start):source_start]
            sentences = list(re.finditer(r'[^.!?\n]+[.!?]?', prefix))
            for sentence in reversed(sentences):
                if re.search(r'следующ\w*\s+наход|друг\w*\s+наход|верн[её]мся\s+к\s+городу', sentence.group(), re.I):
                    break
                matches = [a for a in artifacts if artifact_mentioned(sentence.group(), a)]
                if matches:
                    candidates = matches
                    break
                noun = object_noun(sentence.group())
                if noun:
                    literal_subject = noun
                    candidates = [a for a in artifacts if noun in str(a.get('canonical_name','')).casefold()]
                    if len(candidates)!=1: candidates=[]
                    break
                material_boundary = r'\b(?:грунт\w*|отложени\w*|ил|песок|взвесь)\b' if source_action else r'\b(?:грунт\w*|отложени\w*|материал\w*|ил|песок|взвесь)\b'
                if ARCHITECTURE.search(sentence.group()) or re.search(material_boundary, sentence.group(), re.I):
                    architecture_detail = True
                    break
        elif not story:
            sequence = next((s for s in metadata.get('sequences', []) if event_id in s.get('event_ids', [])), {})
            candidates = [a for a in artifacts if a.get('anchor_id') in sequence.get('artifact_anchor_ids', [])]
        if len(candidates) == 1:
            selected = candidates
        elif DETAIL.search(quote) and not selected and not architecture_detail and not literal_subject and not re.search(
                r'\b(?:грунт\w*|отложени\w*|материал\w*|ил|песок|взвесь|река)\b', quote, re.I):
            errors.append('Не определён предмет, к которому относится описание детали. Уточните контекст сцены.')
    if topic:
        errors = []
    return {'quote': quote, 'artifacts': selected[:1], 'burial': burial, 'errors': errors, 'topic': topic,
            'architecture_detail': architecture_detail, 'literal_subject': literal_subject,
            'source_action': source_action and bool(selected)}


def apply_packet(scene, packet):
    """Replace contradictory old framing while preserving the source quotation."""
    result = {**scene, 'forgotten_cities_prompt_contract': packet,
              'shot': packet['shot_type'], 'time': packet['visual_time_layer'],
              'time_layer': packet['visual_time_layer']}
    if packet.get('source_binding') in ('artifact_detail', 'buried_remains'):
        result.update(visible_people=0, episodic_people=[], characters=[], character_ids=[],
                      depicted_people=[], people_policy='none', framing_policy='OBJECT_FOCUS',
                      composition=packet['visual_direction'], safe_framing_explanation=packet['visual_direction'])
    if packet.get('source_binding') == 'narrated_event':
        roles = packet.get('source_roles') or []
        result.update(visible_people=len(roles), raw_visible_people=len(roles), episodic_people=roles,
                      characters=[], character_ids=[], depicted_people=[],
                      people_policy='anonymous_action' if roles else 'none',
                      source_event_context=packet.get('narrative_context'),
                      source_animals=(packet.get('narrative_context') or {}).get('animals', []),
                      composition=packet['visual_direction'], safe_framing_explanation=packet['visual_direction'])
        result['location_ref'] = ''
        result['props'] = []
        result['location'] = ((packet.get('narrative_context') or {}).get('place') or {}).get('name') or 'Место действия из цитаты'
    if packet.get('source_binding') == 'buried_remains':
        result.update(subject_state='buried_remains', depicted_remains=1)
    elif packet.get('source_binding') == 'artifact_action':
        result.pop('subject_state', None)
        result.pop('depicted_remains', None)
        quote = packet.get('source_quote', '').casefold()
        role = 'женщина' if 'женщин' in quote else 'мужчина' if 'мужчин' in quote else 'мастер'
        roles = packet.get('source_roles') or [role]
        result.update(visible_people=len(roles), episodic_people=roles, people_policy='anonymous_action',
                      composition=packet['visual_direction'])
    else:
        result.pop('subject_state', None)
        result.pop('depicted_remains', None)
    if packet.get('source_binding') in ('architecture', 'context') and not packet.get('required_artifacts'):
        result['props'] = []
    if packet.get('required_artifacts'):
        worn = (packet.get('source_binding') == 'artifact_action' and not packet.get('source_action') and
                bool(re.search(r'\b(?:носил\w*|на\s+лице|wear\w*)\b', packet.get('source_quote', ''), re.I)))
        state = 'worn' if worn else 'placed'
        result['props'] = [{'name': name, 'state': state, 'visible': True, 'continuity': 'continues'}
                           for name in packet['required_artifacts']]
    plan = packet.get('source_fidelity_plan')
    if not plan:
        result.pop('source_fidelity_plan', None)
    if plan:
        result.update(source_fidelity_plan=plan, visible_people=plan['expected_people'],
                      episodic_people=plan['roles'], characters=[], character_ids=[],
                      composition=plan['visual_direction'], safe_framing_explanation=plan['visual_direction'])
        if not packet.get('required_artifacts'):
            result['props'] = []
    result.pop('population_policy', None)
    if packet.get('city_population') == 'inhabited':
        # A single aggregate role records presence, not an exact crowd size.
        result.update(population_policy='inhabited_city', visible_people=1,
                      episodic_people=['жители древнего города'], characters=[], character_ids=[],
                      depicted_people=[], people_policy='anonymous_background',
                      composition=packet['visual_direction'], safe_framing_explanation=packet['visual_direction'])
    elif packet.get('city_population') == 'source_people':
        quote = packet.get('source_quote', '').casefold()
        role = 'археологи' if 'археолог' in quote else 'посетители' if 'посетител' in quote else 'исследователи'
        result.update(population_policy='source_people', visible_people=1, episodic_people=[role],
                      characters=[], character_ids=[], depicted_people=[], people_policy='anonymous_action',
                      composition=packet['visual_direction'], safe_framing_explanation=packet['visual_direction'])
    elif packet.get('city_population') in ('uninhabited', 'detail'):
        result.update(visible_people=0, episodic_people=[], characters=[], character_ids=[],
                      depicted_people=[], people_policy='none', composition=packet['visual_direction'])
    if packet.get('source_binding') == 'narrated_event' and not re.search(r'\b(?:два|двое|двух|оба|обоих)\s+брат\w*', packet.get('source_quote',''), re.I) and any(
            p.get('number') == 'plural' for p in (packet.get('narrative_context') or {}).get('participants', [])):
        result['population_policy'] = 'source_group'
    if packet.get('source_binding') in ('artifact_detail', 'buried_remains', 'artifact_action') or plan or packet.get('city_population'):
        # Presence validation must not re-read the superseded count from the old analysis.
        result['raw_visible_people'] = result.get('visible_people', 0)
    return result


def artifact_source_constraints(names, story=''):
    from .physical_evidence import artifact_rules
    rules = artifact_rules(names, story)
    if any('глаз' in name.casefold() for name in names):
        rules.append('A small artificial eye fitting one human eye socket, never palm-sized or skull-sized. Preserve pupil, radial markings and edge attachment holes when specified. No readable invented museum label or invented definitive material recipe.')
    if any('сосуд' in name.casefold() for name in names) and re.search(r'пять\s+последовательн\w*\s+изображ', story, re.I):
        rules.append('One unchanged pottery vessel and exactly five successive goat poses around its wall; no change from a footed vessel to a different bowl between shots.')
    if any('череп' in name.casefold() for name in names):
        from .artifacts import skull_constraints
        rules.extend(skull_constraints(story))
    if not any('доск' in name.casefold() for name in names):
        return rules
    board = re.search(r'деревянн\w*\s+игров\w*\s+доск\w*', story, re.I)
    evidence = story[board.start():board.end()+350] if board else ''
    if not re.search(r'двадцать\s+кругл\w*\s+игров\w*\s+пол\w*', evidence, re.I):
        return rules
    return rules + ['The source specifies a wooden board with EXACTLY twenty circular playing fields total, count them in a whole-board view. '
            'Do not replace these with a square chessboard grid, chess pieces or a different named game. '
            'Source-described features outrank a conflicting reference image.']


def enforce_source_constraints(packet, story=''):
    """Keep central subject rules in the generation prompt, independently of vision APIs."""
    packet = dict(packet)
    from .physical_evidence import source_backed_direction
    old_direction = packet.get('visual_direction', '')
    packet['visual_direction'] = source_backed_direction(packet.get('required_artifacts') or [], story,
        packet.get('source_quote', ''), old_direction)
    if old_direction and old_direction != packet['visual_direction']:
        packet['shot_contract'] = packet['shot_contract'].replace(old_direction, packet['visual_direction'])
    # Concrete dimensions and defining faces must survive Flow's prompt cap.
    rules = artifact_source_constraints(packet.get('required_artifacts') or [], story)
    rules.append('Depict the selected narration, not a different sentence elsewhere in the story.')
    if packet.get('required_artifacts'):
        rules.append('Use the attached artifact only at its real physical scale and with the same shape and material. '
                     'Macro framing enlarges the view, not the object relative to people, buildings or other objects.')
    else:
        rules.append('Keep the selected source event or physical subject. Do not insert unrelated reference objects, oversized artifacts, museum display tables or invented exhibits into the view.')
    if packet.get('source_binding') == 'buried_remains':
        rules.append('When the selected source describes one person, show that person’s burial only; do not add unrelated additional skeletons. Living and buried states are distinct.')
    quote = packet.get('source_quote', '')
    if packet.get('required_artifacts') and re.search(r'разбит\w*\s+на\s+(?:несколько\s+)?част|broken\s+into\s+(?:several\s+)?pieces', quote, re.I):
        rules.insert(0, 'ONE SPECIMEN IN FRAGMENTS: show corresponding broken parts of one object, never several complete copies or multiple views of the same statue.')
    narrative = packet.get('narrative_context')
    if narrative:
        rules.append('SOURCE EVENT CONTEXT: ' + narrative['scope']['quote'] +
                     ' This is context for interpreting the selected event, not permission to depict every participant in every shot.')
        if narrative.get('time'):
            rules.append('Keep the narrated time anchor: ' + narrative['time']['evidence']['quote'] +
                         '. Clothing and tools follow that time; a narrated past event is not automatically ancient daily life or present-day ruins.')
    from .topics import source_topic, topic_treatment
    topic = source_topic(quote, story)
    treatment = topic_treatment(topic, quote)
    if treatment and not packet.get('source_reviewed') and packet.get('source_binding') != 'narrated_event':
        rules.append('SOURCE SUBJECT: ' + treatment['direction'])
    if packet.get('visual_time_layer') == 'historical_reconstruction':
        rules.append('Reconstruction in the source-stated period: clothing, tools and materials must follow that period. Do not assume every past scene is ancient. Do not replace historical activity with present-day excavation or introduce objects from later periods.')
    start = story.find(quote) if quote else -1
    context = story[max(0, start-100):start+len(quote)+250] if start >= 0 else quote
    if re.search(r'врат\w*[^.!?]{0,50}преисподн|метафор|переносн|наш\w*\s+образ', context, re.I):
        rules.append('Figurative language is a metaphor, not a literal structure: no invented portal, cave gate, hell entrance or supernatural scene. Show the source-supported landscape.')
    packet['source_constraints'] = rules
    packet['shot_contract'] += ' SOURCE CONSTRAINTS: ' + ' '.join(rules)
    packet['compiled_prompt'] = '\n'.join(f'{label}: {packet[key]}' for label, key in
        [('STYLE CONTRACT', 'style_contract'), ('CITY CONTRACT', 'city_contract'), ('TIME LAYER', 'time_layer_contract'),
         ('SHOT CONTRACT', 'shot_contract'), ('NEGATIVE CONTRACT', 'negative_contract')])
    return packet

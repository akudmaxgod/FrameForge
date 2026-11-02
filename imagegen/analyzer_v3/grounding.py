"""Evidence grounded identity and spatial continuity for narrative documents."""
import re

from .models import EntityIR, EntityModalityV3, EntityType

# Name variants are lexical candidates. They merge only when a unique full name
# is present; multiple compatible full names deliberately keep separate IDs.
NAME_VARIANTS = {
    "алексей": {"леша", "алеша", "алешенька"},
    "александр": {"саша", "саня", "шура"},
    "мария": {"маша", "машенька"},
    "екатерина": {"катя", "катюша"},
    "дмитрий": {"дима", "митя"},
    "михаил": {"миша"},
    "наталья": {"наташа"},
    "татьяна": {"таня"},
    "сергей": {"сережа"},
    "евгений": {"женя"},
    "евгения": {"женя"},
    "томас": {"том", "томми"},
    "владимир": {"вова", "володя", "вовочка"},
    "николай": {"коля", "коленька"},
    "иван": {"ваня", "ванечка"},
    "петр": {"петя", "петенька"},
    "павел": {"паша", "павлик"},
    "василий": {"вася", "васенька"},
    "григорий": {"гриша"},
    "георгий": {"гоша", "жора"},
    "юрий": {"юра"},
    "андрей": {"андрюша"},
    "артем": {"тема", "артемка"},
    "виктор": {"витя"},
    "виталий": {"виталик"},
    "валерий": {"валера"},
    "константин": {"костя"},
    "максим": {"макс"},
    "михаил": {"миша", "мишка"},
    "федор": {"федя"},
    "наталья": {"наташа"},
    "елена": {"лена", "леночка"},
    "ольга": {"оля"},
    "ирина": {"ира"},
    "светлана": {"света"},
    "людмила": {"люда"},
    "анастасия": {"настя"},
    "александра": {"саша", "шура"},
    "евгения": {"женя"},
    "ксения": {"ксюша"},
    "дарья": {"даша"},
    "варвара": {"варя"},
    "надежда": {"надя"},
    "любовь": {"люба"},
    "валентина": {"валя"},
}

def _key(name):
    return name.casefold().replace("ё", "е")


def _inside_paired_quote(story, position, opening, closing):
    """Return whether *position* is inside the nearest still-open quote pair."""
    return story.rfind(opening, 0, position) > story.rfind(closing, 0, position)


def _inside_symmetric_quote(story, position, quote='"'):
    """Recognize straight symmetric quotes, honoring escaped quote characters."""
    count = 0
    escaped = False
    for char in story[:position]:
        if char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            count += 1
        escaped = False
    return count % 2 == 1


def inside_direct_speech(story, position):
    """Recognize Russian prose, typographic and screenplay direct speech."""
    paired = (("«", "»"), ("“", "”"), ("„", "“"), ("‘", "’"))
    if any(_inside_paired_quote(story, position, left, right) for left, right in paired):
        return True
    if _inside_symmetric_quote(story, position):
        return True
    return _inside_screenplay_speech(story, position)


def _inside_russian_quote(story, position):
    """Compatibility wrapper used by older grounding rules."""
    return inside_direct_speech(story, position)


def _inside_screenplay_speech(story, position):
    """Recognize an unquoted screenplay speech or dialogue line introduced by a dash or speaker heading."""
    prefix = story[:position]
    last_line = prefix.split("\n")[-1]
    if re.match(r"\s*[—–-]\s*", last_line):
        return True
    # Prose dialogue may begin after a completed narration sentence on the same
    # physical line: ``Он остановился. — Я вернусь``.
    boundary = max(prefix.rfind(mark) for mark in (".", "!", "?", "\n"))
    if re.match(r"\s*[—–-]\s*", prefix[boundary + 1:]):
        return True
    paragraph = re.split(r"\n\s*\n", prefix)[-1]
    if re.match(r"\s*[—–-]\s*", paragraph):
        return True
    if re.search(
        r"(?:^|\n)\s*[А-ЯЁ][А-ЯЁ -]{1,40}\s*\n(?:\s*\([^\n)]*\)\s*\n)?\s*$",
        prefix,
    ):
        return True
    blocks = [block.strip() for block in re.split(r"\n\s*\n", prefix) if block.strip()]
    # The current block contains the speech before the pronoun. The preceding
    # one or two blocks contain the speaker heading and, optionally, a cue.
    for block in blocks[-3:-1]:
        if re.fullmatch(r"[А-ЯЁ][А-ЯЁ -]{1,40}", block):
            return True
    return False


def ensure_first_person_narrator(entities, mentions, doc, story, nlp):
    """Create the implied first-person narrator from grounded, unquoted pronouns."""
    event_map = {e.event_id: e for e in doc.events}
    # 1st-person singular pronouns establish an embodied narrator.
    # Plural forms ("мы", "нас") in expository/documentary texts without singular "я"
    # are authorial/rhetorical we and must not create a physical character.
    singular_forms = {"я", "меня", "мне", "мной", "мною"}
    has_singular = any(
        m.text.casefold() in singular_forms
        and not inside_direct_speech(story, m.span.start)
        for m in mentions
    )
    if not has_singular:
        return entities

    forms = {"я", "меня", "мне", "мной", "мною", "мы", "нас", "нам", "нами"}
    narrator_mentions = [m for m in mentions if m.text.casefold() in forms
                         and not inside_direct_speech(story, m.span.start)
                         and event_map.get(m.event_id) and event_map[m.event_id].event_type != "dialogue"]
    if not narrator_mentions:
        return entities

    gender_counts = {"female": 0, "male": 0}
    narrator_events = {m.event_id for m in narrator_mentions}
    for event in doc.events:
        if event.event_id not in narrator_events:
            continue
        for token in nlp.split_tokens(event.span.text, event.span.start):
            morph = nlp.analyze_token_morph(token.text)
            if morph.pos in {"VERB", "PRTF", "ADJS"} and morph.gender in gender_counts:
                gender_counts[morph.gender] += 1
    gender = "female" if gender_counts["female"] > gender_counts["male"] else (
        "male" if gender_counts["male"] > 0 else None
    )
    name = "Рассказчица" if gender == "female" else "Рассказчик"
    used = {int(e.entity_id.split("_")[-1]) for e in entities if e.entity_id.split("_")[-1].isdigit()}
    next_id = max(used, default=-1) + 1
    narrator = EntityIR(
        entity_id=f"ent_{next_id}", canonical_name=name, entity_type=EntityType.PERSON,
        gender=gender, aliases=sorted({m.text for m in narrator_mentions}),
        first_mention_start=min(m.span.start for m in narrator_mentions),
        mention_ids=[m.mention_id for m in narrator_mentions],
        modality=EntityModalityV3.PHYSICAL, confidence=0.98,
        attributes=["first_person_narrator"], evidence_spans=[m.span for m in narrator_mentions],
        is_recurring=True,
    )

    # A vocative kinship term inside quoted speech addresses the narrator.
    removed = set()
    for entity in entities:
        if _key(entity.canonical_name) not in ({"мама", "мать"} if gender == "female" else {"папа", "отец"}):
            continue
        linked = [m for m in mentions if m.mention_id in entity.mention_ids]
        if linked and all(inside_direct_speech(story, m.span.start) for m in linked):
            narrator.aliases.extend(a for a in entity.aliases if a not in narrator.aliases)
            narrator.mention_ids.extend(entity.mention_ids)
            narrator.evidence_spans.extend(entity.evidence_spans)
            removed.add(entity.entity_id)
    return [e for e in entities if e.entity_id not in removed] + [narrator]

def are_diminutive_or_alias_names(name1: str, name2: str) -> bool:
    """Universal Russian hypocoristic, diminutive and root-stemming matcher."""
    k1 = _key(name1).strip()
    k2 = _key(name2).strip()
    if not k1 or not k2:
        return False
    if k1 == k2:
        return True
    if k1 in NAME_VARIANTS.get(k2, set()) or k2 in NAME_VARIANTS.get(k1, set()):
        return True
    shorter, longer = (k1, k2) if len(k1) <= len(k2) else (k2, k1)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return True
    stem1 = re.sub(r"(?:еньк|ушк|очк|ик|ок|к|ш|чк)?[аяеиыу]?$", "", shorter)
    stem2 = re.sub(r"(?:еньк|ушк|очк|ик|ок|к|ш|чк)?[аяеиыу]?$", "", longer)
    if len(stem1) >= 3 and stem1 == stem2:
        return True
    return False


def link_supported_aliases(entities, mentions, story):
    people = [e for e in entities if e.entity_type == EntityType.PERSON]
    removed = set()
    for alias in people:
        if alias.entity_id in removed:
            continue
        candidates = [
            e for e in people
            if e != alias and e.entity_id not in removed
            and are_diminutive_or_alias_names(alias.canonical_name, e.canonical_name)
            and (not alias.gender or not e.gender or alias.gender == "neuter" or e.gender == "neuter" or alias.gender == e.gender)
        ]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        alias_mentions = [m for m in mentions if m.mention_id in alias.mention_ids]
        is_vocative = any(
            inside_direct_speech(story, m.span.start)
            or bool(re.search(r",\s*" + re.escape(m.text) + r"[.!?,\s]", story[max(0, m.span.start - 60):m.span.end + 2]))
            for m in alias_mentions
        )
        if not alias_mentions or (any(m.syntactic_role.value == "subject" for m in alias_mentions) and not is_vocative):
            continue
        target.aliases = list(dict.fromkeys(target.aliases + alias.aliases))
        target.mention_ids.extend(mid for mid in alias.mention_ids if mid not in target.mention_ids)
        target.evidence_spans.extend(alias.evidence_spans)
        target.first_mention_start = min(target.first_mention_start, alias.first_mention_start)
        for m in alias_mentions:
            m.normalized_text = target.canonical_name
        removed.add(alias.entity_id)
    return [e for e in entities if e.entity_id not in removed]


def link_definite_common_references(entities, mentions, story):
    """Merge explicit 'этот/эта ...' references with one prior same-named entity."""
    mention_map = {m.mention_id: m for m in mentions}
    removed = set()
    ordered = sorted(entities, key=lambda e: e.first_mention_start)
    for entity in ordered:
        linked = [mention_map[mid] for mid in entity.mention_ids if mid in mention_map]
        if not linked:
            continue
        first = min(linked, key=lambda m: m.span.start)
        prefix = story[max(0, first.span.start - 12):first.span.start].casefold()
        if not re.search(r"\b(?:этот|эта|это|эти|того|той)\s*$", prefix):
            continue
        prior = [e for e in ordered if e.entity_id not in removed and e.entity_id != entity.entity_id
                 and _key(e.canonical_name) == _key(entity.canonical_name)
                 and e.first_mention_start < first.span.start]
        if len(prior) != 1:
            continue
        target = prior[0]
        target.aliases = list(dict.fromkeys(target.aliases + entity.aliases))
        target.mention_ids.extend(mid for mid in entity.mention_ids if mid not in target.mention_ids)
        target.evidence_spans.extend(entity.evidence_spans)
        removed.add(entity.entity_id)
    return [e for e in entities if e.entity_id not in removed]

SPACE_NOUNS = {
    "гостиная", "кухня", "спальня", "комната", "кабинет", "коридор", "палата",
    "больница", "офис", "диспетчерская", "улица", "площадь", "парк", "лес",
    "пляж", "берег", "подвал", "чердак", "прихожая", "балкон", "двор", "гараж",
    "вокзал", "станция", "аэропорт", "магазин", "кафе", "ресторан", "школа",
    "дом", "пансионат", "ячейка", "участок", "город", "маяк", "остров",
    "пристань", "башня", "холм", "каюта", "палуба", "мост", "лаборатория",
    "архив", "тоннель", "завод", "склад", "ангар", "бункер", "замок", "крепость", "пещера",
}

SPACE_SURFACE_CANONICAL = {
    "гостиную": "Гостиная", "гостиной": "Гостиная", "гостиная": "Гостиная",
}

MOVE_TO_PLACE_RE = re.compile(
    r"\b(?:приехал\w*|приш[её]л\w*|вернул\w*|спустил\w*|вош\w*|зашагал\w*|поднял\w*|пробежал\w*|выбежал\w*|"
    r"направил\w*|переш[её]л\w*|добрал\w*|прибыл\w*|ворвался\w*|проник\w*)"
    r"[^,.!?;\n]{0,110}?\b(?:в|на)\s+"
    r"(?P<place>[А-ЯЁа-яё-]+(?:\s+[А-ЯЁа-яё0-9№-]+){0,4})",
    re.IGNORECASE,
)
MOVE_PATH_RE = re.compile(
    r"\b\u0447\u0435\u0440\u0435\u0437\s+(?P<place>[\u0410-\u042f\u0401\u0430-\u044f\u0451-]+(?:\s+[\u0410-\u042f\u0401\u0430-\u044f\u0451-]+){0,3})",
    re.IGNORECASE,
)


def _grounded_move_phrases(event, story, nlp):
    """Extract open-vocabulary destination phrases after explicit movement.

    The fixed SPACE_NOUNS lexicon remains useful for static settings. Movement
    destinations are open vocabulary: laboratory, archive, tunnel, engine hall,
    observation deck, and similar phrases must not require a dictionary entry.
    """
    records = []
    stop_words = {"и", "а", "но", "где", "который", "которая", "которое", "чтобы"}
    matches = list(MOVE_TO_PLACE_RE.finditer(event.span.text))
    if re.search(r"\b(?:\u043f\u0440\u043e\u0431\u0435\u0436\u0430\u043b\w*|\u043f\u0440\u043e\u0448[\u0435\u0451]\u043b\w*|\u043f\u0440\u043e\u0435\u0445\u0430\u043b\w*|\u043f\u0440\u043e\u043d[\u0435\u0451]\u0441\u0441\u044f\w*)\b", event.span.text, re.I):
        matches.extend(MOVE_PATH_RE.finditer(event.span.text))
    for match in sorted(matches, key=lambda value: value.start("place")):
        raw = match.group("place")
        kept = []
        cursor = event.span.start + match.start("place")
        for token in nlp.split_tokens(raw, cursor):
            if token.text.casefold() in stop_words:
                break
            morph = nlp.analyze_token_morph(token.text)
            if morph.pos not in {"NOUN", "PROPN", "ADJF", "PRTF", "NUMR"} and not token.text.isdigit():
                break
            kept.append(token)
        if not kept:
            continue
        start, end = kept[0].start, kept[-1].end
        quote = story[start:end]
        canonical_words = []
        preserve_tail = False
        for token in kept:
            morph = nlp.analyze_token_morph(token.text)
            if morph.case == "gent":
                preserve_tail = True
            word = token.text
            if not preserve_tail and morph.case not in (None, "nomn"):
                parsed = nlp.morph_analyzer.parse(token.text)
                inflected = parsed[0].inflect({"nomn"}) if parsed else None
                if inflected:
                    word = inflected.word
            canonical_words.append(word)
        canonical = " ".join(canonical_words)
        records.append({
            "name": canonical[:1].upper() + canonical[1:],
            "kind": "physical",
            "event_id": event.event_id,
            "start": start,
            "end": end,
            "quote": quote,
            "source": "open_vocabulary_movement",
        })
    return records

def _setting_phrase(token,tokens,nlp,story):
    """Keep modifiers/owners: two houses need not be one place."""
    index=tokens.index(token);first=index;last=index
    while first>0 and index-first<3:
        before=tokens[first-1]
        if story[before.end:tokens[first].start].strip() or nlp.analyze_token_morph(before.text).pos not in {'ADJF','PRTF'}:break
        first-=1
    while last+1<len(tokens) and last-index<4:
        following=tokens[last+1];morph=nlp.analyze_token_morph(following.text)
        if story[tokens[last].end:following.start].strip() or morph.case!='gent' or morph.pos not in {'NOUN','PROPN','ADJF'}:break
        last+=1
    words=[]
    prefix_number=nlp.analyze_token_morph(tokens[first].text).number if first<index else None
    for i in range(first,last+1):
        item=tokens[i];word=item.text
        if i<=index:
            parsed=nlp.morph_analyzer.parse(word)
            number={'plur'} if prefix_number=='plural' else set()
            parsed=sorted(parsed,key=lambda p: bool(number and 'plur' not in p.tag))
            nom=parsed[0].inflect({'nomn'}|number) if parsed else None
            if nom:word=nom.word
        words.append(word)
    name=' '.join(words)
    end=tokens[last].end
    number=re.match(r'\s+(?:№\s*|номер\s+)?(\d+)\b',story[end:])
    if number:
        name+=' '+number.group(1);end+=number.end()
    return name[:1].upper()+name[1:],tokens[first].start,end,last


def ground_event_locations(doc, story, nlp):
    """Keep current physical setting distinct from origins and reported settings.

    Records carry source coordinates; a quoted location never changes the room.
    Spatial interpretation stays explicit in metadata rather than inventing LOC NER.
    """
    records = []
    assignments = {}
    current = None
    in_dialogue = False
    for event in doc.events:
        text = event.span.text
        header = re.match(r"\s*([А-ЯЁ][А-ЯЁ ]{2,})\n", text)
        if header:
            in_dialogue = True
        elif event.span.start == 0 or story[max(0, event.span.start - 2):event.span.start] == "\n\n":
            in_dialogue = False
        event_records = []
        movement_records = _grounded_move_phrases(event, story, nlp)
        for record in movement_records:
            if in_dialogue or _inside_russian_quote(story,record['start']):record['kind']='reported'
        records.extend(movement_records)
        event_records.extend(movement_records)
        tokens = nlp.split_tokens(text, event.span.start)
        for token in tokens:
            lemma = nlp.analyze_token_morph(token.text).lemma
            surface_name = SPACE_SURFACE_CANONICAL.get(token.text.casefold())
            if lemma not in SPACE_NOUNS and surface_name is None:
                continue
            before = story[event.span.start:token.start].lower()
            local_before = before[-90:]
            kind = "reported" if in_dialogue or _inside_russian_quote(story, token.start) else "mentioned"
            if re.search(r"\b(?:вспомн\w*|вспомина\w*|мечта\w*|рассказ\w*|обсужда\w*)\b", before) or re.search(r"\b(?:о|об|про)\s*$", before):
                kind = "reported"
            if re.search(r"\b(?:фотографи\w*|фото|сним\w*|нарисовал\w*|изобразил\w*)\b", before):
                kind = "depicted"
            if not in_dialogue and re.search(r"\bиз\s*$", before):
                kind = "origin"
            link=re.search(r'\b(?:в|на|к)\s+((?:[а-яё-]+\s+){0,2})$',local_before,re.I)
            direct_head=bool(link and all(nlp.analyze_token_morph(t.text).pos in {'ADJF','PRTF','NUMR'} for t in nlp.split_tokens(link.group(1))))
            possible_case=any(p.tag.case in {'accs','loct','datv'} for p in nlp.morph_analyzer.parse(token.text))
            if kind=='mentioned' and direct_head and possible_case and not _inside_russian_quote(story, token.start) and re.search(
                r"\b(?:попал\w*|вход\w*|вош\w*|приход\w*|приш\w*|приехал\w*|вернул\w*|"
                r"прив[её]л\w*|подош\w*|оказал\w*|сто(?:ит|ял\w*|яла\w*|ят)|жил\w*)"
                r"[^.!?]{0,45}\b(?:в|на|к)\s+(?:[а-яё-]+\s+){0,2}$",
                local_before,
            ):
                kind = "physical"
            elif not _inside_russian_quote(story, token.start) and not in_dialogue:
                # A bare opening noun is not enough (area, simile, metaphor).
                # A concrete descriptive predicate or static locative can
                # establish a setting without inventing motion or an actor.
                _,_,_,tail=_setting_phrase(token,tokens,nlp,story)
                after=tokens[tail+1:tail+2]
                following=nlp.analyze_token_morph(after[0].text) if after else None
                predicate=bool(following and (following.pos in {'PRTS','ADJS'} or
                    following.pos=='VERB' and following.lemma in {'быть','стоять','лежать','располагаться','находиться','темнеть','светиться','пустеть','шуметь'}))
                prefix_tokens=nlp.split_tokens(before)
                adjective_prefix=all(nlp.analyze_token_morph(t.text).pos in {'ADJF','PRTF'} for t in prefix_tokens)
                static_prefix=bool(re.match(r'^\s*(?:в|на|у|возле|внутри)\s+(?:[а-яё-]+\s+){0,2}$',before,re.I))
                if predicate and (adjective_prefix or static_prefix) and kind=='mentioned':kind='physical'
            name,start,end,_=_setting_phrase(token,tokens,nlp,story)
            if name.casefold()==token.text.casefold():name=surface_name or lemma.capitalize()
            if lemma == "ячейка" and re.search(r"банковск\w*\s*$", local_before):
                name = "Банковская ячейка"
            record = {"name": name, "kind": kind,
                      "event_id": event.event_id, "start": start,
                      "end": end, "quote": story[start:end]}
            records.append(record)
            event_records.append(record)
        physical = [r for r in event_records if r["kind"] == "physical"]
        if physical:
            current = physical[-1]["name"]
        elif re.search(r"\bк\s+моему\s+возвращению\b", text.lower()):
            if any(r["name"] == "Дом" for r in records):
                current = "Дом"
        elif re.match(r"\s*на\s+следующ(?:ий|ее|ую)\s+(?:день|утро|ночь)\b", text.lower()):
            current = None
        event_location = current
        if re.search(r"\bпрожил\w*\s+в\s+н[её]м\b", text.lower()) and any(r["name"] == "Дом" for r in records):
            # A flashback can use its own visual setting without moving the
            # present-day physical timeline.
            event_location = "Дом"
        assignments[event.event_id] = event_location
    # A static "remains in ..." anchor can clarify a generic initial room.
    # Require the source statement of remaining, rather than the mere existence
    # of a later room name, to avoid merging rooms across an unrelated move.
    anchors = [r for r in records if r["kind"] == "physical" and r["name"] != "Комната"
               and re.search(r"\bоста[её]тся\b", story[next(ev.span.start for ev in doc.events if ev.event_id == r["event_id"]):r["start"]])]
    anchor_names = {r["name"] for r in anchors}
    if len(anchor_names) == 1:
        anchor = next(iter(anchor_names))
        assignments = {k: anchor if v == "Комната" else v for k, v in assignments.items()}
    doc.metadata["spatial_grounding"] = {"evidence": records, "event_locations": assignments}

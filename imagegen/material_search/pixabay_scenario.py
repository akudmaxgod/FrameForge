"""Bounded user-initiated stock discovery; local assignment, no paid LLM calls."""
from collections import Counter
import re
from .planner import clean_search_phrase, STOP_WORDS
from .ranking import normalize_requirement, _matches_term

# Search vocabulary, not scene templates: unknown subjects retain their own query.
TOPICS = {
    "eye": ("глаз", "зрени", "зрач", "сетчат", "хрустал", "роговиц", "радужк"),
    "brain": ("мозг", "нейрон", "нервн"),
    "doctor": ("врач", "офтальмолог", "лечени", "пациент"),
    "microscope": ("микроскоп", "клетк"),
    "heart": ("сердц", "кровообращ"),
    "lungs": ("легк", "лёгк", "дыхани"),
    "forest": ("лес", "дерев"), "ocean": ("океан", "морск", "моря"),
    "mountain": ("горн", "горы"), "space": ("космос", "планет", "звёзд", "звезд"),
    "city": ("город", "улиц"), "factory": ("завод", "производств"),
    "computer": ("компьютер", "процессор"), "electricity": ("электрич",),
}

# A stock image of a whole organ cannot stand in for a specifically named part.
DETAILS = {
    "retina": ("сетчат", "колбоч"), "lens": ("хрустал",),
    "cornea": ("роговиц",), "iris": ("радужк",), "pupil": ("зрач",),
    "optic nerve": ("зрительный нерв", "зрительного нерва",),
    "blind spot": ("слепое пятно", "слепого пятна",),
}
ANIMALS = {"cat": ("кош", "кот"), "bird": ("птиц",), "gecko": ("геккон",),
           "dog": ("собак", "пёс", "пес"), "owl": ("сов",), "reptile": ("рептил", "ящер",)}


def details(text):
    text = text.lower()
    return [key for key, aliases in DETAILS.items() if key in text or any(alias in text for alias in aliases)]


def terms(text):
    return set(re.findall(r"[a-zа-яё]{3,}", text.lower()))

def topics(text):
    words = terms(text)
    return [en for en, roots in TOPICS.items()
            if en in words or any(w.startswith(root) for w in words for root in roots)]

def queries(requirements, limit=4):
    counts = Counter()
    for row in requirements:
        req = normalize_requirement(dict(row))
        explicit = req.get("user_queries") or []
        values = explicit or details(req.get("quote", "")) or topics(req.get("quote", "")) or req.get("generated_queries") or [clean_search_phrase(req.get("quote", ""))]
        for value in dict.fromkeys(values):
            value = " ".join(str(value).split())[:100]
            if value:
                counts[value.casefold()] += 1
    return [q for q, _ in counts.most_common(min(4, max(1, limit)))]

def relevance(item, row):
    req = normalize_requirement(dict(row))
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    quote = req.get("quote", "")
    words = terms(quote) - STOP_WORDS
    for animal, aliases in ANIMALS.items():
        if re.search(r"\b" + animal + r"s?\b", text) and not (animal in quote.lower() or any(alias in quote.lower() for alias in aliases)):
            return 0.0
    specific = details(quote)
    if specific and not any(_matches_term(part, text) for part in specific):
        return 0.0
    # Match the original phrase, never the search query that produced this image.
    direct = sum(_matches_term(w, text) for w in words if len(w) >= 4)
    thematic = sum(bool(re.search(r"\b" + re.escape(t) + r"s?\b", text)) for t in topics(quote))
    forbidden = req.get("forbidden_substitutions") or []
    if any(_matches_term(t, text) for t in forbidden):
        return 0.0
    subjects = req.get("subject_entities") or []
    if req.get("requested_role") == "exact" and subjects and not any(_matches_term(t, text) for t in subjects):
        return 0.0
    return min(1.0, (direct + thematic * 2) / max(3, len(words) * .4))

def assign(items, requirements, limit=24):
    """Each result belongs to its strongest phrase once; at most two per phrase."""
    edges = []
    for item in items:
        for row in requirements:
            score = relevance(item, row)
            if score >= .25:
                edges.append((score, dict(row)["id"], item))
    edges.sort(key=lambda e: -e[0])
    used, counts, result = set(), Counter(), {}
    for score, rid, item in edges:
        identity = item.get("canonical_source_id") or f"{item.get('provider')}:{item.get('media_type')}:{item.get('external_id')}"
        if identity in used or counts[rid] >= 2:
            continue
        result.setdefault(rid, []).append(dict(item, scenario_relevance=score))
        used.add(identity)
        counts[rid] += 1
        if len(used) >= limit:
            break
    return result


def source_id(media_type, external_id):
    external_id = str(external_id).replace("pixabay_photo_", "").replace("pixabay_video_", "")
    return f"pixabay:{'photo' if media_type in ('photo', 'image') else 'video'}:{external_id}"


def used_sources(service, pid):
    from ..media_single_use import used_in_project
    doc = service.editing.get(pid)["document"]
    used = used_in_project(service, pid, doc)
    result = set()
    for row in service.db.all("SELECT s.external_id,i.media_type,i.id FROM media_library_sources s JOIN media_library_items i ON i.id=s.item_id WHERE s.project_id=? AND s.provider='pixabay'", (pid,)):
        if "item:" + str(row["id"]).casefold() in used:
            result.add(source_id(row["media_type"], row["external_id"]))
    for clip in doc.get("clips", []):
        layer = clip.get("footage_overlay") or clip
        if layer.get("provider") == "pixabay" and layer.get("external_id"):
            result.add(source_id(layer.get("media_type"), layer["external_id"]))
    return result


def collect(provider, requirements, options, cancellation, progress, excluded=()):
    media = options.get("pixabay_media_type", "both")
    media_types = [media] if media in ("photo", "video") else ["photo", "video"]
    search_queries = queries(requirements)
    items, calls = [], 0
    errors = []
    for query in search_queries:
        for media_type in media_types:
            if cancellation.is_set():
                return {}, calls
            progress(calls, len(search_queries) * len(media_types), query)
            try:
                response = provider.search(query=query, filters={"media_type": media_type, "per_page": 20}, cancellation=cancellation)
                if response.get("ok") is False:
                    raise RuntimeError(response.get("error") or "Pixabay не ответил")
                items.extend(response.get("items", []))
            except Exception as exc:
                errors.append(str(exc))
            calls += 1
    if errors and not items:
        raise RuntimeError(errors[0])
    items = [it for it in items if source_id(it.get("media_type"), it.get("external_id")) not in excluded]
    return assign(items, requirements), calls

"""Candidate ranking, content verification, and diversity assignment."""
import json
import logging
from typing import Dict, Any, List, Optional, Tuple

from ..media_matching import tokenize_words, stem_ru, calculate_match_score
from .entities import DEFAULT_CANON_ENTITIES

logger = logging.getLogger(__name__)

# Build alias and translation map from canon entities
_CANON_MAP: Dict[str, List[str]] = {}
for _ent in DEFAULT_CANON_ENTITIES:
    _all_names = [_ent["canonical_name"].lower()] + [a.lower() for a in _ent.get("aliases", [])]
    for _n in _all_names:
        _CANON_MAP[_n] = _all_names
        for _w in _n.split():
            if len(_w) >= 3 and _w not in _CANON_MAP:
                _CANON_MAP[_w] = _all_names
            _w_stem = stem_ru(_w)
            if len(_w_stem) >= 3 and _w_stem not in _CANON_MAP:
                _CANON_MAP[_w_stem] = _all_names
    _CANON_MAP[_ent["entity_key"].lower()] = _all_names


_CONTEXT_SYNONYMS: Dict[str, List[str]] = {
    "пасётся": ["grazing", "graze", "herd"],
    "пасется": ["grazing", "graze", "herd"],
    "срываются": ["falls", "cascading", "plunging", "rapids"],
    "срывается": ["falls", "cascading", "plunging", "rapids"],
    "озаряют": ["illuminating", "lighting", "lights", "glowing"],
    "озаряет": ["illuminating", "lighting", "lights", "glowing"],
    "идёт": ["leading", "walking", "marching"],
    "идет": ["leading", "walking", "marching"],
    "выходит": ["emerging", "creature", "character", "walking", "sculpture"],

    # Nature & Geography
    "столб": ["column", "pillar", "spout", "water"],
    "воды": ["water", "stream"],
    "вода": ["water", "stream"],
    "кипящей": ["boiling", "thermal", "hot", "steam"],
    "кипящий": ["boiling", "thermal", "hot", "steam"],
    "выбрасывает": ["erupting", "eruption", "spouting", "shooting"],
    "выбрасывать": ["erupting", "eruption", "spouting", "shooting"],
    "деревянный": ["wooden", "wood"],
    "деревян": ["wooden", "wood"],
    "знак": ["sign", "marker"],
    "въезд": ["entrance", "entry", "arch"],
    "въезда": ["entrance", "entry", "arch"],
    "льдинами": ["ice", "pack ice", "sea ice", "floe"],
    "льдина": ["ice", "pack ice", "sea ice", "floe"],
    "арктическими": ["arctic"],
    "арктический": ["arctic"],
    "перепрыгивает": ["jumping", "walking", "crossing", "traversing"],
    "перепрыгивать": ["jumping", "walking", "crossing", "traversing"],
    # Universes & Fiction
    "безупречных": ["unsullied"],
    "безупречные": ["unsullied"],
    "волосами": ["hair"],
    "серебряными": ["silver"],
    "серебряный": ["silver"],
    "главе": ["leading", "head"],
    "летит": ["flying", "flight"],
    "летать": ["flying", "flight"],
    "кронами": ["canopy", "treetop", "pines", "trees"],
    "крона": ["canopy", "treetop", "pines", "trees"],
    "леса": ["forest", "woods", "pines"],
    "лес": ["forest", "woods", "pines"],
    "тёмного": ["dark"],
    "тёмный": ["dark"],
    "огромный": ["huge", "giant", "enormous"],
    "мантии": ["robes", "cloak", "robe"],
    "мантия": ["robes", "cloak", "robe"],
    "гриффиндора": ["gryffindor"],
    "гриффиндор": ["gryffindor"],
    "палочку": ["wand"],
    "палочка": ["wand"],
    "волшебную": ["magic", "wand"],
    "волшебный": ["magic", "wand"],
    "держит": ["holding"],
    "держать": ["holding"],
    "мечей": ["swords", "blades"],
    "меч": ["sword", "blade"],
    "поверженных": ["fallen", "conquered"],
    "поверженный": ["fallen", "conquered"],
    "врагов": ["enemies"],
    "враг": ["enemy"],
    "тронном": ["throne"],
    "трон": ["throne"],
    "зале": ["hall", "room", "studio"],
    "зал": ["hall", "room", "studio"],
    "стоит": ["standing", "seat"],
    "стоять": ["standing", "seat"],
    "мчит": ["crossing", "speeding", "running"],
    "мчаться": ["crossing", "speeding", "running"],
    "парах": ["steam"],
    "пар": ["steam"],
    "виадуку": ["viaduct"],
    "виадук": ["viaduct"],
    "ледяными": ["ice", "icy"],
    "ледяной": ["ice", "icy"],
    "синими": ["blue"],
    "синий": ["blue"],
    "глазами": ["eyes", "eye"],
    "глаз": ["eye", "eyes"],
    "метели": ["blizzard", "snow"],
    "метель": ["blizzard", "snow"],
    "выходит": ["emerging", "walking"],
    "выходить": ["emerging", "walking"],
    "сжимает": ["holding", "clutching", "gripping"],
    "сжимать": ["holding", "clutching", "gripping"],
    "руках": ["hands", "hand"],
    "рука": ["hand", "hands"],
    "костлявых": ["bony"],
    "костлявый": ["bony"],
    "кольцо": ["ring", "one ring"],
    "всевластья": ["one ring", "power"],
    "всевластье": ["one ring", "power"],
    "пожелтение": ["yellowing", "yellow", "icterus"],
    "склер": ["sclera", "scleral"],
    "склера": ["sclera", "scleral"],
    "пациента": ["patient", "clinical"],
    "пациент": ["patient", "clinical"],
    "выраженная": ["severe", "pronounced"],
    "выраженный": ["severe", "pronounced"],
    "явное": ["evident", "presentation"],
    "явный": ["evident", "presentation"],
    "трубкой": ["pipe"],
    "трубка": ["pipe"],
    "кресле": ["chair", "armchair", "study"],
    "кресло": ["chair", "armchair", "study"],
    "задумчиво": ["thoughtful", "pensive"],
    "сидит": ["sitting", "seated", "study"],
    "сидеть": ["sitting", "seated", "study"],
    "бейкер-стрит": ["baker street"],
    "221б": ["221b"],
}

for _k, _vals in _CONTEXT_SYNONYMS.items():
    _CANON_MAP.setdefault(_k, []).extend(_vals)
    _st = stem_ru(_k)
    if len(_st) >= 3:
        _CANON_MAP.setdefault(_st, []).extend(_vals)

_RU_STOP_WORDS = {
    "в", "во", "на", "с", "со", "из", "под", "над", "у", "к", "ко", "по", "за", "о", "об", "от",
    "до", "для", "без", "при", "через", "между", "перед", "сквозь", "и", "а", "но", "да", "или",
    "ли", "то", "же", "бы", "не", "ни", "как", "так", "что", "чтобы", "это", "этот", "эта", "эти",
    "тот", "та", "те", "все", "всё", "весь", "вся", "свой", "своя", "своё", "его", "её", "их"
}

def _matches_term(term: str, text: str) -> bool:
    """Checks if term, its Russian stem, or its known aliases appear in text."""
    term_l = term.lower().strip()
    text_l = text.lower()
    if term_l in text_l:
        return True

    # Check known aliases/translations from canon map
    for al in _CANON_MAP.get(term_l, []):
        if al in text_l:
            return True

    # Check stem if cyrillic
    t_stem = stem_ru(term_l)
    if len(t_stem) >= 3:
        if t_stem in text_l:
            return True
        for al in _CANON_MAP.get(t_stem, []):
            if al in text_l:
                return True

    return False


def normalize_requirement(req: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures requirement dictionary has deserialized lists even if loaded directly from SQLite *_json columns."""
    norm = dict(req)
    for key, json_key in [
        ("subject_entities", "subject_entities_json"),
        ("mandatory_details", "mandatory_details_json"),
        ("forbidden_substitutions", "forbidden_substitutions_json"),
        ("origin_clip_ids", "origin_clip_ids_json"),
        ("generated_queries", "generated_queries_json"),
        ("user_queries", "user_queries_json")
    ]:
        if key not in norm or not isinstance(norm[key], list):
            val = norm.get(json_key)
            if isinstance(val, str) and val.strip():
                try:
                    norm[key] = json.loads(val)
                except Exception:
                    norm[key] = []
            elif not isinstance(norm.get(key), list):
                norm[key] = []
    return norm


def is_verb_action(w: str) -> bool:
    """Identifies finite verbs and verbal action stems in Russian quotes."""
    w = w.lower().strip()
    if len(w) < 3:
        return False
    # Exclude adjective endings
    if w.endswith(('ный', 'ная', 'ное', 'ные', 'ным', 'ных', 'ном', 'ной',
                   'ский', 'ская', 'ское', 'ские', 'ском', 'ских',
                   'тый', 'тая', 'тое', 'тые', 'щий', 'щей', 'щее', 'щие')):
        return False
    verb_endings = (
        'ает', 'яет', 'ует', 'юет', 'ивает', 'ывает',
        'етс', 'итс', 'утс', 'ютс', 'атс', 'ятс', 'отс', 'ётс',
        'ется', 'ится', 'утся', 'ются', 'атся', 'ятся', 'ётся',
        'ет', 'ит', 'ут', 'ют', 'ат', 'ят', 'ёт',
        'ать', 'ять', 'ить', 'еть', 'уть', 'сти', 'зти', 'чь'
    )
    for ve in verb_endings:
        if w.endswith(ve) and len(w) - len(ve) >= 2:
            return True
    st = stem_ru(w)
    if st in {'вход', 'вых', 'лет', 'ид', 'сид', 'сто', 'сп', 'отдых', 'держ', 'сжим',
              'пас', 'прыг', 'мч', 'беж', 'плыв', 'выбрасыв', 'озар', 'срыв', 'мчат', 'пад'}:
        return True
    return False


def extract_scene_context_terms(quote: str, subject_entities: List[str]) -> Tuple[List[str], List[str]]:
    """Extracts non-entity, non-stopword action and setting/location keywords from quote."""
    raw_words = [w.strip('.,!?:;"()«»') for w in quote.lower().split() if len(w.strip('.,!?:;"()«»')) >= 3]
    entity_terms = set()
    for ent in subject_entities:
        ent_l = ent.lower()
        entity_terms.update(tokenize_words(ent_l))
        for al in _CANON_MAP.get(ent_l, []):
            entity_terms.update(tokenize_words(al))
            t_stem = stem_ru(al)
            if len(t_stem) >= 3:
                entity_terms.add(t_stem)

    action_terms = []
    setting_terms = []
    for w in raw_words:
        if w in _RU_STOP_WORDS or w in entity_terms:
            continue
        if is_verb_action(w):
            action_terms.append(w)
        else:
            setting_terms.append(w)
    return action_terms, setting_terms


def evaluate_candidate_candidate(
    candidate: Dict[str, Any],
    requirement: Dict[str, Any],
    qwen_analysis: Optional[Dict[str, Any]] = None
) -> Tuple[str, str, str, List[str], float]:
    """
    Evaluates candidate against segment requirements and optional Qwen visual analysis.
    Returns: (semantic_status, identity_status, rights_status, reasons, rank_score)
    """
    req_norm = normalize_requirement(requirement)
    quote = (req_norm.get("quote") or "").lower()
    mandatory = [m.lower() for m in req_norm.get("mandatory_details", [])]
    forbidden = [f.lower() for f in req_norm.get("forbidden_substitutions", [])]
    requested_role = req_norm.get("requested_role", "exact")
    subject_entities = req_norm.get("subject_entities", [])

    title = (candidate.get("title") or "").lower()
    desc = (candidate.get("description") or "").lower()
    combined_text = f"{title} {desc}"
    reasons: List[str] = []

    # 1. Rights status
    lic_name = (candidate.get("license_name") or "").lower()
    if candidate.get("provider") == "local_library":
        rights_status = "permission_recorded"
        reasons.append("Материал из локальной медиатеки пользователя.")
    elif "cc" in lic_name or "creative commons" in lic_name or "public domain" in lic_name or "pixabay" in lic_name:
        rights_status = "recorded_license"
        reasons.append(f"Указана лицензия: {candidate.get('license_name')}.")
    else:
        rights_status = "unknown"
        reasons.append("Лицензия не зарегистрирована или требует отдельного подтверждения.")

    # 2. Check forbidden substitutions (Hard negative filter)
    for forb in forbidden:
        if _matches_term(forb, combined_text):
            reasons.append(f"Обнаружена недопустимая подмена: '{forb}'.")
            return ("rejected", "contradicted" if subject_entities else "not_required", rights_status, reasons, 0.0)

    # 3. Identity evaluation
    found_entity = False
    if not subject_entities:
        identity_status = "not_required"
    else:
        # Check if subject entity appears in title or description via aliases
        found_entity = any(_matches_term(ent, combined_text) for ent in subject_entities)
        if found_entity:
            identity_status = "confirmed" if requested_role == "exact" else "plausible"
            reasons.append(f"Подтверждено упоминание сущности: {', '.join(subject_entities)}.")
        else:
            identity_status = "unverified"
            reasons.append(f"Личность или объект {', '.join(subject_entities)} не подтверждены напрямую.")

    # 4. Check mandatory details
    missing_mandatory = []
    for mand in mandatory:
        if _matches_term(mand, combined_text):
            continue
        # If entity is confirmed and the mandatory detail is an alias or stem of that entity
        if found_entity and any(_matches_term(mand, ent) for ent in subject_entities):
            continue
        missing_mandatory.append(mand)

    # 5. Qwen Visual Analysis integration
    qwen_verified = False
    qwen_needs_review = False
    if qwen_analysis:
        qwen_sem = qwen_analysis.get("semantic_status") or qwen_analysis.get("status")
        qwen_action = qwen_analysis.get("action_matches")
        qwen_identity = qwen_analysis.get("identity_matches")
        qwen_reason = qwen_analysis.get("reason") or qwen_analysis.get("analysis_summary")

        if qwen_sem == "rejected" or qwen_action is False or qwen_identity is False:
            reasons.append(f"Визуальная проверка Qwen отклонила: {qwen_reason or 'Несоответствие действия или субъекта'}.")
            return (
                "rejected",
                "contradicted" if (subject_entities and qwen_identity is False) else identity_status,
                rights_status,
                reasons,
                0.0
            )
        elif qwen_sem == "needs_review":
            qwen_needs_review = True
            reasons.append(f"Визуальная проверка Qwen требует подтверждения: {qwen_reason or 'Действие или субъект не подтверждены полностью'}.")
        elif qwen_sem == "matched" and qwen_action is not False and qwen_identity is not False:
            qwen_verified = True
            reasons.append("Визуальная проверка Qwen подтвердила ключевой субъект и действие.")

    # 6. Semantic & Action/Location Evaluation
    action_terms, setting_terms = extract_scene_context_terms(quote, subject_entities)
    action_matches = any(_matches_term(at, combined_text) for at in action_terms) if action_terms else True
    setting_matches = any(_matches_term(st, combined_text) for st in setting_terms) if setting_terms else True
    context_matches = action_matches and (setting_matches or not setting_terms)

    quote_tokens = tokenize_words(quote)
    candidate_tokens = tokenize_words(combined_text)
    overlap = len(quote_tokens.intersection(candidate_tokens))
    score = round(min(1.0, overlap / max(1, len(quote_tokens) * 0.4)), 2)

    if missing_mandatory:
        semantic_status = "needs_review"
        reasons.append(f"Отсутствуют обязательные детали: {', '.join(missing_mandatory)}.")
        score = min(score, 0.45)
    elif identity_status == "contradicted":
        semantic_status = "rejected"
        score = 0.0
    elif identity_status == "confirmed":
        if requested_role == "exact":
            # If Qwen explicitly gave needs_review -> MUST stay needs_review!
            if qwen_needs_review:
                semantic_status = "needs_review"
                score = min(score, 0.5)
            # If Qwen visually verified -> matched
            elif qwen_verified:
                semantic_status = "matched"
                reasons.append("Визуальная проверка Qwen подтвердила сцену и действие.")
                score = max(score, 0.9)
            # If quote requires an action, but candidate does not match action -> needs_review!
            elif action_terms and not action_matches:
                semantic_status = "needs_review"
                reasons.append(f"Субъект подтвержден, но требуемое действие ('{' '.join(action_terms)}') не подтверждено текстом или моделью.")
                score = min(score, 0.5)
            # If text matches both action and setting -> matched
            elif context_matches:
                semantic_status = "matched"
                reasons.append("Подтверждено соответствие ключевой сущности и контекста сцены.")
                score = max(score, 0.85)
            else:
                semantic_status = "needs_review"
                reasons.append(f"Сущность подтверждена, но контекст сцены требует визуальной проверки.")
                score = min(score, 0.5)
        else:
            semantic_status = "matched"
            reasons.append("Субъект подтвержден для иллюстративной роли.")
            score = max(score, 0.75)
    elif identity_status == "unverified" and requested_role == "exact":
        semantic_status = "needs_review"
        reasons.append("Субъект не подтвержден полностью.")
        score = min(score, 0.5)
    elif score >= 0.5:
        semantic_status = "matched"
        reasons.append("Достаточный уровень текстового совпадения.")
        score = max(score, 0.7)
    else:
        semantic_status = "needs_review"
        reasons.append("Требуется визуальная проверка.")
        score = min(score, 0.4)

    return (semantic_status, identity_status, rights_status, reasons, score)


def enforce_project_diversity(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensures that the same external asset is not automatically proposed for multiple
    different scenes unless explicitly allowed by the operator.
    """
    seen_canonical_ids = set()
    result = []
    for c in candidates:
        can_id = c.get("canonical_source_id") or f"{c.get('provider')}:{c.get('external_id')}"
        if can_id in seen_canonical_ids:
            # Mark duplicate proposal
            c_copy = dict(c)
            c_copy["is_duplicate"] = True
            reasons = list(c_copy.get("reasons", []))
            reasons.append("Дубликат: этот материал уже предложен для другой сцены проекта.")
            c_copy["reasons"] = reasons
            if c_copy.get("semantic_status") == "matched":
                c_copy["semantic_status"] = "needs_review"
            result.append(c_copy)
        else:
            seen_canonical_ids.add(can_id)
            result.append(c)
    return result

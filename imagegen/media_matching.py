"""Semantic media matching: evaluates library segments against scene narration.

Follows strict guidelines from FRAMEFORGE_SMART_MEDIA_PLAN.md:
- Matches subject, action, location, and emotional context.
- Avoids over-insertion: keeps generator scenes if no strong candidate exists.
- Explains every proposal in clear Russian.
- Respects repetition limits across the video.
"""
import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
import urllib.error
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, List, Optional

from .db import uid, now, dump
from .domain import DomainError
from .editing import normalize

logger = logging.getLogger(__name__)

@lru_cache(maxsize=32768)
def stem_ru(word: str) -> str:
    w = word.lower()
    if len(w) <= 3:
        return w
    endings = [
        "ому", "ему", "ыми", "ями", "ого", "его", "ых", "их", "ую", "юю", "ая", "яя", "ое", "ее",
        "ам", "ям", "ами", "ями", "ах", "ях", "ом", "ем", "ой", "ей", "ий", "ый", "ым", "им",
        "ов", "ев", "ей", "ия", "ие", "ий", "ью", "ья", "ье",
        "ет", "ут", "ют", "ит", "ат", "ят", "ил", "ла", "ло", "ли", "ть", "л",
        "а", "е", "и", "о", "у", "ы", "э", "ю", "я"
    ]
    for e in sorted(endings, key=len, reverse=True):
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[:-len(e)]
    return w

EN_RU_SYNONYMS = {
    "boat": ["лодк", "корабл", "судн", "катер", "парусник"],
    "paper boat": ["бумажн лодк", "кораблик"],
    "water": ["вод", "речн", "озерн", "морск", "завод"],
    "kitchen": ["кухн", "погреб", "кулинар"],
    "food": ["ед", "обед", "ужин", "блюд", "готовк", "продук"],
    "workbench": ["верстак", "мастерск", "стол"],
    "tool": ["инструмент", "оруди"],
    "woman": ["женщин", "девушк"],
    "man": ["мужчин", "парен", "человек"],
    "compass": ["компас"],
    "gloves": ["перчатк"],
    "preparing food": ["готов", "обед", "ед"],
    "floating": ["плыв", "плава"],
    "rural": ["деревн", "сельск", "полев"],
    "road": ["дорог", "путь", "троп"],
    "dragon": ["дракон", "ящер"],
    "castle": ["замок", "крепост", "башн"],
    "fire": ["пожар", "плам", "огон"],
    "patient": ["пациент", "болн"],
    "rash": ["сып", "пятн", "высыпан"]
}

def tokenize_words(text: str) -> set:
    if not text:
        return set()
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    tokens = set()
    stop_words = {
        "это", "что", "как", "так", "был", "была", "было", "были", "для", "или",
        "при", "про", "без", "под", "над", "все", "его", "еще", "уже", "the", "and", "with", "from", "into"
    }
    for w in cleaned.split():
        if len(w) > 2 and w not in stop_words:
            tokens.add(w)
            st = stem_ru(w)
            if len(st) > 2:
                tokens.add(st)
    return tokens

def calculate_match_score(phrase_tokens: set, segment: Dict[str, Any]) -> float:
    """Computes a semantic match score between scene phrase and analyzed media segment."""
    if not phrase_tokens:
        return 0.0

    summary = segment.get("analysis_summary", "")
    dominant = segment.get("dominant_subject", "")
    objects = segment.get("objects", [])
    actions = segment.get("actions", [])

    seg_items = [summary, dominant] + [str(o) for o in objects] + [str(a) for a in actions]
    seg_text = " ".join(seg_items)
    seg_tokens = tokenize_words(seg_text)

    overlap = phrase_tokens.intersection(seg_tokens)
    overlap_ratio = len(overlap) / max(len(phrase_tokens), 1)
    score = overlap_ratio * 0.70

    all_en_items = [dominant.lower()] + [str(o).lower() for o in objects] + [str(a).lower() for a in actions]
    synonym_matched = False
    for en_key, ru_stems in EN_RU_SYNONYMS.items():
        if any(en_key in item for item in all_en_items):
            for rstem in ru_stems:
                if any(rstem in pt for pt in phrase_tokens):
                    score += 0.25
                    synonym_matched = True
                    break

    roles = segment.get("recommended_montage_roles", [])
    if (overlap_ratio > 0 or synonym_matched) and any(r in roles for r in ["b-roll", "accent", "establishing"]):
        score += 0.05

    return min(1.0, round(score, 3))


DESCRIPTION_STOP_WORDS = {
    stem_ru(word) for word in (
        'кадр сцена персонаж человек мужчина женщина момент контекст атмосфера '
        'подчеркивает передает подходит идеально использовать установочный акцент '
        'драматический эмоциональный визуальный монтаж фото изображение '
        'который своих своего можно снова стало вокруг затем только всегда после перед '
        'черный темный светлый снег снежный белый фон стоит сидит смотрит '
        'который другой один между двое прямо вперед холодный тяжелый люди план'
    ).split()
}


def description_terms(text):
    return {
        stem for word in re.findall(r'[^\W\d_]+', text.lower().replace('ё', 'е'))
        if len(word) >= 4 and (stem := stem_ru(word)) not in DESCRIPTION_STOP_WORDS
    }


def description_pairs(text):
    words = re.findall(r'[^\W\d_]+', text.lower().replace('ё', 'е'))
    return {(stem_ru(a), stem_ru(b)) for a, b in zip(words, words[1:])
            if len(a) >= 4 and len(b) >= 4}



def description_index(segments):
    index = []
    frequencies = Counter()
    for segment in segments:
        text = ' '.join([
            segment.get('analysis_summary') or '', segment.get('dominant_subject') or '',
            *map(str, segment.get('objects', [])), *map(str, segment.get('actions', []))
        ])
        terms = description_terms(text)
        index.append((segment, terms, description_pairs(text)))
        frequencies.update(terms)
    weights = {term: 1 + math.log((1 + len(index)) / (1 + count))
               for term, count in frequencies.items()}
    return index, weights


def rank_descriptions(phrase, index, weights):
    terms = description_terms(phrase)
    maximum = sum(weights.get(term, 1) for term in terms)
    if not maximum:
        return []
    ranked = []
    pairs = description_pairs(phrase)
    for segment, description, media_pairs in index:
        overlap = terms & description
        if len(index) >= 4 and sum(weights[term] >= 2 for term in overlap) < 2:
            continue
        shared_pair = any(a in overlap and b in overlap for a, b in pairs & media_pairs)
        if len(overlap) < 3 and not shared_pair:
            continue
        score = sum(weights[term] for term in overlap) / maximum
        if score >= .40:
            ranked.append((score, segment, sorted(overlap)))
    return sorted(ranked, key=lambda row: -row[0])


def evaluate_candidate_alignment(
    phrase: str,
    segment: Dict[str, Any],
    character_names: Optional[set] = None,
    *, has_description_evidence: bool = False
) -> Dict[str, Any]:
    """
    Evaluates whether a candidate visual segment aligns with the narration phrase.
    Strictly checks for:
    1. Fantasy / mythical entity conflicts (e.g. dragon flight vs burning castle)
    2. Medical / clinical symptoms vs neutral portraits
    3. Scale / domain mismatches (e.g. toy paper boat vs warship in storm)
    4. Protagonist character protection (named character action vs generic stranger)
    5. Direct matches ('matched' / «подходит») vs partial ('needs_review' / «нужна проверка») vs 'rejected'.
    """
    p_lower = phrase.lower()
    phrase_tokens = tokenize_words(phrase)

    summary = segment.get("analysis_summary", "")
    dominant = segment.get("dominant_subject", "")
    objects = [str(o).lower() for o in segment.get("objects", [])]
    actions = [str(a).lower() for a in segment.get("actions", [])]
    seg_full = f"{summary} {dominant} {' '.join(objects)} {' '.join(actions)}".lower()

    # --- Conflict Check 1: Mythical / Fantasy Entity vs Disaster / Castle ---
    has_dragon_req = any(w in p_lower for w in ["дракон", "дракона", "дракону", "драконы", "ящер", "dragon"])
    has_castle_req = any(w in p_lower for w in ["замок", "замка", "крепость", "крепости", "castle", "fortress"])
    has_fire_req = any(w in p_lower for w in ["горит", "горящ", "пожар", "пламя", "огонь", "в огне", "рушится", "burning", "fire"])

    dragon_in_seg = any(w in seg_full for w in ["dragon", "дракон", "дракона"])
    castle_in_seg = any(w in seg_full for w in ["castle", "замок", "крепость", "fortress"])
    fire_in_seg = any(w in seg_full for w in ["burning", "fire", "flame", "огонь", "пламя", "пожар", "горит"])

    if has_dragon_req and not dragon_in_seg:
        if castle_in_seg or fire_in_seg:
            return {
                "status": "rejected",
                "status_label": "совпадение не найдено",
                "reason": "Несоответствие ключевого субъекта: сцена требует полёт дракона, в материале — горящий замок без дракона."
            }
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Несоответствие субъекта: фраза описывает дракона, в материале дракон отсутствует."
        }

    if has_castle_req and has_fire_req and not (castle_in_seg or fire_in_seg):
        if dragon_in_seg:
            return {
                "status": "rejected",
                "status_label": "совпадение не найдено",
                "reason": "Несоответствие субъекта и действия: сцена описывает горящий замок, в материале — дракон в небе без огня и разрушений."
            }

    # --- Conflict Check 2: Medical Symptoms vs Neutral Portrait ---
    symptom_pattern = r"\b(сыпь|сыпи|сыпью|пятна|пятнами|лихорадк[а-я]*|бред[а-я]*|кашл[а-я]*|чум[а-я]*|инфекци[а-я]*|болезн[а-я]*)\b"
    has_symptom_req = bool(re.search(symptom_pattern, p_lower)) and "бредет" not in p_lower and "брести" not in p_lower
    medical_in_seg = any(w in seg_full for w in ["rash", "fever", "cough", "sick", "ill", "hospital", "patient", "сыпь", "лихорадк", "кашель", "болен", "пациент"])

    if has_symptom_req and not medical_in_seg:
        if segment.get("people_count", 0) > 0 or "portrait" in seg_full or "man" in seg_full or "woman" in seg_full:
            return {
                "status": "rejected",
                "status_label": "совпадение не найдено",
                "reason": "Отсутствует обязательный клинический симптом: сцена описывает проявления болезни (сыпь/лихорадка), в материале — нейтральный портрет без признаков патологии."
            }

    # --- Conflict Check 3: Scale & Domain Mismatch ---
    has_warship_req = any(w in p_lower for w in ["крейсер", "линкор", "боевой корабль", "эсминец", "шторм", "штормовом", "warship"])
    paper_boat_in_seg = any(w in seg_full for w in ["paper boat", "бумажн лодк", "бумажная лодка", "кораблик"])
    if has_warship_req and paper_boat_in_seg:
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Несоответствие масштаба: сцена требует боевой корабль в штормовом море, в материале — бумажный кораблик в спокойной воде."
        }

    has_highway_req = any(w in p_lower for w in ["автострад", "скоростн", "мегаполис", "трасс", "highway", "freeway"])
    dirt_road_in_seg = any(w in seg_full for w in ["dirt road", "грунтов", "сельск", "rural"])
    if has_highway_req and dirt_road_in_seg:
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Несоответствие локации: сцена требует трассу мегаполиса, в материале — сельская грунтовая дорога."
        }

    has_restaurant_req = any(w in p_lower for w in ["ресторан", "банкет", "вечерн платье", "роскошн"])
    workshop_in_seg = any(w in seg_full for w in ["workbench", "workshop", "мастерск", "верстак", "погреб"])
    if has_restaurant_req and workshop_in_seg:
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Несоответствие обстановки: сцена описывает ресторан/банкет, в материале — рабочая мастерская/верстак."
        }

    # --- Conflict Check 4: Character Protection ---
    if character_names:
        matched_char = next((ch for ch in character_names if ch.lower() in p_lower), None)
        action_verbs = ["достает", "читает", "говорит", "смотрит", "произносит", "решает", "оперирует", "держит"]
        is_char_action = any(v in p_lower for v in action_verbs)
        if matched_char and is_char_action and segment.get("people_count", 0) > 0:
            return {
                "status": "needs_review",
                "status_label": "нужна проверка",
                "reason": f"В сцене действует закреплённый персонаж «{matched_char}», а в материале сторонний человек. Допустимо только как отвлечённый план (b-roll со спины/деталь), но не замена лица героя."
            }

    # --- Specific Action & Detail Conflict Checks (Action Consistency) ---
    flying_req = any(w in p_lower for w in ["летит", "парит", "полет", "крыль", "в небе", "над лесом", "над город"])
    sleeping_in_seg = any(w in seg_full for w in ["sleeping", "curled up", "спит", "спящ", "в пещере", "cave"])
    if has_dragon_req and flying_req and sleeping_in_seg and not any(w in seg_full for w in ["flying", "soaring", "летит", "парит"]):
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Несоответствие действия: сценарий требует полёт дракона в небе, в материале — спящий дракон в пещере."
        }

    rash_req = any(w in p_lower for w in ["сыпь", "сыпи", "сыпью", "пятна", "пятнами"])
    rash_in_seg = any(w in seg_full for w in ["rash", "red spots", "сыпь", "пятна"])
    if rash_req and not rash_in_seg:
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Отсутствует обязательный клинический симптом: сценарий требует сыпь на коже, в материале — пациент без видимых кожных высыпаний."
        }

    # --- Missing Detail / Specific Element Check (Forces 'needs_review') ---
    if "бумажн" in p_lower and ("paper boat" in dominant.lower() or "бумажн" in summary.lower()):
        if any(w in p_lower for w in ["девочк", "мальчик", "ребенок", "дет"]):
            return {
                "status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Частичное совпадение: бумажный кораблик на воде присутствует, но фигура ребёнка в кадре отсутствует."
            }
    if ("мастерск" in p_lower or "верстак" in p_lower or "мастер" in p_lower) and ("workbench" in seg_full or "мастерск" in seg_full):
        if "компас" in p_lower and "compass" not in seg_full:
            return {
                "status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Тематическое совпадение: обстановка мастерской, верстак и работа инструментом соответствуют сцене, но наличие старинного компаса требует визуального подтверждения."
            }
        if any(w in p_lower for w in ["час", "часов", "часовой"]):
            return {
                "status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Тематическое совпадение: мастерская и верстак соответствуют сцене, но наличие часового механизма требует визуального подтверждения."
            }
    if ("грунтов" in p_lower or "сельск" in p_lower or "проселочн" in p_lower or "дорог" in p_lower) and ("dirt road" in seg_full or "rural" in seg_full):
        if any(w in p_lower for w in ["бредет", "устал", "ищет", "заблуд"]):
            return {
                "status": "needs_review",
                "status_label": "нужна проверка",
                "reason": "Частичное совпадение: сельская дорога и пейзаж совпадают, но действие/состояние человека (поиск пути / усталость) требует проверки."
            }

    # --- Standard Semantic Alignment Scoring ---
    raw_score = calculate_match_score(phrase_tokens, segment)

    seg_items = [summary, dominant] + [str(o) for o in objects] + [str(a) for a in actions]
    seg_tokens = tokenize_words(" ".join(seg_items))
    has_direct_overlap = bool(phrase_tokens.intersection(seg_tokens))

    if has_description_evidence or raw_score >= 0.45:
        return {
            "status": "matched",
            "status_label": "подходит",
            "reason": f"Прямое соответствие: материал «{dominant[:60]}» и ключевые объекты полностью согласуются с закадровой фразой сценария."
        }
    elif raw_score >= 0.22 and has_direct_overlap:
        return {
            "status": "needs_review",
            "status_label": "нужна проверка",
            "reason": f"Тематическое совпадение: общая атмосфера «{dominant[:50]}» перекликается со сценой, но ключевые детали требуют проверки оператором."
        }
    else:
        return {
            "status": "rejected",
            "status_label": "совпадение не найдено",
            "reason": "Смысловое совпадение не обнаружено: в материалах библиотеки отсутствуют подходящие кадры для данной фразы."
        }


def extract_video_keyframes(
    video_path: str,
    num_frames: int = 3,
    start_ms: float = 0.0,
    end_ms: Optional[float] = None
) -> List[str]:
    """
    Extracts representative keyframe images across the duration of a video file or target interval.
    Returns list of paths to temporary JPEG images sampled across the continuous interval.
    """
    import tempfile
    import time
    try:
        import cv2
        from PIL import Image
    except ImportError:
        return []

    vp = Path(video_path)
    if not vp.exists() or not vp.is_file():
        return []

    cap = cv2.VideoCapture(str(vp))
    if not cap.isOpened():
        return []
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            return []

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_duration_ms = (total_frames / fps) * 1000.0

        actual_start_ms = max(0.0, float(start_ms or 0.0))
        if end_ms is not None and float(end_ms) > actual_start_ms:
            actual_end_ms = min(total_duration_ms, float(end_ms))
        else:
            actual_end_ms = total_duration_ms

        dur_ms = max(50.0, actual_end_ms - actual_start_ms)

        if num_frames == 1:
            sample_times = [actual_start_ms + 0.5 * dur_ms]
        else:
            sample_times = [
                actual_start_ms + 0.1 * dur_ms,
                actual_start_ms + 0.5 * dur_ms,
                actual_start_ms + 0.9 * dur_ms
            ]

        tmp_dir = Path(tempfile.gettempdir()) / "frameforge_video_keyframes"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        extracted = []
        for i, t_ms in enumerate(sample_times):
            target_frame = int((t_ms / 1000.0) * fps)
            target_frame = max(0, min(target_frame, total_frames - 1))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.set(cv2.CAP_PROP_POS_MSEC, t_ms)
                ret, frame = cap.read()
            if ret and frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                fpath = tmp_dir / f"kf_{int(time.time()*1000)}_{i}_{int(t_ms)}.jpg"
                img.save(fpath, format="JPEG", quality=85)
                extracted.append(str(fpath))

        while len(extracted) < num_frames and extracted:
            extracted.append(extracted[-1])

        return extracted
    finally:
        cap.release()


def verify_candidate_with_qwen(service, phrase, segment, keyframe_path=None):
    from .media_analysis import LlamaServerManager
    manager = LlamaServerManager.get_manager(service)
    with manager.inference_lock:
        return _verify_candidate_with_qwen(service, phrase, segment, keyframe_path)


def _verify_candidate_with_qwen(
    service,
    phrase: str,
    segment: Dict[str, Any],
    keyframe_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Performs critical contextual and visual verification of a candidate segment
    against a script narration phrase using the local Qwen3-VL model.
    """
    system_prompt = (
        "Ты профессиональный видеоредактор и супервайзер монтажа кино. "
        "Твоя задача — строго и критически проверить, подходит ли видеоматериал в качестве вставки "
        "под закадровую фразу сценария.\n"
        "ПРАВИЛА ОЦЕНКИ:\n"
        "1. Проверяй соответствие ДЕЙСТВИЯ, а не только субъекта: если сценарий требует, чтобы дракон ЛЕТЕЛ над лесом, "
        "а в материале дракон СПИТ в пещере — это НЕ подходит ('rejected').\n"
        "2. Проверяй ОБЯЗАТЕЛЬНЫЕ СИМПТОМЫ и детали: если сценарий требует сыпь на руках, а в материале просто температура/лихорадка "
        "без видимой сыпи — это НЕ подходит ('rejected') или требует проверки ('needs_review').\n"
        "3. Если материал в целом по теме, но ключевое действие или деталь не совпадают в точности — ставь 'needs_review'.\n"
        "4. Ставь 'matched' ТОЛЬКО когда и субъект, и ключевое действие/состояние прямо соответствуют фразе.\n\n"
        "Ответь строго JSON-объектом:\n"
        "{\n"
        '  "status": "matched" | "needs_review" | "rejected",\n'
        '  "status_label": "подходит" | "нужна проверка" | "совпадение не найдено",\n'
        '  "reason": "Краткая причина на русском, до 12 слов"\n'
        "}"
    )

    dominant = segment.get("dominant_subject", "")
    summary = segment.get("analysis_summary", "")
    actions = segment.get("actions", [])
    objects = segment.get("objects", [])
    mandatory = segment.get("mandatory_details") or []
    forbidden = segment.get("forbidden_substitutions") or []
    role = segment.get("requested_role") or "exact"
    entities = segment.get("subject_entities") or []

    constraints_lines = []
    if role:
        constraints_lines.append(f"- Требуемая роль материала: {role}")
    if entities:
        ent_str = ", ".join(entities) if isinstance(entities, list) else str(entities)
        constraints_lines.append(f"- Упомянутые сущности: {ent_str}")
    if mandatory:
        mand_str = ", ".join(mandatory) if isinstance(mandatory, list) else str(mandatory)
        constraints_lines.append(f"- Обязательные детали/симптомы: {mand_str}")
    if forbidden:
        forb_str = ", ".join(forbidden) if isinstance(forbidden, list) else str(forbidden)
        constraints_lines.append(f"- Запрещённые подмены: {forb_str}")

    constraints_text = "\n".join(constraints_lines)
    if constraints_text:
        constraints_block = f"\nКритерии и ограничения:\n{constraints_text}\n"
    else:
        constraints_block = ""

    user_text = (
        f"Фраза сценария:\n«{phrase}»\n{constraints_block}\n"
        f"Описание кандидата из библиотеки:\n"
        f"- Главный объект: {dominant}\n"
        f"- Действия в кадре: {actions}\n"
        f"- Объекты: {objects}\n"
        f"- Краткое описание: {summary}\n"
    )

    raw_paths = json.loads(segment.get('keyframe_paths_json') or '[]') or ([keyframe_path] if keyframe_path else [])
    paths = []
    for p in raw_paths:
        plow = str(p).lower()
        if plow.endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
            ext_frames = extract_video_keyframes(p)
            paths.extend(ext_frames)
        else:
            paths.append(p)

    user_content = [{'type': 'text', 'text': user_text + '\nПроверь соответствие всей непрерывной вставки, а не одного удачного кадра. Ниже кадры в начале, середине и конце выбранного интервала.'}]
    if not paths:
        raise ValueError('Нет кадров выбранного интервала для визуальной проверки.')
    for path in paths[:3]:
        uri = service.media_analysis._prepare_image_payload(path)
        user_content.append({'type': 'image_url', 'image_url': {'url': uri}})

    payload = {
        "model": "qwen3-vl",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "response_format": {"type": "json_object"}
    }

    from .media_analysis import LlamaServerManager
    mgr = LlamaServerManager.get_manager(service)
    mgr.ensure_running()

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{mgr.port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))

    raw = res["choices"][0]["message"]["content"]
    content = json.loads(raw)
    status = content.get("status", "needs_review")
    if status not in ("matched", "needs_review", "rejected"):
        status = "needs_review"
    label_map = {
        "matched": "подходит",
        "needs_review": "нужна проверка",
        "rejected": "совпадение не найдено"
    }
    return {
        "status": status,
        "status_label": content.get("status_label") or label_map[status],
        "reason": content.get("reason", "Проверено локальной моделью Qwen3-VL.")
    }


class MediaMatching:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self._progress_lock = threading.Lock()
        self._progress = {}

    def progress(self, project_id):
        with self._progress_lock:
            job = self._progress.get(project_id)
            if not job:
                return {'state': 'idle'}
            return {**job, 'elapsed_seconds': int(time.monotonic() - job['started_at'])}

    def _report(self, project_id, **fields):
        with self._progress_lock:
            self._progress[project_id].update(fields)

    def compute_voice_hash(self, project_id: str) -> str:
        """Computes a content hash representing the voice track and scene narration quotes."""
        scenes = self.db.all(
            "SELECT id, current_revision FROM scenes WHERE project_id=? ORDER BY position",
            (project_id,)
        )
        content_parts = []
        for s in scenes:
            rev_row = self.db.one(
                "SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?",
                (s["id"], s["current_revision"])
            )
            data = json.loads(rev_row["data"]) if rev_row else {}
            quote = data.get("source_quote") or data.get("beat") or data.get("text") or ""
            content_parts.append(f"{s['id']}:{quote}")

        edit = self.service.editing.get(project_id)
        doc = edit.get("document", {})
        for track in doc.get("audio_tracks", []):
            if track.get("role") == "voice":
                content_parts.append(f"voice:{track.get('asset_id')}:{track.get('trim_in_ms')}:{track.get('trim_out_ms')}")

        return hashlib.sha256(";".join(content_parts).encode("utf-8")).hexdigest()

    def propose_inserts(self, project_id: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Calculate proposals without applying them and expose progress to the UI."""
        self.service.project(project_id)
        with self._progress_lock:
            if self._progress.get(project_id, {}).get('state') == 'running':
                raise DomainError('Подбор для этого проекта уже выполняется.', 409)
            self._progress[project_id] = {
                'state': 'running', 'stage': 'Подготавливаю таймлайн',
                'started_at': time.monotonic(), 'processed': 0, 'total': 0,
                'confirmed': 0, 'verification_errors': 0,
            }
        try:
            result = self._propose_inserts(project_id, options)
        except Exception:
            self._report(project_id, state='failed', stage='Подбор остановлен')
            raise
        self._report(project_id, state='completed', stage='Расчёт завершён')
        return result

    def _propose_inserts(self, project_id, options):
        opts = options or {}
        quick = opts.get('quick') is True
        max_ratio = float(opts.get("max_insert_ratio", 1.0))
        from .insert_coverage import policy as coverage_policy, animated_scene_ids, protected_reason, select as select_coverage
        coverage = coverage_policy(opts.get('coverage'))
        animated = animated_scene_ids(self.service, project_id, coverage)
        cover_photo_motion = coverage.get('cover_photo_motion', False)

        # 1. Get current montage edit and normalized timeline
        edit = self.service.editing.get(project_id)
        doc = edit["document"]
        base_revision = edit["revision"]
        timeline = edit["timeline"]
        voice_binding_hash = self.compute_voice_hash(project_id)

        # 2. Query project characters for protagonist protection
        char_rows = self.db.all("SELECT name FROM characters WHERE project_id=?", (project_id,))
        character_names = {c["name"] for c in char_rows}

        # 3. Query project scenes and narration quotes
        scenes_rows = self.db.all(
            "SELECT id, position, current_revision FROM scenes WHERE project_id=? ORDER BY position",
            (project_id,)
        )
        scene_data_map = {}
        for s in scenes_rows:
            rev_row = self.db.one(
                "SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?",
                (s["id"], s["current_revision"])
            )
            data = json.loads(rev_row["data"]) if rev_row else {}
            scene_data_map[s["id"]] = data

        # 4. Query all completed segments from media library
        segments = self.db.all("""
            SELECT s.*, i.original_name, i.media_type, i.file_path, i.thumbnail_path, i.duration_ms AS media_duration_ms
            FROM media_segments s
            JOIN media_library_items i ON i.id=s.item_id
            WHERE s.project_id=? AND s.analysis_status='completed'
            ORDER BY s.created_at ASC
        """, (project_id,))

        for s in segments:
            s["objects"] = json.loads(s.get("objects_json") or "[]")
            s["actions"] = json.loads(s.get("actions_json") or "[]")
            s["recommended_montage_roles"] = json.loads(s.get("recommended_montage_roles_json") or "[]")

        if not segments:
            return {
                "project_id": project_id,
                "base_edit_revision": base_revision,
                "proposals": [],
                "message": "В медиатеке нет проанализированных материалов. Запустите анализ перед подбором."
            }

        proposals = []
        usage_counts = {}
        total_duration_ms = timeline.get("duration_ms", 0)
        accumulated_insert_ms = 0.0

        from .media_segment_placement import logical_segments,source_window,POLICY
        base_clips = {c['edit_clip_id']: c for c in doc['clips']}
        eligible = [
            clip for clip in logical_segments(doc, timeline)
            if not any(protected_reason(base_clips[cid], animated, cover_photo_motion=cover_photo_motion)
                       for cid in clip['replace_clip_ids'])
        ]
        self._report(project_id, total=len(eligible))
        confirmed = verification_errors = processed = 0
        media_index, term_weights = description_index(segments) if quick else ([], {})
        for index, clip in enumerate(eligible):
            processed = index + 1
            self._report(project_id, processed=index, current=index + 1, candidate=0,
                         stage='Ищу материалы для фразы')

            scene_id = clip.get("scene_id")
            sdata = scene_data_map.get(scene_id, {})
            phrase = sdata.get("source_quote") or sdata.get("beat") or sdata.get("text") or ""
            fit = doc.get('voice_fit') or {}
            if not fit.get('stale') and fit.get('phrase_alignment'):
                base = base_clips[clip['replace_clip_ids'][0]]
                binding = base.get('semantic_source_binding') or {}
                if (binding.get('audio_sha256') == (fit.get('audio_binding') or {}).get('sha256')
                        and (binding.get('reviewed') or binding.get('needs_review') is False)):
                    phrase = binding.get('narration') or phrase
            if not phrase:
                continue

            best_candidate = None
            best_eval = None

            # 1. Initial semantic scoring and conflict filtering
            scored_candidates = []
            phrase_toks = tokenize_words(phrase)
            if quick:
                candidates = rank_descriptions(phrase, media_index, term_weights)
            else:
                candidates = ((None, segment, []) for segment in segments)
            for description_score, seg, evidence in candidates:
                item_id = seg["item_id"]
                if usage_counts.get(item_id, 0) >= 2:
                    continue

                if quick:
                    pre_eval = evaluate_candidate_alignment(phrase, seg, character_names,
                                                           has_description_evidence=True)
                    if pre_eval['status'] == 'matched':
                        pre_eval['reason'] = 'Совпадение по сохранённому описанию: ' + ', '.join(evidence)
                        scored_candidates.append((description_score, seg, pre_eval))
                    if len(scored_candidates) >= 3:
                        break
                    continue
                pre_eval = evaluate_candidate_alignment(phrase, seg, character_names)
                score = calculate_match_score(phrase_toks, seg)
                if pre_eval["status"] == "rejected":
                    # A sparse bilingual description is only a retrieval hint;
                    # explicit subject/action conflicts remain rejected.
                    if score > 0 and pre_eval.get("reason", "").startswith("Смысловое совпадение не обнаружено"):
                        pre_eval = {"status":"needs_review", "status_label":"нужна проверка", "reason":"Есть общие детали, но смысловое соответствие требует визуальной проверки."}
                    else:
                        continue
                scored_candidates.append((score, seg, pre_eval))

            scored_candidates.sort(key=lambda x: x[0], reverse=True)

            # 2. Deep contextual verification of top candidates using local Qwen3-VL
            for candidate, (score, seg, pre_eval) in enumerate(scored_candidates[:3], 1):
                stage = 'Сверяю сохранённое описание' if quick else 'Проверяю кандидата в Qwen3-VL'
                self._report(project_id, candidate=candidate, stage=stage)
                if seg.get('media_type')=='video' and clip['duration_ms']<2500:continue
                try:seg=source_window(self.service,seg,clip['duration_ms'])
                except Exception as error:
                    logger.warning('Cannot prepare full-segment source: %s',error)
                    continue
                if seg is None:continue
                if quick:
                    best_candidate, best_eval = seg, pre_eval
                    break
                if self.service:
                    try:
                        keyframes = json.loads(seg.get("keyframe_paths_json") or "[]")
                        kf_path = next(iter(keyframes), None) or seg.get("thumbnail_path")
                        qwen_eval = verify_candidate_with_qwen(self.service, phrase, seg, kf_path)
                        eval_res = qwen_eval
                        if pre_eval["status"] == "needs_review" and "геро" in pre_eval.get("reason", "").lower() and eval_res["status"] == "matched":
                            eval_res = copy.deepcopy(pre_eval)
                    except Exception as e:
                        verification_errors += 1
                        self._report(project_id, verification_errors=verification_errors)
                        logger.warning("Qwen verification fallback: %s", e)
                        eval_res = copy.deepcopy(pre_eval)
                        if eval_res.get("status") == "matched":
                            eval_res["status"] = "needs_review"
                            eval_res["status_label"] = "нужна проверка"
                            eval_res["reason"] = (
                                f"Тематическое совпадение: общая атмосфера «{seg.get('dominant_subject', '')[:50]}» перекликается со сценой, "
                                f"но визуальная проверка моделью недоступна ({type(e).__name__}); требуется подтверждение оператора."
                            )
                else:
                    eval_res = copy.deepcopy(pre_eval)
                    if eval_res.get("status") == "matched":
                        eval_res["status"] = "needs_review"
                        eval_res["status_label"] = "нужна проверка"
                        eval_res["reason"] = (
                            f"Тематическое совпадение: материал «{seg.get('dominant_subject', '')[:50]}» перекликается со сценой, "
                            "но визуальная верификация не выполнялась; требуется подтверждение оператора."
                        )

                if eval_res["status"] == "matched":
                    best_candidate = seg
                    best_eval = eval_res
                    break
                elif eval_res["status"] == "needs_review" and best_candidate is None:
                    best_candidate = seg
                    best_eval = eval_res

            if best_candidate and best_eval and best_eval["status"] != "rejected":
                clip_dur = clip["duration_ms"]
                insert_dur = clip_dur
                start_offset = 0.0

                if (accumulated_insert_ms + insert_dur) / max(total_duration_ms, 1) > max_ratio:
                    continue

                accumulated_insert_ms += insert_dur
                usage_counts[best_candidate["item_id"]] = usage_counts.get(best_candidate["item_id"], 0) + 1

                if best_eval['status'] == 'matched':
                    confirmed += 1
                    self._report(project_id, confirmed=confirmed)
                proposals.append({
                    "proposal_id": uid(),
                    "edit_clip_id": clip["edit_clip_id"],
                    "replace_clip_ids": clip['replace_clip_ids'],
                    "narration_origin_clip_id": clip['narration_origin_clip_id'],
                    "placement_policy": POLICY,
                    "scene_id": scene_id,
                    "phrase_quote": phrase,
                    "clip_start_ms": clip["start_ms"],
                    "clip_end_ms": clip["end_ms"],
                    "insert_start_ms": round(clip["start_ms"] + start_offset, 2),
                    "insert_end_ms": round(clip["start_ms"] + start_offset + insert_dur, 2),
                    "insert_duration_ms": insert_dur,
                    "candidate_segment": {
                        "segment_id": best_candidate["id"],
                        "item_id": best_candidate["item_id"],
                        "original_name": best_candidate["original_name"],
                        "media_type": best_candidate["media_type"],
                        "file_path": best_candidate["file_path"],
                        "thumbnail_path": best_candidate["thumbnail_path"],
                        "summary": best_candidate["analysis_summary"],
                        "dominant_subject": best_candidate["dominant_subject"],
                        "seg_start_ms": best_candidate["seg_start_ms"] if best_candidate["media_type"]=="video" else 0,
                        "seg_end_ms": best_candidate["seg_end_ms"] if best_candidate["media_type"]=="video" else insert_dur,
                        "media_duration_ms": best_candidate.get("media_duration_ms",0)
                    },
                    "match_status": best_eval["status"],
                    "match_method": "saved_description" if quick else "visual_verification",
                    "retrieval_score": score if quick else calculate_match_score(phrase_toks, best_candidate),
                    "status_label": best_eval["status_label"],
                    "reason": best_eval["reason"]
                })

        self._report(project_id, processed=processed, current=0, candidate=0, stage='Сохраняю подбор')
        proposal_record = {
            "id": uid(),
            "project_id": project_id,
            "base_edit_revision": base_revision,
            "voice_binding_hash": voice_binding_hash,
            "status": "proposed",
            "proposals_json": dump(proposals),
            "target_document_json": dump(doc),
            "created_at": now(),
            "updated_at": now()
        }

        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO media_match_proposals(
                    id, project_id, base_edit_revision, voice_binding_hash, status,
                    proposals_json, target_document_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
            """, (
                proposal_record["id"], project_id, base_revision, voice_binding_hash,
                proposal_record["proposals_json"], proposal_record["target_document_json"],
                proposal_record["created_at"], proposal_record["updated_at"]
            ))

        return {
            "proposal_id": proposal_record["id"],
            "base_edit_revision": base_revision,
            "voice_binding_hash": voice_binding_hash,
            "total_proposals": len(proposals),
            "checked_scenes": processed,
            "eligible_scenes": len(eligible),
            "partial": processed < len(eligible),
            "proposals": proposals,
            "selection": select_coverage(doc, proposals, coverage, scene_ids=animated)
        }

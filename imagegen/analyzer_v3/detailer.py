"""Qwen Visual Detailer with fail-closed schema validation, semantic character verification, and deterministic fallback."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

from imagegen.analyzer_v3.models import DocumentIR, EntityModalityV3, EntityType
from imagegen.analyzer_v3.planner_models import (
    CameraAngle,
    SelectedEventIR,
    VisualFramingPolicy,
    VisualShotType,
)

REQUIRED_SHOT_FIELDS = {
    "selected_id",
    "event_id",
    "shot_type",
    "camera_angle",
    "composition_focus",
    "mood_and_atmosphere",
}

OPTIONAL_ALLOWED_FIELDS = {
    "visual_action_description",
}

ALL_ALLOWED_FIELDS = REQUIRED_SHOT_FIELDS | OPTIONAL_ALLOWED_FIELDS

VALID_SHOT_TYPES = {
    VisualShotType.ESTABLISHING_WIDE.value,
    VisualShotType.MEDIUM_ACTION.value,
    VisualShotType.CLOSEUP_FOCUS.value,
    VisualShotType.PROP_DETAIL.value,
    VisualShotType.ENVIRONMENT_ATMOSPHERIC.value,
    VisualShotType.OVER_SHOULDER.value,
    VisualShotType.REACTION_SHOT.value,
}

VALID_CAMERA_ANGLES = {
    CameraAngle.EYE_LEVEL.value,
    CameraAngle.LOW_ANGLE.value,
    CameraAngle.HIGH_ANGLE.value,
    CameraAngle.DUTCH_ANGLE.value,
    CameraAngle.TOP_DOWN.value,
}

PHYSICAL_ACTION_REGEX = re.compile(
    r"\b(?:стоит|стоял|стояла|стояли|сидит|сидел|сидела|сидели|идёт|идет|шёл|шла|шли|смотрит|смотрел|смотрела|смотрели|повернулся|повернулась|улыбается|улыбнулся|держит|держал|входит|вошел|вошёл|вошла|вошли|выходит|вышел|вышёл|вышла|вышли|подходит|подошел|подошёл|подошла|подошли|бежит|бежал|бежала|бежали|на\s+переднем\s+плане|в\s+центре\s+комнаты|в\s+центре\s+сцены|в\s+центре\s+кадра)\b",
    re.IGNORECASE,
)

PRONOUN_PHYSICAL_ACTION_REGEX = re.compile(
    r"\b(?:он|она|оно|они|его|её|ее|их|персонаж|герой|фигура|человек)\b.*?\b(?:стоит|стоял|стояла|стояли|сидит|сидел|сидела|сидели|идёт|идет|шёл|шла|шли|смотрит|смотрел|смотрела|смотрели|подходит|подошел|подошёл|подошла|подошли|бежит|бежал|бежала|бежали|входит|вошел|вошёл|вошла|вошли|держит|держал|в\s+центре|на\s+переднем\s+плане)\b",
    re.IGNORECASE,
)

CINEMATIC_COMMON_WORDS = {
    # Russian cinematic and spatial terms
    "кадр", "сцена", "фокус", "план", "комната", "зал", "стол", "дверь", "шлюз", "окно", "перрон", "поезд",
    "свет", "тень", "стена", "окружение", "интерьер", "экстерьер", "деталь", "атмосфера", "реализм", "кинематографично",
    "напряжение", "драма", "ракурс", "профиль", "анфас", "силуэт", "телефон", "рация", "портрет", "экран",
    "пейзаж", "панорама", "крупный", "средний", "общий", "динамика", "статичный", "акцент", "внимание", "вид",
    "диспетчерская", "коридор", "здание", "кабинет", "рука", "лицо", "взгляд", "предмет", "прибор", "панель",
    "освещение", "композиция", "персонаж", "герой", "фигура", "человек", "напряженная", "драматичный",
    # Russian grammar words, pronouns, prepositions, conjunctions, adverbs
    "в", "на", "за", "перед", "после", "над", "под", "из", "к", "по", "у", "о", "об", "обо", "от", "для",
    "и", "а", "но", "да", "или", "либо", "то", "ни", "как", "так", "будто", "словно", "точно", "чтобы", "чтоб",
    "он", "она", "оно", "они", "его", "ее", "её", "их", "ему", "ей", "им", "этот", "эта", "это", "эти", "тот", "та", "то", "те",
    "кто", "что", "весь", "вся", "всё", "все", "сам", "сама", "сами", "каждый", "каждая", "каждое", "каждые",
    "внезапно", "затем", "потом", "сначала", "далее", "сейчас", "теперь", "тогда", "здесь", "тут", "там",
    "медленно", "быстро", "плавно", "резко", "ярко", "тускло", "тихо", "громко", "четко", "мягко", "прямо",
    # English cinematic and grammar terms
    "dynamic", "character", "focal", "point", "intense", "cinematic", "lighting", "scene", "shot", "focus",
    "camera", "angle", "dramatic", "realistic", "close", "closeup", "medium", "wide", "low", "high", "top",
    "down", "eye", "level", "the", "a", "an", "in", "on", "at", "of", "with", "by", "from", "to", "for",
    "and", "but", "or", "is", "are", "was", "were", "it", "he", "she", "they", "his", "her", "their",
    "atmosphere", "detail", "interior", "exterior", "view", "light", "shadow", "composition", "action", "description"
}

SHOT_JSON_SCHEMA = {
    "name": "cinematic_shots_refinement",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "shots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "selected_id": {"type": "string"},
                        "event_id": {"type": "string"},
                        "shot_type": {
                            "type": "string",
                            "enum": sorted(list(VALID_SHOT_TYPES)),
                        },
                        "camera_angle": {
                            "type": "string",
                            "enum": sorted(list(VALID_CAMERA_ANGLES)),
                        },
                        "composition_focus": {"type": "string"},
                        "mood_and_atmosphere": {"type": "string"},
                    },
                    "required": [
                        "selected_id",
                        "event_id",
                        "shot_type",
                        "camera_angle",
                        "composition_focus",
                        "mood_and_atmosphere",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["shots"],
        "additionalProperties": False,
    },
}


class VisualPlannerLLMDetailer(Protocol):
    """Protocol for LLM shot detailers (e.g. Qwen)."""

    def detail_shots(
        self,
        shots_payload: List[Dict[str, Any]],
        context_prompt: str,
    ) -> List[Dict[str, Any]]:
        """Sends shot refinement request to LLM and returns structured list."""
        ...

    def get_usage(self) -> Dict[str, Any]:
        """Returns token and call usage statistics."""
        ...


class V3QwenVisualDetailer:
    """Real HTTP adapter for Qwen / Ollama / OpenAI-compatible shot detailer."""

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:11434/api/generate",
        model: str = "qwen2.5:7b-instruct",
        timeout_sec: float = 30.0,
        api_key: str = "",
    ):
        self.api_url = api_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.api_key = api_key
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> Optional[V3QwenVisualDetailer]:
        """Creates a V3QwenVisualDetailer if configured in application settings."""
        enable = (
            settings.get("v3_qwen_detailer", False)
            or settings.get("enable_v3_qwen", False)
            or settings.get("qwen_enabled", False)
        )
        if not enable and not settings.get("qwen_api_url"):
            return None

        url = settings.get("qwen_api_url") or settings.get("ollama_url") or "http://127.0.0.1:11434/api/generate"
        model = settings.get("qwen_model") or "qwen2.5:7b-instruct"
        timeout = float(settings.get("qwen_timeout", 30.0))
        api_key = str(settings.get("qwen_api_key", ""))
        return cls(api_url=url, model=model, timeout_sec=timeout, api_key=api_key)

    def get_usage(self) -> Dict[str, Any]:
        return dict(self.usage)

    def detail_shots(
        self,
        shots_payload: List[Dict[str, Any]],
        context_prompt: str,
    ) -> List[Dict[str, Any]]:
        """Sends compact structured request to Qwen endpoint with JSON schema."""
        self.usage["calls"] += 1

        system_instruction = (
            "You are a cinematic storyboard director. Output JSON matching the exact schema with fields: "
            "selected_id, event_id, shot_type, camera_angle, composition_focus, mood_and_atmosphere. "
            "STRICT RULES: DO NOT invent new character names. ONLY feature allowed_physical_characters. Respect safe_framing_description."
        )

        user_content = json.dumps({
            "instruction": context_prompt,
            "shots": shots_payload,
        }, ensure_ascii=False)

        # Decide endpoint format: OpenAI chat completions vs Ollama generate
        if "/chat/completions" in self.api_url or "/v1" in self.api_url:
            req_data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": SHOT_JSON_SCHEMA,
                },
                "temperature": 0.1,
            }
        else:
            req_data = {
                "model": self.model,
                "prompt": f"{system_instruction}\n\nSchema: {json.dumps(SHOT_JSON_SCHEMA, ensure_ascii=False)}\n\nInput:\n{user_content}\n\nResponse:",
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            }

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "FrameForge-Analyzer-V3/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req_bytes = json.dumps(req_data).encode("utf-8")
        http_req = urllib.request.Request(self.api_url, data=req_bytes, headers=headers, method="POST")

        with urllib.request.urlopen(http_req, timeout=self.timeout_sec) as resp:
            raw_body = resp.read().decode("utf-8")

        resp_json = json.loads(raw_body)

        # Extract tokens usage
        if "usage" in resp_json:
            u = resp_json["usage"]
            self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            self.usage["completion_tokens"] += u.get("completion_tokens", 0)
            self.usage["total_tokens"] += u.get("total_tokens", 0)
        elif "prompt_eval_count" in resp_json:
            pt = resp_json.get("prompt_eval_count", 0)
            ct = resp_json.get("eval_count", 0)
            self.usage["prompt_tokens"] += pt
            self.usage["completion_tokens"] += ct
            self.usage["total_tokens"] += (pt + ct)

        # Extract content
        content_str = ""
        if "choices" in resp_json and len(resp_json["choices"]) > 0:
            content_str = resp_json["choices"][0].get("message", {}).get("content", "")
        elif "response" in resp_json:
            content_str = resp_json.get("response", "")

        parsed = json.loads(content_str)
        if isinstance(parsed, dict) and "shots" in parsed:
            return parsed["shots"]
        elif isinstance(parsed, list):
            return parsed
        else:
            raise ValueError(f"Unexpected Qwen root JSON structure: {type(parsed)}")


class QwenVisualDetailer:
    """Refines shot composition using LLM while enforcing strict fail-closed validation."""

    def __init__(self, detailer: Optional[VisualPlannerLLMDetailer] = None):
        self.detailer = detailer

    def refine_plan(
        self,
        selected_events: List[SelectedEventIR],
        doc_ir: DocumentIR,
        story_text: str,
    ) -> List[SelectedEventIR]:
        """Refines selected events with LLM recommendations. Falls back safely on any error."""
        if not self.detailer or not selected_events:
            return selected_events

        # Build compact payload
        shots_payload = []
        for s in selected_events:
            shots_payload.append({
                "selected_id": s.selected_id,
                "event_id": s.event_id,
                "event_text": s.span.text,
                "framing_policy": s.framing_policy.value,
                "allowed_physical_characters": list(s.primary_subject_entity_ids + s.secondary_entity_ids),
                "safe_framing_description": s.safe_framing_description,
                "current_shot_type": s.shot_type.value,
                "current_camera_angle": s.camera_angle.value,
            })

        prompt = (
            "Refine cinematic composition, camera angle, and mood for each selected event. "
            "DO NOT invent new characters, DO NOT modify event IDs, and strictly respect safe framing rules."
        )

        try:
            raw_response = self.detailer.detail_shots(shots_payload, prompt)
            if not isinstance(raw_response, list) or len(raw_response) != len(selected_events):
                for s in selected_events:
                    s.qwen_detailed = False
                    s.qwen_diagnostics = "Qwen output length mismatch or invalid root format; used deterministic plan"
                return selected_events

            # Build allowed name lookup tables from DocumentIR
            entity_map = {e.entity_id: e for e in doc_ir.entities}

            for s, r_item in zip(selected_events, raw_response):
                is_valid, val_error, refined_shot = self._validate_and_apply_shot(
                    s=s,
                    r_item=r_item,
                    entity_map=entity_map,
                )

                if is_valid:
                    s.shot_type = refined_shot["shot_type"]
                    s.camera_angle = refined_shot["camera_angle"]
                    # Action description stays deterministic from EventIR
                    s.composition_focus = refined_shot["composition_focus"]
                    s.mood_and_atmosphere = refined_shot["mood_and_atmosphere"]
                    s.qwen_detailed = True
                    s.qwen_diagnostics = None
                else:
                    s.qwen_detailed = False
                    s.qwen_diagnostics = val_error

            return selected_events

        except Exception as e:
            for s in selected_events:
                s.qwen_detailed = False
                s.qwen_diagnostics = f"Qwen refinement exception ({type(e).__name__}: {e}); retained deterministic plan"
            return selected_events

    def _validate_and_apply_shot(
        self,
        s: SelectedEventIR,
        r_item: Any,
        entity_map: Dict[str, Any],
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Validates single Qwen shot response fail-closed against schema, enums, and character constraints."""
        if not isinstance(r_item, dict):
            return False, "Qwen shot item is not a dictionary", None

        # 1. Check exact required fields
        item_keys = set(r_item.keys())
        missing_fields = REQUIRED_SHOT_FIELDS - item_keys
        if missing_fields:
            return False, f"Missing required Qwen shot fields: {sorted(missing_fields)}", None

        # 2. Check for extra unexpected fields outside allowed set
        extra_fields = item_keys - ALL_ALLOWED_FIELDS
        if extra_fields:
            return False, f"Unexpected extra fields in Qwen shot: {sorted(extra_fields)}", None

        # 3. Check selected_id and event_id match
        if r_item["selected_id"] != s.selected_id or r_item["event_id"] != s.event_id:
            return False, f"Qwen ID mismatch (expected {s.selected_id}/{s.event_id}, got {r_item.get('selected_id')}/{r_item.get('event_id')})", None

        # 4. Check shot_type enum validity
        raw_st = str(r_item["shot_type"]).lower()
        if raw_st not in VALID_SHOT_TYPES:
            return False, f"Unknown shot_type '{r_item['shot_type']}' not in allowed enums {sorted(VALID_SHOT_TYPES)}", None
        shot_type_enum = VisualShotType(raw_st)

        # 5. Check camera_angle enum validity
        raw_ca = str(r_item["camera_angle"]).lower()
        if raw_ca not in VALID_CAMERA_ANGLES:
            return False, f"Unknown camera_angle '{r_item['camera_angle']}' not in allowed enums {sorted(VALID_CAMERA_ANGLES)}", None
        camera_angle_enum = CameraAngle(raw_ca)

        # 6. Check string field types & non-empty
        focus = r_item["composition_focus"]
        mood = r_item["mood_and_atmosphere"]

        if not isinstance(focus, str) or not focus.strip():
            return False, "Empty or non-string composition_focus", None
        if not isinstance(mood, str) or not mood.strip():
            return False, "Empty or non-string mood_and_atmosphere", None

        # 7. Semantic character & entity validation
        allowed_entity_ids = set(s.primary_subject_entity_ids + s.secondary_entity_ids)
        allowed_names: Set[str] = set()
        for eid in allowed_entity_ids:
            if eid in entity_map:
                ent = entity_map[eid]
                for w in ent.canonical_name.lower().split():
                    allowed_names.add(w)
                for alias in ent.aliases:
                    for w in alias.lower().split():
                        allowed_names.add(w)

        # Forbidden non-physical entities
        forbidden_names: Set[str] = set()
        for eid in s.non_physical_entity_ids:
            if eid in entity_map:
                ent = entity_map[eid]
                for w in ent.canonical_name.lower().split():
                    forbidden_names.add(w)
                for alias in ent.aliases:
                    for w in alias.lower().split():
                        forbidden_names.add(w)

        action_desc_check = r_item.get("visual_action_description", "")
        combined_text = f"{action_desc_check} {focus} {mood}"
        combined_text_lower = combined_text.lower()

        # Rule 7a: Disallow non-physical entity depicted as physical participant
        for fn in forbidden_names:
            if fn in combined_text_lower:
                if s.framing_policy in (VisualFramingPolicy.PROP_DETAIL, VisualFramingPolicy.EMPTY_LOCATION, VisualFramingPolicy.AUDIO_ONLY_INDICATOR):
                    return False, f"Non-physical entity '{fn}' described in physical composition for {s.framing_policy.value}", None
                if PHYSICAL_ACTION_REGEX.search(combined_text):
                    return False, f"Non-physical entity '{fn}' depicted as physical actor in scene description", None

        # Rule 7b: Disallow pronoun physical actions for non-physical framings or when non-physical entities are present
        if s.non_physical_entity_ids or s.framing_policy in (
            VisualFramingPolicy.PROP_DETAIL,
            VisualFramingPolicy.EMPTY_LOCATION,
            VisualFramingPolicy.AUDIO_ONLY_INDICATOR,
            VisualFramingPolicy.SILHOUETTE_WITHOUT_IDENTITY,
            VisualFramingPolicy.REACTION_OF_CONFIRMED_CHARACTER,
        ) or not allowed_entity_ids:
            if PRONOUN_PHYSICAL_ACTION_REGEX.search(combined_text):
                return False, f"Physical pronoun action forbidden for non-physical entity or framing '{s.framing_policy.value}'", None

        # Rule 7c: Strict Anthropomym check (NO new anthroponyms or names of ANY kind)
        all_doc_words: Set[str] = set()
        for ent in entity_map.values():
            for w in ent.canonical_name.lower().split():
                all_doc_words.add(w)
            for alias in ent.aliases:
                for w in alias.lower().split():
                    all_doc_words.add(w)

        # Detect any capitalized words in focus / mood that are not in doc and not generic cinematic words
        words = re.findall(r"\b[A-ZА-ЯЁ][a-zа-яё]+\b", f"{focus} {action_desc_check}")
        for word in words:
            w_low = word.lower()
            if w_low in CINEMATIC_COMMON_WORDS:
                continue
            if w_low not in all_doc_words and len(word) >= 2:
                # Any capitalized word not in doc entities and not common word is rejected
                return False, f"Disallowed or invented named entity '{word}' in visual composition", None

        refined = {
            "shot_type": shot_type_enum,
            "camera_angle": camera_angle_enum,
            "composition_focus": focus.strip(),
            "mood_and_atmosphere": mood.strip(),
        }
        return True, None, refined

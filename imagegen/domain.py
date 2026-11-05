"""Validation, immutable input snapshots and prompt construction."""
from dataclasses import dataclass, field
import difflib
import hashlib
import json
import re
from typing import Optional, Dict, Any, List, Sequence
from .db import dump


MAX_SCENES = 2000
MAX_LOCATIONS = 200
MAX_PROPS = 200

# The analyser may omit a character from one scene even though the source clearly
# describes a person.  We use the source itself when deciding whether a "no people"
# validation is appropriate, instead of treating every unreferenced scene as empty.
_HUMAN_CUE = re.compile(
    r'\b(?:person|people|human|man|woman|men|women|boy|girl|child(?:ren)?|baby|'
    r'husband|wife|mother|father|son|daughter|friend|stranger|passerby|driver|'
    r'he|she|him|her|they|someone|anyone|\u0447\u0435\u043b\u043e\u0432\u0435\u043a\w*|\u043b\u044e\u0434\w*|\u043c\u0443\u0436\w*|\u0436\u0435\u043d\w*|\u043c\u0443\u0436\u0447\u0438\u043d\w*|\u0436\u0435\u043d\u0449\u0438\u043d\w*|'
    r'\u0434\u0435\u0432\u043e\u0447\u043a\w*|\u043c\u0430\u043b\u044c\u0447\u0438\u043a\w*|\u0440\u0435\u0431\u0451\u043d\u043e\u043a\w*|\u043c\u0430\u0442\u044c|\u043c\u0430\u043c\w*|\u043e\u0442\u0435\u0446\w*|\u043f\u0430\u043f\w*|\u0441\u044b\u043d\w*|\u0434\u043e\u0447\w*|\u043e\u043d\u0430|\u043e\u043d\u0438|\u0438\u0445|\u0438\u043c|\u0438\u043c\u0438|\u043e\u043d|\u0435\u0451|\u0435\u0433\u043e)\b',
    re.IGNORECASE,
)
# A pronoun such as "he", "his" or "его" says nothing about who should be
# rendered when the analyser failed to link a scene to a locked reference.
# Only a concrete role/person description may legitimately create an
# unreferenced one-off person in a frame.
_EXPLICIT_UNBOUND_PERSON_CUE = re.compile(
    r'\b(?:a|an|the)\s+(?:man|woman|boy|girl|child|guard|driver|waiter|waitress|doctor|nurse|employee|receptionist|staff member|'
    r'police officers?|detectives?|strangers?|passersby|carpenters?|tanners?|blacksmiths?|apprentices?|couriers?|messengers?|soldiers?|workers?)|'
    r'\b(?:another|other|second|third|different|unknown)\s+(?:woman|man|girl|boy|person|stranger|lady|gentleman|employee|worker)|'
    r'\b(?:другая|другой|другую|другим|другого|другие|вторая|второй|вторую|второго|вторым|третья|третий|третью|третьего|третьим|незнакомая|незнакомый|незнакомую|незнакомого|незнакомой|незнакомым|чужая|чужой|чужую|чужого|чужим)\s+'
    r'(?:женщин[а-яё]*|мужчин[а-яё]*|девушк[а-яё]*|парн[а-яё]*|человек\w*|сотрудниц[а-яё]*|сотрудник[а-яё]*)|'
    r'\b(?:мужчин[а-яё]*|женщин[а-яё]*|мальчик[а-яё]*|девочк[а-яё]*|реб[её]н[а-яё]*|охранник\w*|водител\w*|'
    r'врач\w*|медсестр\w*|полицейск\w*|детектив\w*|незнакомец\w*|незнакомка\w*|прохож\w*|сотрудник\w*|сотрудниц\w*|администратор\w*|'
    r'плотник\w*|кожевн\w*|кузнец\w*|ученик\w*|гонец\w*|послан\w*|солдат\w*|'
    r'воин\w*|ремесленник\w*|рабоч\w*)\b',
    re.IGNORECASE,
)

_DEPICTED_ON_MEDIA_CUE = re.compile(
    r'\b(?:фотографи[яиеюей]|фото|сним[оккаеуом]|портрет\w*|изображени[еяюем]|'
    r'photo(?:graph)?s?|pictures?|portraits?|snapshots?|images?)\b[^\n.,;!?]{0,50}\b(?:мужа|жены|героя|героини|человека|[а-яёa-z]+)|'
    r'\b(?:на\s+фотографи[ие]|на\s+фото|на\s+снимке|на\s+экране|на\s+мониторе|на\s+телевизоре|на\s+плакате|на\s+видеозаписи|в\s+телефоне|по\s+телевизору|в\s+видеозаписи|'
    r'shown\s+on\s+(?:the\s+)?(?:screen|phone|monitor|tv|television)|seen\s+on\s+(?:the\s+)?(?:screen|monitor|phone)|on\s+(?:the\s+)?phone\s+screen|in\s+a\s+photograph|video\s+recording|portrait\s+on\s+the\s+wall)\b',
    re.IGNORECASE
)

_VOICE_ONLY_PATTERNS = [
    r'\b(?:(?:услышал\w*|услышав\w*|слыш\w*|звучит\w*|звучал\w*|раздался|раздалась|раздалось|раздавались|раздавался|раздается|раздаётся|донесся|донёсся|донеслась|донеслось|донеслись|доносился|доносилась|доносилось|доносились|доносится|доносятся)\s+(?:[а-яёa-z]+\s+){0,3}(?:голос\w*|крик\w*|звук\w*|разговор\w*|ш[её]пот\w*|смех\w*)|(?:знакомый|чужой|далёкий|далекий|глухой|тихий|громкий|хриплый|приглуш[её]нный|женский|мужской|детский|родной)\s+(?:голос\w*|крик\w*|ш[её]пот\w*|смех\w*)|голос\s+за\s+дверью|голос\s+из\s+соседней\s+комнаты|голос\s+с\s+лестницы|голос\s+сверху|голос\s+снизу|голос\s+из-за\s+двери|слышен\s+голос|слышался\s+голос|крик\s+за\s+дверью|кричит\s+за\s+дверью)\b',
    r'\b(?:на\s+том\s+конце\s+провода|на\s+другом\s+конце|в\s+трубке|голос\s+в\s+трубке|голос\s+из\s+динамика|голос\s+по\s+телефону|голос\s+из\s+рации|голос\s+по\s+рации|по\s+рации|из\s+рации|в\s+рации|по\s+радио|из\s+радио|диктор\s+по\s+радио)\b',
    r'\b(?:heard\s+(?:[a-z]+\s+){0,3}(?:voice|cry|shout|whisper|call|laughter)|(?:voice|cry|shout|whisper)\s+(?:[a-z]+\s+){0,3}(?:from\s+(?:upstairs|downstairs|next\s+room|behind\s+the\s+door|the\s+hallway|the\s+corridor|outside)|over\s+the\s+phone|through\s+the\s+receiver|over\s+the\s+radio|from\s+the\s+speaker)|walkie[\s\-]*talkie|offscreen\s+voice|voiceover|voice\s+on\s+the\s+stairs|shouting\s+from\s+behind\s+the\s+door)\b'
]

_FEMALE_GENERIC_NOUNS = {
    'женщина', 'женщины', 'женщине', 'женщину', 'женщиной',
    'девушка', 'девушки', 'девушке', 'девушку', 'девушкой',
    'супруга', 'супруги', 'супруге', 'супругу', 'супругой',
    'жена', 'жены', 'жене', 'жену', 'женой',
    'сотрудница', 'сотрудницы', 'сотруднице', 'сотрудницу', 'сотрудницей',
    'кассирша', 'медсестра', 'администраторша',
    'woman', 'girl', 'wife', 'spouse', 'female employee'
}

_MALE_GENERIC_NOUNS = {
    'мужчина', 'мужчины', 'мужчине', 'мужчину', 'мужчиной',
    'парень', 'парня', 'парню', 'парнем',
    'супруг', 'супруга', 'супругу', 'супругом',
    'муж', 'мужа', 'мужу', 'мужем',
    'сотрудник', 'сотрудника', 'сотруднику', 'сотрудником',
    'охранник', 'охранника', 'охраннику', 'охранником',
    'водитель', 'врач', 'лесничий',
    'man', 'guy', 'husband', 'spouse', 'male employee', 'guard', 'driver'
}

_DISTINCT_PERSON_MODIFIERS = {
    'другая', 'другой', 'другую', 'другим', 'другого', 'другие', 'других', 'другими',
    'вторая', 'второй', 'вторую', 'второго', 'вторым', 'вторыми',
    'третья', 'третий', 'третью', 'третьего', 'третьим', 'третьими',
    'ещё одна', 'еще одна', 'ещё один', 'еще один',
    'незнакомка', 'незнакомки', 'незнакомку', 'незнакомкой',
    'незнакомец', 'незнакомца', 'незнакомцу', 'незнакомцем',
    'незнакомая', 'незнакомый', 'незнакомую', 'незнакомого', 'незнакомой', 'незнакомым', 'незнакомые', 'незнакомых',
    'чужая', 'чужой', 'чужую', 'чужого', 'чужим', 'чужие', 'чужих', 'чужими',
    'another', 'other', 'second', 'third', 'different', 'unknown', 'stranger'
}

_ACTIVE_VISUAL_VERBS = re.compile(
    r'\b(?:вош[её]л|вошла|вошли|застыл|застыла|застыли|замер|замерла|замерли|'
    r'поднялся|поднялась|поднялись|повернулся|повернулась|повернулись|'
    r'положил|положила|положили|посмотрел|посмотрела|посмотрели|сел|села|сели|'
    r'встал|встала|встали|вышел|вышла|вышли|подош[её]л|подошла|подошли|'
    r'entered|froze|stopped|turned|looked|stepped|stood|sat|approached|walked|climbed)\b',
    re.IGNORECASE
)

def generate_episodic_role_id(event_id: Optional[str], role_label: str, index: int = 1) -> str:
    from .cast import slugify_id
    evt = event_id or "event_01"
    slug = slugify_id(role_label).removeprefix("char_")
    return f"episodic_role_{evt}_{slug}"

def extract_scene_staging_count(scene: Dict[str, Any]) -> Optional[int]:
    shot = str(scene.get('shot') or '').casefold()
    beat = str(scene.get('beat') or '').casefold()
    comp = str(scene.get('composition') or '').casefold()
    # Source narration and reviewed dialogue may count absent relatives or abstract
    # "both sides". Only explicit framing/composition specifies bodies in the shot.
    narration = scene.get('source_alignment') == 'exact_text_frame' or bool(scene.get('project_review'))
    text = f"{shot} {'' if narration else beat} {comp}"

    # 0 figures / uninhabited / atmosphere
    if re.search(r'\b(?:empty\s+(?:room|setting|hall|landscape|space|forest|street)|uninhabited|no\s+people|безлюдн\w*|пустая\s+комната|пустое\s+помещение)\b', text):
        return 0

    # Explicit numbers (e.g. 11 people, 25 figures, 100 participants)
    num_match = re.search(r'\b(\d+)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', text, re.I)
    if num_match:
        return int(num_match.group(1))

    # Verbal number patterns
    num_map = [
        (10, re.compile(r'\b(?:ten|десять|десятеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (9, re.compile(r'\b(?:nine|девять|девятеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (8, re.compile(r'\b(?:eight|восемь|восьмеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (7, re.compile(r'\b(?:seven|семь|семеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (6, re.compile(r'\b(?:six|шесть|шестеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (5, re.compile(r'\b(?:five|пять|пятеро)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (4, re.compile(r'\b(?:four|четыре|четверо)\s+(?:figures|people|characters|participants|men|women|actors|человек\w*|фигур\w*|персонаж\w*|участник\w*)\b', re.I)),
        (3, re.compile(r'\b(?:three\s+(?:figures|people|characters|participants|men|women|actors)|трое|троих|три\s+(?:человека|фигуры|персонажа|участника)|три\s+фигуры|three\s+figures|трио)\b', re.I)),
        (2, re.compile(r'\b(?:two[\s\-]*shot|2[\s\-]*shot|two\s+(?:figures|people|characters|participants|men|women)|двое|двоих|два\s+(?:человека|персонажа|участника)|две\s+фигуры|two\s+figures|обе|оба|both)\b', re.I)),
        (1, re.compile(r'\b(?:alone|solo\s+shot|solo|одна\s+в\s+комнате|один\s+в\s+комнате|в\s+одиночестве|соло)\b', re.I)),
    ]
    for count, pat in num_map:
        if pat.search(text):
            return count

    # A list of materials, places or eras is not a list of three people.
    # Only literal human-role nouns or explicitly declared character names
    # support this fallback; explicit numeric person counts above still win.
    people = re.compile(r'^(?:man|woman|girl|boy|child|person|soldier|guard|resident|worker|archaeologist|'
                        r'мужчин\w*|женщин\w*|девушк\w*|мальчик\w*|реб[её]нок|солдат\w*|воин\w*|'
                        r'часов\w*|жител\w*|рабоч\w*|археолог\w*)$', re.I)
    declared = {str(name).casefold() for name in scene.get('characters', [])}
    for match in re.finditer(r'\b([a-zа-яё]+)\s*,\s*([a-zа-яё]+)\s+(?:и|and)\s+(?:(?:другая|другой|another)\s+)?([a-zа-яё]+)\b', text):
        if all(people.match(noun) or noun in declared for noun in match.groups()):
            return 3

    return None

SHOT_TYPE_ESTABLISHING = 'establishing'
SHOT_TYPE_MAIN_ACTION = 'main_action'
SHOT_TYPE_PROTAGONIST_REACTION = 'protagonist_reaction'
SHOT_TYPE_PROP_DETAIL = 'prop_detail'
SHOT_TYPE_ACTION_AFTERMATH = 'action_aftermath'
SHOT_TYPE_ATMOSPHERE = 'atmosphere'

VALID_SHOT_TYPES = {
    SHOT_TYPE_ESTABLISHING,
    SHOT_TYPE_MAIN_ACTION,
    SHOT_TYPE_PROTAGONIST_REACTION,
    SHOT_TYPE_PROP_DETAIL,
    SHOT_TYPE_ACTION_AFTERMATH,
    SHOT_TYPE_ATMOSPHERE,
}

BANNED_FILLER_NAMES = {
    'wanderer', 'man', 'person', 'traveler', 'someone', 'figure', 'human',
    'странник', 'путник', 'человек', 'фигура', 'незнакомец', 'мужчина', 'женщина', 'прохожий'
}

_SEASON_PATTERNS = {
    'winter': re.compile(r'\b(?:winter|snow|blizzard|frost|зима|зимн\w*|снег\w*|мороз\w*|метел\w*|вьюг\w*)\b', re.IGNORECASE),
    'summer': re.compile(r'\b(?:summer|heat|sun|sunshine|лето|летн\w*|жар\w*|зной\w*)\b', re.IGNORECASE),
    'autumn': re.compile(r'\b(?:autumn|fall|foliage|leaves|осень|осенн\w*|листопад\w*)\b', re.IGNORECASE),
    'spring': re.compile(r'\b(?:spring|bloom|thaw|весна|весенн\w*|капел\w*|оттепел\w*)\b', re.IGNORECASE),
}

_DAY_PHASE_PATTERNS = {
    'morning': re.compile(r'\b(?:morning|dawn|sunrise|утро|утр\w*|рассвет\w*|заря)\b', re.IGNORECASE),
    'night': re.compile(r'\b(?:night|midnight|darkness|ночь|ночн\w*|полночь|тьма)\b', re.IGNORECASE),
    'evening': re.compile(r'\b(?:evening|sunset|dusk|twilight|вечер|вечерн\w*|закат\w*|сумерк\w*)\b', re.IGNORECASE),
    'day': re.compile(r'\b(?:day|afternoon|midday|noon|день|дневн\w*|полдень)\b', re.IGNORECASE),
}

_WEATHER_PATTERNS = {
    'snowy': re.compile(r'\b(?:snow|snowing|blizzard|снег|снегопад|пурга|метель)\b', re.IGNORECASE),
    'rainy': re.compile(r'\b(?:rain|raining|downpour|shower|дождь|ливень|дожд\w*)\b', re.IGNORECASE),
    'foggy': re.compile(r'\b(?:fog|mist|haze|туман|мгла|дымка)\b', re.IGNORECASE),
    'clear': re.compile(r'\b(?:clear|sunny|bright|ясно|солнечно|безоблачно)\b', re.IGNORECASE),
    'cloudy': re.compile(r'\b(?:cloudy|overcast|clouds|пасмурно|облачно|тучи)\b', re.IGNORECASE),
}

_EXACT_RANGE = re.compile(
    r'\b(120|1[01]\d|[1-9]?\d)\s*[-–—‑\u2011]\s*(120|1[01]\d|[1-9]?\d)\s*(?:years?[- ]old|year[- ]old|y/o|yo|\u043b\u0435\u0442|\u0433\u043e\u0434\u0430?|\u0433\u043e\u0434)?\b',
    re.IGNORECASE
)
_EXACT_AGE = re.compile(
    r'\b(?:aged\s+|age\s+|\u0432\s+\u0432\u043e\u0437\u0440\u0430\u0441\u0442\u0435\s+)?(120|1[01]\d|[1-9]?\d)[\s-]*(?:years?[- ]old|year[- ]old|y/o|yo|\u043b\u0435\u0442|\u0433\u043e\u0434\u0430?|\u0433\u043e\u0434)\b|\baged\s+(\d{1,2})\b',
    re.IGNORECASE
)
_DECADE_AGE = re.compile(r'\b([1-9]0)s\b', re.IGNORECASE)
_WRITTEN_EXACT_AGES = (
    (re.compile(r'\b(?:двадцати\s+пяти|двадцать\s+пять)(?:\s+лет)?\b', re.IGNORECASE), 25),
    (re.compile(r'\b(?:twenty[- ]five)(?:[- ]years?[- ]old)?\b', re.IGNORECASE), 25),
)
_MIDDLE_AGED = re.compile(
    r'\b(?:middle[- ]aged|mid[- ]adult|\u0441\u0440\u0435\u0434\u043d\u0438\u0445\s+\u043b\u0435\u0442|\u0441\u0440\u0435\u0434\u043d\u0435\u0433\u043e\s+\u0432\u043e\u0437\u0440\u0430\u0441\u0442\u0430)\b',
    re.IGNORECASE
)
_YOUNG_ADULT = re.compile(r'\b(?:young(?:\s+adult)?|\u043c\u043e\u043b\u043e\u0434\w*|\u044e\u043d\w*)\b', re.IGNORECASE)
_TEENAGER = re.compile(r'\b(?:teenager|teen|\u043f\u043e\u0434\u0440\u043e\u0441\u0442\w*)\b', re.IGNORECASE)
_ELDERLY = re.compile(
    r'\b(?:elderly|senior|geriatric|grandm\w*|grandfath\w*|'
    r'old\s+(?:man|woman|person|lady|gentleman|fellow|couple)|'
    r'very\s+old|old-aged|'
    r'\u043f\u043e\u0436\u0438\u043b\w*|\u0441\u0442\u0430\u0440\u0438\w*|\u0441\u0442\u0430\u0440\u0443\u0445\w*|'
    r'\u0431\u0430\u0431\u0443\u0448\u043a\w*|\u0434\u0435\u0434\u0443\u0448\u043a\w*)\b',
    re.IGNORECASE
)
_CHILD = re.compile(r'\b(?:child|kid|boy|girl|\u0440\u0435\u0431\u0451\u043d\u043e\u043a\w*|\u043c\u0430\u043b\u044c\u0447\u0438\u043a\w*|\u0434\u0435\u0432\u043e\u0447\u043a\w*)\b', re.IGNORECASE)
_FEMALE_CUE = re.compile(r'\b(?:woman|female|girl|daughter|wife|mother|sister|\u0436\u0435\u043d\u0449\u0438\u043d\w*|\u0434\u043e\u0447\w*|\u0436\u0435\u043d\u0430|\u043c\u0430\u0442\u044c|\u0441\u0435\u0441\u0442\u0440\w*)\b', re.IGNORECASE)
_MALE_CUE = re.compile(r'\b(?:man|male|boy|son|husband|father|brother|\u043c\u0443\u0436\u0447\u0438\u043d\w*|\u0441\u044b\u043d\w*|\u043c\u0443\u0436|\u043e\u0442\u0435\u0446\w*|\u0431\u0440\u0430\u0442\w*)\b', re.IGNORECASE)


def age_constraint(description):
    """Extract an explicit age range from a character description when present."""
    if not isinstance(description, str):
        return None
    # 1. Explicit numeric ranges e.g. 70-75 years old, 35-40 лет
    rng = _EXACT_RANGE.search(description)
    if rng:
        mn, mx = int(rng.group(1)), int(rng.group(2))
        if mn > mx:
            mn, mx = mx, mn
        if 0 < mn <= 120 and 0 < mx <= 120 and mx - mn <= 25:
            return {'minimum': mn, 'maximum': mx, 'label': f'{mn}–{mx} years old'}
    # 2. Exact single age e.g. 42 года, 35-year-old, aged 35
    exact = _EXACT_AGE.search(description)
    if exact:
        age_val = exact.group(1) or exact.group(2)
        if age_val:
            age = int(age_val)
            return {'minimum': age, 'maximum': age, 'label': f'{age} years old'}
    # 3. Common written-out exact ages found in Russian and English character notes.
    for pattern, age in _WRITTEN_EXACT_AGES:
        if pattern.search(description):
            return {'minimum': age, 'maximum': age, 'label': f'{age} years old'}
    # 4. Decades e.g. in her 30s, in his 40s
    decade = _DECADE_AGE.search(description)
    if decade:
        decade_start = int(decade.group(1))
        return {'minimum': decade_start, 'maximum': decade_start + 9,
                'label': f'{decade_start}–{decade_start + 9} years old'}
    # 4. Middle-aged / средних лет
    if _MIDDLE_AGED.search(description):
        return {'minimum': 40, 'maximum': 55, 'label': '40–55 years old'}
    # 5. Young adult / молодой
    if _YOUNG_ADULT.search(description):
        return {'minimum': 25, 'maximum': 35, 'label': '25–35 years old'}
    # 6. Teenager / подросток
    if _TEENAGER.search(description):
        return {'minimum': 13, 'maximum': 19, 'label': '13–19 years old'}
    # 7. Elderly (with negative phrases like "do not make elderly" stripped)
    sanitized = re.sub(
        r'\b(?:do\s+not|don[\'’]t|not|never|\u0431\u0435\u0437|\u043d\u0435)\s+(?:make\s+(?:her|him|them)\s+)?(?:elderly|senior|old|\u0441\u0442\u0430\u0440\w*|\u043f\u043e\u0436\u0438\u043b\w*)\b',
        '',
        description,
        flags=re.IGNORECASE
    )
    if _ELDERLY.search(sanitized):
        return {'minimum': 65, 'maximum': 85, 'label': '65–85 years old'}
    # 8. Child / ребёнок
    if _CHILD.search(description):
        return {'minimum': 6, 'maximum': 17, 'label': '6–17 years old'}
    return None


def gender_constraint(description, name=''):
    """Derive a stable gender requirement from an explicit role or description."""
    text = f'{description or ""} {name or ""}'
    female = bool(_FEMALE_CUE.search(text))
    male = bool(_MALE_CUE.search(text))
    if female and not male:
        return {'value': 'female', 'label': 'woman'}
    if male and not female:
        return {'value': 'male', 'label': 'man'}
    return None


def character_requirements(description, name='', entity_type=None):
    """Stable visual requirements used by casting and its approval gate."""
    if is_nonhuman_character({'name': name, 'description': description, 'entity_type': entity_type}):
        return {'gender': None, 'age': None, 'one_person': False, 'entity_type': 'nonhuman'}
    gender = gender_constraint(description, name)
    age = age_constraint(description)
    if gender and age and age['maximum'] <= 17:
        gender = {**gender, 'label': 'girl' if gender['value'] == 'female' else 'boy'}
    if gender and not age:
        # The source did not provide a number. Use domain-specific generational roles:
        combined = f'{description} {name}'
        if re.search(r'\b(?:daughter|son|\u0434\u043e\u0447\w*|\u0441\u044b\u043d\w*)\b', combined, re.IGNORECASE):
            age = {'minimum': 30, 'maximum': 42, 'label': '30–42 years old'}
        elif re.search(r'\b(?:granddaughter|grandson|\u0432\u043d\u0443\u043a\w*)\b', combined, re.IGNORECASE):
            age = {'minimum': 10, 'maximum': 22, 'label': '10–22 years old'}
        elif re.search(r'\b(?:grandmother|grandfather|\u0431\u0430\u0431\u0443\u0448\u043a\w*|\u0434\u0435\u0434\u0443\u0448\u043a\w*)\b', combined, re.IGNORECASE):
            age = {'minimum': 65, 'maximum': 85, 'label': '65–85 years old'}
        else:
            age = {'minimum': 30, 'maximum': 55, 'label': '30–55 years old'}
    return {'gender': gender, 'age': age, 'one_person': True}


def differentiate_character_visual_features(name: str, description: str = '', gender: Optional[Dict[str, Any]] = None, age: Optional[Dict[str, Any]] = None) -> str:
    """Generate unique, non-overlapping physical appearance cues (hair, face structure, eye color, facial hair, styling)
    to ensure characters in the cast never look like clones of each other."""
    desc_lower = (description or '').lower()
    name_clean = (name or '').strip()

    char_seed = int(hashlib.md5(name_clean.casefold().encode('utf-8')).hexdigest()[:8], 16) if name_clean else 42

    is_female = bool(gender and gender.get('value') == 'female') or any(w in desc_lower for w in ('female', 'woman', 'girl', 'женщин', 'девушк', 'девочк'))

    male_hairstyles = [
        'short dark brown wavy hair, neatly textured',
        'cropped black buzz cut, clean modern styling',
        'sandy brown textured crew cut with slight side part',
        'thick dark brown tousled hair, medium short length',
        'receding dark hair with natural silver-touched temples',
        'auburn brown wavy hair swept slightly back',
        'straight dark chestnut hair with subtle texture',
        'close-shaved dark hair with athletic fade'
    ]

    female_hairstyles = [
        'shoulder-length wavy chestnut brown hair',
        'straight dark brown hair with center part framing the face',
        'curly honey-blonde hair, natural volume',
        'textured auburn bob cut with subtle layers',
        'long dark wavy hair loosely styled over shoulders',
        'sleek dark hair tied in a clean low ponytail'
    ]

    male_facial_features = [
        'clean-shaven, strong angular jawline, defined cheekbones',
        'light 5-o\'clock shadow stubble, focused gaze, square chin',
        'neatly trimmed short boxed beard and mustache, rugged masculine features',
        'clean-shaven with soft approachable jaw and warm natural brow',
        'groomed dark stubble along jaw, sharp masculine facial contours',
        'clean-shaven, prominent brow and weathered outdoorsy skin texture'
    ]

    female_facial_features = [
        'delicate angular jawline, high defined cheekbones, clear open gaze',
        'soft rounded facial structure, subtle natural freckles across bridge of nose',
        'strong expressive eyes, refined elegant facial contours',
        'warm natural complexion, distinct defined jawline and serene expression'
    ]

    eye_complexions = [
        'warm hazel eyes, healthy balanced natural skin tone',
        'cool blue-grey eyes, fair natural complexion',
        'deep dark brown eyes, lightly tanned skin tone',
        'amber-brown eyes, natural subtle olive skin undertone',
        'striking green-hazel eyes, clear natural complexion',
        'dark slate eyes, rugged weathered complexion'
    ]

    portrait_tops = [
        'simple dark navy blue crewneck top',
        'charcoal grey textured waffle-knit sweater',
        'olive green casual canvas overshirt over dark tee',
        'slate blue long-sleeve knit shirt',
        'dark forest green casual crewneck pullover',
        'matte black casual zip-collar top',
        'dark burgundy lightweight knit sweater',
        'muted denim-blue casual collared shirt'
    ]

    traits = []

    if not any(w in desc_lower for w in ('hair', 'волос', 'лыс', 'bald', 'кудр', 'curly', 'прям', 'straight', 'рыж', 'блон', 'темн', 'черн', 'русы')):
        hairstyles = female_hairstyles if is_female else male_hairstyles
        traits.append(hairstyles[char_seed % len(hairstyles)])

    if not any(w in desc_lower for w in ('beard', 'бород', 'усы', 'mustache', 'stubble', 'щетин', 'jaw', 'челюст', 'скул', 'cheekbone', 'clean-shaven', 'гладковыбрит')):
        features = female_facial_features if is_female else male_facial_features
        traits.append(features[(char_seed // 7) % len(features)])

    if not any(w in desc_lower for w in ('eye', 'глаз', 'skin', 'кож', 'complexion', 'freckle', 'веснушк')):
        traits.append(eye_complexions[(char_seed // 13) % len(eye_complexions)])

    traits.append(portrait_tops[(char_seed // 19) % len(portrait_tops)])

    return ', '.join(traits)


_NONHUMAN_CHARACTER_CUE = re.compile(
    r'(?:entity\s*type\s*:\s*(?:animal|creature|non-?human)|'
    r'тип\s*(?:сущности|субъекта)\s*:\s*(?:животн\w*|существ\w*|нечеловек\w*)|'
    r'\b(?:animal|creature|non-?human|mammal|bird|fish|reptile|amphibian|insect|arachnid|'
    r'mollusc|mollusk|cephalopod|животн\w*|существ\w*|млекопитающ\w*|птиц\w*|рыб\w*|'
    r'пресмыкающ\w*|земноводн\w*|насеком\w*|паукообразн\w*|моллюск\w*|головоног\w*)\b)', re.IGNORECASE)

_LITERAL_ANIMAL_REFERENCE_CUE = re.compile(
    r'(?:entity\s*type\s*:\s*animal|тип\s*(?:сущности|субъекта)\s*:\s*животн\w*|'
    r'\bspecies\s*:|\b(?:mammal|bird|fish|reptile|amphibian|insect|arachnid|mollusc|mollusk|cephalopod|'
    r'млекопитающ\w*|птиц\w*|рыб\w*|пресмыкающ\w*|земноводн\w*|насеком\w*|паукообразн\w*|моллюск\w*|головоног\w*)\b)',
    re.IGNORECASE)


_VIEWPOINT_HAND_CUE = re.compile(
    r'\b(?:опустил[аи]?\s+рук\w*|ваш\w*\s+пальц\w*|ваш\w*\s+рук\w*|'
    r'lower(?:ed)?\s+(?:your\s+)?hand|your\s+finger\w*|your\s+hand)\b', re.IGNORECASE)


def scene_has_anonymous_viewpoint_hand(scene):
    """A second-person hand is a POV detail, not a recurring human character."""
    text = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat')) if isinstance(scene, dict) else str(scene or '')
    return bool(_VIEWPOINT_HAND_CUE.search(text))


def is_nonhuman_character(character):
    """Whether a recurring reference is an animal/creature, never a human count."""
    from .subject_policy import policy
    return policy(character)['entity_type'] in ('animal', 'creature')


def is_literal_animal_reference(character):
    """Animals need literal zoology, unlike a story's actual fictional creatures."""
    if isinstance(character, dict):
        if character.get('entity_type') == 'creature':
            return False
        text = ' '.join(str(character.get(key, '')) for key in ('name', 'canonical_name', 'description', 'role'))
    else:
        text = str(character or '')
    return bool(_LITERAL_ANIMAL_REFERENCE_CUE.search(text))


def nonhuman_scale_instruction(characters, compact=False):
    """Keep wildlife at species scale and fictional creatures at story scale."""
    creatures = [c for c in characters if isinstance(c, dict) and c.get('entity_type') == 'creature']
    animals = [c for c in characters if is_nonhuman_character(c) and c not in creatures]
    creature_rule = ''
    if creatures:
        names = ', '.join(c['name'] for c in creatures)
        creature_rule = (
            f"Creature anatomy for {names}: preserve the reference's non-human body plan, "
            "including its natural limbs, wings and tail where present. No humanoid body, human face, hands or clothing. "
            "Use the creature's scale at this point in the story."
        )
    if not animals:
        return creature_rule
    base = ('SPECIES SCALE INVARIANT: use real-world adult dimensions and natural proportions of referenced wildlife; '
            'never make animals human-sized, gigantic or miniature.')
    if compact:
        return ' '.join(filter(None, (creature_rule, base)))
    profiles = [str(item.get('description', '')) for item in animals if isinstance(item, dict)]
    profile_text = ' '.join(profile for profile in profiles if profile).strip()
    wildlife_rule = ('SPECIES SCALE INVARIANT: use the real-world adult dimensions, mass and proportions of the exact referenced '
            'species, never dramatic or fantasy scale. Keep the same scale in every frame and show a natural environmental '
            'reference whenever perspective could be misleading. A small animal must remain small beside plants, paths and '
            'human-built objects; a large animal must remain large beside them. Never make wildlife human-sized, gigantic or miniature. '
            f'Biological reference: {profile_text[:900]}')
    return ' '.join(filter(None, (creature_rule, wildlife_rule)))


def scene_mentions_people(scene, known_names=()):
    if isinstance(scene, dict) and scene.get('population_policy') == 'object_only':
        return False
    """Return whether the source supplies evidence that a person belongs in this scene."""
    # Continuity fields describe neighbouring frames. Including them here makes
    # "Alexey enters next" count Alexey in the preceding object-only frame.
    from .cast import _is_character_offscreen_or_mentioned_only
    quote = str(scene.get('source_quote', '') or '')
    beat = str(scene.get('beat', '') or '')
    shot = str(scene.get('shot', '') or '')
    text = f'{quote} {beat}'
    if _HUMAN_CUE.search(text):
        return True
    lowered = text.casefold()
    for name in known_names:
        if isinstance(name, str) and len(name.strip()) > 1 and name.casefold() in lowered:
            if not _is_character_offscreen_or_mentioned_only(quote, beat, shot, name):
                return True
    return False


def scene_anonymous_people_count(scene, known_names=()):
    if isinstance(scene, dict) and scene.get('population_policy') == 'object_only':
        return 0
    """Count concrete one-off human roles in a scene, capped by its visual contract.

    Recurring characters and episodic roles are deliberately separate: a carpenter,
    soldier or guide can be visible without receiving a permanent portrait card.  A
    recognised personal name adjacent to a role ("blacksmith Martin") is counted
    through its character binding instead of as an extra anonymous person.
    """
    text = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat'))
    lowered = text.casefold()
    names = [name.casefold() for name in known_names if isinstance(name, str) and len(name.strip()) > 1]
    roles = []
    for match in _EXPLICIT_UNBOUND_PERSON_CUE.finditer(text):
        start, end = match.span()
        nearby = lowered[max(0, start - 32):min(len(lowered), end + 32)]
        if any(name in nearby for name in names):
            continue
        role = re.sub(r'\W+', ' ', match.group(0).casefold()).strip()
        if role and role not in roles:
            roles.append(role)
    if not roles:
        return 0
    plural_group = any(
        re.search(r'\b(?:people|men|women|children|soldiers|warriors|workers)\b', role, re.I)
        or re.search(r'(?:солдат(?:ы|ов|ам|ами|ах)|воин(?:ы|ов|ам|ами|ах)|люди)', role, re.I)
        for role in roles
    )
    return 3 if plural_group and len(roles) < 3 else len(roles)


def scene_has_explicit_unbound_person(scene, known_names=()):
    """Whether the scene requires at least one concrete anonymous participant."""
    return scene_anonymous_people_count(scene, known_names) > 0 or bool(extract_scene_episodic_roles(scene, known_names))


BANNED_EPISODIC_FILLERS = {
    'wanderer', 'man', 'person', 'traveler', 'someone', 'figure', 'human',
    'странник', 'путник', 'человек', 'фигура', 'незнакомец', 'someone'
}

_EPISODIC_ROLE_CLEANUP = [
    (re.compile(r'\b(?:a|an|the)\s+', re.I), ''),
    (re.compile(r'\b(?:другая|другую|другой|другим|другого|вторая|вторую|второй|вторым|третья|третий|третью|третьей|третьим|незнакомая|незнакомую|незнакомый|незнакомой|незнакомым|чужая|чужую|чужой|чужим|another|other|second|third|different|unknown)\s+(?:женщин[а-яё]*|девушк[а-яё]*|woman|lady)\b', re.I), 'другая женщина'),
    (re.compile(r'\b(?:другой|другого|другим|второй|второго|вторым|третий|третьего|третьим|незнакомый|незнакомого|незнакомым|чужой|чужого|чужим|another|other|second|third|different|unknown)\s+(?:мужчин[а-яё]*|парн[а-яё]*|man|gentleman)\b', re.I), 'другой мужчина'),
    (re.compile(r'\b(?:сотрудниц[а-яё]*|сотрудник[а-яё]*|visitor[- ]center employee|female employee|male employee|receptionist|clerk)(?:\s+(?:центра|гостиницы|отеля|парка|за\s+стойкой))?\b', re.I), 'сотрудница центра'),
    (re.compile(r'\b(?:прохож[а-яё]*|passerby|passersby)\b', re.I), 'прохожий'),
    (re.compile(r'\b(?:охранник[а-яё]*|сторож[а-яё]*|guard|security guard|watchman)\b', re.I), 'охранник'),
    (re.compile(r'\b(?:продав[а-яё]+|кассир[а-яё]*|shopkeeper|vendor|cashier)\b', re.I), 'продавец'),
    (re.compile(r'\b(?:водител[а-яё]*|шоф[её]р[а-яё]*|таксист[а-яё]*|driver|chauffeur|taxi driver)\b', re.I), 'водитель'),
    (re.compile(r'\b(?:врач[а-яё]*|доктор[а-яё]*|doctor|physician)\b', re.I), 'врач'),
    (re.compile(r'\b(?:медсестр[а-яё]*|nurse)\b', re.I), 'медсестра'),
    (re.compile(r'\b(?:администратор[а-яё]*|administrator)\b', re.I), 'администратор'),
    (re.compile(r'\b(?:кузнец[а-яё]*|blacksmith)\b', re.I), 'кузнец'),
    (re.compile(r'\b(?:плотник[а-яё]*|carpenter)\b', re.I), 'плотник'),
    (re.compile(r'\b(?:леснич[а-яё]*|егер[а-яё]*|рейнджер[а-яё]*|ranger)\b', re.I), 'лесничий'),
    (re.compile(r'\b(?:гид[а-яё]*|экскурсовод[а-яё]*|проводник[а-яё]*|guide)\b', re.I), 'гид'),
]


def extract_scene_episodic_roles(scene, known_names=()) -> List[str]:
    """Extract concrete episodic/background human roles described in the scene,
    merging explicit episodic_people / background_roles with roles extracted from text."""
    from .cast import stem_ru_word
    roles = []
    for key in ('episodic_people', 'background_roles'):
        explicit = scene.get(key)
        if isinstance(explicit, list):
            for r in explicit:
                r_str = str(r).strip()
                slot = re.search(r'\s*·\s*участник\s*№(\d+)\s*$',r_str)
                if slot:r_str=r_str[:slot.start()].strip()
                canonical_role = r_str
                for pat, replacement in _EPISODIC_ROLE_CLEANUP:
                    if pat.search(r_str):
                        canonical_role = replacement
                        break
                if canonical_role and canonical_role.casefold() not in BANNED_EPISODIC_FILLERS and slot:
                    canonical_role += ' · участник №'+slot.group(1)
                if canonical_role and canonical_role.casefold() not in BANNED_EPISODIC_FILLERS and canonical_role not in roles:
                    roles.append(canonical_role)
        elif isinstance(explicit, str) and explicit.strip():
            r_str = explicit.strip()
            canonical_role = r_str
            for pat, replacement in _EPISODIC_ROLE_CLEANUP:
                if pat.search(r_str):
                    canonical_role = replacement
                    break
            if canonical_role and canonical_role.casefold() not in BANNED_EPISODIC_FILLERS and canonical_role not in roles:
                roles.append(canonical_role)

    # Fully enumerated participants describe this camera selection. Mentioning
    # a role elsewhere in narration cannot add another person to those slots.
    declared=scene.get('raw_visible_people',scene.get('visible_people'))
    slots=[re.search(r'·\s*участник\s*№(\d+)\s*$',role) for role in roles]
    if (type(declared) is int and declared>0 and len(roles)==declared and all(slots)
            and sorted(int(m.group(1)) for m in slots)==list(range(1,declared+1))):
        return roles

    text = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat', 'shot', 'composition'))
    lowered = text.casefold()
    names = [name.casefold() for name in known_names if isinstance(name, str) and len(name.strip()) > 1]
    for match in _EXPLICIT_UNBOUND_PERSON_CUE.finditer(text):
        start, end = match.span()
        before_context = lowered[max(0, start - 20):start]
        if re.search(r'\b(?:стойк[а-яё]*|за\s+стойк[а-яё]*)\s*$', before_context):
            continue
        raw_role = match.group(0).strip().casefold()
        canonical_role = raw_role
        for pat, replacement in _EPISODIC_ROLE_CLEANUP:
            if pat.search(raw_role):
                canonical_role = replacement
                break
        if canonical_role and canonical_role.casefold() not in BANNED_EPISODIC_FILLERS:
            is_distinct = any(m in canonical_role for m in _DISTINCT_PERSON_MODIFIERS)
            c_stem = stem_ru_word(canonical_role.casefold())
            if not is_distinct:
                if any(stem_ru_word(r.casefold()) == c_stem or c_stem in stem_ru_word(r.casefold()) or stem_ru_word(r.casefold()) in c_stem for r in roles):
                    continue
                if names:
                    is_appositive = any(
                        bool(re.search(r'\b(?:' + re.escape(raw_role) + r'\s+' + re.escape(name) + r'|' + re.escape(name) + r'[\s\-]+' + re.escape(raw_role) + r')\b', lowered))
                        for name in names
                    )
                    if is_appositive:
                        continue
            if canonical_role not in roles:
                roles.append(canonical_role)
    return roles


def scene_over_shoulder_anonymous_people_count(scene, known_names=()):
    """Count a source-described foreground observer explicitly visible in an OTS shot."""
    shot = str(scene.get('shot', '') or '')
    if not re.search(r'over[\s\-\u2011]*the[\s\-\u2011]*shoulder|через\s+плечо|из[\s\-]+за\s+плеча', shot, re.I):
        return 0
    # The person being watched can be named later in the shot; only the
    # foreground observer belongs to the over-the-shoulder role phrase.
    observer = re.split(r'\b(?:looking\s+(?:toward|at)|watching|facing|смотр\w*\s+на|наблюда\w*\s+за)\b', shot, maxsplit=1, flags=re.I)[0]
    return scene_anonymous_people_count({'source_quote': '', 'beat': observer}, known_names)


def visual_source_evidence(source_quote):
    """Keep narrative action for image grounding, while removing dialogue text."""
    if not isinstance(source_quote, str):
        return ''
    # Quoted dialogue makes compact diffusion models attempt captions. The
    # narrative around it still anchors a repaired/expanded shot in the story.
    without_quotes = re.sub(r'[«"](?:[^»"]*)[»"]', '', source_quote)
    without_speech_lines = re.sub(r'(?m)^\s*[—-].*$', '', without_quotes)
    return ' '.join(without_speech_lines.split())[:1000]


def source_anonymous_participants(scene, story):
    """Accept a generic person only through independently rebuilt source evidence.

    Model role lists, staging text and pronouns alone cannot authorize a face.
    This also retains a resolved generic antecedent without inventing identity.
    """
    quote = scene.get('source_quote')
    if not isinstance(story, str) or not story or not isinstance(quote, str) or not quote:
        return []
    start = scene.get('source_start')
    if start is not None and (type(start) is not int or story[start:start+len(quote)] != quote):
        return []
    from .forgotten_cities.event_context import for_source
    context = for_source(story, quote, start)
    if not context or context.get('needs_review'):
        return []
    result = []
    for person in context['participants']:
        evidence = person['evidence']
        link = person.get('link') or evidence
        name = person['name']
        if (person.get('human') is True and person.get('number') == 'singular'
                and name.casefold() in BANNED_EPISODIC_FILLERS
                and story[evidence['start']:evidence['end']] == evidence['quote'] == name
                and context['source']['start'] <= link['start'] < link['end'] <= context['source']['end']
                and not any(p['evidence'] == evidence for p in result)):
            result.append(person)
    return result


def verified_source_participants(scene, story):
    """Share the selected source actors instead of mining every mentioned role."""
    checked=scene.get('visual_content')
    if checked is not None:
        from .story_visual_plan import verified_participants
        return verified_participants(scene,story)
    if scene.get('narration_visual_mode'):
        from .narration_expansion import neutral_additional
        if neutral_additional(scene, story):
            return []
    packet = scene.get('forgotten_cities_prompt_contract') or {}
    if not isinstance(packet, dict):
        return None
    context = packet.get('narrative_context') or {}
    if not isinstance(context, dict):
        return None
    source = context.get('source') or {}
    if not story or packet.get('source_binding') != 'narrated_event':
        return None
    def literal(span):
        return (isinstance(span, dict) and type(span.get('start')) is int and type(span.get('end')) is int
                and 0 <= span['start'] < span['end'] <= len(story)
                and story[span['start']:span['end']] == span.get('quote'))
    child_source = {'start':scene.get('source_start'),'end':scene.get('source_end'),'quote':scene.get('source_quote')}
    inherited_source = (scene.get('narration_prompt_policy') == 'preserve_parent_scope/1'
        and bool(scene.get('narration_parent')) and scene.get('semantic_parent_source') == source
        and literal(source) and literal(child_source)
        and source['start'] <= child_source['start'] < child_source['end'] <= source['end']
        and packet.get('source_quote') == child_source['quote'])
    if (not literal(source) or (packet.get('source_quote') != source['quote'] and not inherited_source)
            or context.get('story_hash') != hashlib.sha256(story.encode()).hexdigest()):
        return None
    decision = packet.get('editorial_decision')
    if decision:
        if (not isinstance(decision, dict) or decision.get('version') != 'editorial-scene-plan/1' or not decision.get('id')
                or decision['id'] != scene.get('editorial_plan_id') or decision.get('focus') != source):
            return None
        people = decision.get('participants')
        if not isinstance(people, list) or people != context.get('participants'):
            return None
    else:
        from .forgotten_cities.event_context import for_source
        rebuilt = for_source(story, source['quote'], source['start'])
        if not rebuilt or rebuilt.get('needs_review'):
            return None
        people = rebuilt['participants']
    if any(not isinstance(p, dict) or not p.get('human') or not literal(p.get('evidence'))
           or p.get('name') != p['evidence']['quote']
           or p['evidence']['end'] > source['end'] for p in people):
        return None
    names = list(dict.fromkeys(p['name'] for p in people))
    if packet.get('source_roles') != names:
        return None
    return people


def resolve_scene_presence_contract(
    scene: Dict[str, Any],
    characters: Sequence[Dict[str, Any]],
    locations: Sequence[Dict[str, Any]] = (),
    story: str = '',
    previous_contract: Optional[Dict[str, Any]] = None,
    parent_event: Optional[Dict[str, Any]] = None,
    require_locked_portraits: bool = False,
    scene_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Unified semantic presence contract determining visible people, character presence,
    episodic roles, location continuity, and validation invariants across all phases."""
    from .cast import (
        strip_combining_accents, stem_ru_word, slugify_id, get_search_terms,
        _is_distinct_gender_lemma_pair, _match_gender_pair_in_quote,
        _is_character_offscreen_or_mentioned_only, _mentioned_only_as_discussion,
        _PRONOUNS_MALE, _PRONOUNS_FEMALE
    )

    from .character_selection import resolve_links
    from .entity_identity import gender as identity_gender, mentions as identity_mentions
    from .subject_policy import enrich, normalize_scene, requires_portrait
    characters = enrich(characters)
    scene, link_errors = resolve_links(scene, characters)
    scene = normalize_scene(scene, characters)
    quote = strip_combining_accents(str(scene.get('source_quote') or '')).strip()
    beat = strip_combining_accents(str(scene.get('beat') or '')).strip()
    shot = strip_combining_accents(str(scene.get('shot') or '')).strip()
    shot_type = str(scene.get('shot_type') or '').strip().lower()

    pos = scene_index if scene_index is not None else scene.get('position', 1)

    quote_lower = quote.casefold()
    beat_lower = beat.casefold()
    shot_lower = shot.casefold()
    full_text = f'{quote_lower} {beat_lower} {shot_lower}'.strip()

    is_atmosphere = (shot_type == SHOT_TYPE_ATMOSPHERE)

    errors = list(link_errors)
    warnings = []
    evidence = {
        'characters': {},
        'episodic': [],
        'location': None,
        'presence_cues': []
    }

    # 1. Characters catalog mapping
    active_characters = [
        c for c in characters
        if isinstance(c, dict) and requires_portrait(c) and str(c.get('canonical_name') or c.get('name', '')).strip().casefold() not in BANNED_FILLER_NAMES
    ]
    char_by_cast_id = {}
    char_by_id = {}
    name_to_char = {}
    for c in active_characters:
        cid = c.get('id') or ''
        cast_id = c.get('cast_id') or cid or slugify_id(c.get('canonical_name') or c.get('name'))
        char_by_id[cid] = c
        char_by_cast_id[cast_id] = c
        cname = str(c.get('canonical_name') or c.get('name') or '').strip()
        if cname:
            name_to_char[cname.casefold()] = c
        for alias in c.get('aliases', []):
            if isinstance(alias, str) and alias.strip():
                name_to_char[alias.strip().casefold()] = c

    # An explicit provider presence list must not be expanded by legacy protagonist heuristics.
    if scene.get('analysis_authority') == 'deepseek-v2':
        allowed = set(scene.get('characters', []))
        active_characters = [c for c in active_characters if c.get('name') in allowed]
        char_by_cast_id = {k:v for k,v in char_by_cast_id.items() if v in active_characters}
        char_by_id = {k:v for k,v in char_by_id.items() if v in active_characters}
        name_to_char = {k:v for k,v in name_to_char.items() if v in active_characters}

    # Declared character IDs / names in scene
    declared_char_ids = [str(cid) for cid in scene.get('character_ids', []) if cid]
    declared_char_names = [str(cn).strip() for cn in scene.get('characters', []) if cn and str(cn).strip()]

    # Protagonist identity
    protagonist_char = None
    explicit_protagonist = None
    for c in active_characters:
        role = str(c.get('role', '')).casefold()
        desc = str(c.get('description', '')).casefold()
        if any(w in role or w in desc for w in ('protagonist', 'главный', 'lead', 'hero')):
            explicit_protagonist = c
            protagonist_char = c
            break
    # Do NOT assign characters[0] as protagonist if none is explicitly designated;
    # empty scenes or anthology stories must not be populated with an assumed protagonist.
    protagonist_char = explicit_protagonist

    visible_ids = []
    visible_names = []
    voice_only_ids = []
    voice_only_names = []
    offscreen_ids = []
    offscreen_names = []
    mentioned_ids = []
    mentioned_names = []
    depicted_ids = []
    depicted_names = []
    unknown_people = []

    # Remote voice / phone / radio patterns
    phone_remote_patterns = [
        r'\b(?:на\s+том\s+конце\s+провода|на\s+другом\s+конце|в\s+трубке|голос\s+в\s+трубке|голос\s+из\s+динамика|голос\s+по\s+телефону|голос\s+из\s+рации|голос\s+по\s+рации|по\s+рации|из\s+рации|в\s+рации|по\s+радио|из\s+радио|диктор\s+по\s+радио)\b',
        r'\b(?:on\s+the\s+other\s+end\s+of\s+the\s+line|voice\s+through\s+the\s+receiver|voice\s+over\s+the\s+phone|transmission\s+to\s+air\s+traffic\s+control|spoke\s+(?:in\s+a\s+\w+\s+voice\s+)?through\s+the\s+receiver|over\s+the\s+radio|on\s+the\s+radio|through\s+the\s+radio|walkie[\s\-]*talkie)\b'
    ]
    has_remote_phone_cue = any(re.search(p, full_text, re.I) for p in phone_remote_patterns)

    # Off-screen narration patterns
    offscreen_patterns = [
        r'\b(?:голос\s+за\s+кадром|за\s+кадром|голос\s+из-за\s+кадра|off[\s\-]*screen|out\s+of\s+frame|voiceover|voice[\s\-]*over)\b'
    ]
    has_offscreen_cue = any(re.search(p, full_text, re.I) for p in offscreen_patterns)

    # Detect distinct gender lemma pairs (e.g. ворон / ворона)
    gender_pairs = []
    for i, c1 in enumerate(active_characters):
        for c2 in active_characters[i+1:]:
            n1 = strip_combining_accents(c1.get('canonical_name') or c1.get('name') or '').casefold()
            n2 = strip_combining_accents(c2.get('canonical_name') or c2.get('name') or '').casefold()
            if _is_distinct_gender_lemma_pair(n1, n2):
                if n1.endswith(('а', 'я')):
                    gender_pairs.append((c2, c1))
                else:
                    gender_pairs.append((c1, c2))

    authoritative_selection = (bool(scene.get('project_review')) or (scene.get('storyboard_frame') or {}).get('version') == 'text-storyboard/3')
    safely_named = {i for i,_,_ in identity_mentions(quote+' '+beat,active_characters)}
    matched_chars = []
    handled_pair_cids = set()

    for masc_c, fem_c in gender_pairs:
        m_cid = masc_c.get('cast_id') or masc_c.get('id') or slugify_id(masc_c.get('canonical_name') or masc_c.get('name'))
        f_cid = fem_c.get('cast_id') or fem_c.get('id') or slugify_id(fem_c.get('canonical_name') or fem_c.get('name'))
        handled_pair_cids.add(m_cid)
        handled_pair_cids.add(f_cid)
        has_masc, has_fem = _match_gender_pair_in_quote(masc_c, fem_c, full_text)
        if has_masc and has_fem:
            for pair_c in (masc_c, fem_c):
                p_cname = pair_c.get('canonical_name') or pair_c.get('name')
                p_cid = pair_c.get('cast_id') or pair_c.get('id') or slugify_id(p_cname)
                aliases = {p_cname.casefold()} | {str(a).casefold() for a in pair_c.get('aliases', []) if isinstance(a, str)}
                matched_chars.append((pair_c, p_cid, p_cname, aliases, False, True))
        elif has_masc:
            p_cname = masc_c.get('canonical_name') or masc_c.get('name')
            p_cid = masc_c.get('cast_id') or masc_c.get('id') or slugify_id(p_cname)
            aliases = {p_cname.casefold()} | {str(a).casefold() for a in masc_c.get('aliases', []) if isinstance(a, str)}
            matched_chars.append((masc_c, p_cid, p_cname, aliases, False, True))
        elif has_fem:
            p_cname = fem_c.get('canonical_name') or fem_c.get('name')
            p_cid = fem_c.get('cast_id') or fem_c.get('id') or slugify_id(p_cname)
            aliases = {p_cname.casefold()} | {str(a).casefold() for a in fem_c.get('aliases', []) if isinstance(a, str)}
            matched_chars.append((fem_c, p_cid, p_cname, aliases, False, True))

    # Characters not handled by gender pairs
    for c in active_characters:
        cname = str(c.get('canonical_name') or c.get('name'))
        cid = c.get('cast_id') or c.get('id') or slugify_id(cname)
        if cid in handled_pair_cids:
            continue
        aliases = set(get_search_terms(cname, c.get('aliases', [])))
        aliases.add(cname.casefold())
        latin_alias = slugify_id(cname).removeprefix('char_').replace('_', ' ')
        if latin_alias:
            aliases.add(latin_alias.casefold())
        in_declared = (cid in declared_char_ids or any(a in {cn.casefold() for cn in declared_char_names} for a in aliases))
        has_text_mention = any(c is active_characters[i] for i in safely_named)
        if in_declared or (has_text_mention and not authoritative_selection):
            matched_chars.append((c, cid, cname, aliases, in_declared, has_text_mention))

    # Pronoun continuity & active verb subject resolution if needed
    words = set(re.findall(r'\b[a-zа-яё]+\b', full_text))
    prev_vis_ids = previous_contract.get('visible_character_ids', []) if previous_contract else []
    parent_vis_ids = parent_event.get('visible_character_ids', []) if (parent_event and isinstance(parent_event, dict)) else []

    if not is_atmosphere and not authoritative_selection:
        # Check active female visual action
        has_female_pronoun = bool(re.search(r'(?:^|[.!?]\s+)(?:(?:но|и|затем|потом)\s+)?(?:она|she)\b',quote_lower or beat_lower))
        has_active_verb = bool(_ACTIVE_VISUAL_VERBS.search(full_text))
        has_female_in_matched = any(
            identity_gender(mc[0]) == 'femn'
            for mc in matched_chars
        )
        if has_female_pronoun and not has_female_in_matched:
            target_c = None
            female_prev_cands = []
            for p_cid in (prev_vis_ids + parent_vis_ids):
                cand = char_by_cast_id.get(p_cid) or char_by_id.get(p_cid)
                if cand and (identity_gender(cand) == 'femn'):
                    if cand not in female_prev_cands:
                        female_prev_cands.append(cand)
            if len(female_prev_cands) == 1:
                target_c = female_prev_cands[0]
            elif len(female_prev_cands) > 1 and has_female_pronoun:
                errors.append({
                    'code': 'ambiguous_antecedent',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: местоимение женского рода (она) не имеет однозначного антецедента среди персонажей предыдущего кадра.',
                    'details': {'scene_index': pos, 'candidates': [c.get('canonical_name') for c in female_prev_cands]}
                })
            elif not female_prev_cands:
                female_cands = [c for c in active_characters if identity_gender(c)=='femn']
                if len(female_cands) == 1 and female_cands[0] is explicit_protagonist:
                    target_c = female_cands[0]
                elif len(female_cands) > 1 and has_female_pronoun:
                    errors.append({
                        'code': 'ambiguous_antecedent',
                        'scene_index': pos,
                        'message': f'Кадр {pos}: местоимение женского рода (она) не имеет однозначного антецедента среди персонажей.',
                        'details': {'scene_index': pos, 'candidates': [c.get('canonical_name') for c in female_cands]}
                    })
                elif not female_cands and explicit_protagonist:
                    if identity_gender(explicit_protagonist) == 'femn':
                        target_c = explicit_protagonist
            if target_c:
                cid = target_c.get('cast_id') or target_c.get('id') or slugify_id(target_c.get('canonical_name') or target_c.get('name'))
                cname = target_c.get('canonical_name') or target_c.get('name')
                aliases = {cname.casefold()} | {str(a).casefold() for a in target_c.get('aliases', []) if isinstance(a, str)}
                if not any(mc[1] == cid for mc in matched_chars):
                    matched_chars.append((target_c, cid, cname, aliases, False, False))
                    evidence['presence_cues'].append(f'Active subject resolution (she/она) -> {cname}')

        # Check active male visual action
        has_male_pronoun = bool(re.search(r'(?:^|[.!?]\s+)(?:(?:но|и|затем|потом)\s+)?(?:он|he)\b',quote_lower or beat_lower))
        has_male_in_matched = any(
            identity_gender(mc[0]) == 'masc'
            for mc in matched_chars
        )
        if has_male_pronoun and not has_male_in_matched:
            target_c = None
            male_prev_cands = []
            for p_cid in (prev_vis_ids + parent_vis_ids):
                cand = char_by_cast_id.get(p_cid) or char_by_id.get(p_cid)
                if cand and identity_gender(cand) == 'masc':
                    if cand not in male_prev_cands:
                        male_prev_cands.append(cand)
            if len(male_prev_cands) == 1:
                target_c = male_prev_cands[0]
            elif len(male_prev_cands) > 1 and has_male_pronoun:
                errors.append({
                    'code': 'ambiguous_antecedent',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: местоимение мужского рода (он) не имеет однозначного антецедента среди персонажей предыдущего кадра.',
                    'details': {'scene_index': pos, 'candidates': [c.get('canonical_name') for c in male_prev_cands]}
                })
            elif not male_prev_cands:
                male_cands = [c for c in active_characters if identity_gender(c)=='masc']
                if len(male_cands) == 1 and male_cands[0] is explicit_protagonist:
                    target_c = male_cands[0]
                elif len(male_cands) > 1 and has_male_pronoun:
                    errors.append({
                        'code': 'ambiguous_antecedent',
                        'scene_index': pos,
                        'message': f'Кадр {pos}: местоимение мужского рода (он) не имеет однозначного антецедента среди персонажей.',
                        'details': {'scene_index': pos, 'candidates': [c.get('canonical_name') for c in male_cands]}
                    })
                elif not male_cands and explicit_protagonist:
                    if identity_gender(explicit_protagonist) == 'masc':
                        target_c = explicit_protagonist
            if target_c:
                cid = target_c.get('cast_id') or target_c.get('id') or slugify_id(target_c.get('canonical_name') or target_c.get('name'))
                cname = target_c.get('canonical_name') or target_c.get('name')
                aliases = {cname.casefold()} | {str(a).casefold() for a in target_c.get('aliases', []) if isinstance(a, str)}
                if not any(mc[1] == cid for mc in matched_chars):
                    matched_chars.append((target_c, cid, cname, aliases, False, False))
                    evidence['presence_cues'].append(f'Active subject resolution (he/он) -> {cname}')

    # Classify matched characters across categories
    for c, cid, cname, aliases, in_declared, has_text_mention in matched_chars:
        c_stem = stem_ru_word(cname.casefold())

        # 1. Depicted on media check (photo on phone, TV, monitor, portrait on wall, recording)
        depicted_match = bool(
            re.search(r'\b(?:фотографи\w*|фото|снимок\w*|портрет\w*|photo\w*|portrait\w*|picture\w*)\s+(?:[а-яёa-z]+\s+)?' + re.escape(c_stem), full_text, re.I) or
            re.search(r'\b' + re.escape(c_stem) + r'\w*\s+(?:на\s+фото\w*|на\s+экране|в\s+телефоне|на\s+мониторе|на\s+телевизоре|shown\s+in\s+a\s+photo|on\s+the\s+phone\s+screen)', full_text, re.I) or
            re.search(r'\b(?:фото\w*|на\s+экране|в\s+телефоне|на\s+снимке|on\s+screen|in\s+a\s+photo\w*|on\s+monitor|on\s+the\s+wall)\b[^\n.!?]{0,35}\b' + re.escape(c_stem), full_text, re.I) or
            re.search(r'\b' + re.escape(c_stem) + r'\w*\b[^\n.!?]{0,35}\b(?:на\s+фото\w*|на\s+экране|в\s+телефоне|on\s+screen|in\s+a\s+photo\w*)', full_text, re.I)
        )
        if depicted_match:
            room_action = bool(re.search(
                r'\b' + re.escape(c_stem) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|вош[её]л|вошла|застыл|застыла|замер|замерла|stands|sits|walks|runs|looks|holds|writes|takes|enters|froze)',
                beat_lower
            ))
            if not room_action:
                depicted_ids.append(cid)
                depicted_names.append(cname)
                evidence['characters'][cname] = 'depicted (photo/screen/media)'
                continue

        # 2. Voiceover narration check (offscreen voiceover)
        is_voiceover = bool(re.search(
            r'\b(?:(?:закадровый\s+голос|голос\s+за\s+кадром|за\s+кадром\s+(?:звучал\w*|звучит\w*|раздавал\w*|слыш\w*)?\s*голос)\s+' + re.escape(stem_ru_word(cname.casefold())) + r'\w*|голос\s+' + re.escape(stem_ru_word(cname.casefold())) + r'\w*\s+за\s+кадром|voiceover\s+(?:of\s+)?' + re.escape(cname.casefold()) + r')\b',
            full_text, re.I
        ))
        if is_voiceover:
            offscreen_ids.append(cid)
            offscreen_names.append(cname)
            evidence['characters'][cname] = 'offscreen (voiceover)'
            continue

        # 3. Voice-only check (phone/radio/remote caller/behind door/upstairs)
        is_voice_only = False
        voice_cue_found = has_remote_phone_cue or any(re.search(p, full_text, re.I) for p in _VOICE_ONLY_PATTERNS)
        if voice_cue_found:
            is_holding_phone = bool(re.search(
                r'\b' + re.escape(c_stem) + r'\w*\b[^\n.!?]{0,35}\b(?:с\s+(?:телефон|трубк|раци)\w*|держ\w*\s+(?:трубк|раци|телефон)\w*|слуша\w*|у\s+(?:телефон|раци)\w*|поднес\w*|прижал\w*|holds?\s+(?:phone|radio|receiver)|listen\w*|with\s+(?:phone|radio))\b',
                full_text, re.I
            ))
            is_addressed = bool(re.search(r'\b(?:зов[её]т|звал\w*|окликнул\w*|зовущ\w*|calling|calls?|called)\s+(?:[а-яёa-z]+\s+){0,2}' + re.escape(c_stem) + r'\w*', full_text, re.I))
            remote_voice_match = not is_addressed and bool(
                re.search(
                    r'\b(?:(?:услышал\w*|услышав\w*|слыш\w*|звучит\w*|звучал\w*|раздался|раздалась|раздалось|раздавались|раздавался|раздается|раздаётся|донесся|донёсся|донеслась|донеслось|донеслись|доносился|доносилась|доносилось|доносились|доносится|доносятся)\s+(?:[а-яёa-z]+\s+){0,3}(?:голос\w*|крик\w*|ш[её]пот\w*|смех\w*)\s+(?:[а-яёa-z]+\s+){0,2}' + re.escape(c_stem) + r'\w*|'
                    r'(?:знакомый|чужой|далёкий|далекий|глухой|тихий|громкий|хриплый|приглуш[её]нный|женский|мужской|детский|родной)?\s*(?:голос\w*|крик\w*|ш[её]пот\w*|смех\w*)\s+(?:[а-яёa-z]+\s+){0,2}' + re.escape(c_stem) + r'\w*|'
                    r'' + re.escape(c_stem) + r'\w*\s+(?:голос\w*|крик\w*|ш[её]пот\w*)|'
                    r'' + re.escape(c_stem) + r'\w*\s+(?:на\s+том\s+конце|в\s+трубке|из\s+рации|из\s+динамика|за\s+дверью|с\s+лестницы|сверху|снизу|из-за\s+двери|из\s+соседней\s+комнаты|в\s+наушниках)|'
                    r'(?:крик|кричит|шепчет|слышен|слышна|слышно|доносится)\s+(?:[а-яёa-z]+\s+){0,2}' + re.escape(c_stem) + r'\w*\s+(?:за\s+дверью|из-за\s+двери|с\s+лестницы|сверху|снизу|из\s+соседней\s+комнаты|в\s+трубке)|'
                    r'heard\s+(?:[a-z]+\s+){0,3}(?:voice|cry|shout)\s+of\s+' + re.escape(cname.casefold()) + r'|'
                    r'voice\s+(?:[a-z]+\s+){0,2}of\s+' + re.escape(cname.casefold()) + r'|'
                    r'' + re.escape(cname.casefold()) + r'[\'’]s\s+voice|'
                    r'' + re.escape(cname.casefold()) + r'\s+(?:on\s+the\s+other\s+end|over\s+the\s+phone|through\s+the\s+receiver|behind\s+the\s+door|from\s+upstairs|from\s+downstairs|from\s+the\s+next\s+room))\b',
                    full_text, re.I
                )
            )
            if (remote_voice_match and not is_holding_phone) or (has_remote_phone_cue and not is_holding_phone and not in_declared):
                room_action = bool(re.search(
                    r'\b' + re.escape(c_stem) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|вош[её]л|вошла|застыл|застыла|замер|замерла|поднял|поднялась|stands|sits|walks|runs|looks|holds|writes|takes|enters)',
                    beat_lower
                ))
                if not room_action:
                    is_voice_only = True
            elif remote_voice_match and is_holding_phone and len(matched_chars) > 1:
                other_holds = any(
                    other_c != c and bool(re.search(
                        r'\b' + re.escape(stem_ru_word(str(other_c.get('canonical_name') or other_c.get('name')).casefold())) + r'\w*\b[^\n.!?]{0,35}\b(?:держ\w*|слуша\w*|поднес\w*|прижал\w*|holds?|listen\w*)',
                        full_text, re.I
                    ))
                    for other_c, _, _, _, _, _ in matched_chars
                )
                if other_holds:
                    is_voice_only = True

        if is_voice_only:
            voice_only_ids.append(cid)
            voice_only_names.append(cname)
            evidence['characters'][cname] = 'voice_only (phone/radio/behind-door)'
            continue

        # 4. Off-screen check (framing exclusion / OTS observer excluded)
        is_offscreen = False
        is_deepseek_declared = ((scene.get('analysis_authority') == 'deepseek-v2' or authoritative_selection) and (cname in declared_char_names or cid in declared_char_ids))
        if not is_deepseek_declared and _is_character_offscreen_or_mentioned_only(quote, beat, shot, cname):
            direct_action = bool(re.search(
                r'\b' + re.escape(stem_ru_word(cname.casefold())) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|вош[её]л|вошла|застыл|застыла|замер|замерла|слуша\w*|плач\w*|улыб\w*|stands|sits|walks|runs|looks|holds|writes|takes|enters|froze|listens?)',
                beat_lower
            ))
            if not direct_action:
                if any(w in full_text for w in (
                    'вспомин', 'думал', 'recalls', 'remember', 'about', 'говор', 'рассказ', 'сообщени', 'message from',
                    'спрос', 'расспрос', 'спрашив', 'расспрашив', 'обсужд', 'пропавш', 'исчез', 'поиск', 'asked', 'ask', 'talk', 'discuss', 'missing'
                )):
                    if cid not in mentioned_ids:
                        mentioned_ids.append(cid)
                        mentioned_names.append(cname)
                    evidence['characters'][cname] = 'mentioned_only (memory/topic/message)'
                    continue
                else:
                    is_offscreen = True

        if is_offscreen:
            if cid not in offscreen_ids:
                offscreen_ids.append(cid)
                offscreen_names.append(cname)
            evidence['characters'][cname] = 'offscreen (framing_exclusion/voiceover)'
            continue

        # 4. Mentioned-only check (discussion / story topic)
        is_mentioned = False
        if not is_deepseek_declared and _mentioned_only_as_discussion(quote, beat, [cname], shot):
            direct_action = bool(re.search(
                r'\b' + re.escape(stem_ru_word(cname.casefold())) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|вош[её]л|вошла|застыл|застыла|замер|замерла|слуша\w*|плач\w*|улыб\w*|stands|sits|walks|runs|looks|holds|writes|takes|enters|froze|listens?)',
                beat_lower
            ))
            if not direct_action:
                is_mentioned = True

        if is_mentioned:
            if cid not in mentioned_ids:
                mentioned_ids.append(cid)
                mentioned_names.append(cname)
            evidence['characters'][cname] = 'mentioned_only (discussion/topic)'
            continue

        # 5. Physically visible
        if cid not in visible_ids:
            visible_ids.append(cid)
            visible_names.append(cname)
            evidence['characters'][cname] = 'visible (in-frame subject)'

    # 2. Episodic roles resolution & coreference deduplication
    episodic_roles = []
    episodic_role_ids = []
    coreference_audits = []
    source_selection = verified_source_participants(scene, story)
    if scene.get('visual_content') and source_selection is not None:
        # The source-reviewed composition owns visible actors. Mere mentions
        # elsewhere in this quote cannot reinsert an omitted cast member.
        visible_names=[name for name in declared_char_names if name.casefold() in name_to_char]
        visible_ids=[name_to_char[name.casefold()].get('id') or name_to_char[name.casefold()].get('cast_id')
            or slugify_id(name) for name in visible_names]
    evt_id = str(scene.get('event_id') or (parent_event.get('event_id') if parent_event else None) or "event_01")

    if not is_atmosphere:
        known_char_names = [c.get('canonical_name') or c.get('name') for c in active_characters]
        raw_episodic = (list(dict.fromkeys(p['name'] for p in source_selection))
                        if source_selection is not None else extract_scene_episodic_roles(scene, known_char_names))
        for r in raw_episodic:
            r_clean = str(r).strip()
            if not r_clean or (source_selection is None and r_clean.casefold() in BANNED_EPISODIC_FILLERS):
                continue

            r_lower = r_clean.casefold()
            is_distinct_candidate = any(m in r_lower for m in _DISTINCT_PERSON_MODIFIERS) or any(
                m in full_text[max(0, full_text.find(r_lower) - 20):full_text.find(r_lower) + len(r_lower) + 10]
                for m in _DISTINCT_PERSON_MODIFIERS
            )

            resolved_to_char = None
            if not is_distinct_candidate:
                # Check generic female noun coreference
                if r_lower in _FEMALE_GENERIC_NOUNS or stem_ru_word(r_lower) in {stem_ru_word(w) for w in _FEMALE_GENERIC_NOUNS}:
                    for vc in visible_names:
                        c_obj = name_to_char.get(vc.casefold())
                        if c_obj and (identity_gender(c_obj) == 'femn'):
                            resolved_to_char = vc
                            break
                    if not resolved_to_char and active_characters:
                        for c_obj in active_characters:
                            if identity_gender(c_obj) == 'femn':
                                if c_obj.get('canonical_name') in declared_char_names or c_obj.get('id') in declared_char_ids:
                                    resolved_to_char = c_obj.get('canonical_name')
                                    break
                # Check generic male noun coreference
                elif r_lower in _MALE_GENERIC_NOUNS or stem_ru_word(r_lower) in {stem_ru_word(w) for w in _MALE_GENERIC_NOUNS}:
                    for vc in visible_names:
                        c_obj = name_to_char.get(vc.casefold())
                        if c_obj and identity_gender(c_obj) == 'masc':
                            resolved_to_char = vc
                            break
                    if not resolved_to_char and active_characters:
                        for c_obj in active_characters:
                            if identity_gender(c_obj) == 'masc':
                                if c_obj.get('canonical_name') in declared_char_names or c_obj.get('id') in declared_char_ids:
                                    resolved_to_char = c_obj.get('canonical_name')
                                    break

            if resolved_to_char:
                coreference_audits.append({
                    "surface_form": r_clean.casefold(),
                    "resolved_as": resolved_to_char,
                    "resolution": "coreference",
                    "created_episodic_role": False
                })
                evidence['presence_cues'].append(f"Coreference: '{r_clean}' resolved to recurring character '{resolved_to_char}'")
                continue

            dup = False
            for vc in visible_names:
                if (r_clean.casefold() in vc.casefold() or vc.casefold() in r_clean.casefold()) and not is_distinct_candidate:
                    dup = True
                    break
            if not dup and r_clean.casefold() not in {role.casefold() for role in episodic_roles}:
                episodic_roles.append(r_clean)
                role_id = generate_episodic_role_id(evt_id, r_clean, len(episodic_roles))
                episodic_role_ids.append(role_id)

    # A literal anonymous actor must not be erased by the generic-role ban.
    # Rebuild its evidence from the story; never trust a saved model packet.
    declared_people = scene.get('raw_visible_people', scene.get('visible_people'))
    if (source_selection is None and not is_atmosphere and not visible_ids and not episodic_roles
            and type(declared_people) is int and declared_people > 0
            and scene.get('people_policy') != 'none'):
        source_people = source_anonymous_participants(scene, story)
        for person in source_people:
            name = person['name']
            episodic_roles.append(name)
            origin = 'source_' + fingerprint(story)[:16] + '_' + str(person['evidence']['start'])
            episodic_role_ids.append(generate_episodic_role_id(origin, name, 1))
        if source_people:
            evidence['source_anonymous_participants'] = source_people
            evidence['presence_cues'].append('Anonymous actor verified against the literal source action')

    # Event continuity for episodic roles
    prev_episodic = []
    if source_selection is not None:
        evidence['selected_source_participants'] = source_selection
    elif previous_contract and str(previous_contract.get('event_id') or '') == evt_id:
        prev_episodic = previous_contract.get('episodic_people', []) or []
    elif parent_event and str(parent_event.get('event_id') or '') == evt_id:
        prev_episodic = parent_event.get('episodic_people', []) or []

    for pe in prev_episodic:
        if pe and pe.casefold() not in {role.casefold() for role in episodic_roles}:
            pe_stem = stem_ru_word(pe.casefold())
            in_bg = bool(re.search(r'\b(?:background|фоне|сзади|оста[её]тся|remains?)\b', full_text, re.I) and (pe_stem in full_text or 'женщин' in full_text or 'woman' in full_text or 'мужчин' in full_text or 'man' in full_text))
            if in_bg:
                episodic_roles.append(pe)
                role_id = generate_episodic_role_id(evt_id, pe, len(episodic_roles))
                if role_id not in episodic_role_ids:
                    episodic_role_ids.append(role_id)
                evidence['presence_cues'].append(f"Episodic continuity: '{pe}' retained in background with role_id {role_id}")
            else:
                offscreen_names.append(pe)
                offscreen_ids.append(generate_episodic_role_id(evt_id, pe))
                evidence['presence_cues'].append(f"Episodic continuity: '{pe}' offscreen in this camera shot")

    # Staging Contradiction & Reconcile Check
    staging_count = extract_scene_staging_count(scene)

    # 3. Computed visible_people
    if is_atmosphere:
        visible_people = 0
        visible_ids = []
        visible_human_ids = []
        visible_names = []
        episodic_roles = []
        episodic_role_ids = []
    else:
        # Check if staging requires more figures than we have, and text/event mentions an episodic person
        if source_selection is None and staging_count and staging_count > (len(visible_ids) + len(episodic_roles)):
            for m in _EXPLICIT_UNBOUND_PERSON_CUE.finditer(full_text):
                raw_c = m.group(0).strip()
                cand = raw_c
                for pat, rep in _EPISODIC_ROLE_CLEANUP:
                    if pat.search(raw_c):
                        cand = rep
                        break
                if cand and cand.casefold() not in BANNED_EPISODIC_FILLERS and cand.casefold() not in {role.casefold() for role in episodic_roles}:
                    episodic_roles.append(cand)
                    role_id = generate_episodic_role_id(evt_id, cand, len(episodic_roles))
                    episodic_role_ids.append(role_id)
                    evidence['presence_cues'].append(f"Staging composition ({staging_count} figures) reconciled with episodic role: {cand}")
                    if len(visible_ids) + len(episodic_roles) >= staging_count:
                        break

        visible_human_ids = [
            cid for cid in visible_ids
            if not is_nonhuman_character(char_by_cast_id.get(cid) or char_by_id.get(cid) or {})
        ]
        visible_people = len(visible_human_ids) + len(episodic_roles)

    evidence['episodic'] = list(episodic_roles)

    # 4. Locations resolution & continuity
    loc_map = {}
    canonical_loc_names = set()
    for loc in locations:
        if isinstance(loc, dict):
            lid = loc.get('id') or ''
            lname = str(loc.get('name') or '').strip()
            if lname:
                loc_map[lname.casefold()] = loc
                canonical_loc_names.add(lname)
            if lid:
                loc_map[lid] = loc
            for alias in loc.get('aliases', []):
                if isinstance(alias, str) and alias.strip():
                    loc_map[alias.strip().casefold()] = loc

    resolved_loc_name = None
    resolved_loc_id = None
    resolved_loc_desc = ""
    loc_confidence = 1.0

    raw_loc = str(scene.get('location_ref') or scene.get('location') or '').strip()
    if raw_loc and raw_loc.casefold() in loc_map:
        matched = loc_map[raw_loc.casefold()]
        resolved_loc_name = matched.get('name')
        resolved_loc_id = matched.get('id')
        resolved_loc_desc = matched.get('description', '')
        evidence['location'] = f"explicit: {resolved_loc_name}"
    elif raw_loc:
        for lk, lobj in loc_map.items():
            if len(lk) >= 3 and (lk in raw_loc.casefold() or raw_loc.casefold() in lk):
                resolved_loc_name = lobj.get('name')
                resolved_loc_id = lobj.get('id')
                resolved_loc_desc = lobj.get('description', '')
                evidence['location'] = f"fuzzy_match: {resolved_loc_name}"
                break
        if not resolved_loc_name:
            resolved_loc_name = raw_loc
            evidence['location'] = f"raw_unlinked: {raw_loc}"

    if not resolved_loc_name:
        for lname_k, loc_obj in loc_map.items():
            if len(lname_k) < 3:
                continue
            pattern = re.compile(r'\b(?:приехал\s+в|прибыл\s+в|вош[её]л\s+в|вышел\s+на|направился\s+к|arrived\s+at|entered|walked\s+to|drove\s+to|reached)\s+[^\n.,;!?]*\b' + re.escape(stem_ru_word(lname_k)), re.I)
            if pattern.search(full_text):
                resolved_loc_name = loc_obj.get('name')
                resolved_loc_id = loc_obj.get('id')
                resolved_loc_desc = loc_obj.get('description', '')
                evidence['location'] = f"transition: {resolved_loc_name}"
                break

    if not resolved_loc_name:
        for lname_k, loc_obj in loc_map.items():
            if len(lname_k) < 3:
                continue
            pattern = re.compile(r'\b' + re.escape(stem_ru_word(lname_k)) + r'[a-zа-яё]*\b', re.I)
            if pattern.search(full_text):
                resolved_loc_name = loc_obj.get('name')
                resolved_loc_id = loc_obj.get('id')
                resolved_loc_desc = loc_obj.get('description', '')
                evidence['location'] = f"text_mention: {resolved_loc_name}"
                break

    if not resolved_loc_name:
        if parent_event and (parent_event.get('location_ref') or parent_event.get('location')):
            p_loc = str(parent_event.get('location_ref') or parent_event.get('location')).strip()
            if p_loc.casefold() in loc_map:
                matched = loc_map[p_loc.casefold()]
                resolved_loc_name = matched.get('name')
                resolved_loc_id = matched.get('id')
                resolved_loc_desc = matched.get('description', '')
                evidence['location'] = f"inherited_parent_event: {resolved_loc_name}"
                loc_confidence = 0.85
            elif p_loc:
                resolved_loc_name = p_loc
                evidence['location'] = f"inherited_parent_event_raw: {p_loc}"
                loc_confidence = 0.8
        elif previous_contract and previous_contract.get('location_ref'):
            resolved_loc_name = previous_contract.get('location_ref')
            resolved_loc_id = previous_contract.get('location_id')
            resolved_loc_desc = previous_contract.get('location_description', '')
            evidence['location'] = f"inherited_previous_scene: {resolved_loc_name}"
            loc_confidence = 0.8
        elif len(locations) == 1 and isinstance(locations[0], dict):
            matched = locations[0]
            resolved_loc_name = matched.get('name')
            resolved_loc_id = matched.get('id')
            resolved_loc_desc = matched.get('description', '')
            evidence['location'] = f"single_project_location: {resolved_loc_name}"
            loc_confidence = 0.7

    # 5. Validation Invariants
    if not beat and not is_atmosphere:
        errors.append({
            'code': 'empty_scene_metadata',
            'scene_index': pos,
            'message': f'Кадр {pos}: отсутствует описание действия (beat).',
            'details': {'scene_index': pos, 'field': 'beat'}
        })

    if locations and not resolved_loc_name and not is_atmosphere:
        errors.append({
            'code': 'empty_scene_metadata',
            'scene_index': pos,
            'message': f'Кадр {pos}: указаны люди ({visible_people}), но отсутствует локация.' if visible_people > 0 else f'Кадр {pos}: отсутствует локация.',
            'details': {'scene_index': pos, 'field': 'location'}
        })

    # Staging contradiction error checks
    # Forgotten Cities selects people explicitly through its source-backed
    # people policy. A narrated event can mention several people while this
    # particular documentary shot deliberately shows an artifact, architecture
    # or one anonymous participant, so generic wording must not demand a crowd.
    is_forgotten_cities_scene = (
        isinstance(scene.get('forgotten_cities_prompt_contract'), dict)
        or str(scene.get('people_policy') or '') in {'none', 'anonymous_background', 'anonymous_action'}
    )
    if staging_count is not None and not is_atmosphere and not is_forgotten_cities_scene:
        if staging_count != visible_people:
            if staging_count == 1 and visible_people > 1:
                errors.append({
                    'code': 'composition_contradiction',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: композиция указывает на одного человека (alone), но контракт содержит {visible_people}.',
                    'details': {'scene_index': pos, 'expected': staging_count, 'actual': visible_people}
                })
            elif staging_count > 1 and visible_people < staging_count:
                errors.append({
                    'code': 'composition_contradiction',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: композиция требует {staging_count} физических участников, но контракт содержит {visible_people}.',
                    'details': {'scene_index': pos, 'expected': staging_count, 'actual': visible_people}
                })

    raw_people = scene.get('raw_visible_people') if scene.get('raw_visible_people') is not None else scene.get('visible_people')
    if isinstance(raw_people, int) and raw_people > 0:
        if raw_people > visible_people:
            non_vis_count = len(mentioned_ids) + len(voice_only_ids) + len(offscreen_ids) + len(depicted_ids)
            total_accounted = len(visible_ids) + len(episodic_roles) + non_vis_count + len(coreference_audits)
            if total_accounted >= raw_people:
                evidence['presence_cues'].append(
                    f'LLM visible_people ({raw_people}) reconciled with canonical count ({visible_people}): '
                    f'offscreen/mentioned/depicted/coreferenced entities ({non_vis_count + len(coreference_audits)}) accounted.'
                )
            elif visible_people == 0 and not is_atmosphere:
                errors.append({
                    'code': 'unassigned_humans',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: заявлено {raw_people} человек (visible_people={raw_people}), но персонажи не привязаны и в тексте нет признанной эпизодической роли.',
                    'details': {'scene_index': pos, 'visible_people': raw_people}
                })
            else:
                errors.append({
                    'code': 'unassigned_humans',
                    'scene_index': pos,
                    'message': f'Кадр {pos}: заявлено {raw_people} человек (visible_people={raw_people}), но подтверждено только {len(visible_ids)} персонажей и {len(episodic_roles)} эпизодических ролей. Нераспознанный человек без роли.',
                    'details': {'scene_index': pos, 'visible_people': raw_people, 'characters_count': len(visible_ids), 'episodic_count': len(episodic_roles)}
                })

    declared_char_names_lower = {str(cn).strip().casefold() for cn in scene.get('characters', []) if cn and str(cn).strip()}
    declared_char_ids_set = {str(cid) for cid in scene.get('character_ids', []) if cid}
    explicitly_declared = ('characters' in scene or 'character_ids' in scene)

    for c in active_characters:
        cname = str(c.get('canonical_name') or c.get('name'))
        cid = c.get('cast_id') or c.get('id') or slugify_id(cname)
        aliases = {cname.casefold()} | {str(a).casefold() for a in c.get('aliases', []) if isinstance(a, str)}
        has_direct_text_mention = any(mc[1] == cid and mc[5] for mc in matched_chars)
        has_char_name = any(mc[1] == cid for mc in matched_chars)
        if has_char_name and not is_atmosphere:
            is_bound = (cid in declared_char_ids_set or any(a in declared_char_names_lower for a in aliases))
            if (explicitly_declared and has_direct_text_mention and not is_bound) or (cid not in visible_ids and cname not in visible_names):
                if cid not in offscreen_ids and cid not in voice_only_ids and cid not in mentioned_ids and cid not in depicted_ids:
                    role = str(c.get('role', '')).casefold()
                    is_lead = any(w in role for w in ('protagonist', 'главный', 'lead', 'hero'))
                    code = 'protagonist_missing' if is_lead else 'missing_character_scenes'
                    errors.append({
                        'code': code,
                        'scene_index': pos,
                        'message': f'Кадр {pos}: главный герой ({cname}) активно действует в источнике, но отсутствует в персонажах кадра.' if is_lead else f'Кадр {pos}: персонаж {cname} активно упоминается в источнике/действии, но отсутствует в персонажах кадра.',
                        'details': {'scene_index': pos, 'character': cname}
                    })

    render_errors = []
    if visible_ids and scene.get('portrait_references_enabled', True):
        missing_portraits = []
        for cid in visible_ids:
            c_obj = char_by_cast_id.get(cid) or char_by_id.get(cid)
            has_portrait = bool(c_obj and c_obj.get('locked') and (c_obj.get('references', {}).get('portrait') or c_obj.get('portrait')))
            if not has_portrait:
                missing_portraits.append(c_obj.get('canonical_name') or c_obj.get('name') if c_obj else cid)
        if missing_portraits:
            render_errors.append({
                'code': 'missing_portrait',
                'scene_index': pos,
                'message': f'Кадр {pos}: отсутствует закреплённый портрет персонажа: {", ".join(missing_portraits)}.',
                'details': {'scene_index': pos, 'missing_portraits': missing_portraits}
            })

    semantic_errors = [e for e in errors if e.get('code') != 'missing_portrait']
    if require_locked_portraits:
        all_errors = semantic_errors + render_errors
    else:
        all_errors = list(semantic_errors)

    semantic_valid = len(semantic_errors) == 0
    render_ready = len(render_errors) == 0
    valid = semantic_valid and (render_ready if require_locked_portraits else True)

    final_loc_ref = resolved_loc_name if (resolved_loc_name and resolved_loc_name in canonical_loc_names) else ''
    final_loc_id = resolved_loc_id if final_loc_ref else None

    event_participants = list(dict.fromkeys(visible_names + episodic_roles + offscreen_names))
    shot_visible_people = list(dict.fromkeys(visible_names + episodic_roles))

    reconciliation_reason = ""
    if raw_people != visible_people and raw_people is not None:
        reconciliation_reason = (
            f"LLM visible_people ({raw_people}) reconciled to canonical ({visible_people}): "
            f"visible_characters={visible_names}, episodic={episodic_roles}, depicted={depicted_names}, voice_only={voice_only_names}"
        )

    audit = {
        'raw_visible_people': raw_people,
        'canonical_visible_people': visible_people,
        'visible_characters': list(visible_names),
        'visible_character_ids': list(visible_ids),
        'episodic_people': list(episodic_roles),
        'episodic_role_ids': list(episodic_role_ids),
        'depicted_people': list(depicted_names),
        'depicted_person_ids': list(depicted_ids),
        'voice_only_characters': list(voice_only_names),
        'voice_only_character_ids': list(voice_only_ids),
        'mentioned_characters': list(mentioned_names),
        'mentioned_character_ids': list(mentioned_ids),
        'offscreen_characters': list(offscreen_names),
        'offscreen_character_ids': list(offscreen_ids),
        'unknown_people': list(unknown_people),
        'event_participants': list(event_participants),
        'coreference_resolutions': list(coreference_audits),
        'reconciliation_reason': reconciliation_reason,
    }

    return {
        'visible_character_ids': visible_ids,
        'visible_human_character_ids': visible_human_ids,
        'visible_characters': visible_names,
        'episodic_people': episodic_roles,
        'episodic_role_ids': episodic_role_ids,
        'offscreen_character_ids': offscreen_ids,
        'offscreen_characters': offscreen_names,
        'voice_only_character_ids': voice_only_ids,
        'voice_only_characters': voice_only_names,
        'mentioned_character_ids': mentioned_ids,
        'mentioned_characters': mentioned_names,
        'depicted_person_ids': depicted_ids,
        'depicted_people': depicted_names,
        'unknown_people': unknown_people,
        'event_participants': event_participants,
        'shot_visible_people': shot_visible_people,
        'visible_people': visible_people,
        'raw_visible_people': raw_people,
        'visible_people_audit': audit,
        'event_id': evt_id,
        'location_id': final_loc_id,
        'location_ref': final_loc_ref,
        'location_description': resolved_loc_desc,
        'confidence': loc_confidence,
        'evidence': evidence,
        'errors': all_errors,
        'semantic_errors': semantic_errors,
        'render_errors': render_errors,
        'warnings': warnings,
        'semantic_valid': semantic_valid,
        'render_ready': render_ready,
        'valid': valid,
    }


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class SourceImageRejected(DomainError):
    """Confirmed semantic rejection, with a durable bounded retry decision."""
    def __init__(self, message, retry):
        super().__init__(message, 422)
        self.details = {'code': 'SOURCE_IMAGE_REJECTED', 'retry': retry}


def required_text(value, name, maximum=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainError(f'{name}: требуется непустой текст (до {maximum} символов).')
    return value.strip()


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


VALID_PROP_STATES = {
    'absent', 'present', 'held', 'worn', 'worn_on_head',
    'placed', 'carried', 'given', 'lost', 'destroyed'
}

VALID_PROP_IMPORTANCES = {
    'story', 'incidental', 'wardrobe', 'set_dressing'
}
LEGACY_STORY_PROP_IMPORTANCES = {'', 'story', 'story_critical', 'standard', 'recurring', 'key', 'high', 'primary'}


def prop_is_story(prop):
    """Treat legacy persistent-prop labels as story props without rewriting user data."""
    if not isinstance(prop, dict):
        return False
    return str(prop.get('importance', 'story') or '').strip().casefold() in LEGACY_STORY_PROP_IMPORTANCES


def normalize_prop_continuity(data):
    """Ensure temporal consistency of props across scenes according to the pot story rules."""
    if not isinstance(data, dict):
        return data
    props = data.get('props', [])
    scenes = data.get('scenes', [])
    if not isinstance(scenes, list) or not scenes:
        return data

    # Only story props participate in state continuity and reference tracking
    story_props = [p for p in props if prop_is_story(p)]
    if not story_props:
        for scene in scenes:
            if isinstance(scene, dict):
                scene['props'] = []
        return data

    tracked = {}
    for p in story_props:
        pname = p['name']
        p_lower = pname.casefold()
        p_stem = p_lower[:max(3, len(p_lower) - 2)]
        # The first appearance must be justified by the original source, not a
        # model-invented prop tag in an earlier card. For example, moving boxes
        # stay absent until the sentence where packing begins.
        source_appearances = [index for index, scene in enumerate(scenes, start=1)
                              if isinstance(scene, dict) and
                              (p_lower in str(scene.get('source_quote', '')).casefold() or
                               p_stem in str(scene.get('source_quote', '')).casefold())]
        first_appearance = source_appearances[0] if source_appearances else p.get('first_appearance', 1)
        tracked[p['name']] = {
            'state': p.get('default_state', 'absent'),
            'holder': p.get('owner', ''),
            'location': None,
            'active': False,
            'first_appearance': max(1, int(first_appearance or 1))
        }

    for idx, scene in enumerate(scenes, start=1):
        scene_props_map = {sp['name']: sp for sp in scene.get('props', []) if isinstance(sp, dict) and 'name' in sp}
        scene_loc = scene.get('location_ref') or scene.get('location') or ''
        scene_beat = str(scene.get('beat', '')).casefold()
        scene_quote = str(scene.get('source_quote', '')).casefold()
        scene_text = f"{scene_beat} {scene_quote}"
        scene_chars = scene.get('characters', [])
        visible_people = scene.get('visible_people')
        if type(visible_people) is not int:
            visible_people = len(scene_chars) if scene_chars else (1 if scene_mentions_people(scene) else 0)

        updated_scene_props = []

        for p in story_props:
            pname = p['name']
            t = tracked[pname]
            first_app = t['first_appearance']

            explicit_sp = scene_props_map.get(pname)
            current_state = t['state']
            current_holder = t['holder']

            p_lower = pname.casefold()
            p_stem = p_lower[:max(3, len(p_lower) - 2)]
            prop_mentioned_here = (p_lower in scene_text or p_stem in scene_text)

            # Object-specific textual triggers:
            # 1. Worn on head (e.g. pot, helmet, crown)
            head_words = ('на голову', 'на голове', 'надел', 'надевает', 'надев', 'worn on head', 'on head', 'wearing', 'put on head', 'puts on head')
            cue_worn_head = False
            if prop_mentioned_here and any(hw in scene_text for hw in head_words):
                cue_worn_head = True
            elif any(w in p_lower for w in ('кастрюл', 'pot', 'шлем', 'helmet', 'шапк', 'hat', 'корон', 'crown')) and any(hw in scene_text for hw in (
                'надевает кастрюл', 'надел кастрюл', 'wearing pot', 'put pot on head', 'puts pot on head',
                'wearing cooking pot', 'pot on his head', 'pot on her head', 'на голову', 'на голове'
            )):
                cue_worn_head = True

            # 2. Placed / left behind:
            placed_verbs = (
                'снял', 'сняла', 'оставил', 'оставила', 'положил', 'положила',
                'took off', 'takes off', 'placed', 'leaves behind', 'left on'
            )
            cue_placed = False
            if t['active'] and any(pv in scene_text for pv in placed_verbs):
                if prop_mentioned_here:
                    cue_placed = True
                elif current_state in ('worn', 'worn_on_head', 'held', 'carried') and any(pv in scene_text for pv in ('снял', 'сняла', 'took off', 'takes off', 'оставил на столе', 'оставила на столе', 'left on table', 'left on the table')):
                    cue_placed = True

            # 3. Held in hands: MUST be specifically associated with this prop
            held_verbs = ('в руках', 'в руку', 'держит', 'взял', 'взяла', 'holds', 'holding', 'picked up', 'takes in hand', 'retrieves')
            cue_held = False
            if prop_mentioned_here and any(hv in scene_text for hv in held_verbs):
                cue_held = True

            # An explicit model tag cannot place a story prop before its
            # source-backed first appearance.
            if idx < first_app:
                continue

            if idx >= first_app:
                t['active'] = True

            if explicit_sp:
                new_state = explicit_sp.get('state', current_state)
                new_holder = explicit_sp.get('holder', current_holder)
                continuity = explicit_sp.get('continuity', 'changed' if new_state != current_state else 'continues')
            elif cue_placed and t['active']:
                new_state = 'placed'
                new_holder = ''
                continuity = 'placed'
            elif cue_worn_head:
                new_state = 'worn_on_head'
                new_holder = t['holder'] or (scene_chars[0] if scene_chars else '')
                continuity = 'worn' if current_state != 'worn_on_head' else 'continues'
            elif cue_held:
                new_state = 'held'
                new_holder = t['holder'] or (scene_chars[0] if scene_chars else '')
                continuity = 'held' if current_state != 'held' else 'continues'
            elif t['active']:
                if current_state in {'worn', 'worn_on_head', 'held', 'carried'}:
                    new_state = current_state
                    new_holder = current_holder
                    continuity = 'continues'
                elif current_state == 'placed':
                    if t['location'] == scene_loc and scene_loc:
                        new_state = 'placed'
                        new_holder = ''
                        continuity = 'continues'
                    else:
                        new_state = 'absent'
                        new_holder = ''
                        continuity = 'absent'
                else:
                    if t['location'] == scene_loc and scene_loc and prop_mentioned_here:
                        new_state = current_state
                        new_holder = current_holder
                        continuity = 'continues'
                    else:
                        new_state = 'absent'
                        new_holder = ''
                        continuity = 'absent'
            else:
                new_state = 'absent'
                new_holder = ''
                continuity = 'absent'

            t['state'] = new_state
            t['holder'] = new_holder
            t['location'] = scene_loc

            # Server-side Owner & Visibility Invariants
            if new_state in ('worn', 'worn_on_head', 'held', 'carried'):
                if not new_holder and scene_chars:
                    new_holder = scene_chars[0]
                # Invariant: holder must be in scene characters, and scene must have people
                if not new_holder or new_holder not in scene_chars or visible_people == 0:
                    visible = False
                else:
                    visible = True
            elif new_state == 'placed':
                new_holder = ''
                visible = True if (t['location'] == scene_loc or not t['location']) else False
            elif new_state == 'present':
                visible = True if (t['location'] == scene_loc or not t['location']) else False
            else:
                visible = False

            if new_state != 'absent':
                updated_scene_props.append({
                    'name': pname,
                    'state': new_state,
                    'holder': new_holder,
                    'visible': visible,
                    'continuity': continuity
                })

        scene['props'] = updated_scene_props

    return data


_STORY_SECTION_HEADING = re.compile(
    r'^\s*#{1,6}\s*(?:сценарий|сюжет|история|story|screenplay|plot)\s*:?\s*#*\s*$', re.IGNORECASE)
_SCENE_HEADING = re.compile(r'^\s*#{1,6}\s*(?:сцена|scene)\s*\d+[.:]?\s*$', re.IGNORECASE)
_STORY_METADATA_LINE = re.compile(
    r'^\s*\*{0,2}(?:жанр|genre|визуальный\s+стиль|visual\s+style)\*{0,2}\s*:', re.IGNORECASE)
_SOURCE_ATTRIBUTION_LINE = re.compile(
    r'^\s*(?:(?:https?://|www\.)\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?|'
    r'(?:source|источник|credit(?:s)?|via)\s*[:—-].*)\s*$', re.IGNORECASE)
_INLINE_ATTRIBUTION_LABEL = re.compile(
    r'(?m)(?<=[.!?…])[ \t]+(?:[A-ZА-ЯЁ][\w-]*)(?:[ \t]+[A-ZА-ЯЁ][\w-]*){0,4}[ \t]*(?=\n)')
_INLINE_DOMAIN_ATTRIBUTION = re.compile(
    r'(?im)(?<=[.!?…])[ \t]+(?:(?:https?://|www\.)\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)[ \t]*(?=\n|$)')


def story_segments(story):
    """Return exact, nonempty narrative spans without source-attribution lines."""
    text = str(story or '')
    lines = text.splitlines()
    heading_index = next((index for index, line in enumerate(lines)
                          if _STORY_SECTION_HEADING.match(line)), None)
    if heading_index is not None:
        lines = lines[heading_index + 1:]
    narrative = '\n'.join(line for line in lines
                          if line.strip() and not _SCENE_HEADING.match(line)
                          and not _STORY_METADATA_LINE.match(line)
                          and not _SOURCE_ATTRIBUTION_LINE.match(line))
    narrative = _INLINE_ATTRIBUTION_LABEL.sub('', narrative)
    narrative = _INLINE_DOMAIN_ATTRIBUTION.sub('', narrative)
    narrative = re.sub(r'(?im)^\s*(?:(?:https?://|www\.)\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)\s*\n?', '', narrative)
    raw = [match.group(0).strip() for match in re.finditer(r'[^.!?…]+(?:[.!?…]+|$)', narrative)]
    segments = []
    for segment in raw:
        while len(segment) > 12000:
            split = segment.rfind(' ', 0, 12000)
            if split < 1000:
                split = 12000
            segments.append(segment[:split].strip())
            segment = segment[split:].strip()
        if segment:
            segments.append(segment)
    return segments


_DASH_RE = re.compile(r'[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]|\-{2,}')
_QUOTE_RE = re.compile(r'[\u00ab\u00bb\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2039\u203a\u2032\u2033\u2034\u2035\u2036\u2037"\'`]')
_WS_RE = re.compile(r'[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+')


def normalize_text_for_matching(text):
    if not isinstance(text, str):
        return ''
    t = _DASH_RE.sub('-', text)
    t = _QUOTE_RE.sub("'", t)
    t = _WS_RE.sub(' ', t).strip()
    return t.casefold()


def extract_meaningful_tokens(text):
    if not isinstance(text, str):
        return []
    norm = normalize_text_for_matching(text)
    return [w for w in re.findall(r'[\w]+', norm, re.UNICODE) if len(w) >= 2]


def find_quote_in_story(quote, story, segments=None, last_start=0):
    """Multi-tier quote alignment against story narrative.

    Returns: (start_idx, end_idx, verbatim_quote, confidence_score, diagnostics_string)
    """
    if not isinstance(quote, str) or not quote.strip() or not isinstance(story, str) or not story:
        return None, None, None, 0.0, 'Пустая цитата или текст истории'

    clean_quote = quote.strip()

    if segments is None:
        segments = story_segments(story)

    # 1. Tier 1: Exact Segment Match or Segment Substring/Prefix
    for idx, seg in enumerate(segments):
        if clean_quote == seg:
            seg_start = story.find(seg, last_start)
            if seg_start < 0:
                seg_start = story.find(seg, 0)
            if seg_start >= 0:
                return seg_start, seg_start + len(seg), seg, 1.0, ''

    for idx, seg in enumerate(segments):
        if clean_quote in seg or seg in clean_quote:
            seg_start = story.find(seg, last_start)
            if seg_start < 0:
                seg_start = story.find(seg, 0)
            if seg_start >= 0:
                return seg_start, seg_start + len(seg), seg, 1.0, ''

    # Direct substring in story if not aligned with a single segment
    start = story.find(clean_quote, last_start)
    if start >= 0:
        return start, start + len(clean_quote), clean_quote, 1.0, ''
    start = story.find(clean_quote, 0)
    if start >= 0:
        return start, start + len(clean_quote), clean_quote, 1.0, ''

    # 2. Tier 2: Normalized Substring / Formatted Match
    norm_quote = normalize_text_for_matching(clean_quote)
    norm_quote_tokens = extract_meaningful_tokens(clean_quote)

    for idx, seg in enumerate(segments):
        norm_seg = normalize_text_for_matching(seg)
        if norm_quote == norm_seg or norm_quote in norm_seg or norm_seg in norm_quote:
            seg_start = story.find(seg, last_start)
            if seg_start < 0:
                seg_start = story.find(seg, 0)
            if seg_start >= 0:
                return seg_start, seg_start + len(seg), seg, 0.95, ''

    if norm_quote_tokens:
        tok_patterns = [re.escape(t) for t in norm_quote_tokens]
        if len(tok_patterns) >= 1:
            pat = r'[\s\W\d_]*'.join(tok_patterns[:15])
            match = re.search(pat, story[last_start:], re.IGNORECASE)
            if match:
                m_start = last_start + match.start()
                m_end = last_start + match.end()
                return m_start, m_end, story[m_start:m_end], 0.90, ''
            match = re.search(pat, story, re.IGNORECASE)
            if match:
                return match.start(), match.end(), story[match.start():match.end()], 0.90, ''

    # 3. Tier 3: Token Overlap & Fuzzy Similarity Matching
    if not norm_quote_tokens:
        return None, None, None, 0.0, 'Недостаточно значащих слов для сопоставления'

    q_set = set(norm_quote_tokens)
    scored_candidates = []

    for idx, seg in enumerate(segments):
        s_tokens = extract_meaningful_tokens(seg)
        if not s_tokens:
            continue
        s_set = set(s_tokens)
        common = q_set & s_set
        if not common:
            continue
        coverage = len(common) / max(1, len(q_set))
        jaccard = len(common) / max(1, len(q_set | s_set))
        norm_seg = normalize_text_for_matching(seg)
        seq_ratio = difflib.SequenceMatcher(None, norm_quote, norm_seg).ratio()

        score = 0.45 * coverage + 0.35 * seq_ratio + 0.20 * jaccard

        seg_start = story.find(seg, last_start)
        if seg_start >= 0:
            score += 0.05
        else:
            seg_start = story.find(seg, 0)

        scored_candidates.append((score, idx, seg, seg_start))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    if scored_candidates:
        best_score, best_idx, best_seg, best_start = scored_candidates[0]
        if best_score >= 0.35 or (len(q_set & set(extract_meaningful_tokens(best_seg))) >= 2 and best_score >= 0.28):
            if best_start >= 0:
                return best_start, best_start + len(best_seg), best_seg, best_score, ''
            else:
                best_toks = extract_meaningful_tokens(best_seg)
                if best_toks:
                    pat = r'[\s\W\d_]*'.join(re.escape(t) for t in best_toks[:10])
                    m = re.search(pat, story, re.IGNORECASE)
                    if m:
                        return m.start(), m.end(), story[m.start():m.end()], best_score, ''
                return 0, len(story), best_seg, best_score, ''

    # 4. Tier 4: Ungrounded Diagnostic
    top_preview = ', '.join([f'"{c[2][:60]}..." (score: {c[0]:.2f})' for c in scored_candidates[:3]])
    return None, None, None, 0.0, top_preview or 'совпадений с сегментами не найдено'


def validate_analysis(data, story):
    if not isinstance(data, dict):
        raise DomainError('Разбор истории должен быть объектом JSON.')
    characters = data.get('characters')
    locations = data.get('locations', [])
    if 'locations' not in data:
        data['locations'] = locations
    props = data.get('props', [])
    if 'props' not in data:
        data['props'] = props
    scenes = data.get('scenes')
    if not isinstance(characters, list) or len(characters) > 100:
        raise DomainError('Поддерживается 0–100 постоянных персонажей в проекте.')
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
        raise DomainError(f'Нужно от 1 до {MAX_SCENES} сцен.')
    names = set()
    character_by_name = {}
    for character in characters:
        if not isinstance(character, dict):
            raise DomainError('Неверная запись персонажа.')
        name = required_text(character.get('name'), 'Имя', 100)
        if name in names:
            raise DomainError('Имена персонажей должны быть уникальными.')
        names.add(name)
        character_by_name[name] = character
        required_text(character.get('description'), 'Описание персонажа', 4000)
        wardrobe = character.get('wardrobe', {})
        if not isinstance(wardrobe, dict) or len(wardrobe) > 12:
            raise DomainError('Гардероб должен быть объектом, максимум 12 вариантов.')
        for key, description in wardrobe.items():
            required_text(key, 'Код одежды', 80)
            required_text(description, 'Описание одежды', 1000)
    if not isinstance(locations, list) or len(locations) > MAX_LOCATIONS:
        raise DomainError('Поддерживается 0–6 постоянных локаций.')
    location_names = set()
    for location in locations:
        if not isinstance(location, dict):
            raise DomainError('Неверная запись локации.')
        name = required_text(location.get('name'), 'Название локации', 100)
        if name in location_names:
            raise DomainError('Названия локаций должны быть уникальными.')
        location_names.add(name)
        required_text(location.get('description'), 'Описание локации', 4000)

    if not isinstance(props, list) or len(props) > MAX_PROPS:
        raise DomainError('Поддерживается 0–6 постоянных предметов реквизита.')
    prop_names = set()
    for prop in props:
        if not isinstance(prop, dict):
            raise DomainError('Неверная запись предмета реквизита.')
        pname = required_text(prop.get('name'), 'Название предмета', 100)
        if pname in prop_names:
            raise DomainError('Названия предметов должны быть уникальными.')
        prop_names.add(pname)
        required_text(prop.get('description'), 'Описание предмета', 4000)
        if not isinstance(prop.get('owner', ''), str):
            prop['owner'] = ''
        if 'first_appearance' in prop and prop['first_appearance'] is not None:
            try:
                prop['first_appearance'] = max(1, int(prop['first_appearance']))
            except (ValueError, TypeError):
                prop['first_appearance'] = 1
        else:
            prop['first_appearance'] = 1
        if prop.get('default_state') not in VALID_PROP_STATES:
            prop['default_state'] = 'absent'
        imp = str(prop.get('importance', 'story')).lower().strip()
        if imp in ('standard', 'recurring', 'key', 'high', 'primary'):
            imp = 'story'
        elif imp not in VALID_PROP_IMPORTANCES:
            imp = 'story'
        prop['importance'] = imp

    story_clean = str(story or '')
    segments = story_segments(story_clean) if story_clean else []

    last_start = 0
    resolved_scenes = []
    for scene_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise DomainError('Неверная запись сцены.')
        beat = scene.get('beat')
        if not isinstance(beat, str) or not beat.strip():
            raw_q = scene.get('source_quote')
            if isinstance(raw_q, str) and raw_q.strip():
                scene['beat'] = raw_q.strip()[:4000]
            else:
                scene['beat'] = 'Сюжетное событие сцены'
        required_text(scene.get('beat'), 'Событие сцены', 4000)
        raw_quote = scene.get('source_quote')
        required_text(raw_quote, 'Цитата истории', 70000 if scene.get('analysis_authority') == 'deepseek-v2' else 10000)

        source_start, source_end = scene.get('source_start'), scene.get('source_end')
        if (type(source_start) is int and type(source_end) is int and
                0 <= source_start < source_end <= len(story_clean) and
                story_clean[source_start:source_end] == raw_quote):
            # V3 can merge dependent sentences into one event. Preserve its
            # complete grounded span instead of choosing a shorter sentence.
            match_start, match_end, matched_quote, score, diagnostics = source_start, source_end, raw_quote, 1, ''
        else:
            match_start, match_end, matched_quote, score, diagnostics = find_quote_in_story(
                raw_quote, story_clean, segments=segments, last_start=last_start
            )
        if match_start is None or match_start < 0:
            diag_suffix = f' Ближайшие фрагменты: {diagnostics}' if diagnostics else ''
            raise DomainError(
                f'Цитата сцены {scene_idx + 1} отсутствует в исходном тексте или не подтверждена источником: "{raw_quote[:100]}".{diag_suffix}'
            )
        scene['source_quote'] = matched_quote
        scene['source_start'] = match_start
        scene['source_end'] = match_end
        last_start = match_start
        resolved_scenes.append(scene)

    resolved_scenes.sort(key=lambda s: (s.get('source_start', 0), scenes.index(s)))
    scenes[:] = resolved_scenes

    prev_contract = None
    for scene_idx, scene in enumerate(scenes):
        if not isinstance(scene.get('emotion'), str) or not scene['emotion'].strip():
            scene['emotion'] = 'calm and attentive, appropriate to the event'
        for key in ('location', 'time', 'shot', 'continuity_before', 'continuity_after', 'negative'):
            if not isinstance(scene.get(key, ''), str):
                raise DomainError(f'{key}: требуется строка.')

        character_by_name_casefold = {name.casefold(): name for name in character_by_name}
        for name in scene.get('characters', []):
            if not isinstance(name, str) or name.strip().casefold() not in character_by_name_casefold:
                raise DomainError(f'Неизвестный персонаж в сцене: {name}')
        outfits = scene.get('wardrobe', {})
        if not isinstance(outfits, dict):
            raise DomainError('Гардероб сцены должен быть объектом.')
        for name, outfit in outfits.items():
            if not isinstance(name, str) or name.strip().casefold() not in character_by_name_casefold:
                raise DomainError(f'Неизвестный персонаж в гардеробе сцены: {name}')
            canonical_char_name = character_by_name_casefold[name.strip().casefold()]
            if outfit not in character_by_name[canonical_char_name].get('wardrobe', {}):
                raise DomainError(f'Неизвестная одежда "{outfit}" для персонажа {name}')

        # Unified Scene Presence Contract
        contract = resolve_scene_presence_contract(
            scene, characters, locations, story=story_clean,
            previous_contract=prev_contract,
            require_locked_portraits=False,
            scene_index=scene_idx + 1
        )
        prev_contract = contract

        raw_vp = scene.get('visible_people')
        canonical_vp = contract['visible_people']
        audit = {
            'raw_visible_people': raw_vp,
            'canonical_visible_people': canonical_vp,
            'visible_characters': list(contract['visible_characters']),
            'episodic_people': list(contract['episodic_people']),
            'voice_only_characters': list(contract['voice_only_characters']),
            'mentioned_characters': list(contract['mentioned_characters']),
            'offscreen_characters': list(contract['offscreen_characters']),
        }
        if raw_vp != canonical_vp and raw_vp is not None:
            audit['reconciliation_reason'] = (
                f"LLM visible_people ({raw_vp}) reconciled to canonical ({canonical_vp}): "
                f"characters={contract['visible_characters']}, episodic={contract['episodic_people']}"
            )
        scene['raw_visible_people'] = raw_vp
        scene['visible_people_audit'] = audit

        scene['characters'] = list(contract['visible_characters'])
        scene['character_ids'] = list(contract['visible_character_ids'])
        scene['visible_people'] = canonical_vp
        scene['episodic_people'] = list(contract['episodic_people'])
        scene['depicted_people'] = list(dict.fromkeys((scene.get('depicted_people', []) if scene.get('analysis_authority') == 'deepseek-v2' else []) + list(contract['depicted_people'])))
        scene['voice_only_characters'] = list(contract['voice_only_characters'])
        scene['offscreen_characters'] = list(contract['offscreen_characters'])
        scene['mentioned_characters'] = list(dict.fromkeys((scene.get('mentioned_characters', []) if scene.get('analysis_authority') == 'deepseek-v2' else []) + list(contract['mentioned_characters'])))
        scene['location_ref'] = contract['location_ref'] or ''
        if contract['location_ref']:
            scene['location'] = contract['location_ref']

        if contract['semantic_errors']:
            fatal_errors = [e for e in contract['semantic_errors'] if e['code'] in ('empty_scene_metadata',)]
            if fatal_errors:
                raise DomainError(fatal_errors[0]['message'])

        cleaned_outfits = {}
        for name, outfit in outfits.items():
            if name in scene['characters'] and name in character_by_name:
                char_w = character_by_name[name].get('wardrobe', {})
                if outfit in char_w:
                    cleaned_outfits[name] = outfit
        scene['wardrobe'] = cleaned_outfits

        # Props in scene
        scene_props = scene.get('props', [])
        if not isinstance(scene_props, list):
            scene_props = []
        validated_scene_props = []
        for sp in scene_props:
            if not isinstance(sp, dict):
                continue
            p_name = sp.get('name', '').strip()
            if not p_name or p_name not in prop_names:
                continue
            state = sp.get('state', 'present')
            if state not in VALID_PROP_STATES:
                state = 'present'
            holder = sp.get('holder', '')
            if not isinstance(holder, str):
                holder = ''
            visible = bool(sp.get('visible', True)) if state != 'absent' else False
            continuity = str(sp.get('continuity', 'continues'))
            validated_scene_props.append({
                'name': p_name,
                'state': state,
                'holder': holder,
                'visible': visible,
                'continuity': continuity
            })
        scene['props'] = validated_scene_props

    normalize_prop_continuity(data)
    forgotten = (data.get('v3_metadata') or data.get('v2_metadata') or {}).get('forgotten_cities')
    if forgotten:
        # Generic presence/prop reconciliation must not turn deceased remains
        # into a live extra or drop the subject of an archaeological closeup.
        from .forgotten_cities.prompts import build_prompt_packet
        from .forgotten_cities.grounding import apply_packet
        # The source-reviewed document is unchanged throughout this validation.
        # Verify it once, then ground each scene against that checked document.
        # Calling ground() in the loop revalidated the complete story for
        # every frame, making final saves quadratic in scene count.
        from .narrative_contract import sequence_grounder
        ground_narrative = sequence_grounder(story_clean,data.get('v3_metadata') or data.get('v2_metadata') or {})
        for index, scene in enumerate(scenes, 1):
            reviewed=ground_narrative(scene)
            if reviewed is not None:
                scene.update(reviewed)
                continue
            packet = build_prompt_packet(forgotten, scene.get('forgotten_event_id') or '',
                scene.get('beat', ''), scene.get('location', ''), ordinal=index,
                story_text=story_clean, source_quote=scene.get('source_quote', ''),
                source_start=scene.get('source_start'))
            scene.update(apply_packet(scene, packet))
    return data


VALID_PROFILE_TYPES = {'stickman', 'stickman_story', 'custom_story', 'anime_story', 'comic_story', 'forgotten_cities'}
VALID_CHARACTER_ROLES = {'main', 'recurring', 'secondary'}
VALID_CHANNEL_ASSET_TYPES = {'character_front', 'character_pose', 'expression_sheet', 'style_reference', 'palette_reference'}

STICKMAN_DEFAULT_VERSION = {
    'style_prompt': 'Minimalist hand-drawn stickman story illustration on clean white background, expressive thick black ink outlines, clean solid color accents, consistent stick figure cartoon character design, 2D vector animation style.',
    'negative_prompt': 'photorealistic, 3D render, complex shading, realism, noisy background, text, captions, subtitles, logos, watermarks, deformed anatomy, human skin pores, hyperrealistic faces, cinematic CGI, blurry lines.',
    'composition_rules': 'Clear 2D staging, single camera view, wide expressive framing, character centered or rule of thirds, clean horizon and ground plane, uncluttered negative space.',
    'color_palette': 'Minimalist palette: stark white background (#FFFFFF), bold pitch-black ink outlines (#000000), bright primary accent colors for key props and signature character items.',
    'line_style': 'Smooth bold uniform black ink lines, consistent stroke weight throughout, rounded line ends, hand-drawn digital ink clarity.',
    'background_style': 'Pure minimalist flat white background with sparse architectural or environmental line art only when required by scene context.',
    'aspect_ratio': '16:9',
    'animation_rules': '2D keyframe cut-out movement, clear poses, expressive gestures, snappy comic pacing.',
}

STICKMAN_DEFAULT_CHARACTER = {
    'canonical_name': 'Стикмен',
    'role': 'main',
    'description': 'Канонический Стикмен: круглая голова с выразительными точками-глазами и улыбкой, четкое палочное тело и конечности со стабильными пропорциями.',
    'appearance_prompt': 'Canonical Stickman protagonist: round head with simple expressive dot eyes, clean stick figure body, consistent head-to-body 1:3 proportions, recognizable silhouette.',
    'negative_prompt': 'realistic face, detailed nose, realistic eyes, muscles, 3D body, complex costume details, photorealism.',
    'is_locked': 1,
    'is_active': 1,
}

STICKMAN_DEFAULT_CHARACTER_VERSION = {
    'appearance_prompt': 'Canonical Stickman protagonist: round head with simple expressive dot eyes, clean stick figure body, consistent head-to-body 1:3 proportions, recognizable silhouette.',
    'wardrobe_rules': 'Consistent signature red beanie or blue scarf (optional), simple single-color shirt or bare stick body with uniform line width.',
    'proportion_rules': 'Stable head diameter equals 1/3 of total stickman height; limbs equal length, smooth joints.',
    'face_rules': 'Simple black dot eyes, clean curved line mouth for expressions, no nose, no realistic ears, minimal stick hair or signature cap.',
    'color_rules': 'Solid black lines (#000000) for body, vibrant single-color accent for signature accessory.',
}

FORGOTTEN_CITIES_DEFAULT_VERSION = {
    'style_prompt': 'Historically grounded archaeological documentary imagery with consistent architecture, materials, landscape, weather and restrained cinematic lighting.',
    'negative_prompt': 'anachronisms, mixed historical periods, modern objects in reconstruction, invented inscriptions, sensational fantasy, text, captions, logos, watermarks.',
    'composition_rules': 'Treat the city as the recurring subject. Keep modern ruins, excavation, museum display, maps and historical reconstruction visually distinct. Use at most one primary architectural anchor and one primary artifact anchor per shot.',
    'color_palette': 'Natural mineral and earth pigments derived from the documented city, its materials and landscape.',
    'line_style': '',
    'background_style': 'Evidence-led documentary environments with stable geography and architecture.',
    'aspect_ratio': '16:9',
    'animation_rules': 'Animate only approved stills; preserve architecture, artifact geometry, time layer and evidence level.',
}


@dataclass
class GenerationReferenceBundle:
    main_character_reference: Optional[str] = None
    extra_pose_references: List[str] = field(default_factory=list)
    style_reference: Optional[str] = None
    palette_reference: Optional[str] = None
    weights: Dict[str, float] = field(default_factory=dict)
    canonical_character_ids: List[str] = field(default_factory=list)
    reference_asset_ids: List[str] = field(default_factory=list)
    textual_style_contract: str = ""
    negative_style_rules: str = ""
    aspect_ratio: str = "16:9"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'main_character_reference': self.main_character_reference,
            'extra_pose_references': list(self.extra_pose_references),
            'style_reference': self.style_reference,
            'palette_reference': self.palette_reference,
            'weights': dict(self.weights),
            'canonical_character_ids': list(self.canonical_character_ids),
            'reference_asset_ids': list(self.reference_asset_ids),
            'textual_style_contract': self.textual_style_contract,
            'negative_style_rules': self.negative_style_rules,
            'aspect_ratio': self.aspect_ratio,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> 'GenerationReferenceBundle':
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            main_character_reference=data.get('main_character_reference'),
            extra_pose_references=list(data.get('extra_pose_references') or []),
            style_reference=data.get('style_reference'),
            palette_reference=data.get('palette_reference'),
            weights=dict(data.get('weights') or {}),
            canonical_character_ids=list(data.get('canonical_character_ids') or []),
            reference_asset_ids=list(data.get('reference_asset_ids') or []),
            textual_style_contract=str(data.get('textual_style_contract') or ''),
            negative_style_rules=str(data.get('negative_style_rules') or ''),
            aspect_ratio=str(data.get('aspect_ratio') or '16:9'),
        )


def compile_channel_style_contract(profile: Dict[str, Any], version: Dict[str, Any], characters: Optional[List[Dict[str, Any]]] = None) -> str:
    """Compile an explicit, repeatable visual contract string representing the channel's active/snapshot version."""
    lines = []
    p_type = profile.get('profile_type', 'stickman_story')
    p_name = profile.get('name', 'Unnamed Channel')
    v_num = version.get('version_number', 1)
    lines.append(f"Channel Profile: {p_name} (Type: {p_type}, Version: {v_num})")
    if version.get('style_prompt'):
        lines.append(f"Style: {version['style_prompt']}")
    if version.get('line_style'):
        lines.append(f"Line Style: {version['line_style']}")
    if version.get('background_style'):
        lines.append(f"Background Style: {version['background_style']}")
    if version.get('color_palette'):
        lines.append(f"Color Palette: {version['color_palette']}")
    if version.get('composition_rules'):
        lines.append(f"Composition: {version['composition_rules']}")
    if p_type in ('stickman', 'stickman_story'):
        lines.append("Stickman Invariants: Strict 2D vector stick figure art. Prohibit photorealism, 3D CGI, skin textures, realistic facial anatomy. Keep uniform line thickness and consistent character proportions.")
    if p_type == 'forgotten_cities':
        lines.append("Forgotten Cities Invariants: Preserve the approved city architecture and source-backed time layer. Clearly mark reconstruction and hypothesis. Do not turn mentioned regions or researcher names into scene locations. Do not add recurring people unless explicitly configured.")
    if characters:
        for c in characters:
            char_role = c.get('role', 'main')
            c_name = c.get('canonical_name') or c.get('name', 'Character')
            c_desc = c.get('appearance_prompt') or c.get('description', '')
            c_prop = c.get('proportion_rules', '')
            c_face = c.get('face_rules', '')
            c_ward = c.get('wardrobe_rules', '')
            c_col = c.get('color_rules', '')
            char_rules = [r for r in (c_desc, c_prop, c_face, c_ward, c_col) if r]
            lines.append(f"Canonical Character ({char_role}): @{c_name} -> {' | '.join(char_rules)}")
    return '\n'.join(lines)


def source_depicts_death(scene):
    source = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat'))
    return bool(re.search(r'\b(?:dies?|dead|death|killed|corpse|funeral|buried|burial|entombed|умер\w*|смерт\w*|погиб\w*|убит\w*|труп\w*|погреб\w*|похорон\w*|захорон\w*)\b', source, re.I))



# A cast registry answers which identity reference to attach. Missing a name
# does not prove the narration contains no patient, worker, hand or other actor.
SOURCE_PEOPLE_UNSPECIFIED = (
    'Human presence has no verified exact census. Depict only the people and body parts '
    'actually required by the CURRENT source action or bodily state. A source-described '
    'unnamed person is allowed without a recurring identity reference. Do not replace '
    'a person experiencing a bodily state with an empty room. Do not invent bystanders, '
    'extra observers, identities, symptoms, injuries or medical procedures. '
    'A mentioned author, portrait or statue is not a living participant.')

def people_count_unresolved(scene):
    if scene.get('source_groups'): return True
    if (scene.get('population_policy') in ('object_only','source_reviewed_participants','source_group','source_people','inhabited_city')
            or scene.get('visual_content') or scene.get('participant_identity')
            or scene.get('subject_state')=='buried_remains'
            or scene.get('source_animals')):
        return False
    # Local text partitioning resolves recurring identities, not every anonymous
    # participant in an action. Its registry length is never an exact census.
    # Read old drafts through this rule too; do not rewrite source revisions.
    draft = scene.get('storyboard_frame') or {}
    if (scene.get('population_count_basis') == 'named_identity_registry' or
            draft.get('interpretation_status') == 'source_bound_draft'):
        return True
    if scene.get('population_policy')=='source_unspecified':return True
    return (scene.get('analysis_authority')=='deepseek-v2'
            and scene.get('visible_people')==0
            and not scene.get('characters') and not scene.get('character_ids'))

def compile_prompt(project, scene, characters, locations=(), props=(), allow_unlocked=False, channel_context=None, return_contract=False):
    from .subject_policy import enrich, normalize_scene, requires_portrait, source_description
    characters = enrich(characters, project.get('cast_bible'))
    scene = normalize_scene(scene, characters)
    from .panorama_camera import owns_framing, wide_framing
    panorama_framing = owns_framing(project, scene)
    camera_project = {**project, 'characters': project.get('characters') or characters}
    subject_text = source_description(scene)
    characters = [c for c in characters if requires_portrait(c)]
    from .mode_policy import allows
    if not allows(project, 'location'): locations = ()
    if not allows(project, 'prop'): props = ()
    if not allows(project, 'character'): characters = [{**c, 'locked': False} for c in characters]
    # Defense-in-depth: scope characters to scene if scene declares participants
    if characters and (scene.get('characters') or scene.get('character_ids')):
        sc_names = {str(n).casefold() for n in scene.get('characters', []) if n}
        sc_ids = {str(i) for i in scene.get('character_ids', []) if i}
        matching_chars = [
            c for c in characters
            if str(c.get('name', '')).casefold() in sc_names
            or str(c.get('canonical_name', '')).casefold() in sc_names
            or str(c.get('id', '')) in sc_ids
            or str(c.get('cast_id', '')) in sc_ids
        ]
        if matching_chars:
            characters = matching_chars

    if project.get('mode') == 'illustration': allow_unlocked = True
    references = []
    person_mode = project.get('mode') in {'characters', 'dynamic'}
    subject_mode = project.get('mode') == 'objects'
    channel_profile = (channel_context or {}).get('profile') if channel_context else None
    channel_version = (channel_context or {}).get('version') if channel_context else None
    channel_chars = (channel_context or {}).get('characters') or []
    channel_assets = (channel_context or {}).get('reference_assets') or []
    profile_type = (channel_profile.get('profile_type') if channel_profile else None) or project.get('profile_type') or ('stickman_story' if channel_profile else '')
    visible_people = scene.get('visible_people')
    if type(visible_people) is not int:
        visible_people = sum(not is_nonhuman_character(c) for c in characters) if characters else (1 if scene_mentions_people(scene) else 0)
    # Forgotten Cities may use anonymous historical people even though its UI
    # runs in Objects mode.  Its source-backed people policy is authoritative;
    # do not turn an action scene into an empty landscape solely due to mode.
    has_people = visible_people > 0 and (person_mode or profile_type == 'forgotten_cities' or scene.get('population_policy') == 'source_reviewed_participants')
    unresolved_people = people_count_unresolved(scene)
    has_living_nonhuman = bool(scene.get('source_animals')) or any(is_nonhuman_character(c) for c in characters)

    if channel_version and channel_version.get('style_prompt'):
        style = channel_version['style_prompt']
        if channel_version.get('line_style'):
            style += f", {channel_version['line_style']}"
        if channel_version.get('background_style'):
            style += f", {channel_version['background_style']}"
        if channel_version.get('color_palette'):
            style += f", palette: {channel_version['color_palette']}"
    else:
        style = project.get('style', '')

    if not has_people:
        for old, new in (
            ('consistent animation character design', 'coherent animation design'),
            ('realistic human faces and anatomy', 'precise natural and material detail'),
            ('consistent character design', 'coherent visual design'),
            ('natural skin texture', 'natural surface texture'),
            ('expressive faces', 'expressive visual storytelling'),
        ):
            style = style.replace(old, new)
        if unresolved_people:
            lines = [SOURCE_PEOPLE_UNSPECIFIED]
        elif scene.get('subject_state') == 'buried_remains':
            lines = ['SOURCE-DESCRIBED BURIAL. Show the deceased remains in the grave. No living version of the buried person, no invented living observers. Non-graphic archaeological depiction.']
        elif (scene.get('forgotten_cities_prompt_contract') or {}).get('source_binding') == 'artifact_detail':
            lines = ['OBJECT DETAIL SHOT. The named artifact and its source-described surface details fill the frame. No people. Do not replace the object with architecture or landscape.']
        elif has_living_nonhuman:
            lines = ['SOURCE-DESCRIBED ANIMALS. Show the source-described animals and their setting. No human participants unless explicitly required. Do not replace living animals with an empty setting or inanimate objects.']
        elif channel_profile and profile_type in ('stickman', 'stickman_story'):
            lines = ['UNINHABITED SCENE. STICKMAN STYLE ENVIRONMENT ONLY. '
                     'Render only the minimalist 2D setting and non-living elements explicitly described below. '
                     'No people, no characters, no stick figures, no human silhouette, no hands, no human body parts, '
                     'no empty floating or standing clothing in human shape, no phantom figures.']
        else:
            lines = ['UNINHABITED SCENE. ENVIRONMENT AND INANIMATE PHENOMENA ONLY. '
                     'Render only the setting and non-living elements explicitly described below. '
                     'No people, no characters, no human silhouette, no hands, no human body parts, '
                     'no empty floating or standing clothing in human shape, no phantom figures.']
    else:
        lines = []

    from .visual_style import is_marker_style, marker_contract, is_gritty_style, GRITTY_FLOW_STYLE
    if is_marker_style(style) and not has_people and not unresolved_people:
        style = marker_contract(False)
    lines.extend([f"Visual style: {style}.", f"World/era: {project.get('world', '')}."])
    if channel_version and channel_version.get('composition_rules'):
        lines.append(f"Composition rules: {channel_version['composition_rules']}.")

    narrative_cues = (' where ', ' meets ', ' meet ', ' finds ', ' find ', ' learns ', ' learn ', ' arrives ',
                      ' reads ', ' retrieves ', ' discovers ', ' woman ', ' man ', ' person ', ' people ',
                      ' приходит', ' встреча', ' находит', ' узнаёт', ' узнает', ' приезжает', ' женщина', ' мужчина')
    for index, location in enumerate(locations, 1):
        loc_desc = location.get('description', '')
        if any(cue in loc_desc.casefold() for cue in narrative_cues):
            clean_desc = f"A believable, permanently consistent {location['name']} with fixed architectural layout and interior fixtures."
        else:
            clean_desc = loc_desc

        if not location.get('locked'):
            lines.append(f"Location {index}: {location['name']}. Setting and architecture: {clean_desc}.")
            continue
        refs = json.loads(location['references_json']) if isinstance(location.get('references_json'), str) else location.get('references', {})
        if 'environment' not in refs:
            lines.append(f"Location {index}: {location['name']}. Setting and architecture: {clean_desc}.")
            continue
        references.append({'asset_id': refs['environment'], 'location_id': location['id'],
                           'revision': location['revision'], 'role': 'environment',
                           **({'scope': location['reference_scope'], 'anchor_id': location.get('anchor_id')}
                              if location.get('reference_scope') else {})})
        if location.get('reference_scope') == 'source_context':
            lines.append('Environment reference is compatible material and landscape context only. '
                         'The source scene defines exact buildings, era, layout, people and state; '
                         'do not copy unrequested structures or modern additions from the reference.')
            continue
        if location.get('reference_scope') == 'material_context':
            from .forgotten_cities.references import context_instruction
            lines.append(context_instruction(location['name']))
            continue
        lines.append(f"Location reference image {index}: {location['name']}. Preserve this exact place: "
                     f"{clean_desc}. Keep the architectural footprint, facade, door and window "
                     "positions, proportions, room layout, permanent fixtures and landmark details consistent. "
                     "Maintain the exact same room layout, wall positions, door/window placements, furniture arrangement and fixtures across all camera angles. "
                     "Change only camera angle, lighting, weather, temporary props and source-described damage.")
    for index, character in enumerate(characters, len(locations) + 1):
        wardrobe_key = scene.get('wardrobe', {}).get(character['name'])
        wardrobe_dict = json.loads(character['wardrobe']) if isinstance(character.get('wardrobe'), str) else character.get('wardrobe', {})
        outfit = wardrobe_dict.get(wardrobe_key, '') if isinstance(wardrobe_dict, dict) else ''
        char_tag = f"@{character['name']}" if not character['name'].startswith('@') else character['name']
        if not character.get('locked'):
            if not allow_unlocked:
                raise DomainError(f"Сначала LOCK персонажа {character['name']}.", 409)
            if is_nonhuman_character(character):
                lines.append(f"Non-human subject {index}: {char_tag}. Appearance and anatomy: {character['description']}. "
                             "Preserve its species and body plan. No human face, body, hands or clothing.")
                continue
            clothing_str = f"Clothing: {outfit}." if outfit else f"Fixed clothing: {char_tag} wears the same signature outfit across all scenes."
            lines.append(f"Character {index}: {char_tag}. Appearance: {character['description']}. {clothing_str}")
            continue
        refs = json.loads(character['references_json']) if isinstance(character.get('references_json'), str) else character.get('references', {})
        if 'portrait' not in refs:
            if not allow_unlocked:
                raise DomainError(f"Нет одобренного референса внешности для {character['name']}.", 409)
            clothing_str = f"Clothing: {outfit}." if outfit else f"Fixed clothing: {char_tag} wears the same signature outfit across all scenes."
            lines.append(f"Character {index}: {char_tag}. Appearance: {character['description']}. {clothing_str}")
            continue
        references.append({'asset_id': refs['portrait'], 'character_id': character['id'],
                           'revision': character['revision'], 'role': 'portrait',
                           'entity_type': character['entity_type']})
        if is_nonhuman_character(character):
            lines.append(f"Reference image {index}: {char_tag}. Preserve this non-human subject's species, "
                         f"body plan, markings and distinguishing features: {character['description']}. "
                         "No human face, body, hands, wardrobe or gear.")
            continue
        if subject_mode:
            lines.append(f"Reference image {index}: {character['name']}. Preserve this object's defining form, "
                         f"materials, structure and recognisable details: {character['description']}. "
                         "Use it only as an object, landmark, vehicle or architectural reference; do not turn it "
                         "into a living figure.")
        else:
            if outfit:
                clothing_clause = f"Fixed clothing for this scene: {outfit}. Maintain this exact costume."
            else:
                clothing_clause = (f"Fixed character wardrobe: {char_tag} must wear the exact same consistent signature outfit "
                                   "across all scenes in this story. Do not change outfit, jacket, shirt, colors, or clothing style between frames.")
            lines.append(f"Reference image {index}: {char_tag}. Preserve this character's identity: "
                         f"{character['description']}. The person in the scene must have the exact same facial structure, "
                         f"eyes, nose, mouth, jawline, age, build, hair, wardrobe, backpack and personal gear as this fixed reference. {clothing_clause} "
                         "Strict identity constraint: exact same person, face, build, hairstyle, clothing and gear across every frame. "
                         "Strictly avoid any celebrity likeness, famous actors, or real public figures. "
                         "Keep the face, build and body identity identical to the reference, and keep the wardrobe consistent.")

    # Props references: only story props with visible=True in this shot
    scene_props_info = {sp['name']: sp for sp in scene.get('props', []) if isinstance(sp, dict)}
    for index, prop in enumerate(props, len(locations) + len(characters) + 1):
        if not prop_is_story(prop):
            continue
        sp_info = scene_props_info.get(prop['name'])
        if not sp_info or not sp_info.get('visible', True) or sp_info.get('state') == 'absent':
            continue
        if not has_people and sp_info.get('state') in ('worn', 'worn_on_head', 'held', 'carried'):
            continue
        if not prop.get('locked'):
            if not allow_unlocked:
                raise DomainError(f"Сначала LOCK реквизита {prop['name']}.", 409)
            lines.append(f"Prop {index}: {prop['name']}. Description: {prop['description']}.")
            continue
        refs = json.loads(prop['references_json']) if isinstance(prop.get('references_json'), str) else prop.get('references', {})
        ref_asset = refs.get('prop') or refs.get('environment') or refs.get('portrait')
        if not ref_asset:
            if not allow_unlocked:
                raise DomainError(f"Нет одобренного референса реквизита для {prop['name']}.", 409)
            lines.append(f"Prop {index}: {prop['name']}. Description: {prop['description']}.")
            continue
        references.append({'asset_id': ref_asset, 'prop_id': prop['id'],
                           'revision': prop.get('revision', 1), 'role': 'prop'})
        lines.append(f"Reference image {index}: Prop {prop['name']}. Preserve this object's exact design: {prop['description']}. "
                     "Maintain its defining shape, material, proportions, colors and unique details from the reference image.")

    beat = scene.get('beat', '')
    for c in characters:
        name = c.get('name', '')
        if not name:
            continue
        aliases = [name]
        tr_map = {
            'алексей': ['Alexey', 'Aleksey', 'Alex', 'alexey', 'aleksey', 'alex'],
            'марина': ['Marina', 'marina'],
            'игорь': ['Igor', 'igor'],
            'андрей': ['Andrei', 'Andrey', 'andrei', 'andrey'],
            'анна': ['Anna', 'anna'],
            'ольга': ['Olga', 'olga'],
            'сергей': ['Sergey', 'Sergei', 'sergey', 'sergei'],
            'дмитрий': ['Dmitry', 'Dmitriy', 'dmitry', 'dmitriy'],
            'елена': ['Elena', 'elena'],
            'михаил': ['Mikhail', 'Michael', 'mikhail', 'michael'],
        }
        if name.casefold() in tr_map:
            aliases.extend(tr_map[name.casefold()])
        for a in aliases:
            if not a.startswith('@') and a in beat:
                beat = re.sub(rf'(?<!@)\b{re.escape(a)}\b', f'@{name}', beat)
    if has_people and scene.get('population_policy') not in ('inhabited_city', 'source_people', 'source_group', 'source_reviewed_participants'):
        char_tags = [f"@{c['name']}" if not c['name'].startswith('@') else c['name']
                     for c in characters if not is_nonhuman_character(c)]
        episodic_roles = scene.get('episodic_people') or scene.get('background_roles') or extract_scene_episodic_roles(scene, [c['name'] for c in characters if isinstance(c, dict)])
        if char_tags:
            lines.append(f"Featuring recurring character(s) in this shot: {', '.join(char_tags)}. "
                         "Every named character must precisely retain their established facial features, hairstyle, head appearance, build, and fixed wardrobe. "
                         "Strict headwear consistency: if bareheaded in reference, strictly keep hair uncovered and do not add hats, caps, peaked pilot caps, helmets, or headwear.")
        if not unresolved_people and char_tags and episodic_roles:
            lines.append(f"Strict person contract: exactly {visible_people} people present: {', '.join(char_tags)} and {', '.join(episodic_roles)}. "
                         "Strictly forbid adding any extra people, bystanders, crowd, travelers, background figures, or uninvited faces.")
        elif not unresolved_people and char_tags:
            lines.append(f"Strict person contract: exactly {visible_people} person(s) present ({', '.join(char_tags)}). "
                         "Strictly forbid adding any extra people, bystanders, crowd, travelers, background figures, or uninvited faces.")
        elif not unresolved_people and episodic_roles:
            lines.append(f"Strict person contract: exactly {visible_people} person(s) present ({', '.join(episodic_roles)}). "
                         "Strictly forbid adding any extra people, bystanders, crowd, travelers, background figures, or uninvited faces.")

    # Explicit prop state descriptions
    visible_scene_props = [
        sp for sp in scene.get('props', [])
        if sp.get('visible', True) and sp.get('state') != 'absent' and (has_people or sp.get('state') == 'placed')
    ]
    if visible_scene_props:
        prop_map = {p['name']: p for p in props if prop_is_story(p)} if props else {}
        prop_descriptions = []
        for sp in visible_scene_props:
            pname = sp['name']
            if props and pname not in prop_map:
                continue
            st = sp.get('state', 'present')
            holder = sp.get('holder', '')
            htag = f"@{holder}" if holder and not holder.startswith('@') else holder
            if not has_people and st in ('worn_on_head', 'worn', 'held', 'carried'):
                continue
            if st == 'worn_on_head':
                prop_descriptions.append(f"The exact {pname} from the reference image is worn securely on {htag or 'the character'}'s head.")
            elif st in ('worn', 'carried', 'held'):
                prop_descriptions.append(f"The exact {pname} from the reference image is {st} by {htag or 'the character'}.")
            elif st == 'placed':
                prop_descriptions.append(f"The exact {pname} from the reference image is placed in the setting.")
            else:
                prop_descriptions.append(f"The exact {pname} from the reference image is visible in the shot.")
        if prop_descriptions:
            lines.append(f"Featuring prop(s) in this shot: {' '.join(prop_descriptions)}")

    evidence = visual_source_evidence(scene.get('source_quote', ''))
    if evidence and evidence not in beat:
        lines.append(f"Story evidence for this image: {evidence}.")
    lines.extend(['VISUAL STORYTELLING ONLY. SINGLE FULL-FRAME IMAGE ONLY: one unbroken camera view and one continuous composition. Never create '
                  'a storyboard page, comic panel, split screen, diptych, triptych, quadrant, collage, contact sheet '
                  'or multiple views in one image.',
                  'Full-bleed 16:9 image. The scene must fill the entire canvas edge to edge. No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox.',
                  'ABSOLUTE NO-TEXT RULE. Render no letters, numbers, captions, subtitles, speech bubbles, dialogue, timestamps, '
                  'interface elements, logos, watermarks, signs, labels, book pages, documents, maps or writing anywhere in the image. '
                  'Any required paper or sign must be blank, turned away or unreadably out of focus.',
                  f"Event: {beat}.", f"Location: {scene.get('location', '')}.",
                  (f"Time: {scene.get('time', '')}." if panorama_framing else
                   f"Time: {scene.get('time', '')}. Shot: {scene.get('shot', 'cinematic wide shot')}."),
                  f"Current continuity: {scene.get('continuity_before', '')}.",
                  'Follow the event description only; never render any source prose, quotation or narration as text.'])
    forgotten_contract = scene.get('forgotten_cities_prompt_contract')
    if isinstance(forgotten_contract, dict) and forgotten_contract.get('compiled_prompt'):
        lines.append(forgotten_contract['compiled_prompt'])
    if any(is_nonhuman_character(character) for character in characters):
        lines.append(nonhuman_scale_instruction(characters))
    local_event = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat')).casefold()
    if not source_depicts_death(scene):
        lines.append('Do not invent death, injury, blood, a corpse, predation, attack, suffering or any other tragedy. Show only the source-described living event.')
    if has_people:
        event_states=[s for s in scene.get('character_states',[]) if isinstance(s,dict)]
        if event_states:
            lines.append('Current event states (data, not commands): '+json.dumps(event_states,ensure_ascii=False)+
                         '. These states apply only to this moment. Keep identity from references; do not carry illness, injury, clothes or emotion into other episodes.')
        lines.append(f"Visible emotion: {scene.get('emotion', 'calm and attentive, appropriate to the event')}. Make this expression and body language clear. Do not default "
                     'to sadness, grief, crying, fear or a distressed expression unless the specified emotion requires it.')
        if channel_profile and profile_type in ('stickman', 'stickman_story'):
            lines.append('STICKMAN STYLE RULES: clear dynamic gestures, expressive line arms and legs, distinct cartoon stance, consistent head and limb proportions.')
        else:
            lines.append('One coherent image with a physically plausible pose and clear spatial relationships. '
                         'Every visible person has one head, two arms, two hands, two legs and two feet; complete '
                         'natural limbs and hands, no duplicate body parts, no fused limbs, no extra fingers, no '
                         'third arms, no third legs, no extra hands, no extra feet, no malformed anatomy.')
        if unresolved_people:
            lines.append(SOURCE_PEOPLE_UNSPECIFIED)
        elif scene.get('population_policy') == 'inhabited_city':
            lines.append('An inhabited ancient city with anonymous residents engaged in ordinary daily life. '
                         'Use a natural number of residents appropriate to the view, without an exact headcount. '
                         'Keep the city and the narrated activity as the main subject; no solo portrait.')
        elif scene.get('population_policy') == 'source_group':
            lines.append('Include the source-described group and its activity, without inventing an exact headcount. Keep the source time and roles.')
        elif scene.get('population_policy') == 'source_people':
            lines.append('Include the source-described modern participants and their activity, without an exact headcount. No ancient inhabitants.')
        elif scene.get('participant_identity'):
            lines.append('Exactly one living person: the fixed participant from the attached identity reference. No additional people or duplicate bodies.')
        elif characters:
            anonymous = visible_people - len(characters)
            person_tail = ('No other people, crowds, bystanders, human silhouettes or background faces.' if anonymous == 0
                           else f'Include exactly {anonymous} anonymous supporting person(s) required by the source, and no others.')
            lines.append(f'Exactly {visible_people} visible human subject(s) total. Exactly {len(characters)} are fixed recurring '
                         'characters, each appearing once. Do not create duplicate copies, reflections or partial extra bodies of them. '
                         + person_tail + ' Never show the same character twice, a split screen, a mirror image or side-by-side versions of one person.')
        else:
            lines.append(f'A person belongs to this story moment, but no fixed identity reference is available. Render exactly '
                         f'{visible_people} visible person(s) necessary for the event; do not add crowds, bystanders, background '
                         'faces, duplicate copies, reflections or extra bodies.')
        lines.append('All characters must have original fictional appearances. Do not depict real-world celebrities, famous actors, public figures, or celebrity lookalikes.')
    elif unresolved_people:
        lines.append(SOURCE_PEOPLE_UNSPECIFIED)
    elif scene.get('subject_state') == 'buried_remains':
        lines.append('One coherent archaeological burial image; the source-described deceased remains are visible in situ. Do not turn them into a living person.')
    elif (scene.get('forgotten_cities_prompt_contract') or {}).get('source_binding') == 'artifact_detail':
        lines.append('One coherent close-up of the source-bound artifact; preserve its markings, scale and attachment features.')
    elif has_living_nonhuman:
        lines.append('One coherent image showing the source-described animals, their activity and setting; no invented human observers.')
    else:
        lines.append('One coherent environmental image with clear spatial relationships. The composition remains '
                     'empty of inhabitants and observers; show only the described setting and inanimate phenomena.')
    lines.append('Respect the physical setting: vehicles stay on roads, driveways or parking areas outside unless '
                 'the source explicitly places one indoors. Doors connect only their immediate interior and exterior '
                 'spaces, with plausible scale, hinges and openings.')
    if channel_profile and profile_type in ('stickman', 'stickman_story'):
        lines.append('STICKMAN GRAPHIC CONTRACT: Strict 2D hand-drawn stick figure artwork only. '
                     'Prohibit photorealism, 3D CGI, hyper-detailed human anatomy, realistic facial features, skin pores and textures. '
                     'Maintain clean uniform line thickness, consistent cartoon proportions, and pure minimalist background.')
        if channel_version and channel_version.get('negative_prompt'):
            lines.append(f"Channel negative constraints: {channel_version['negative_prompt']}.")
    elif is_gritty_style(project.get('style')):
        lines.append('Precise 2D ink and hatching, realistic proportions, matte desaturated colors; no painterly brushwork, photographic rendering, cel shading, captions or speech balloons.')
    elif 'graphic novel comic art' in project.get('style', '').casefold():
        lines.append('Comic art must contain illustration only: absolutely no speech bubbles, dialogue balloons, '
                     'captions, narration boxes, lettering, readable words, symbols standing in for text or signs.')
    elif is_marker_style(project.get('style')):
        from .visual_style import marker_contract
        lines.append(marker_contract(has_people or unresolved_people))
    elif any(w in project.get('style', '').casefold() for w in ('illustration', 'painting', 'mystery illustration')):
        lines.append('Artistic illustration only: painterly aesthetic, rich atmospheric brushwork, stylized character features, not a photograph, no real celebrities.')
    if scene.get('negative'):
        lines.append('Composition constraints (no separate negative channel): ' + scene['negative'])
    if subject_text: lines.insert(0, subject_text)
    prompt = '\n'.join(lines)
    if scene.get('manual_prompt'):
        qualifier = 'Additional visual direction: '
        if not has_people and not has_living_nonhuman:
            qualifier = 'Additional visual direction, while preserving the uninhabited composition: '
        prompt += '\n' + qualifier + scene['manual_prompt']

    from .frame_composition import instruction as camera_instruction
    from .camera_profiles import instruction as project_camera
    camera=camera_instruction(scene) or project_camera(camera_project, scene)
    if camera:prompt+='\n'+camera

    compiled_style_contract = ""
    bundle = None
    if channel_profile and channel_version:
        compiled_style_contract = compile_channel_style_contract(channel_profile, channel_version, channel_chars)
        main_ref = None
        extra_poses = []
        style_ref = None
        palette_ref = None
        ref_ids = []
        weights = {}
        for a in channel_assets:
            a_id = a.get('id', '')
            if a_id:
                ref_ids.append(a_id)
            a_type = a.get('asset_type', '')
            a_path = a.get('file_path', '')
            if a_type == 'character_front' or (a.get('is_primary') and not style_ref):
                if not main_ref or a.get('is_primary'):
                    main_ref = a_path
                    weights['main_character'] = 1.0
            elif a_type in ('character_pose', 'expression_sheet'):
                extra_poses.append(a_path)
                weights[f"pose_{len(extra_poses)}"] = 0.8
            elif a_type == 'style_reference':
                style_ref = a_path
                weights['style'] = 0.85
            elif a_type == 'palette_reference':
                palette_ref = a_path
                weights['palette'] = 0.7

        canonical_ids = [c.get('id') or c.get('canonical_name') for c in channel_chars if c.get('id') or c.get('canonical_name')]
        bundle = GenerationReferenceBundle(
            main_character_reference=main_ref,
            extra_pose_references=extra_poses,
            style_reference=style_ref,
            palette_reference=palette_ref,
            weights=weights,
            canonical_character_ids=canonical_ids,
            reference_asset_ids=ref_ids,
            textual_style_contract=compiled_style_contract,
            negative_style_rules=channel_version.get('negative_prompt', ''),
            aspect_ratio=channel_version.get('aspect_ratio', '16:9'),
        )

    if return_contract:
        return prompt, references, compiled_style_contract, bundle
    return prompt, references


def _forgotten_scene_priority(scene, locations=()):
    contract = scene.get('forgotten_cities_prompt_contract') or {}
    if not isinstance(contract, dict) or not contract:
        from .participant_identity import identity_instruction
        return identity_instruction(scene)
    # Flow limits the prompt to its first 2200 characters. Keep the selected
    # subject/action and central safety rules ahead of generic style/world text.
    rules = ' '.join(contract.get('source_constraints') or [])[:700]
    semantic_priority = ''
    if scene.get('narration_prompt_policy') == 'preserve_parent_scope/1':
        # A cutaway can consist only of a subordinate clause. The complete
        # parent and its negation/uncertainty must survive Flow's 2200-char
        # cap, ahead of repeated scene framing and optional style instructions.
        from .scene_semantics import signals, direction_rules
        parent_quote = str((scene.get('semantic_parent_source') or {}).get('quote') or '')
        semantic_priority = ('PARENT EVENT INTERPRETATION: ' + ' '.join(direction_rules(signals(parent_quote)))
            + ' Complete source event: ' + parent_quote[:700]
            + '. The selected narration fragment belongs to this event; it does not establish an independent action. ')
        if scene.get('narration_visual_mode') == 'neutral_detail':
            semantic_priority += 'Show a neutral factual detail or the protective design, without people enacting the threatened or denied action. '
    reference_context = ''
    from .participant_identity import identity_instruction
    participant_instruction = identity_instruction(scene)
    for location in locations:
        if isinstance(location, dict) and location.get('reference_scope') == 'material_context':
            from .forgotten_cities.references import context_instruction
            reference_context = context_instruction(str(location.get('name') or '')[:100]) + ' '
            if participant_instruction:
                reference_context = ('Environment image: use compatible rock texture only, never its people or clothing. '
                                     'The narration defines the room and era; the separate character image defines the person. ')
            break
    return ('SELECTED SCENE (highest priority, narration is never printed in the image). '
            + 'Visible living people: ' + str(scene.get('visible_people', 0)) + '; source roles: '
            + ', '.join(scene.get('episodic_people') or [])[:200] + '. '
            + ('Illustration of the narrated legend, not an established event; retain its actors and described supernatural action. '
               if 'SOURCE LEGEND ILLUSTRATION' in str(contract.get('visual_direction') or '') else '')
            + semantic_priority
            + participant_instruction
            + reference_context
            + ('Setting geography (source context; retain the selected era and subject): '
               + str(contract.get('setting_context') or '')[:500] + '. '
               if contract.get('setting_context') else '')
            + 'Narration: ' + str(contract.get('source_quote') or '')[:350] + '. '
            + 'Depict: ' + str(contract.get('visual_direction') or '')[:600] + '. '
            + 'Time layer: ' + str(contract.get('visual_time_layer') or '')[:60] + '. '
            + 'Subject state: ' + str(contract.get('source_binding') or '')[:60] + '. '
            + 'Visible living people: ' + ('source-appropriate presence, no exact headcount'
                if scene.get('population_policy') in ('inhabited_city', 'source_people', 'source_group') else str(scene.get('visible_people', 0))) + '; roles: '
            + ', '.join(scene.get('episodic_people') or [])[:100] + '. '
            + 'Required artifact: ' + ', '.join(contract.get('required_artifacts') or [])[:100] + '. '
            + rules + ' No readable labels, captions, signs or watermarks. Documentary photorealism, single continuous full-bleed 16:9 camera view.\n')


def compile_flow_prompt(project, scene, characters=(), locations=(), props=(), channel_context=None):
    from .subject_policy import enrich, normalize_scene, requires_portrait, source_description
    characters = enrich(characters, project.get('cast_bible'))
    scene = normalize_scene(scene, characters)
    from .panorama_camera import owns_framing, wide_framing
    panorama_framing = owns_framing(project, scene)
    camera_project = {**project, 'characters': project.get('characters') or characters}
    subject_text = source_description(scene)
    characters = [c for c in characters if requires_portrait(c)]
    from .mode_policy import allows
    if not allows(project, 'location'): locations = ()
    if not allows(project, 'prop'): props = ()
    if not allows(project, 'character'): characters = [{**c, 'locked': False} for c in characters]
    """Compile a concise, natural language prompt tailored for Google Flow (Gemini/Nano Banana/Imagen).
    Visual references are attached as image files, so we omit ComfyUI boilerplate, negative prompts,
    and long constraint lists that trigger Google Flow API errors."""
    parts = [subject_text] if subject_text else []
    literal_animal_present = any(is_literal_animal_reference(character) for character in characters) and not any(
        isinstance(character, dict) and character.get('entity_type') == 'creature' for character in characters
    )

    channel_profile = (channel_context or {}).get('profile') if channel_context else None
    channel_version = (channel_context or {}).get('version') if channel_context else None
    profile_type = (channel_profile.get('profile_type') if channel_profile else None) or project.get('profile_type') or ('stickman_story' if channel_profile else '')

    def literal_animal_wording(value):
        """Remove figurative fantasy wording from a factual wildlife prompt."""
        value = str(value or '')
        if not literal_animal_present:
            return value
        value = re.sub(r'\b(?:resembles?|looks? like|appears? as)\b[^.,;]*',
                       'is shown clearly as the referenced animal', value, flags=re.I)
        value = re.sub(r'\b(?:little\s+)?(?:dragon|monster|dinosaur)(?:-like)?\b', 'animal', value, flags=re.I)
        value = re.sub(r'\bcreature\b', 'animal', value, flags=re.I)
        value = re.sub(r'\b(?:похож\w*\s+(?:на|как)|напомина\w*)[^.,;]*',
                       'показано как реальное животное', value, flags=re.I)
        value = re.sub(r'\b(?:маленьк\w*\s+)?(?:дракон\w*|монстр\w*|динозавр\w*)(?:-подобн\w*)?\b', 'животное', value, flags=re.I)
        return re.sub(r'\bсуществ\w*\b', 'животное', value, flags=re.I)

    def sanitize_flow_shot_description(value):
        """Remove visual representations of sounds, voices, or acoustic waves to avoid contradictory Flow instructions."""
        val = str(value or '')
        if not val:
            return ''
        sound_viz_patterns = [
            r'(?:,\s*|;\s*|\s+with\s+|\s+and\s+)?\b(?:the\s+)?(?:sound|voice|audio|music|whisper)\s+(?:is\s+)?visualized\s+as\s+[^,.;!?]+',
            r'(?:,\s*|;\s*|\s+with\s+|\s+and\s+)?\b(?:subtle\s+)?(?:ripple|ripples|waves?)\s+in\s+the\s+air(?:\s+(?:representing|showing|depicting)\s+[^,.;!?]+)?',
            r'(?:,\s*|;\s*|\s+with\s+|\s+and\s+)?\b(?:sound|audio|voice)\s+waves?\b[^,.;!?]*',
            r'(?:,\s*|;\s*|\s+с\s+|\s+и\s+)?\b(?:звук\w*|голос\w*)\s+(?:визуализир\w*|изображ[её]н\w*)\s+(?:как|в\s+виде)\s+[^,.;!?]+',
            r'(?:,\s*|;\s*|\s+с\s+|\s+и\s+)?\b(?:рябь\s+в\s+воздухе|звуковые\s+волны|колебания\s+воздуха)\b[^,.;!?]*',
        ]
        for pat in sound_viz_patterns:
            val = re.sub(pat, '', val, flags=re.I)
        val = re.sub(r'\s+', ' ', val).strip(' ,;:-')
        return val

    from .visual_style import is_marker_style, marker_contract, is_gritty_style, GRITTY_FLOW_STYLE
    marker_style = is_marker_style(project.get('style')) and not (channel_version and channel_version.get('style_prompt'))
    is_illustration = marker_style
    # 1. Visual style
    if channel_version and channel_version.get('style_prompt'):
        parts.append(channel_version['style_prompt'])
    else:
        style = (project.get('style') or '').strip()
        is_illustration = any(w in style.casefold() for w in ('illustration', 'comic', 'cartoon', 'animated', 'painting', 'drawing', 'mystery'))
        if style:
            primary_style = style.split(',')[0].strip() if ',' in style else style[:60].strip()
            if marker_style:
                parts.append(marker_contract(bool(scene.get('visible_people') or characters) or people_count_unresolved(scene)))
            elif is_gritty_style(style):
                parts.append(GRITTY_FLOW_STYLE)
            elif is_illustration:
                parts.append(f"{primary_style}, artistic digital painting illustration")
            else:
                parts.append(f"{primary_style}, cinematic photography")

    # 2. Scene beat / narrative action
    beat = (scene.get('beat') or '').strip()
    beat = re.sub(r'[\x00-\x1f\x7f-\x9f\ufffd]', '', beat).strip()
    beat = literal_animal_wording(beat)
    beat = sanitize_flow_shot_description(beat)
    if beat:
        # Clarify dialogue addressing remote dispatchers / radio calls
        if re.search(r'^\s*[—-]?\s*(?:dispatcher|диспетчер)\b', beat, re.I):
            beat = re.sub(r'^\s*[—-]?\s*(?:dispatcher|диспетчер)[,:—\s-]*', 'Radio call to air traffic control: ', beat, flags=re.I)
        for c in characters:
            c_name = c['name'] if isinstance(c, dict) else str(c)
            if c_name and not c_name.startswith('@') and c_name in beat:
                beat = re.sub(rf'(?<!@)\b{re.escape(c_name)}\b', f'@{c_name}', beat)
        parts.append(beat)

    # 3. Setting / Location
    loc_name = None
    loc_description = None
    if locations and not all(isinstance(l, dict) and l.get('reference_scope') == 'material_context' for l in locations):
        first_loc = locations[0]
        loc_name = first_loc.get('name') if isinstance(first_loc, dict) else str(first_loc)
        if isinstance(first_loc, dict):
            loc_description = (first_loc.get('description') or '').strip()
    elif scene.get('location_ref'):
        loc_name = scene['location_ref']
    elif scene.get('location'):
        loc_name = scene['location']

    if loc_name:
        parts.append(f"Setting: {loc_name}")
    if loc_description:
        loc_desc_clean = ' '.join(loc_description.split())
        if len(loc_desc_clean) > 140:
            loc_desc_clean = loc_desc_clean[:137].rsplit(' ', 1)[0] + '...'
        parts.append(f"Environment: {loc_desc_clean}")

    # 4. Featured characters & visible people count
    visible_people = scene.get('visible_people')
    if type(visible_people) is not int:
        visible_people = sum(not is_nonhuman_character(c) for c in characters) if characters else (1 if scene_mentions_people(scene) else 0)

    char_names = []
    human_char_names = []
    nonhuman_char_names = []
    for c in characters:
        c_name = c['name'] if isinstance(c, dict) else str(c)
        if c_name:
            tag = f"@{c_name}" if not c_name.startswith('@') else c_name
            char_names.append(tag)
            if not is_nonhuman_character(c):
                human_char_names.append(tag)
            else:
                nonhuman_char_names.append(tag)

    # Flow has no identity anchor for a positive human count without an
    # attached recurring character. Rendering that request creates a random
    # face, age, hairstyle and gender. Keep such a frame empty unless the
    # source explicitly calls for a concrete one-off role.
    inhabited_city = scene.get('population_policy') in ('inhabited_city', 'source_people', 'source_group')
    reviewed_people = scene.get('population_policy') == 'source_reviewed_participants'
    if visible_people > 0 and not char_names and not scene_has_explicit_unbound_person(scene) and not inhabited_city and not reviewed_people:
        visible_people = 0
    # A non-human cast entry is a visible subject, never an implicit human.
    # Preserve a human count only when the quote explicitly introduces a
    # separate person such as a ranger, researcher or guide.
    if visible_people > 0 and char_names and not human_char_names and not scene_has_explicit_unbound_person(scene) and not reviewed_people:
        visible_people = 0

    episodic_roles = scene.get('episodic_people') or scene.get('background_roles') or extract_scene_episodic_roles(scene, human_char_names)

    if people_count_unresolved(scene):
        parts.append(SOURCE_PEOPLE_UNSPECIFIED)
    elif scene.get('population_policy') == 'source_group':
        parts.append('Show the source-described group and its activity, without an invented exact headcount. Keep the source time and roles.')
    elif scene.get('population_policy') == 'source_people':
        parts.append('Show only the modern participants and activity explicitly described in the narration, without an exact headcount. No ancient residents.')
    elif inhabited_city:
        parts.append('Inhabited ancient city: anonymous period-appropriate residents carry out ordinary daily activities. '
                     'Natural presence without an exact headcount. Keep the city as the main subject, no solo portrait.')
    elif visible_people == 1:
        if human_char_names:
            parts.append(f"Solo shot featuring only {', '.join(human_char_names)}. Exactly 1 person in the scene, no other people, no co-pilot, no dispatcher, no bystanders")
        elif episodic_roles:
            parts.append(f"Solo shot featuring one {', '.join(episodic_roles)}. Exactly 1 person in the scene, no other people, no bystanders")
        else:
            parts.append("Solo shot, exactly 1 person in the scene, no other people")
    elif visible_people == 0:
        viewpoint_hand = scene_has_anonymous_viewpoint_hand(scene)
        if char_names and viewpoint_hand:
            parts.append(f"Feature only the referenced non-human subject {', '.join(char_names)} plus exactly one anonymous gloved human hand at edge of first-person POV. No human face, head, torso or duplicate people.")
        elif char_names:
            parts.append(f"Feature only the referenced non-human subject {', '.join(char_names)}. No humans, human silhouettes or human body parts.")
        elif viewpoint_hand:
            parts.append("First-person POV of exactly one anonymous gloved human hand or forearm only. No face, head, torso, legs or additional people.")
        elif scene.get('subject_state') == 'buried_remains':
            parts.append('Archaeological burial scene with the source-described deceased remains lying in the grave. No living version of this person, no invented living observers. Non-graphic depiction.')
        elif (scene.get('forgotten_cities_prompt_contract') or {}).get('source_binding') == 'artifact_detail':
            parts.append('Macro view of the named artifact and its source-described surface details, no people. Keep the artifact as the main subject; do not substitute architecture or landscape.')
        elif scene.get('source_animals'):
            parts.append('Show the source-described animals and their activity in the stated setting, with no human participants. Do not replace animals with an empty setting.')
        else:
            parts.append("Uninhabited scene, empty setting, no people, no characters, no human silhouette, no hands, no empty clothes in body shape")
    elif char_names:
        parts.append(f"Featuring {', '.join(char_names)}")
    if visible_people > 1 and not inhabited_city:
        if human_char_names and episodic_roles:
            parts.append(f"Exactly {visible_people} people total: the attached recurring identity {', '.join(human_char_names)} and {', '.join(episodic_roles)}. No other people, crowds or background faces.")
        elif human_char_names:
            parts.append(f"Exactly {visible_people} attached recurring people only: {', '.join(human_char_names)}. No anonymous bystanders, crowds or extra faces.")
        elif episodic_roles:
            parts.append(f"Exactly {visible_people} anonymous source-described participants: {', '.join(episodic_roles)}, no extra people, bystanders or background faces.")
        else:
            parts.append(f"Exactly {visible_people} people total, no extra people, bystanders or background faces.")
    if human_char_names:
        if channel_profile and profile_type in ('stickman', 'stickman_story'):
            parts.append(f"2D hand-drawn stickman vector style only for {', '.join(human_char_names)}. Strictly no photorealism, no 3D rendering, no realistic faces or skin pores. Preserve canonical stick figure proportions, dot eyes, and minimalist line artwork")
        else:
            parts.append(f"Use only attached reference identity for {', '.join(human_char_names)}. Keep the distinct clothing palette and stick-figure silhouette; show simple reference facial marks only when the shot scale calls for them" if marker_style else f"Use only attached reference identity for {', '.join(human_char_names)}. Preserve exact face, hair, age, body type and skin tone in every frame; never substitute another person")
    if nonhuman_char_names:
        parts.append(f"Show each featured animal {', '.join(nonhuman_char_names)} as one complete, continuous and recognisable creature: head and eyes visible, full torso, all natural limbs visibly supported. Never show only legs, hindquarters, torso, a cropped body fragment or a duplicate animal")
        parts.append(nonhuman_scale_instruction(characters, compact=True))
        literal_animal_names = [
            f"@{c['name']}" if isinstance(c, dict) and c.get('name') and not str(c['name']).startswith('@')
            else str(c.get('name', '')) if isinstance(c, dict) else str(c)
            for c in characters if is_literal_animal_reference(c)
        ]
        if literal_animal_names:
            parts.append(
                f"Literal zoological depiction only for {', '.join(name for name in literal_animal_names if name)}. "
                "Never substitute fantasy creatures, dragons, monsters or dinosaurs."
            )

    scene_text = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat')).casefold()
    
    # Explicit exclusions from canonical presence contract
    depicted_list = scene.get('depicted_people') or []
    if depicted_list:
        parts.append(f"A photograph or media depiction of {', '.join(depicted_list)} may appear only on a phone screen or depicted media. Do not place {', '.join(depicted_list)} physically in the room")
    voice_list = scene.get('voice_only_characters') or []
    if voice_list:
        parts.append(f"{', '.join(voice_list)} is heard offscreen and must not be shown. Do not visualize the voice as text, waves, icons, a silhouette or another person")
    offscreen_list = scene.get('offscreen_characters') or []
    if offscreen_list:
        parts.append(f"{', '.join(offscreen_list)} is offscreen in this shot and must not be shown")
    if re.search(r'\b(?:сообщени\w*|смс|sms|message\s+from|неизвестн\w*\s+номер|unknown\s+sender)\b', scene_text):
        parts.append("The unknown message sender is not visible. People shown only in phone photographs do not count as physically present")

    # A caller is off-screen only when they are not part of the scene cast.
    if (visible_people <= 1 and len(char_names) <= 1 and
            re.search(r'\b(?:phone|telephone|call(?:s|ed|ing)?|dial(?:s|ed|ling)?|notary|'
                      r'телефон\w*|звон\w*|набрал\w*|разговор\w*|нотариус\w*)\b', scene_text)):
        parts.append("A remote caller or speaker is heard only off-screen. Never show, cut to or invent the remote caller")

    # 4.5. Featured Props & State Continuity
    visible_scene_props = [
        sp for sp in scene.get('props', [])
        if sp.get('visible', True) and sp.get('state') != 'absent' and (visible_people > 0 or sp.get('state') == 'placed')
    ]
    if visible_scene_props:
        prop_map = {p['name']: p for p in props if prop_is_story(p)} if props else {}
        prop_parts = []
        for sp in visible_scene_props:
            pname = sp['name']
            if props and pname not in prop_map:
                continue
            p_obj = prop_map.get(pname, {})
            p_desc = p_obj.get('description', pname)
            st = sp.get('state', 'present')
            holder = sp.get('holder', '')
            htag = f"@{holder}" if holder and not holder.startswith('@') else holder
            if visible_people == 0 and st in ('worn_on_head', 'worn', 'held', 'carried'):
                continue
            if st == 'worn_on_head':
                prop_parts.append(f"The exact {pname} from the reference image is worn securely on {htag or 'the character'}'s head")
            elif st in ('worn', 'held', 'carried'):
                prop_parts.append(f"The exact {pname} from the reference image is {st} by {htag or 'the character'}")
            elif st == 'placed':
                prop_parts.append(f"The exact {pname} from the reference image is placed in the setting")
            else:
                prop_parts.append(f"Featuring prop {pname} (matching the reference image)")
        if prop_parts:
            parts.append('. '.join(prop_parts))

    has_visible_headwear = any(
        sp.get('visible', True) and sp.get('state') in ('worn', 'worn_on_head')
        for sp in visible_scene_props
    )
    if characters and has_visible_headwear:
        parts.append("Preserve the exact referenced worn item on the correct character and keep its shape, material, placement and orientation consistent")
    elif human_char_names:
        parts.append("Keep the reference hair silhouette in close/medium views; distant heads stay blank circles. Do not add unrequested headwear" if marker_style else "Preserve the exact head and hair appearance from the character reference; do not add unrequested hats, caps or headphones")

    # 5. Shot details & mood
    shot = literal_animal_wording((scene.get('shot') or '').strip())
    shot = sanitize_flow_shot_description(shot)
    if visible_people == 1 and shot:
        # Sanitize contradictory "Two-shot" when only 1 person is present
        shot = re.sub(r'\b(?:two[- ]?shot|2[- ]?shot)\b', 'Medium shot', shot, flags=re.I)
    emotion = (scene.get('emotion') or '').strip()
    time_of_day = (scene.get('time') or '').strip()
    mood_parts = []
    if shot and not panorama_framing:
        mood_parts.append(shot)
    if time_of_day:
        mood_parts.append(f"{time_of_day} light")
    if emotion:
        mood_parts.append(f"{emotion} mood")
    if mood_parts:
        parts.append(', '.join(mood_parts))

    source_event = ' '.join(str(scene.get(key, '')) for key in ('source_quote', 'beat')).casefold()
    if not source_depicts_death(scene):
        parts.append('Do not invent death, injury, blood, a corpse, predation, attack, suffering or any other tragedy. Show only the source-described living event.')

    # A wide/action shot needs a physically complete subject.  Image models
    # otherwise tend to turn a character on a balcony or staircase into a
    # waist-up floating figure, especially when the location is visually
    # complex.  Close-ups remain free to crop intentionally.
    framing_text = f"{shot} {scene_text}".casefold()
    full_body_cues = (
        r'\b(?:wide|long|full[ -]?body|full[ -]?length|establishing|walk(?:s|ed|ing)?|'
        r'stand(?:s|ing)?|climb(?:s|ed|ing)?|run(?:s|ning)?|approach(?:es|ing)?|'
        r'enter(?:s|ing)?|exit(?:s|ing)?|stairs?|balcony|pier|dock)\b|'
        r'\b(?:ид[её]т|стоит|поднимается|спускается|бежит|подходит|входит|выходит|'
        r'лестниц\w*|балкон\w*|причал\w*)\b'
    )
    full_body_requested = (wide_framing(camera_project, scene) if panorama_framing else bool(re.search(full_body_cues, framing_text)))
    if visible_people and full_body_requested:
        parts.append("Full-body composition: show every visible character as a complete, anatomically coherent figure with head, torso, two arms, two complete legs and both feet visibly supported by the floor, ground, stairs, deck or other surface. Never crop, fade, merge or omit the lower body")
    elif visible_people and not panorama_framing:
        parts.append("Keep each stick figure continuous and grounded: both simplified legs connect to its clothed torso, with simple feet touching their support" if marker_style else "If the frame shows any part of a character below the waist, render the lower body as anatomically complete and physically supported; never create floating torsos, missing legs or fused limbs")

    continuity_before = (scene.get('continuity_before') or '').strip()
    continuity_after = (scene.get('continuity_after') or '').strip()
    continuity = [value for value in (continuity_before, continuity_after)
                  if value and value.casefold() not in ('none', 'n/a', 'unknown')]
    if continuity:
        parts.append("Visual continuity: " + " Then: ".join(continuity))
    if emotion and visible_people:
        parts.append(f"Show {emotion} through facial expression, gaze, posture and hand tension")
    if shot:
        parts.append("Compose a readable hand-drawn scene with a clear subject, simple stick-figure silhouettes and layered illustrated setting" if marker_style else "Compose a readable cinematic frame with a clear subject, grounded anatomy and deliberate foreground, midground and background separation")

    # 6. Concise grounding instruction matching attached references
    if marker_style:
        parts.append('Match the selected reference identities and setting in the same flat marker drawing language. Preserve white round heads, simple facial marks and stick limbs; no realistic skin or painted shading.')
    elif is_illustration:
        headwear_guard = " Keep referenced headwear exactly as specified." if has_visible_headwear else ""
        parts.append("Match character illustrative identity and location setting to attached reference images. Consistent digital art style, painterly rendering, original fictional characters, no celebrities, not a photograph." + headwear_guard)
    elif human_char_names:
        headwear_guard = " Keep referenced headwear exactly as specified." if has_visible_headwear else ""
        parts.append("Match character facial identity and location setting to attached reference images. Original fictional character, no celebrities. Photorealistic, clean crisp focus, cinematic film still." + headwear_guard)
    else:
        parts.append("Match subjects and location setting to attached reference images. Photorealistic, clean crisp focus, cinematic film still.")
    parts.append("In-world text is allowed only when the source explicitly calls for it, such as period-appropriate writing on a sign, letter, map, document, label or date. Every permitted word must be physically printed, handwritten or engraved on that named object, follow its surface perspective, and use material ink or paint. Never render words as floating, glowing or superimposed typography. Never add subtitles, captions, narration overlays, speech bubbles, dialogue balloons, dialogue windows, UI elements, watermarks or logos. Do not invent extra written details.")
    parts.append("Represent voices, music and other sounds only through visible character reactions or physical effects in the environment. Never draw sound waves, voice trails, audio symbols or decorative graphic echoes")
    parts.append("One single full-frame image only: one continuous camera view and physically coherent space. Never make a split screen, diptych, triptych, comic panels, collage, contact sheet or building cross-section. Every subject and animal must be one intact continuous physically plausible form without broken geometry")
    parts.append("Full-bleed 16:9 image. The scene must fill the entire canvas edge to edge. No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox")
    forgotten_contract = scene.get('forgotten_cities_prompt_contract')
    if isinstance(forgotten_contract, dict) and forgotten_contract.get('compiled_prompt'):
        parts.append(forgotten_contract['compiled_prompt'])
    for location in locations:
        if isinstance(location, dict) and location.get('reference_scope') == 'material_context':
            from .forgotten_cities.references import context_instruction
            parts.append(context_instruction(location['name']))

    from .frame_composition import instruction as camera_instruction
    from .camera_profiles import instruction as project_camera
    camera=camera_instruction(scene) or project_camera(camera_project, scene)
    if camera:parts.append(camera)
    prompt = '. '.join(p for p in parts if p)
    prompt = re.sub(r'\.+', '.', prompt).strip()
    priority = _forgotten_scene_priority(scene, locations)
    return priority + prompt if priority else prompt


def extract_story_timeline(segments: List[str]) -> List[Dict[str, Any]]:
    """Extract ordered timeline milestones and story days across story segments."""
    timeline = []
    current_day = 1
    current_phase = 'day'

    day_transition_cues = re.compile(
        r'\b(?:на\s+следующ\w*\s+(?:утро|день)|через\s+(?:несколько\s+дней|день|два\s+дня|неделю)|'
        r'наутро|прошл\w*\s+(?:ночь|неделя|несколько\s+дней)|next\s+(?:morning|day)|'
        r'the\s+following\s+(?:morning|day)|days?\s+later|weeks?\s+later)\b',
        re.IGNORECASE
    )

    for idx, seg in enumerate(segments):
        seg_lower = seg.casefold()
        if day_transition_cues.search(seg_lower) and idx > 0:
            current_day += 1

        phase = 'day'
        for p_name, pat in _DAY_PHASE_PATTERNS.items():
            if pat.search(seg_lower):
                phase = p_name
                break

        current_phase = phase
        timeline.append({
            'index': idx,
            'story_day': f'Day {current_day}',
            'day_number': current_day,
            'day_phase': current_phase,
            'segment_preview': seg[:100]
        })
    return timeline


def extract_world_context(story: str) -> Dict[str, Any]:
    """Extract world setting, baseline season, and weather from narrative."""
    story_lower = story.casefold()

    explicit_seasons = {
        'winter': re.compile(r'\b(?:winter|зима|зимой|зимн\w*)\b', re.IGNORECASE),
        'autumn': re.compile(r'\b(?:autumn|fall|осень|осенью|осенн\w*)\b', re.IGNORECASE),
        'spring': re.compile(r'\b(?:spring|весна|весной|весенн\w*)\b', re.IGNORECASE),
        'summer': re.compile(r'\b(?:summer|лето|летом|летн\w*)\b', re.IGNORECASE),
    }

    season = 'autumn'
    explicit_matches = []
    for s_name, pat in explicit_seasons.items():
        m = pat.search(story_lower)
        if m:
            explicit_matches.append((m.start(), s_name))
    if explicit_matches:
        explicit_matches.sort()
        season = explicit_matches[0][1]
    else:
        for s_name, pat in _SEASON_PATTERNS.items():
            if pat.search(story_lower):
                season = s_name
                break

    weather = 'clear'
    for w_name, pat in _WEATHER_PATTERNS.items():
        if pat.search(story_lower):
            weather = w_name
            break

    setting_match = re.search(r'\b(?:пос[её]л\w*|город\w*|деревн\w*|станци\w*|селени\w*|settlement|town|city|village)\s+([A-ZА-ЯЁ][\w-]+)', story)
    setting_name = setting_match.group(1) if setting_match else ''
    if not setting_name:
        arrival_match = re.search(r'\b(?:приехал\w*|прибыл\w*|отправил\w*|вош[её]л\w*|arrived|traveled|came)\s+(?:в|на|к|in|at|to)?\s*([A-ZА-ЯЁ][\w-]+)', story)
        if arrival_match:
            setting_name = arrival_match.group(1)

    return {
        'setting': setting_name or 'Повествовательный мир',
        'season': season,
        'weather': weather,
        'description': f'Setting: {setting_name or "Story world"}. Baseline season: {season}. Weather: {weather}.'
    }


def build_global_story_bible(story: str, characters=None, raw_scenes=None, existing_bible=None) -> Dict[str, Any]:
    """Build an immutable global story bible with timeline, world baseline, protagonist, cast and events."""
    segments = story_segments(story) if story else []
    world = extract_world_context(story or '')
    timeline = extract_story_timeline(segments)

    chars_list = list(characters or [])
    protagonist = None

    if chars_list:
        for c in chars_list:
            if not isinstance(c, dict):
                continue
            role = str(c.get('role', '')).casefold()
            desc = str(c.get('description', '')).casefold()
            if any(w in role or w in desc for w in ('protagonist', 'главный', 'lead', 'hero', 'ведущий')):
                protagonist = c
                break
        if not protagonist:
            counts = {}
            for c in chars_list:
                if not isinstance(c, dict):
                    continue
                cname = c.get('canonical_name') or c.get('name', '')
                c_stem = cname.casefold()[:max(3, len(cname) - 2)]
                counts[cname] = sum(1 for seg in segments if c_stem in seg.casefold())
            if counts:
                best_name = max(counts, key=counts.get)
                protagonist = next((c for c in chars_list if isinstance(c, dict) and (c.get('canonical_name') or c.get('name')) == best_name), chars_list[0])

    protagonist_data = {}
    if protagonist and isinstance(protagonist, dict):
        p_name = protagonist.get('canonical_name') or protagonist.get('name', '')
        protagonist_data = {
            'id': protagonist.get('id', 'char_protagonist'),
            'name': p_name,
            'canonical_name': p_name,
            'aliases': protagonist.get('aliases', [p_name]),
            'role': protagonist.get('role') or 'Главный герой',
            'description': protagonist.get('description', ''),
            'wardrobe': protagonist.get('wardrobe', {}),
        }

    events = []
    last_start = 0
    story_clean = str(story or '')
    for idx, seg in enumerate(segments):
        event_id = f'evt_{idx + 1}'
        tl_entry = timeline[idx] if idx < len(timeline) else {'story_day': 'Day 1', 'day_phase': 'day'}

        m_start = story_clean.find(seg, last_start)
        if m_start < 0:
            m_start = story_clean.find(seg, 0)
        m_end = m_start + len(seg) if m_start >= 0 else 0
        if m_start >= 0:
            last_start = m_start

        event = {
            'event_id': event_id,
            'event_index': idx + 1,
            'source_quote': seg,
            'source_start': m_start if m_start >= 0 else 0,
            'source_end': m_end if m_end >= 0 else len(seg),
            'story_day': tl_entry.get('story_day', 'Day 1'),
            'day_phase': tl_entry.get('day_phase', 'day'),
            'season': world.get('season', 'autumn'),
            'weather': world.get('weather', 'clear'),
            'summary': f'Событие {idx + 1}: {seg[:120]}',
            'characters': [],
            'props': [],
            'location_ref': '',
        }
        events.append(event)

    return {
        'version': 1,
        'story_hash': fingerprint(story or ''),
        'protagonist': protagonist_data,
        'world': world,
        'timeline': timeline,
        'cast': chars_list,
        'events': events,
    }


def validate_project_preflight(project_or_data: Dict[str, Any], story: Optional[str] = None) -> Dict[str, Any]:
    """Comprehensive pre-generation validation blocking narrative, character, temporal, and metadata errors."""
    if not isinstance(project_or_data, dict):
        return {'valid': False, 'errors': [{'code': 'invalid_payload', 'message': 'Данные проекта должны быть словарем'}], 'warnings': [], 'stats': {}}

    scenes = project_or_data.get('scenes', [])
    characters = project_or_data.get('characters', [])
    locations = project_or_data.get('locations', [])
    props = project_or_data.get('props', [])
    story_text = story or project_or_data.get('story', '')
    story_clean = str(story_text or '')

    story_bible = project_or_data.get('story_bible') or {}
    cast_bible = project_or_data.get('cast_bible') or {}

    protagonist_name = None
    protagonist_aliases = set()
    if isinstance(story_bible, dict) and story_bible.get('protagonist'):
        p = story_bible['protagonist']
        protagonist_name = p.get('canonical_name') or p.get('name')
        if protagonist_name:
            protagonist_aliases = {protagonist_name.casefold()} | {str(a).casefold() for a in p.get('aliases', [])}
    if not protagonist_name and characters:
        for c in characters:
            if not isinstance(c, dict):
                continue
            role = str(c.get('role', '')).casefold()
            desc = str(c.get('description', '')).casefold()
            if any(w in role or w in desc for w in ('protagonist', 'главный', 'lead', 'hero')):
                protagonist_name = c.get('canonical_name') or c.get('name')
                protagonist_aliases = {protagonist_name.casefold()} | {str(a).casefold() for a in c.get('aliases', [])}
                break
        # If no character has a protagonist role, do not assume characters[0] is protagonist
        pass

    errors = []
    warnings = []

    for c in characters:
        if not isinstance(c, dict):
            continue
        c_name = str(c.get('canonical_name') or c.get('name', '')).strip().casefold()
        if c_name in BANNED_FILLER_NAMES:
            errors.append({
                'code': 'abstract_filler_roles',
                'message': f'Запрещено использование абстрактного имени персонажа "{c_name}". Персонаж должен иметь конкретное имя или подтверждённую роль.',
                'details': {'character': c_name}
            })

    events_scenes = {}
    events_first_scene = {}
    count_with_people = 0
    count_atmosphere = 0
    unassigned_human_scenes = 0
    prev_contract = None

    for idx, scene_entry in enumerate(scenes):
        scene = scene_entry.get('data', scene_entry) if isinstance(scene_entry, dict) else {}
        if not isinstance(scene, dict):
            continue

        evt_id = scene.get('event_id')
        if evt_id and evt_id not in events_first_scene:
            events_first_scene[evt_id] = scene

        quote = str(scene.get('source_quote', '')).strip()
        beat = str(scene.get('beat', '')).strip()

        # Quote verification against story source
        if not quote and not beat:
            errors.append({
                'code': 'quote_contradiction',
                'scene_index': idx + 1,
                'message': f'Кадр {idx + 1}: отсутствует описание события (beat) и подтверждённая цитата источника.',
                'details': {'scene_index': idx + 1}
            })
        elif quote and story_clean:
            match_start, match_end, _, score, _ = find_quote_in_story(quote, story_clean)
            if match_start is None or score < 0.25:
                warnings.append({
                    'code': 'quote_unmatched',
                    'scene_index': idx + 1,
                    'message': f'Кадр {idx + 1}: цитата "{quote[:60]}..." не имеет точного совпадения в исходном тексте истории.',
                    'details': {'scene_index': idx + 1, 'quote': quote[:100]}
                })

        # Unified Scene Presence Contract
        contract = resolve_scene_presence_contract(
            scene, characters, locations, story=story_clean,
            previous_contract=prev_contract,
            parent_event=events_first_scene.get(evt_id),
            require_locked_portraits=False,
            scene_index=idx + 1
        )
        errors.extend(contract['errors'])
        warnings.extend(contract['warnings'])
        # Strict presence invariants
        vis_ids_set = set(contract.get('visible_character_ids', []))
        voice_ids_set = set(contract.get('voice_only_character_ids', []))
        mentioned_ids_set = set(contract.get('mentioned_character_ids', []))
        depicted_ids_set = set(contract.get('depicted_person_ids', []))
        episodic_ids_set = set(contract.get('episodic_role_ids', []))

        if vis_ids_set & voice_ids_set:
            errors.append({
                'code': 'presence_invariant_violation',
                'scene_index': idx + 1,
                'message': f'Кадр {idx + 1}: персонаж одновременно классифицирован как видимый и только голосовой: {vis_ids_set & voice_ids_set}',
                'details': {'scene_index': idx + 1, 'conflict': list(vis_ids_set & voice_ids_set)}
            })
        if vis_ids_set & mentioned_ids_set:
            errors.append({
                'code': 'presence_invariant_violation',
                'scene_index': idx + 1,
                'message': f'Кадр {idx + 1}: персонаж одновременно классифицирован как видимый и только упомянутый: {vis_ids_set & mentioned_ids_set}',
                'details': {'scene_index': idx + 1, 'conflict': list(vis_ids_set & mentioned_ids_set)}
            })
        if vis_ids_set & depicted_ids_set:
            errors.append({
                'code': 'presence_invariant_violation',
                'scene_index': idx + 1,
                'message': f'Кадр {idx + 1}: персонаж одновременно классифицирован как видимый и изображённый на медиа: {vis_ids_set & depicted_ids_set}',
                'details': {'scene_index': idx + 1, 'conflict': list(vis_ids_set & depicted_ids_set)}
            })

        expected_vp = len(set(contract['visible_human_character_ids'])) + len(episodic_ids_set)
        if contract['visible_people'] != expected_vp and not (contract.get('shot_type') == SHOT_TYPE_ATMOSPHERE):
            errors.append({
                'code': 'presence_count_invariant_violation',
                'scene_index': idx + 1,
                'message': f'Кадр {idx + 1}: visible_people ({contract["visible_people"]}) не совпадает с числом видимых людей и эпизодических ролей ({expected_vp}).',
                'details': {'scene_index': idx + 1, 'visible_people': contract['visible_people'], 'expected': expected_vp}
            })

        raw_vp = scene.get('visible_people', 0)
        has_people_in_scene = (contract['visible_people'] > 0 or (isinstance(raw_vp, int) and raw_vp > 0))
        if has_people_in_scene:
            count_with_people += 1
            if any(e['code'] in ('unassigned_humans', 'unexplained_humans', 'protagonist_missing', 'missing_character_scenes') for e in contract['errors']):
                unassigned_human_scenes += 1
        else:
            count_atmosphere += 1

        prev_contract = contract

        # Intra-event tracking
        if evt_id:
            events_scenes.setdefault(evt_id, []).append((idx + 1, scene))

    for evt_id, evt_list in events_scenes.items():
        if len(evt_list) <= 1:
            continue
        first_idx, first_scene = evt_list[0]
        first_day = first_scene.get('story_day')
        first_season = first_scene.get('season')
        first_loc = first_scene.get('location_ref') or first_scene.get('location')
        first_wardrobe = first_scene.get('wardrobe', {})

        for cur_idx, cur_scene in evt_list[1:]:
            cur_day = cur_scene.get('story_day')
            cur_season = cur_scene.get('season')
            cur_loc = cur_scene.get('location_ref') or cur_scene.get('location')
            cur_wardrobe = cur_scene.get('wardrobe', {})

            if first_day and cur_day and first_day != cur_day:
                errors.append({
                    'code': 'intra_event_conflict',
                    'scene_index': cur_idx,
                    'message': f'Кадр {cur_idx} имеет конфликт story_day ("{cur_day}" vs "{first_day}") с кадром {first_idx} того же события {evt_id}.',
                    'details': {'event_id': evt_id, 'field': 'story_day'}
                })
            if first_season and cur_season and first_season != cur_season:
                errors.append({
                    'code': 'intra_event_conflict',
                    'scene_index': cur_idx,
                    'message': f'Кадр {cur_idx} имеет конфликт сезона ("{cur_season}" vs "{first_season}") с кадром {first_idx} того же события {evt_id}.',
                    'details': {'event_id': evt_id, 'field': 'season'}
                })
            if first_loc and cur_loc and first_loc != cur_loc and cur_scene.get('shot_type') != SHOT_TYPE_ATMOSPHERE:
                errors.append({
                    'code': 'intra_event_conflict',
                    'scene_index': cur_idx,
                    'message': f'Кадр {cur_idx} имеет конфликт локации ("{cur_loc}" vs "{first_loc}") с кадром {first_idx} того же события {evt_id}.',
                    'details': {'event_id': evt_id, 'field': 'location'}
                })
            for char_name, w_code in first_wardrobe.items():
                if char_name in cur_wardrobe and cur_wardrobe[char_name] != w_code:
                    errors.append({
                        'code': 'intra_event_conflict',
                        'scene_index': cur_idx,
                        'message': f'Кадр {cur_idx} меняет одежду персонажа {char_name} ("{cur_wardrobe[char_name]}" vs "{w_code}") внутри одного события {evt_id}.',
                        'details': {'event_id': evt_id, 'character': char_name, 'field': 'wardrobe'}
                    })

    if count_with_people > 0 and unassigned_human_scenes > 0:
        unassigned_pct = (unassigned_human_scenes / float(count_with_people)) * 100.0
        if unassigned_pct > 1.0:
            errors.append({
                'code': 'unassigned_humans_threshold',
                'message': f'Более 1% кадров с людьми ({unassigned_human_scenes}/{count_with_people} = {unassigned_pct:.1f}%) не имеют привязки к персонажу.',
                'details': {'unassigned_count': unassigned_human_scenes, 'total_people_scenes': count_with_people, 'percentage': unassigned_pct}
            })

    stats = {
        'total_scenes': len(scenes),
        'total_events': len(events_scenes) if events_scenes else len(scenes),
        'protagonist': protagonist_name,
        'characters_count': len(characters),
        'locations_count': len(locations),
        'props_count': len(props),
        'scenes_with_people': count_with_people,
        'atmosphere_scenes': count_atmosphere,
        'unassigned_people_scenes': unassigned_human_scenes,
    }

    analysis_metadata = project_or_data.get('analysis_metadata') or {}
    if isinstance(analysis_metadata, str):
        try:
            analysis_metadata = json.loads(analysis_metadata)
        except ValueError:
            analysis_metadata = {}
    profile_type = (project_or_data.get('channel_profile') or {}).get('profile_type')
    if profile_type == 'forgotten_cities' or (isinstance(analysis_metadata, dict) and analysis_metadata.get('channel_profile_type') == 'forgotten_cities'):
        try:
            from imagegen.forgotten_cities.preflight import ForgottenCitiesPreflightValidator
            fc_report = ForgottenCitiesPreflightValidator().validate_project(project_or_data, story_clean)
            errors.extend(fc_report.get('errors', []))
            warnings.extend(fc_report.get('warnings', []))
            stats['forgotten_cities'] = fc_report.get('stats', {})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({
                'code': 'invalid_forgotten_cities_analysis',
                'message': f'Профильный анализ «Забытые города» повреждён: {exc}',
                'details': {},
            })

    return {
        'ok': len(errors) == 0,
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'stats': stats,
    }

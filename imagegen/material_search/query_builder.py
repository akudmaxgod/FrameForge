"""Universe context normalizer and targeted query builder for web image search."""
import re
from typing import Dict, Any, List, Optional, Tuple

# Built-in Universe registry
UNIVERSES: Dict[str, Dict[str, Any]] = {
    "game_of_thrones": {
        "title_ru": "Игра престолов",
        "title_en": "Game of Thrones",
        "aliases": ["игра престолов", "песнь льда и пламени", "вестерос", "game of thrones", "got", "asoiaf"],
        "default_adaptation": "series",
        "actor_negative_keywords": ["red carpet", "photoshoot", "interview", "premiere", "emmys", "comic con"]
    },
    "witcher": {
        "title_ru": "Ведьмак",
        "title_en": "The Witcher",
        "aliases": ["ведьмак", "геральт", "witcher", "the witcher"],
        "default_adaptation": "series",
        "actor_negative_keywords": ["red carpet", "photoshoot", "interview", "premiere"]
    },
    "lord_of_the_rings": {
        "title_ru": "Властелин колец",
        "title_en": "Lord of the Rings",
        "aliases": ["властелин колец", "средиземье", "хоббит", "lord of the rings", "lotr", "middle-earth"],
        "default_adaptation": "movie",
        "actor_negative_keywords": ["red carpet", "interview", "premiere"]
    },
    "harry_potter": {
        "title_ru": "Гарри Поттер",
        "title_en": "Harry Potter",
        "aliases": ["гарри поттер", "хогвартс", "harry potter", "hogwarts"],
        "default_adaptation": "movie",
        "actor_negative_keywords": ["red carpet", "premiere", "interview"]
    },
    "mcu": {
        "title_ru": "Киновселенная Marvel",
        "title_en": "Marvel Cinematic Universe",
        "aliases": ["марвел", "мстители", "marvel", "mcu", "avengers"],
        "default_adaptation": "movie",
        "actor_negative_keywords": ["red carpet", "premiere", "interview"]
    },
    "star_wars": {
        "title_ru": "Звёздные войны",
        "title_en": "Star Wars",
        "aliases": ["звездные войны", "звёздные войны", "star wars"],
        "default_adaptation": "movie",
        "actor_negative_keywords": ["red carpet", "premiere", "interview"]
    },
    "nature_geography": {
        "title_ru": "Природа и география",
        "title_en": "Nature and Wildlife",
        "aliases": ["природа", "география", "дикая природа", "nature", "wildlife", "geography"],
        "default_adaptation": "any",
        "actor_negative_keywords": []
    }
}

# Known character/location RU->EN mappings
CANONICAL_ENTITIES_MAP: Dict[str, Tuple[str, str]] = {
    # (ru_name_lower): (en_name, universe_id)
    "дейнерис": ("Daenerys Targaryen", "game_of_thrones"),
    "дейенерис": ("Daenerys Targaryen", "game_of_thrones"),
    "дейнерис таргариен": ("Daenerys Targaryen", "game_of_thrones"),
    "кхалиси": ("Daenerys Targaryen", "game_of_thrones"),
    "джон сноу": ("Jon Snow", "game_of_thrones"),
    "тирион": ("Tyrion Lannister", "game_of_thrones"),
    "тирион ланнистер": ("Tyrion Lannister", "game_of_thrones"),
    "арья старк": ("Arya Stark", "game_of_thrones"),
    "серсея": ("Cersei Lannister", "game_of_thrones"),
    "серсея ланнистер": ("Cersei Lannister", "game_of_thrones"),
    "железный трон": ("Iron Throne", "game_of_thrones"),
    "винтерфелл": ("Winterfell", "game_of_thrones"),
    "белый ходок": ("White Walker", "game_of_thrones"),
    "король ночи": ("Night King", "game_of_thrones"),
    "дрогон": ("Drogon dragon", "game_of_thrones"),

    "геральт": ("Geralt of Rivia", "witcher"),
    "йеннифэр": ("Yennefer of Vengerberg", "witcher"),
    "йеннифер": ("Yennefer of Vengerberg", "witcher"),
    "цири": ("Ciri", "witcher"),
    "лютик": ("Jaskier Dandelion", "witcher"),

    "гарри поттер": ("Harry Potter", "harry_potter"),
    "гермиона": ("Hermione Granger", "harry_potter"),
    "гермиона грейнджер": ("Hermione Granger", "harry_potter"),
    "рон уизли": ("Ron Weasley", "harry_potter"),
    "дамблдор": ("Albus Dumbledore", "harry_potter"),
    "снейп": ("Severus Snape", "harry_potter"),
    "северус снейп": ("Severus Snape", "harry_potter"),
    "хогвартс": ("Hogwarts castle", "harry_potter"),
    "хогвартс-экспресс": ("Hogwarts Express", "harry_potter"),

    "фродо": ("Frodo Baggins", "lord_of_the_rings"),
    "гэндальф": ("Gandalf", "lord_of_the_rings"),
    "гендальф": ("Gandalf", "lord_of_the_rings"),
    "арагорн": ("Aragorn", "lord_of_the_rings"),
    "леголас": ("Legolas", "lord_of_the_rings"),
    "голлум": ("Gollum Smeagol", "lord_of_the_rings"),
    "мордор": ("Mordor", "lord_of_the_rings"),

    "бизон": ("American bison", "nature_geography"),
    "американский бизон": ("American bison", "nature_geography"),
    "гейзер": ("Geyser eruption", "nature_geography"),
    "йеллоустоун": ("Yellowstone national park", "nature_geography"),
    "ниагарский водопад": ("Niagara Falls", "nature_geography"),
    "гранд-каньон": ("Grand Canyon", "nature_geography"),
    "северное сияние": ("Aurora borealis", "nature_geography"),
    "байкал": ("Lake Baikal", "nature_geography"),
    "белый медведь": ("Polar bear Arctic", "nature_geography"),
    "африканский слон": ("African elephant savannah", "nature_geography"),
    "пустыня сахара": ("Sahara desert", "nature_geography")
}

# Common Russian action phrases to English equivalents
ACTION_MAP: Dict[str, str] = {
    "входит в тронный зал": "entering throne room",
    "в тронном зале": "throne room",
    "на железном троне": "on Iron Throne",
    "на стене": "battlements Wall",
    "стоит на стене": "standing on battlements",
    "летит": "flying",
    "летит над": "flying over",
    "отдыхает в саду": "resting in garden",
    "в саду": "in garden",
    "сражается": "fighting sword combat",
    "в бою": "in battle",
    "читает книгу": "reading book library",
    "на коне": "riding horse",
    "в замке": "in castle interior",
    "зима близко": "winter is coming snow"
}


def detect_universe_and_entities(query_or_quote: str) -> Dict[str, Any]:
    """Detect mentioned universe and canonical entities from user query or quote text."""
    text_lower = query_or_quote.lower().strip()
    detected_universe = None
    detected_entities = []

    # Check universe keywords
    for ukey, udata in UNIVERSES.items():
        for alias in udata["aliases"]:
            if alias in text_lower:
                detected_universe = ukey
                break
        if detected_universe:
            break

    # Check entity keywords
    for ru_name, (en_name, ukey) in CANONICAL_ENTITIES_MAP.items():
        pattern = r'\b' + re.escape(ru_name) + r'\b'
        if re.search(pattern, text_lower):
            detected_entities.append({"ru": ru_name, "en": en_name, "universe_id": ukey})
            if not detected_universe and ukey != "nature_geography":
                detected_universe = ukey

    return {
        "universe_id": detected_universe,
        "entities": detected_entities
    }


def build_targeted_queries(
    user_query: str,
    universe_id: Optional[str] = None,
    adaptation: str = "any",     # "series" | "movie" | "book" | "game" | "any"
    image_type: str = "any",     # "still" | "illustration" | "map" | "any"
    max_queries: int = 2
) -> List[str]:
    """
    Build 1-2 targeted search queries combining universe, canonical character/location,
    and action terms for high precision web search.
    """
    raw_query = user_query.strip()
    if not raw_query:
        return []

    detection = detect_universe_and_entities(raw_query)
    effective_universe = universe_id or detection["universe_id"]
    detected_entities = detection["entities"]

    u_data = UNIVERSES.get(effective_universe or "")
    u_title_en = u_data["title_en"] if u_data else ""

    # Translate action phrases if matched
    en_action = ""
    text_lower = raw_query.lower()
    for ru_act, act_en in ACTION_MAP.items():
        if ru_act in text_lower:
            en_action = act_en
            break

    # Determine adaptation & type qualifiers
    qualifiers = []
    if adaptation == "series":
        qualifiers.append("series")
    elif adaptation == "movie":
        qualifiers.append("movie")
    elif adaptation == "book":
        qualifiers.append("book illustration")
    elif adaptation == "game":
        qualifiers.append("game screenshot")

    if image_type == "still":
        if "series" not in qualifiers and "movie" not in qualifiers:
            qualifiers.append("still")
        else:
            qualifiers.append("still")
    elif image_type == "illustration":
        qualifiers.append("concept art")
    elif image_type == "map":
        qualifiers.append("map")

    qualifier_str = " ".join(qualifiers).strip()

    queries = []

    # Replace known phrases in-place; unrecognised content remains searchable.
    text = raw_query
    replacements = dict(ACTION_MAP)
    replacements.update({name: target[0] for name,target in CANONICAL_ENTITIES_MAP.items()
                         if not effective_universe or target[1]==effective_universe})
    pattern = re.compile(r"\b(?:"+'|'.join(re.escape(k) for k in sorted(replacements,key=len,reverse=True))+r")\b",re.I)
    text = pattern.sub(lambda m: replacements[m.group().lower()],text)
    parts = [text]
    if u_title_en and u_title_en.casefold() not in text.casefold():parts.insert(0,u_title_en)
    if qualifier_str:parts.append(qualifier_str)
    negative = (u_data or {}).get('actor_negative_keywords',[]) if image_type!='illustration' else []
    parts.extend('-"'+word+'"' for word in negative[:3])
    queries = [' '.join(parts)]

    # Limit to max_queries and enforce query length limits (max 400 chars, max 50 words)
    cleaned_queries = []
    for q in queries[:max_queries]:
        words = q.split()[:45]
        trimmed = " ".join(words)[:380].strip()
        if trimmed and trimmed not in cleaned_queries:
            cleaned_queries.append(trimmed)

    # Fallback to raw user query if nothing built
    if not cleaned_queries:
        cleaned_queries.append(raw_query)

    return cleaned_queries

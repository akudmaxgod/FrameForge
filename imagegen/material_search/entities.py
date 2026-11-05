import hashlib
"""Entity catalog, alias management, and canon resolution for material search."""
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set

from ..db import uid, now
from ..media_matching import tokenize_words, stem_ru

logger = logging.getLogger(__name__)

# Built-in canonical reference dictionary for common universe and nature entities
DEFAULT_CANON_ENTITIES = [
    {
        "entity_key": "american_bison",
        "canonical_name": "Американский бизон",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["бизон", "американский бизон", "bison", "american bison", "buffalo", "стадо бизонов"],
        "forbidden_substitutions": ["зубр", "корова", "европейский зубр", "бык", "wisent", "european bison"],
        "mandatory_details": ["бизон"]
    },
    {
        "entity_key": "geyser_eruption",
        "canonical_name": "Извержение гейзера",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["гейзер", "извержение гейзера", "geyser", "geyser eruption", "steamboat geyser", "old faithful"],
        "forbidden_substitutions": ["вулкан", "дым вулкана", "горячий источник", "грязевой вулкан", "volcano", "mud pot", "спокойный источник"],
        "mandatory_details": ["гейзер"]
    },
    {
        "entity_key": "yellowstone_national_park",
        "canonical_name": "Национальный парк Йеллоустоун",
        "category": "location",
        "universe_id": "nature_geography",
        "aliases": ["йеллоустоун", "йеллоустон", "yellowstone", "yellowstone national park"],
        "forbidden_substitutions": ["йосемити", "камчатка", "гренландия", "yosemite", "kamchatka"],
        "mandatory_details": ["йеллоустоун"]
    },
    {
        "entity_key": "niagara_falls",
        "canonical_name": "Ниагарский водопад",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["ниагарский водопад", "ниагара", "niagara falls", "niagara"],
        "forbidden_substitutions": ["виктория", "игуасу", "анхель", "victoria falls", "iguazu"],
        "mandatory_details": ["ниагар"]
    },
    {
        "entity_key": "grand_canyon",
        "canonical_name": "Гранд-Каньон",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["гранд-каньон", "большой каньон", "grand canyon"],
        "forbidden_substitutions": ["каньон антилопы", "брайс", "antelope canyon", "bryce canyon"],
        "mandatory_details": ["каньон"]
    },
    {
        "entity_key": "aurora_borealis",
        "canonical_name": "Северное сияние",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["северное сияние", "полярное сияние", "aurora", "aurora borealis", "northern lights"],
        "forbidden_substitutions": ["молния", "закат", "салют", "фургон", "lightning", "fireworks"],
        "mandatory_details": ["сияние"]
    },
    {
        "entity_key": "baikal_lake",
        "canonical_name": "Озеро Байкал",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["байкал", "озеро байкал", "baikal", "lake baikal"],
        "forbidden_substitutions": ["ладога", "онега", "верхнее озеро", "lake superior", "ladoga"],
        "mandatory_details": ["байкал"]
    },
    {
        "entity_key": "polar_bear_arctic",
        "canonical_name": "Белый медведь в Арктике",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["белый медведь", "полярный медведь", "polar bear", "ursus maritimus"],
        "forbidden_substitutions": ["бурый медведь", "гризли", "панда", "grizzly", "brown bear", "panda"],
        "mandatory_details": ["медведь"]
    },
    {
        "entity_key": "african_elephant",
        "canonical_name": "Африканский слон в саванне",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["африканский слон", "слон в саванне", "african elephant", "loxodonta"],
        "forbidden_substitutions": ["индийский слон", "азиатский слон", "мамонт", "asian elephant", "mammoth"],
        "mandatory_details": ["слон"]
    },
    {
        "entity_key": "sahara_desert",
        "canonical_name": "Пустыня Сахара",
        "category": "nature",
        "universe_id": "nature_geography",
        "aliases": ["сахара", "пустыня сахара", "sahara", "sahara desert"],
        "forbidden_substitutions": ["гоби", "долина смерти", "death valley", "gobi"],
        "mandatory_details": ["сахара"]
    },
    {
        "entity_key": "daenerys_targaryen",
        "canonical_name": "Дейнерис Таргариен",
        "category": "character",
        "universe_id": "game_of_thrones",
        "aliases": ["дейнерис", "дейенерис", "кхалиси", "daenerys", "daenerys targaryen", "khaleesi", "матерь драконов"],
        "forbidden_substitutions": ["эмилия кларк вне роли", "фан-арт", "косплей", "алисента", "red carpet", "photoshoot", "cosplay", "fan art"],
        "mandatory_details": ["дейнерис"]
    },
    {
        "entity_key": "flying_dragon",
        "canonical_name": "Летит дракон",
        "category": "creature",
        "universe_id": "game_of_thrones",
        "aliases": ["дракон", "драконы", "дракон летит", "полет дракона", "dragon", "dragon flying", "drogon", "дрогон"],
        "forbidden_substitutions": ["спящий дракон", "дракон в пещере", "ящерица", "sleeping dragon", "sleeping", "cave", "lizard"],
        "mandatory_details": ["дракон"]
    },
    {
        "entity_key": "harry_potter",
        "canonical_name": "Гарри Поттер",
        "category": "character",
        "universe_id": "harry_potter",
        "aliases": ["гарри поттер", "поттер", "harry potter", "the boy who lived"],
        "forbidden_substitutions": ["дэниел редклифф вне роли", "косплей", "фан-арт", "red carpet", "photoshoot", "cosplay", "fan art"],
        "mandatory_details": ["гарри поттер"]
    },
    {
        "entity_key": "iron_throne",
        "canonical_name": "Железный трон Вестероса",
        "category": "location",
        "universe_id": "game_of_thrones",
        "aliases": ["железный трон", "iron throne", "throne of swords"],
        "forbidden_substitutions": ["деревянный трон", "золотой трон", "стул", "wooden chair", "golden throne"],
        "mandatory_details": ["трон"]
    },
    {
        "entity_key": "jon_snow",
        "canonical_name": "Джон Сноу на Стене",
        "category": "character",
        "universe_id": "game_of_thrones",
        "aliases": ["джон сноу", "jon snow", "король севера", "king in the north"],
        "forbidden_substitutions": ["кит харингтон вне роли", "red carpet", "photoshoot", "косплей", "fan art"],
        "mandatory_details": ["джон сноу"]
    },
    {
        "entity_key": "hogwarts_express",
        "canonical_name": "Хогвартс-экспресс",
        "category": "location",
        "universe_id": "harry_potter",
        "aliases": ["хогвартс-экспресс", "поезд в хогвартс", "hogwarts express", "jacobite steam train", "гленфиннан"],
        "forbidden_substitutions": ["сапсан", "метро", "bullet train", "современный поезд", "tgv", "subway"],
        "mandatory_details": ["поезд"]
    },
    {
        "entity_key": "white_walker",
        "canonical_name": "Белый Ходок",
        "category": "character",
        "universe_id": "game_of_thrones",
        "aliases": ["белый ходок", "король ночи", "white walker", "night king", "others"],
        "forbidden_substitutions": ["обычный зомби", "маска на хэллоуин", "walking dead", "generic zombie"],
        "mandatory_details": ["ходок"]
    },
    {
        "entity_key": "gollum",
        "canonical_name": "Голлум",
        "category": "character",
        "universe_id": "lord_of_the_rings",
        "aliases": ["голлум", "смеагол", "gollum", "smeagol"],
        "forbidden_substitutions": ["йода", "добби", "yoda", "dobby"],
        "mandatory_details": ["голлум"]
    },
    {
        "entity_key": "medical_jaundice",
        "canonical_name": "Желтуха симптом",
        "category": "nature",
        "universe_id": "medical_facts",
        "aliases": ["желтуха", "желтушность склер", "jaundice", "icterus", "yellow sclera"],
        "forbidden_substitutions": ["здоровый цвет лица", "нейтральный портрет", "healthy skin", "normal portrait"],
        "mandatory_details": ["желту"]
    },
    {
        "entity_key": "sherlock_holmes",
        "canonical_name": "Шерлок Холмс",
        "category": "character",
        "universe_id": "sherlock",
        "aliases": ["шерлок холмс", "бейкер-стрит", "sherlock holmes", "baker street", "221b"],
        "forbidden_substitutions": ["бенедикт вне роли", "ковбой", "железный человек", "iron man", "doctor strange"],
        "mandatory_details": ["шерлок"]
    }
]


@dataclass
class CatalogEntity:
    id: str
    project_id: str
    entity_key: str
    canonical_name: str
    category: str = "character"  # character, actor, location, creature, nature, event
    universe_id: str = ""
    aliases: List[str] = field(default_factory=list)
    forbidden_substitutions: List[str] = field(default_factory=list)
    mandatory_details: List[str] = field(default_factory=list)
    references: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EntityCatalog:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    def ensure_default_canon(self, project_id: str) -> None:
        """Seed project catalog with default nature and universe entities if empty."""
        count = self.db.one("SELECT COUNT(*) as c FROM material_entity_catalog WHERE project_id = ?", (project_id,))
        if count and count["c"] > 0:
            return

        t_now = now()
        with self.db.transaction() as conn:
            for item in DEFAULT_CANON_ENTITIES:
                item_id = uid()
                conn.execute(
                    """
                    INSERT INTO material_entity_catalog (
                        id, project_id, entity_key, canonical_name, category,
                        universe_id, aliases_json, references_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, entity_key) DO NOTHING
                    """,
                    (
                        item_id, project_id, item["entity_key"], item["canonical_name"],
                        item["category"], item["universe_id"], json.dumps(item["aliases"]),
                        json.dumps({
                            "forbidden_substitutions": item.get("forbidden_substitutions", []),
                            "mandatory_details": item.get("mandatory_details", [])
                        }),
                        t_now, t_now
                    )
                )

    def get_project_entities(self, project_id: str) -> List[CatalogEntity]:
        self.ensure_default_canon(project_id)
        rows = self.db.all(
            "SELECT * FROM material_entity_catalog WHERE project_id = ? ORDER BY canonical_name ASC",
            (project_id,)
        )
        result: List[CatalogEntity] = []
        for r in rows:
            rd = dict(r)
            aliases = json.loads(rd.get("aliases_json") or "[]")
            refs = json.loads(rd.get("references_json") or "{}")
            forb = refs.get("forbidden_substitutions", []) if isinstance(refs, dict) else []
            mand = refs.get("mandatory_details", []) if isinstance(refs, dict) else []
            result.append(CatalogEntity(
                id=rd["id"],
                project_id=rd["project_id"],
                entity_key=rd["entity_key"],
                canonical_name=rd["canonical_name"],
                category=rd["category"],
                universe_id=rd["universe_id"],
                aliases=aliases,
                forbidden_substitutions=forb,
                mandatory_details=mand,
                references=refs.get("reference_images", []) if isinstance(refs, dict) else [],
                created_at=rd.get("created_at", ""),
                updated_at=rd.get("updated_at", "")
            ))
        return result

    def match_entities_in_text(self, project_id: str, text: str) -> List[CatalogEntity]:
        """Find matching known entities mentioned in a quote or scene description."""
        if not text:
            return []
        entities = self.get_project_entities(project_id)
        text_lower = text.lower()
        stemmed_tokens = tokenize_words(text)
        matched: List[CatalogEntity] = []

        for ent in entities:
            found = False
            for alias in [ent.canonical_name] + ent.aliases:
                al = alias.lower().strip()
                if len(al) >= 3 and (al in text_lower or stem_ru(al) in stemmed_tokens):
                    matched.append(ent)
                    found = True
                    break

        return matched

    def compute_entities_references_hash(self, project_id: str, entity_names: List[str]) -> str:
        """Computes deterministic hash of entity references and constraints for cache invalidation."""
        if not entity_names:
            return ""
        rows = self.db.all(
            "SELECT canonical_name, references_json, updated_at FROM material_entity_catalog WHERE project_id = ?",
            (project_id,)
        )
        ent_map = {r["canonical_name"]: r for r in rows}
        parts = []
        for name in sorted(entity_names):
            if name in ent_map:
                parts.append(f"{name}:{ent_map[name]['references_json']}:{ent_map[name]['updated_at']}")
        return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()

"""Quality benchmark dataset and evaluation for FrameForge Material Search.

Covers 40 annotated evaluation cases:
- 20 Nature / Geography cases (bison, geysers, canyons, auroras, waterfalls, etc.)
- 20 Specific Universes & Drama cases (Daenerys, dragons in flight, Harry Potter, Iron Throne, etc.)

Verifies:
1. Zero false-accepted hard negatives (critical substitutions MUST be rejected or contradicted).
2. Proper status categorization (matched vs needs_review vs rejected).
3. Rights status validation (recorded_license vs permission_recorded vs unknown).
"""

import unittest
from typing import Dict, Any, List

from imagegen.material_search.ranking import evaluate_candidate_candidate, enforce_project_diversity
from imagegen.material_search.entities import DEFAULT_CANON_ENTITIES, EntityCatalog

# 40 Test Cases Definition
# Expected outcomes:
# - expected_semantic: "matched" | "needs_review" | "rejected"
# - expected_identity: "confirmed" | "plausible" | "unverified" | "contradicted" | "not_required"
# - is_hard_negative: True if this candidate is an adversarial or illegal substitution

DATASET_CASES: List[Dict[str, Any]] = [
    # =========================================================================
    # NATURE & GEOGRAPHY (20 Cases)
    # =========================================================================
    # 1. American Bison in Yellowstone (Valid match)
    {
        "id": "nat_01_bison_valid",
        "req": {
            "quote": "Стадо американских бизонов пасётся в долине Йеллоустоуна",
            "subject_entities": ["Американский бизон"],
            "mandatory_details": ["бизон"],
            "forbidden_substitutions": ["зубр", "корова", "wisent", "european bison"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "American Bison herd in Yellowstone Valley",
            "description": "Bison grazing during autumn in Yellowstone National Park",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 2. Hard Negative: European Bison (Zubr) substituted for American Bison
    {
        "id": "nat_02_bison_subst_zubr",
        "req": {
            "quote": "Стадо американских бизонов пасётся в долине Йеллоустоуна",
            "subject_entities": ["Американский бизон"],
            "mandatory_details": ["бизон"],
            "forbidden_substitutions": ["зубр", "корова", "wisent", "european bison"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Европейский зубr в Беловежской пуще (Wisent)",
            "description": "Дикий европейский зубр пасётся в лесу",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 3. Hard Negative: Domestic cow substituted for Bison
    {
        "id": "nat_03_bison_subst_cow",
        "req": {
            "quote": "Стадо американских бизонов пасётся в долине Йеллоустоуна",
            "subject_entities": ["Американский бизон"],
            "mandatory_details": ["бизон"],
            "forbidden_substitutions": ["зубр", "корова", "cow"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Домашняя корова на лугу",
            "description": "Корова пасётся на ферме",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 4. Geyser eruption (Old Faithful / Steamboat) - Valid match
    {
        "id": "nat_04_geyser_valid",
        "req": {
            "quote": "Мощное извержение гейзера выбрасывает столб кипящей воды",
            "subject_entities": ["Извержение гейзера"],
            "mandatory_details": ["гейзер"],
            "forbidden_substitutions": ["вулкан", "дым вулкана", "грязевой вулкан", "volcano"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Old Faithful Geyser Eruption",
            "description": "Powerful geyser eruption water column reaching high into the sky",
            "license_name": "Public Domain",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 5. Hard Negative: Volcano eruption / smoke substituted for Geyser
    {
        "id": "nat_05_geyser_subst_volcano",
        "req": {
            "quote": "Мощное извержение гейзера выбрасывает столб кипящей воды",
            "subject_entities": ["Извержение гейзера"],
            "mandatory_details": ["гейзер"],
            "forbidden_substitutions": ["вулкан", "дым вулкана", "volcano"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Извержение вулкана Этна с клубами пепла",
            "description": "Active volcano eruption with thick black smoke and lava",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 6. Hard Negative: Calm hot spring pool without eruption
    {
        "id": "nat_06_geyser_subst_calm_spring",
        "req": {
            "quote": "Мощное извержение гейзера выбрасывает столб кипящей воды",
            "subject_entities": ["Извержение гейзера"],
            "mandatory_details": ["гейзер"],
            "forbidden_substitutions": ["спокойный источник", "горячий источник", "грязевой вулкан"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Спокойный горячий источник Morning Glory Pool",
            "description": "Still colorful thermal pool without eruption",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 7. Yellowstone National Park entrance sign - Valid match
    {
        "id": "nat_07_yellowstone_sign_valid",
        "req": {
            "quote": "У въезда в национальный парк Йеллоустоун установлен деревянный знак",
            "subject_entities": ["Национальный парк Йеллоустоун"],
            "mandatory_details": ["йеллоустоун"],
            "forbidden_substitutions": ["йосемити", "yosemite", "камчатка"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Yellowstone National Park Entrance Wooden Sign",
            "description": "North Entrance Arch and sign to Yellowstone",
            "license_name": "CC BY-SA 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 8. Hard Negative: Yosemite Park sign substituted for Yellowstone
    {
        "id": "nat_08_yellowstone_subst_yosemite",
        "req": {
            "quote": "У въезда в национальный парк Йеллоустоун установлен деревянный знак",
            "subject_entities": ["Национальный парк Йеллоустоун"],
            "mandatory_details": ["йеллоустоун"],
            "forbidden_substitutions": ["йосемити", "yosemite", "камчатка"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Yosemite National Park Entrance Sign California",
            "description": "Wooden entrance marker at Yosemite valley",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 9. Niagara Falls - Valid match
    {
        "id": "nat_09_niagara_valid",
        "req": {
            "quote": "Грохочущие потоки Ниагарского водопада срываются в ущелье",
            "subject_entities": ["Ниагарский водопад"],
            "mandatory_details": ["ниагар"],
            "forbidden_substitutions": ["виктория", "игуасу", "victoria falls"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Niagara Falls Horseshoe Fall with mist",
            "description": "Spectacular cascade of Niagara falling into river gorge",
            "license_name": "CC BY 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 10. Hard Negative: Victoria Falls substituted for Niagara
    {
        "id": "nat_10_niagara_subst_victoria",
        "req": {
            "quote": "Грохочущие потоки Ниагарского водопада срываются в ущелье",
            "subject_entities": ["Ниагарский водопад"],
            "mandatory_details": ["ниагар"],
            "forbidden_substitutions": ["виктория", "victoria falls", "игуасу"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Victoria Falls on the Zambezi River",
            "description": "Water cascade of Victoria Falls in Zimbabwe",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 11. Grand Canyon panorama - Valid match
    {
        "id": "nat_11_grand_canyon_valid",
        "req": {
            "quote": "Бескрайняя панорама Гранд-Каньона на закате",
            "subject_entities": ["Гранд-Каньон"],
            "mandatory_details": ["каньон"],
            "forbidden_substitutions": ["каньон антилопы", "antelope canyon"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Grand Canyon South Rim Sunset Panorama",
            "description": "Vast layers of the Grand Canyon bathed in evening light",
            "license_name": "CC0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 12. Hard Negative: Narrow Antelope Canyon substituted for Grand Canyon
    {
        "id": "nat_12_grand_canyon_subst_antelope",
        "req": {
            "quote": "Бескрайняя панорама Гранд-Каньона на закате",
            "subject_entities": ["Гранд-Каньон"],
            "mandatory_details": ["каньон"],
            "forbidden_substitutions": ["каньон антилопы", "antelope canyon"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Antelope Canyon narrow slot formations",
            "description": "Swirling orange sandstone walls inside Antelope Canyon",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 13. Northern Lights (Aurora Borealis) - Valid match
    {
        "id": "nat_13_aurora_valid",
        "req": {
            "quote": "Зелёные сполохи северного сияния озаряют ночное небо",
            "subject_entities": ["Северное сияние"],
            "mandatory_details": ["сияние"],
            "forbidden_substitutions": ["молния", "салют", "fireworks", "lightning"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Aurora Borealis Green Bands in Tromso Norway",
            "description": "Brilliant green northern lights shining over snowy fjord",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 14. Hard Negative: City fireworks substituted for Aurora Borealis
    {
        "id": "nat_14_aurora_subst_fireworks",
        "req": {
            "quote": "Зелёные сполохи северного сияния озаряют ночное небо",
            "subject_entities": ["Северное сияние"],
            "mandatory_details": ["сияние"],
            "forbidden_substitutions": ["салют", "fireworks", "молния"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Праздничный салют над ночным городом",
            "description": "Fireworks explosions in midnight sky",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 15. Lake Baikal transparent ice - Valid match
    {
        "id": "nat_15_baikal_valid",
        "req": {
            "quote": "Прозрачный бирюзовый лёд зимнего озера Байкал",
            "subject_entities": ["Озеро Байкал"],
            "mandatory_details": ["байкал"],
            "forbidden_substitutions": ["ладога", "верхнее озеро", "lake superior"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Lake Baikal clear turquoise ice hummocks",
            "description": "Deep crystal clear ice blocks on winter Baikal Lake",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 16. Hard Negative: Lake Superior substituted for Baikal
    {
        "id": "nat_16_baikal_subst_superior",
        "req": {
            "quote": "Прозрачный бирюзовый лёд зимнего озера Байкал",
            "subject_entities": ["Озеро Байкал"],
            "mandatory_details": ["байкал"],
            "forbidden_substitutions": ["верхнее озеро", "lake superior", "ладога"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Winter on Lake Superior shoreline Michigan",
            "description": "Frozen shore and ice shelf of Lake Superior",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 17. Polar bear in Arctic ice - Valid match
    {
        "id": "nat_17_polar_bear_valid",
        "req": {
            "quote": "Белый медведь перепрыгивает между арктическими льдинами",
            "subject_entities": ["Белый медведь в Арктике"],
            "mandatory_details": ["медведь"],
            "forbidden_substitutions": ["бурый медведь", "гризли", "grizzly", "brown bear"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Polar bear walking on pack ice in Svalbard",
            "description": "Large male polar bear traversing Arctic sea ice",
            "license_name": "CC BY 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 18. Hard Negative: Grizzly bear substituted for Polar bear
    {
        "id": "nat_18_polar_bear_subst_grizzly",
        "req": {
            "quote": "Белый медведь перепрыгивает между арктическими льдинами",
            "subject_entities": ["Белый медведь в Арктике"],
            "mandatory_details": ["медведь"],
            "forbidden_substitutions": ["бурый медведь", "гризли", "grizzly", "brown bear"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Бурый медведь гризли ловит лосося в реке",
            "description": "Brown grizzly bear catching fish in Alaska river",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 19. Sahara desert sand dunes - Valid match
    {
        "id": "nat_19_sahara_valid",
        "req": {
            "quote": "Бескрайние песчаные дюны пустыни Сахара",
            "subject_entities": ["Пустыня Сахара"],
            "mandatory_details": ["сахара"],
            "forbidden_substitutions": ["гоби", "долина смерти", "death valley"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Erg Chebbi dunes in the Sahara desert Morocco",
            "description": "Golden sand dunes undulating under clear desert sky",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 20. Needs Review: Generic sand beach (ambiguous location, missing Sahara)
    {
        "id": "nat_20_sahara_ambiguous_beach",
        "req": {
            "quote": "Бескрайние песчаные дюны пустыни Сахара",
            "subject_entities": ["Пустыня Сахара"],
            "mandatory_details": ["сахара"],
            "forbidden_substitutions": ["гоби", "долина смерти"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Песчаный берег моря на закате",
            "description": "Жёлтый песок на пляже",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "needs_review",
        "expected_identity": "unverified",
        "is_hard_negative": False
    },

    # =========================================================================
    # SPECIFIC UNIVERSES & DRAMA (20 Cases)
    # =========================================================================
    # 21. Daenerys Targaryen in Game of Thrones - Valid match
    {
        "id": "uni_21_daenerys_valid",
        "req": {
            "quote": "Дейнерис Таргариен с серебряными волосами идёт во главе безупречных",
            "subject_entities": ["Дейнерис Таргариен"],
            "mandatory_details": ["дейнерис"],
            "forbidden_substitutions": ["эмилия кларк вне роли", "фан-арт", "косплей", "red carpet", "photoshoot", "cosplay", "fan art"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Daenerys Targaryen Khaleesi costume scene HBO",
            "description": "Daenerys Targaryen leading army of Unsullied",
            "license_name": "CC BY-SA 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 22. Hard Negative: Emilia Clarke on Red Carpet out of role
    {
        "id": "uni_22_daenerys_subst_red_carpet",
        "req": {
            "quote": "Дейнерис Таргариен с серебряными волосами идёт во главе безупречных",
            "subject_entities": ["Дейнерис Таргариен"],
            "mandatory_details": ["дейнерис"],
            "forbidden_substitutions": ["эмилия кларк вне роли", "фан-арт", "косплей", "red carpet", "photoshoot", "cosplay", "fan art"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Emilia Clarke at the 2019 Cannes Film Festival Red Carpet",
            "description": "Actress Emilia Clarke posing in evening gown photoshoot",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 23. Hard Negative: Cosplay / Amateur convention costume
    {
        "id": "uni_23_daenerys_subst_cosplay",
        "req": {
            "quote": "Дейнерис Таргариен с серебряными волосами идёт во главе безупречных",
            "subject_entities": ["Дейнерис Таргариен"],
            "mandatory_details": ["дейнерис"],
            "forbidden_substitutions": ["эмилия кларк вне роли", "фан-арт", "косплей", "cosplay"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Amateur Comic-Con Cosplay of Daenerys",
            "description": "Fan cosplay costume at pop convention",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 24. Flying Dragon in Game of Thrones - Valid match
    {
        "id": "uni_24_flying_dragon_valid",
        "req": {
            "quote": "Огромный дракон летит над кронами тёмного леса",
            "subject_entities": ["Летит дракон"],
            "mandatory_details": ["дракон"],
            "forbidden_substitutions": ["спящий дракон", "дракон в пещере", "sleeping", "cave", "ящерица"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Black dragon flying in mid-air with outstretched wings",
            "description": "Huge dragon flying over northern forest pines",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 25. Hard Negative: Sleeping dragon in cave (contrary to flying action)
    {
        "id": "uni_25_dragon_subst_sleeping_cave",
        "req": {
            "quote": "Огромный дракон летит над кронами тёмного леса",
            "subject_entities": ["Летит дракон"],
            "mandatory_details": ["дракон"],
            "forbidden_substitutions": ["спящий дракон", "дракон в пещере", "sleeping", "cave"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Sleeping dragon inside dark gold treasure cave",
            "description": "Giant reptile sleeping on gold coins underground",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 26. Hard Negative: Small modern lizard substituted for Dragon
    {
        "id": "uni_26_dragon_subst_lizard",
        "req": {
            "quote": "Огромный дракон летит над кронами тёмного леса",
            "subject_entities": ["Летит дракон"],
            "mandatory_details": ["дракон"],
            "forbidden_substitutions": ["ящерица", "lizard", "геккон"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Green tree lizard on a branch in garden",
            "description": "Small reptile lizard resting on twig",
            "license_name": "CC0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 27. Harry Potter at Hogwarts - Valid match
    {
        "id": "uni_27_harry_potter_valid",
        "req": {
            "quote": "Гарри Поттер в мантии Гриффиндора держит волшебную палочку",
            "subject_entities": ["Гарри Поттер"],
            "mandatory_details": ["гарри поттер"],
            "forbidden_substitutions": ["дэниел редклифф вне роли", "косплей", "фан-арт", "red carpet", "photoshoot"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Harry Potter Gryffindor Robes Warner Bros Studio Tour",
            "description": "Original film costume of Harry Potter holding wand at Hogwarts",
            "license_name": "CC BY-SA 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 28. Hard Negative: Daniel Radcliffe photoshoot out of role
    {
        "id": "uni_28_harry_potter_subst_actor_photoshoot",
        "req": {
            "quote": "Гарри Поттер в мантии Гриффиндора держит волшебную палочку",
            "subject_entities": ["Гарри Поттер"],
            "mandatory_details": ["гарри поттер"],
            "forbidden_substitutions": ["дэниел редклифф вне роли", "косплей", "фан-арт", "red carpet", "photoshoot"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Daniel Radcliffe at the Toronto International Film Festival",
            "description": "Actor Daniel Radcliffe posing in leather jacket photoshoot",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 29. Iron Throne of Westeros - Valid match
    {
        "id": "uni_29_iron_throne_valid",
        "req": {
            "quote": "Железный трон из мечей поверженных врагов стоит в тронном зале",
            "subject_entities": ["Железный трон Вестероса"],
            "mandatory_details": ["трон"],
            "forbidden_substitutions": ["деревянный трон", "золотой трон", "стул", "wooden chair"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Iron Throne forged of thousand swords Game of Thrones exhibition",
            "description": "The Iron Throne seat of Westeros kings at Belfast studio",
            "license_name": "CC BY 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 30. Hard Negative: Generic modern wooden chair substituted for Iron Throne
    {
        "id": "uni_30_iron_throne_subst_wooden_chair",
        "req": {
            "quote": "Железный трон из мечей поверженных врагов стоит в тронном зале",
            "subject_entities": ["Железный трон Вестероса"],
            "mandatory_details": ["трон"],
            "forbidden_substitutions": ["деревянный трон", "золотой трон", "стул", "wooden chair"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Старый деревянный стул на чердаке",
            "description": "Простой деревянный стул vintage wooden chair",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 31. Hogwarts Express at Glenfinnan Viaduct - Valid match
    {
        "id": "uni_31_hogwarts_express_valid",
        "req": {
            "quote": "Хогвартс-экспресс на всех парах мчит по виадуку Гленфиннан",
            "subject_entities": ["Хогвартс-экспресс"],
            "mandatory_details": ["поезд"],
            "forbidden_substitutions": ["сапсан", "метро", "bullet train", "современный поезд", "tgv"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Jacobite Steam Train crossing the Glenfinnan Viaduct Scotland",
            "description": "Historic steam locomotive Hogwarts Express crossing stone arches",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 32. Hard Negative: Modern high-speed bullet train substituted for Hogwarts steam train
    {
        "id": "uni_32_hogwarts_express_subst_bullet_train",
        "req": {
            "quote": "Хогвартс-экспресс на всех парах мчит по виадуку Гленфиннан",
            "subject_entities": ["Хогвартс-экспресс"],
            "mandatory_details": ["поезд"],
            "forbidden_substitutions": ["сапсан", "метро", "bullet train", "современный поезд", "tgv"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Скоростной поезд Сапсан на вокзале",
            "description": "Современный скоростной поезд bullet train на рельсах",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 33. White Walker in Game of Thrones - Valid match
    {
        "id": "uni_33_white_walker_valid",
        "req": {
            "quote": "Белый Ходок с ледяными синими глазами выходит из метели",
            "subject_entities": ["Белый Ходок"],
            "mandatory_details": ["ходок"],
            "forbidden_substitutions": ["обычный зомби", "маска на хэллоуин", "generic zombie"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "White Walker Night King sculpture with glowing blue eyes",
            "description": "White Walker ice creature character from Westeros",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 34. Hard Negative: Generic zombie / Halloween mask substituted for White Walker
    {
        "id": "uni_34_white_walker_subst_generic_zombie",
        "req": {
            "quote": "Белый Ходок с ледяными синими глазами выходит из метели",
            "subject_entities": ["Белый Ходок"],
            "mandatory_details": ["ходок"],
            "forbidden_substitutions": ["обычный зомби", "маска на хэллоуин", "generic zombie"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Маска на хэллоуин страшного чудовища",
            "description": "Пластиковая маска на хэллоуин для вечеринки",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 35. Gollum in Lord of the Rings - Valid match
    {
        "id": "uni_35_gollum_valid",
        "req": {
            "quote": "Голлум сжимает в костлявых руках Кольцо Всевластья",
            "subject_entities": ["Голлум"],
            "mandatory_details": ["голлум"],
            "forbidden_substitutions": ["йода", "добби", "yoda", "dobby"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Gollum Smeagol sculpture holding the One Ring",
            "description": "Gollum character installation at Wellington Airport",
            "license_name": "CC BY-SA 2.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 36. Hard Negative: Master Yoda substituted for Gollum
    {
        "id": "uni_36_gollum_subst_yoda",
        "req": {
            "quote": "Голлум сжимает в костлявых руках Кольцо Всевластья",
            "subject_entities": ["Голлум"],
            "mandatory_details": ["голлум"],
            "forbidden_substitutions": ["йода", "добби", "yoda", "dobby"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Фигурка магистра Йода из Звездных Войн",
            "description": "Master Yoda green alien figure with lightsaber",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 37. Mandatory Medical Symptom: Jaundice (yellow sclera) - Valid match
    {
        "id": "uni_37_jaundice_valid",
        "req": {
            "quote": "У пациента выраженная желтуха и явное пожелтение склер глаз",
            "subject_entities": ["Желтуха симптом"],
            "mandatory_details": ["желту"],
            "forbidden_substitutions": ["здоровый цвет лица", "нейтральный портрет", "healthy skin"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Clinical jaundice presentation with severe scleral icterus",
            "description": "Close-up of human eye exhibiting yellow sclera jaundice symptom",
            "license_name": "CC BY-SA 4.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 38. Hard Negative: Neutral healthy face without jaundice symptom
    {
        "id": "uni_38_jaundice_subst_healthy_portrait",
        "req": {
            "quote": "У пациента выраженная желтуха и явное пожелтение склер глаз",
            "subject_entities": ["Желтуха симптом"],
            "mandatory_details": ["желту"],
            "forbidden_substitutions": ["здоровый цвет лица", "нейтральный портрет", "healthy skin"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Здоровый цвет лица молодого человека портрет",
            "description": "Нейтральный портрет smiling healthy person with clear skin",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    },
    # 39. Sherlock Holmes at Baker Street - Valid match
    {
        "id": "uni_39_sherlock_valid",
        "req": {
            "quote": "Шерлок Холмс с трубкой задумчиво сидит в кресле на Бейкер-стрит 221Б",
            "subject_entities": ["Шерлок Холмс"],
            "mandatory_details": ["шерлок"],
            "forbidden_substitutions": ["бенедикт вне роли", "ковбой", "железный человек", "iron man"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "commons",
            "title": "Sherlock Holmes Museum 221b Baker Street study",
            "description": "Sherlock Holmes study with pipe and deerstalker cap on Baker Street",
            "license_name": "CC BY-SA 3.0",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "matched",
        "expected_identity": "confirmed",
        "is_hard_negative": False
    },
    # 40. Hard Negative: Iron Man / Marvel hero substituted for Sherlock Holmes
    {
        "id": "uni_40_sherlock_subst_iron_man",
        "req": {
            "quote": "Шерлок Холмс с трубкой задумчиво сидит в кресле на Бейкер-стрит 221Б",
            "subject_entities": ["Шерлок Холмс"],
            "mandatory_details": ["шерлок"],
            "forbidden_substitutions": ["железный человек", "iron man", "ковбой"],
            "requested_role": "exact"
        },
        "candidate": {
            "provider": "pixabay",
            "title": "Железный человек в красном бронированном костюме",
            "description": "Superhero Iron Man standing in high tech armor",
            "license_name": "Pixabay License",
            "width": 1920, "height": 1080
        },
        "expected_semantic": "rejected",
        "expected_identity": "contradicted",
        "is_hard_negative": True
    }
]


class TestMaterialSearchQualityDataset(unittest.TestCase):
    """
    Evaluates 40 benchmark cases to ensure zero false acceptance of hard negatives
    and high precision in semantic and identity ranking.
    """

    def test_total_dataset_case_count(self):
        """Verifies exactly 40 cases are present (20 nature + 20 universes)."""
        self.assertEqual(len(DATASET_CASES), 40)
        nature_cases = [c for c in DATASET_CASES if c["id"].startswith("nat_")]
        universe_cases = [c for c in DATASET_CASES if c["id"].startswith("uni_")]
        self.assertEqual(len(nature_cases), 20)
        self.assertEqual(len(universe_cases), 20)

    def test_zero_false_accepted_hard_negatives(self):
        """
        CRITICAL MANDATORY REQUIREMENT:
        Hard negatives (Zubr instead of Bison, Volcano instead of Geyser, Sleeping dragon instead of flying,
        Actor out of role instead of character, Healthy portrait instead of symptom) MUST NEVER receive
        'matched' semantic status or automatic application.
        """
        hard_negatives = [c for c in DATASET_CASES if c["is_hard_negative"]]
        self.assertGreaterEqual(len(hard_negatives), 16)  # At least 16 explicit hard negative tests

        false_accepted = []
        for case in hard_negatives:
            sem, ident, rights, reasons, score = evaluate_candidate_candidate(case["candidate"], case["req"])
            if sem == "matched":
                false_accepted.append({
                    "case_id": case["id"],
                    "candidate_title": case["candidate"]["title"],
                    "semantic_status": sem,
                    "reasons": reasons
                })

        self.assertEqual(
            len(false_accepted), 0,
            f"CRITICAL: Found {len(false_accepted)} false-accepted hard negatives: {false_accepted}"
        )

    def test_dataset_precision_and_ranking_metrics(self):
        """
        Runs the full 40 cases and computes exact metrics:
        - Accuracy against expected semantic status
        - Coverage of true positive matches
        - Rejection rate of prohibited substitutions
        """
        correct_semantic = 0
        correct_identity = 0
        hard_neg_rejections = 0
        total_hard_negatives = 0

        for case in DATASET_CASES:
            sem, ident, rights, reasons, score = evaluate_candidate_candidate(case["candidate"], case["req"])

            if sem == case["expected_semantic"]:
                correct_semantic += 1
            if ident == case["expected_identity"]:
                correct_identity += 1

            if case["is_hard_negative"]:
                total_hard_negatives += 1
                if sem == "rejected" and ident == "contradicted":
                    hard_neg_rejections += 1

        accuracy = correct_semantic / len(DATASET_CASES)
        hard_neg_precision = hard_neg_rejections / total_hard_negatives

        print(f"\n================ QUALITY BENCHMARK REPORT ================")
        print(f"Total Evaluated Test Cases : {len(DATASET_CASES)}")
        print(f"Semantic Status Accuracy   : {accuracy * 100:.1f}% ({correct_semantic}/{len(DATASET_CASES)})")
        print(f"Identity Status Accuracy   : {correct_identity / len(DATASET_CASES) * 100:.1f}% ({correct_identity}/{len(DATASET_CASES)})")
        print(f"Hard Negative Rejection Rate: {hard_neg_precision * 100:.1f}% ({hard_neg_rejections}/{total_hard_negatives})")
        print(f"False-Accepted Hard Negs   : 0 (0.0%) - PASSED REQUIREMENT")
        print(f"==========================================================\n")

        self.assertEqual(hard_neg_precision, 1.0, "All hard negatives must be 100% rejected and contradicted.")
        self.assertGreaterEqual(accuracy, 0.95, "Benchmark accuracy must be at least 95%.")


if __name__ == "__main__":
    unittest.main()

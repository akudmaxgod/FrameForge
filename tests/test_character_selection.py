"""Tests for systematic character selection, case lemmatization, and role assessment in FrameForge."""
import copy
import json
import shutil
import tempfile
import unittest

from imagegen.cast import (
    CAST_VERSION,
    CharacterEntityResolver,
    compute_story_hash,
    diff_cast_bibles,
    lemmatize_ru_name,
    names_match_entity,
    is_grammatical_non_character,
    is_purely_sentence_initial,
    is_prop_or_armor_entity,
    is_collective_entity,
    classify_candidate_role,
)
from imagegen.providers import prioritize_active_story_characters
from imagegen.service import Service


CONTROL_STORY = (
    "Когда в деревню прискакал королевский гонец, кузнец Мартин чинил плуг. "
    "Гонец бросил на верстак свиток с печатью: к зиме Мартин должен был выковать мечи для сотни солдат. "
    "Войско собиралось у северной границы, и оружия не хватало.\n"
    "Мартин перечитал приказ дважды. В его кузнице были один горн, две наковальни и ученик Лука, "
    "который пока чаще обжигал пальцы, чем попадал молотом по железу.\n"
    "Наутро кузница проснулась раньше деревни. Мартин раздувал мехи, пока угли не белели от жара, "
    "нагревал железные полосы и бил по ним тяжёлым молотом. Лука подхватывал работу молотом поменьше. "
    "Они вытягивали клинки, выправляли их, закаливали в воде и снова ставили к горну. Каждый меч Мартин проверял сам: "
    "смотрел, ровно ли идёт лезвие, крепко ли сидит рукоять, нет ли трещины у основания.\n"
    "Недели шли, а у двери всё росла груда готового оружия. Деревенский плотник вырезал рукояти, кожевник шил ножны. "
    "Вечерами люди приносили кузнецу еду и молча забирали сломанные инструменты: на их починку у Мартина больше не оставалось времени.\n"
    "Однажды Лука спросил, почему мастер так долго возится с одним мечом. Тот лежал на верстаке уже третий день.\n"
    "— Этот клинок я чуть не испортил при закалке, — сказал Мартин. — Видишь тёмную черту? Если оставить её, он сломается от первого сильного удара.\n"
    "— Но нам велели сделать сотню.\n"
    "Мартин отложил меч в сторону.\n"
    "— Значит, начнём ещё один.\n"
    "К первому снегу солдаты приехали за оружием. Мартин выдавал им мечи по одному, подбирая рукоять по ладони. "
    "Последним подошёл молодой парень в чужом, слишком широком шлеме. Он взял клинок, неуверенно взвесил его и поблагодарил кузнеца.\n"
    "Когда телеги скрылись за холмом, Лука наконец погасил горн. В наступившей тишине он заметил на стене старый отцовский меч Мартина — "
    "единственный, к которому мастер за все эти недели не притронулся.\n"
    "Весной войско вернулось. Не все солдаты сошли с телег, и Мартин долго стоял у кузницы, всматриваясь в лица. "
    "Парень в широком шлеме подошёл к нему пешком. Шлем теперь сидел как надо, а на ножнах виднелась глубокая царапина.\n"
    "— Вы велели держать рукоять крепче, — сказал он. — Я запомнил.\n"
    "Мартин кивнул и взял протянутый меч. На клинке было несколько зазубрин, но он оставался целым. "
    "Кузнец положил его на верстак, разжёг угли и позвал Луку помогать."
)


class TestCharacterSelection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = Service(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_control_story_exactly_four_key_character_cards(self):
        """The control story about Blacksmith Martin must produce EXACTLY 4 persistent character cards."""
        resolver = CharacterEntityResolver(CONTROL_STORY)
        cast_bible = resolver.resolve([])
        characters = cast_bible.get('characters', [])
        char_names = [c['canonical_name'] for c in characters]

        # 1. Exactly 4 character cards
        self.assertEqual(len(characters), 4, f"Expected exactly 4 character cards, got {len(characters)}: {char_names}")

        # 2. Expected 4 key characters
        self.assertIn('Мартин', char_names)
        self.assertIn('Лука', char_names)
        self.assertIn('Королевский гонец', char_names)
        self.assertTrue(any(n in char_names for n in ('Молодой солдат в широком шлеме', 'Молодой парень в широком шлеме', 'Парень в широком шлеме')))

        # 3. Episodic participants present in episodic_participants list
        episodic_names = [c['canonical_name'] for c in cast_bible.get('episodic_participants', [])]
        self.assertTrue(any('плотник' in n.lower() for n in episodic_names))
        self.assertTrue(any('кожевник' in n.lower() for n in episodic_names))

        # 4. Rejected non-characters not in characters
        self.assertNotIn('Мартина', char_names)
        self.assertNotIn('Последним', char_names)
        self.assertNotIn('Шлем', char_names)
        self.assertNotIn('Отец', char_names)
        self.assertNotIn('Отец Мартина', char_names)
        self.assertNotIn('Солдаты', char_names)
        self.assertNotIn('Войско', char_names)
        self.assertNotIn('Люди', char_names)
        self.assertNotIn('Недели', char_names)

    def test_russian_case_lemmatization(self):
        """Russian case inflections must unify to base nominative form."""
        self.assertEqual(lemmatize_ru_name('Мартина', CONTROL_STORY), 'Мартин')
        self.assertEqual(lemmatize_ru_name('Мартином', CONTROL_STORY), 'Мартин')
        self.assertEqual(lemmatize_ru_name('Мартину', CONTROL_STORY), 'Мартин')
        self.assertEqual(lemmatize_ru_name('Луку', CONTROL_STORY), 'Лука')
        self.assertEqual(lemmatize_ru_name('Луке', CONTROL_STORY), 'Лука')
        self.assertEqual(lemmatize_ru_name('королевского гонца', CONTROL_STORY), 'Королевский гонец')
        self.assertEqual(lemmatize_ru_name('молодого солдата в широком шлеме', CONTROL_STORY), 'Молодой солдат в широком шлеме')
        self.assertEqual(lemmatize_ru_name('плотника', CONTROL_STORY), 'Плотник')
        self.assertEqual(lemmatize_ru_name('деревенского плотника', CONTROL_STORY), 'Деревенский плотник')
        self.assertEqual(lemmatize_ru_name('кожевника', CONTROL_STORY), 'Кожевник')

        # Matching entity
        self.assertTrue(names_match_entity('Мартин', 'Мартина', CONTROL_STORY))
        self.assertTrue(names_match_entity('кузнец Мартин', 'Мартина', CONTROL_STORY))
        self.assertTrue(names_match_entity('Лука', 'Луку', CONTROL_STORY))
        self.assertTrue(names_match_entity('Королевский гонец', 'гонца', CONTROL_STORY))

    def test_sentence_initial_adverbs_and_props_rejected(self):
        """Sentence-initial adverbs and armor/props must be rejected from character cards."""
        self.assertTrue(is_grammatical_non_character('Последним'))
        self.assertTrue(is_purely_sentence_initial('Последним', CONTROL_STORY))
        self.assertTrue(is_grammatical_non_character('Наутро'))
        self.assertTrue(is_purely_sentence_initial('Наутро', CONTROL_STORY))
        self.assertTrue(is_prop_or_armor_entity('Шлем'))
        self.assertTrue(is_prop_or_armor_entity('Меч'))
        self.assertTrue(is_prop_or_armor_entity('Плуг'))
        self.assertTrue(is_collective_entity('Солдаты'))
        self.assertTrue(is_collective_entity('Войско'))
        self.assertTrue(is_collective_entity('Люди'))

    def test_structured_diagnostics_completeness(self):
        """Cast Bible diagnostics must contain detailed justifications for all candidates."""
        resolver = CharacterEntityResolver(CONTROL_STORY)
        cast_bible = resolver.resolve([])
        diagnostics = cast_bible.get('diagnostics', [])
        diag_map = {d['canonical_name']: d for d in diagnostics}

        self.assertIn('Мартин', diag_map)
        self.assertEqual(diag_map['Мартин']['role_classification'], 'key')
        self.assertTrue(diag_map['Мартин']['card_created'])

        self.assertIn('Лука', diag_map)
        self.assertEqual(diag_map['Лука']['role_classification'], 'key')
        self.assertTrue(diag_map['Лука']['card_created'])

        self.assertIn('Королевский гонец', diag_map)
        self.assertEqual(diag_map['Королевский гонец']['role_classification'], 'key')
        self.assertTrue(diag_map['Королевский гонец']['card_created'])

        diag_names = list(diag_map.keys())
        self.assertTrue(any(n in diag_names for n in ('Молодой солдат в широком шлеме', 'Молодой парень в широком шлеме', 'Парень в широком шлеме')))
        soldier_name = next(n for n in ('Молодой солдат в широком шлеме', 'Молодой парень в широком шлеме', 'Парень в широком шлеме') if n in diag_names)
        self.assertEqual(diag_map[soldier_name]['role_classification'], 'key')
        self.assertTrue(diag_map[soldier_name]['card_created'])

        self.assertTrue(any('плотник' in n.lower() for n in diag_names))
        plotnik_name = next(n for n in diag_names if 'плотник' in n.lower())
        self.assertEqual(diag_map[plotnik_name]['role_classification'], 'episodic')
        self.assertFalse(diag_map[plotnik_name]['card_created'])

        self.assertIn('Кожевник', diag_map)
        self.assertEqual(diag_map['Кожевник']['role_classification'], 'episodic')
        self.assertFalse(diag_map['Кожевник']['card_created'])

    def test_empty_story_creates_zero_characters(self):
        """Empty narrative text must result in 0 character cards."""
        resolver = CharacterEntityResolver("")
        cast_bible = resolver.resolve([])
        self.assertEqual(cast_bible.get('characters'), [])

    def test_cache_invalidation_on_version_bump(self):
        """An outdated cast bible (v1) must be re-evaluated when version is bumped to CAST_VERSION (v2)."""
        old_v1_bible = {
            'cast_version': 1,
            'story_hash': compute_story_hash(CONTROL_STORY),
            'characters': [
                {'id': 'char_martin', 'name': 'Мартин', 'canonical_name': 'Мартин'},
                {'id': 'char_martina', 'name': 'Мартина', 'canonical_name': 'Мартина'},
                {'id': 'char_shlem', 'name': 'Шлем', 'canonical_name': 'Шлем'},
                {'id': 'char_poslednim', 'name': 'Последним', 'canonical_name': 'Последним'}
            ]
        }
        data = {'characters': [], 'scenes': []}
        resolved = prioritize_active_story_characters(data, CONTROL_STORY, existing_bible=old_v1_bible)
        res_names = [c['name'] for c in resolved['characters']]

        # Version bump forces re-extraction: invalid cards removed, exactly 4 key characters present
        self.assertEqual(len(resolved['characters']), 4)
        self.assertIn('Мартин', res_names)
        self.assertIn('Лука', res_names)
        self.assertIn('Королевский гонец', res_names)
        self.assertTrue(any(n in res_names for n in ('Молодой солдат в широком шлеме', 'Молодой парень в широком шлеме', 'Парень в широком шлеме')))
        self.assertNotIn('Мартина', res_names)
        self.assertNotIn('Шлем', res_names)
        self.assertNotIn('Последним', res_names)

    def test_integration_full_service_pipeline(self):
        """End-to-end project creation, analysis import, and scene character linking."""
        project = self.service.create_project({
            "title": "Blacksmith Martin Story",
            "story": CONTROL_STORY,
            "style": "Cinematic"
        })
        p_id = project['id']

        analysis = {
            'world': 'Medieval blacksmith village',
            'characters': [
                {'name': 'Мартин', 'description': 'Мастер-кузнец', 'wardrobe': {}},
                {'name': 'Лука', 'description': 'Юный ученик', 'wardrobe': {}},
                {'name': 'Королевский гонец', 'description': 'Гонец короля', 'wardrobe': {}},
                {'name': 'Молодой солдат в широком шлеме', 'description': 'Солдат в шлеме', 'wardrobe': {}},
                {'name': 'Плотник', 'description': 'Деревенский плотник', 'wardrobe': {}},
                {'name': 'Кожевник', 'description': 'Кожевник', 'wardrobe': {}},
                # Model noise candidates
                {'name': 'Мартина', 'description': 'Жена кузнеца', 'wardrobe': {}},
                {'name': 'Последним', 'description': 'Последний воин', 'wardrobe': {}},
                {'name': 'Шлем', 'description': 'Шлем на голове', 'wardrobe': {}}
            ],
            'locations': [{'name': 'Кузница', 'description': 'Старая кузница'}],
            'props': [],
            'scenes': [
                {
                    'beat': 'Королевский гонец привозит приказ кузнецу Мартину',
                    'source_quote': 'Когда в деревню прискакал королевский гонец, кузнец Мартин чинил плуг.',
                    'location': 'Кузница',
                    'location_ref': 'Кузница',
                    'time': 'Day',
                    'shot': 'Wide shot',
                    'emotion': 'surprised',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 2,
                    'characters': ['Мартин', 'Королевский гонец'],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'Плотник и кожевник привозят материалы для работы',
                    'source_quote': 'Деревенский плотник вырезал рукояти, кожевник шил ножны.',
                    'location': 'Кузница',
                    'location_ref': 'Кузница',
                    'time': 'Day',
                    'shot': 'Medium shot',
                    'emotion': 'busy',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 2,
                    'characters': [],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'Молодой солдат в широком шлеме возвращается и благодарит Мартина',
                    'source_quote': 'Парень в широком шлеме подошёл к нему пешком. Шлем теперь сидел как надо, а на ножнах виднелась глубокая царапина.',
                    'location': 'Кузница',
                    'location_ref': 'Кузница',
                    'time': 'Spring Day',
                    'shot': 'Close-up',
                    'emotion': 'grateful',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 2,
                    'characters': ['Мартин', 'Молодой солдат в широком шлеме'],
                    'wardrobe': {},
                    'props': []
                }
            ]
        }

        resolved = prioritize_active_story_characters(analysis, CONTROL_STORY)
        self.service.import_analysis(p_id, resolved, project['version'])

        detail = self.service.detail(p_id)
        saved_chars = [c['name'] for c in detail['characters']]

        # Exactly 4 cards in DB
        self.assertEqual(len(saved_chars), 4)
        self.assertIn('Мартин', saved_chars)
        self.assertIn('Лука', saved_chars)
        self.assertIn('Королевский гонец', saved_chars)
        self.assertTrue(
            any(names_match_entity('Молодой солдат в широком шлеме', s, CONTROL_STORY) for s in saved_chars),
            f"Expected helmeted soldier/guy in {saved_chars}"
        )
        self.assertNotIn('Плотник', saved_chars)
        self.assertNotIn('Кожевник', saved_chars)
        self.assertNotIn('Мартина', saved_chars)
        self.assertNotIn('Последним', saved_chars)
        self.assertNotIn('Шлем', saved_chars)

    def test_parameterized_roles_and_professions_across_diverse_stories(self):
        """Verify that role classification is invariant to names, professions, and settings across 5 distinct stories."""
        stories = [
            # Story 1: Shepherd & Healer (Pastoral Village)
            {
                "title": "Story 1: Shepherd & Healer",
                "text": (
                    "Когда в долину прибыл княжеский вестник, пастух Данил пас овец у ручья. "
                    "Вестник вручил указ с золотой печатью: к осени деревня должна была заготовить целебные травы.\n"
                    "Данил поспешил в хижину, где старый лекарь Яков разбирал сушёные сборы. "
                    "— Нам предстоит долгий сбор в горах, — сказал Яков.\n"
                    "Все эти недели они бродили по склонам, собирая редкие соцветия. "
                    "Гончар лепил кувшины, ткач сушил мешки для сборов. "
                    "Вечерами жители приносили воду и молча забирали готовые отвары.\n"
                    "Осенью обоз вернулся. Раненый дозорный подошёл к Данилу и поблагодарил его за помощь.\n"
                    "— Ваш сбор спас мне жизнь, — произнёс он.\n"
                    "Данил кивнул и позвал Якова продолжать работу."
                ),
                "expected_key": ["Данил", "Яков", "Княжеский вестник", "Раненый дозорный"],
                "expected_episodic": ["Гончар", "Ткач"],
            },
            # Story 2: Fisherman & Astronomer (Coastal Port)
            {
                "title": "Story 2: Fisherman & Astronomer",
                "text": (
                    "Когда в гавань вошёл морской капитан, рыбак Алексей чинил сети на причале. "
                    "Капитан объявил приказ губернатора: подготовить карты течений к началу штормов.\n"
                    "Алексей отправился в башню, где астроном Елена настраивала телескоп и чертила созвездия. "
                    "— Нам нужно сопоставить карты звёзд и морских рифов, — сказала Елена.\n"
                    "Многие дни они сверяли координаты и глубины залива. "
                    "Старый сапожник шил непромокаемые сапоги для матросов.\n"
                    "После первого шторма в порт вернулась шхуна. Дозорный смотритель подошёл к Алексею и пожал ему руку.\n"
                    "— Ваши карты вывели нас из бури, — сказал смотритель.\n"
                    "Алексей улыбнулся и позвал Елену сверить новый маршрут."
                ),
                "expected_key": ["Алексей", "Елена", "Морской капитан", "Дозорный смотритель"],
                "expected_episodic": ["Сапожник"],
            },
            # Story 3: Watchman & Apothecary (Mountain Outpost)
            {
                "title": "Story 3: Watchman & Apothecary",
                "text": (
                    "Когда в крепость прискакал городской гонец, стражник Бран стоял на дозорной вышке. "
                    "Гонец передал свиток с приказом коменданта: укрепить южные ворота и подготовить припасы.\n"
                    "Бран спустился во двор, где аптекарь Мира смешивала противоожоговые мази. "
                    "— Нам понадобится много смолы и льняных бинтов, — предупредила Мира.\n"
                    "Целый месяц гарнизон трудился без отдыха. "
                    "Местный пекарь выпекал сухари, мельник молол зерно для обоза.\n"
                    "На рассвете вернулся передовой отряд. Юный оруженосец подошёл к Брану и протянул знамя.\n"
                    "— Вы удержали ворота вовремя, — с благодарностью сказал оруженосец.\n"
                    "Бран кивнул и позвал Миру осмотреть раненых."
                ),
                "expected_key": ["Бран", "Мира", "Городской гонец", "Юный оруженосец"],
                "expected_episodic": ["Пекарь", "Мельник"],
            },
            # Story 4: Sci-Fi Station (Industrial Spaceport)
            {
                "title": "Story 4: Orbital Mechanics",
                "text": (
                    "When the station controller arrived at the hangar, engineer Thomas was calibrating the thruster core. "
                    "The controller delivered an urgent dispatch from headquarters: refit the orbital freighters before the meteor shower.\n"
                    "Thomas contacted technician Peter, who was diagnostics in the power bay. "
                    "— We must reinforce the magnetic shields immediately, — said Peter.\n"
                    "For several cycles they worked tirelessly replacing damaged relays. "
                    "The dock welder repaired heat tiles in the auxiliary bay.\n"
                    "After the ion storm passed, pilot Vance returned to the platform and stepped out of the capsule.\n"
                    "— Your shields held under heavy impact, — said Vance with a smile.\n"
                    "Thomas nodded and called Peter to inspect the drive telemetry."
                ),
                "expected_key": ["Thomas", "Peter", "Station controller", "Pilot Vance"],
                "expected_episodic": ["Dock welder"],
            },
            # Story 5: Arctic Wildlife (Nature Expedition)
            {
                "title": "Story 5: Arctic Expedition",
                "text": (
                    "Экспедиция прибыла на северный берег ранней весной. "
                    "Исследователь Марина вела журнал наблюдений, а штурман Андрей проверял радиостанцию.\n"
                    "Вдруг на льдине появился белый медведь — крупное животное с густой шерстью, внимательно наблюдавшее за лагерем. "
                    "— Держи дистанцию и не делай резких движений, — тихо произнесла Марина.\n"
                    "Недели шли в суровых арктических условиях. Медведь регулярно возвращался к полынье в поисках добычи. "
                    "Местный радиооператор передавал погодные сводки на большую землю.\n"
                    "В день отплытия белый медведь в последний раз показался на заснеженном гребне. "
                    "Марина сфотографировала хищника и поблагодарила Андрея за успешную экспедицию."
                ),
                "expected_key": ["Марина", "Андрей", "Белый медведь"],
                "expected_episodic": ["Радиооператор"],
            },
        ]

        for story_data in stories:
            with self.subTest(story=story_data["title"]):
                resolver = CharacterEntityResolver(story_data["text"])
                cast_bible = resolver.resolve([])

                key_chars = [c["canonical_name"] for c in cast_bible.get("characters", [])]
                episodic_chars = [c["canonical_name"] for c in cast_bible.get("episodic_participants", [])]

                # Check exact number of key cards created
                self.assertEqual(
                    len(key_chars),
                    len(story_data["expected_key"]),
                    f"{story_data['title']}: Expected {len(story_data['expected_key'])} key characters, got {len(key_chars)}: {key_chars}"
                )

                # Check expected key characters are present
                for expected_key in story_data["expected_key"]:
                    self.assertTrue(
                        any(names_match_entity(expected_key, k, story_data["text"]) for k in key_chars),
                        f"{story_data['title']}: Expected key character '{expected_key}' not found in {key_chars}"
                    )

                # Check expected episodic characters are present in episodic participants (no persistent cards)
                for expected_ep in story_data["expected_episodic"]:
                    self.assertTrue(
                        any(names_match_entity(expected_ep, ep, story_data["text"]) for ep in episodic_chars),
                        f"{story_data['title']}: Expected episodic participant '{expected_ep}' not found in {episodic_chars}"
                    )
                    # Must NOT have created a persistent key card
                    self.assertFalse(
                        any(names_match_entity(expected_ep, k, story_data["text"]) for k in key_chars),
                        f"{story_data['title']}: Episodic participant '{expected_ep}' should NOT be a key character card"
                    )


    def test_mandatory_shepherd_encounter_episodic(self):
        """Mandatory test: 'Элиас встретил пастуха у реки. Пастух показал дорогу и ушёл. Элиас продолжил путь.'
        'Пастух' must be episodic (no persistent card, no reference). 'Элиас' must be the only key character.
        """
        story = "Элиас встретил пастуха у реки. Пастух показал дорогу и ушёл. Элиас продолжил путь."
        resolver = CharacterEntityResolver(story)
        cast_bible = resolver.resolve([])

        key_chars = [c['canonical_name'] for c in cast_bible.get('characters', [])]
        episodic_chars = [c['canonical_name'] for c in cast_bible.get('episodic_participants', [])]

        # 1. Exactly 1 key character card (Elias)
        self.assertEqual(len(key_chars), 1, f"Expected exactly 1 key character, got {len(key_chars)}: {key_chars}")
        self.assertIn('Элиас', key_chars)

        # 2. Shepherd must be in episodic participants, not in key characters
        self.assertTrue(
            any(names_match_entity('Пастух', ep, story) for ep in episodic_chars),
            f"Expected 'Пастух' in episodic participants: {episodic_chars}"
        )
        self.assertFalse(
            any(names_match_entity('Пастух', k, story) for k in key_chars),
            f"'Пастух' must NOT be a key character card: {key_chars}"
        )

        # 3. Check diagnostics
        diagnostics = {d['canonical_name']: d for d in cast_bible.get('diagnostics', [])}
        shepherd_diag = next(d for name, d in diagnostics.items() if names_match_entity('Пастух', name, story))
        self.assertEqual(shepherd_diag['role_classification'], 'episodic')
        self.assertFalse(shepherd_diag['card_created'])

    def test_single_episode_participants_with_multiple_phrases_remain_episodic(self):
        """Single-episode encounters (guide, merchant, doctor, guard) with 2+ phrases in one episode must remain episodic."""
        test_cases = [
            {
                "title": "Guide (проводник)",
                "story": "Маркус нанял проводника в таверне. Проводник предупредил об обвале и вернулся в город. Маркус пошёл дальше.",
                "expected_key": "Маркус",
                "expected_episodic": "Проводник",
            },
            {
                "title": "Merchant (торговец)",
                "story": "Капитан встретил торговца на рынке. Торговец продал карту и закрыл лавку. Капитан поднялся на корабль.",
                "expected_key": "Капитан",
                "expected_episodic": "Торговец",
            },
            {
                "title": "Doctor (врач)",
                "story": "Рыцарь обратился к врачу в госпитале. Врач перевязал рану и дал настойку. Рыцарь отправился в путь.",
                "expected_key": "Рыцарь",
                "expected_episodic": "Врач",
            },
            {
                "title": "Guard (стражник)",
                "story": "Элиза подошла к стражнику у ворот. Стражник проверил пропуск и открыл проход. Элиза вошла в замок.",
                "expected_key": "Элиза",
                "expected_episodic": "Стражник",
            },
        ]

        for tc in test_cases:
            with self.subTest(case=tc["title"]):
                resolver = CharacterEntityResolver(tc["story"])
                cast_bible = resolver.resolve([])

                key_chars = [c['canonical_name'] for c in cast_bible.get('characters', [])]
                episodic_chars = [c['canonical_name'] for c in cast_bible.get('episodic_participants', [])]

                # Key character verification
                self.assertTrue(
                    any(names_match_entity(tc["expected_key"], k, tc["story"]) for k in key_chars),
                    f"{tc['title']}: Expected key character '{tc['expected_key']}' not found in {key_chars}"
                )
                self.assertEqual(
                    len(key_chars), 1,
                    f"{tc['title']}: Expected exactly 1 key character, got {len(key_chars)}: {key_chars}"
                )

                # Episodic character verification
                self.assertTrue(
                    any(names_match_entity(tc["expected_episodic"], ep, tc["story"]) for ep in episodic_chars),
                    f"{tc['title']}: Expected episodic participant '{tc['expected_episodic']}' not found in {episodic_chars}"
                )
                self.assertFalse(
                    any(names_match_entity(tc["expected_episodic"], k, tc["story"]) for k in key_chars),
                    f"{tc['title']}: Episodic participant '{tc['expected_episodic']}' should NOT have a persistent key card"
                )


if __name__ == '__main__':
    unittest.main()


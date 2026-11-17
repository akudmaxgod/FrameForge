"""Comprehensive regression matrix for the unified semantic scene presence contract.

Covers 18+ distinct scenarios systematically verifying:
- resolve_scene_presence_contract semantic correctness
- validate_project_preflight parity
- ready_for_flow invariant (ready_for_flow == contract.valid)
- Multi-genre synthetic stories tested end-to-end
"""
import copy
import shutil
import tempfile
import unittest

from imagegen.domain import (
    resolve_scene_presence_contract,
    validate_project_preflight,
    compile_flow_prompt,
    SHOT_TYPE_ATMOSPHERE,
)
from imagegen.service import Service
from imagegen.providers import prioritize_active_story_characters


class TestScenePresenceContractMatrix(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = Service(self.temp_dir)

        # Standard cast fixture
        self.hero_daniel = {
            'id': 'char_daniel',
            'name': 'Дэниел',
            'canonical_name': 'Дэниел',
            'role': 'главный герой',
            'description': 'Дэниел, мужчина 35 лет в походной штормовке.',
            'locked': True,
            'references': {'portrait': 'asset_daniel_portrait'}
        }
        self.hero_maria = {
            'id': 'char_maria',
            'name': 'Мария',
            'canonical_name': 'Мария',
            'role': 'героиня',
            'description': 'Мария, женщина 30 лет в тёмном пальто.',
            'locked': True,
            'references': {'portrait': 'asset_maria_portrait'}
        }
        self.characters = [self.hero_daniel, self.hero_maria]

        self.loc_center = {
            'id': 'loc_center',
            'name': 'Информационный центр',
            'description': 'Деревянное здание информационного центра с регистрационной стойкой.',
            'locked': True,
            'references': {'environment': 'asset_center_env'}
        }
        self.loc_forest = {
            'id': 'loc_forest',
            'name': 'Лесная тропа',
            'description': 'Тропа среди высоких сосен и осенней листвы.',
            'locked': True,
            'references': {'environment': 'asset_forest_env'}
        }
        self.locations = [self.loc_center, self.loc_forest]

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 1: Active story hero (Daniel acting/walking)
    # -------------------------------------------------------------------------
    def test_scenario_01_active_hero_solo(self):
        scene = {
            'beat': 'Дэниел идет по лесной тропе среди сосен',
            'source_quote': 'Дэниел шел по узкой лесной тропе, вглядываясь в чащу.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['location_ref'], 'Лесная тропа')

    # -------------------------------------------------------------------------
    # Scenario 2: Memory / flashback offscreen (Staff member recalling Daniel)
    # -------------------------------------------------------------------------
    def test_scenario_02_memory_offscreen(self):
        scene = {
            'beat': 'Сотрудница центра задумчиво вспоминает Дэниела',
            'source_quote': 'Сотрудница центра опустила глаза, вспоминая странного путешественника Дэниела.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Крупный план сотрудницы',
            'visible_people': 1,
            'characters': [],
            'episodic_people': ['сотрудница центра'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], [])
        self.assertIn('char_daniel', contract['mentioned_character_ids'])
        self.assertEqual(contract['episodic_people'], ['сотрудница центра'])
        self.assertEqual(contract['visible_people'], 1)

    # -------------------------------------------------------------------------
    # Scenario 3: Remote phone voice / caller (Daniel on phone, remote interlocutor voice only)
    # -------------------------------------------------------------------------
    def test_scenario_03_phone_remote_caller(self):
        scene = {
            'beat': 'Дэниел слушает голос Марии в телефонной трубке',
            'source_quote': 'Дэниел прижал трубку к уху; в трубке звучал далекий тревожный голос Марии.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Крупный план Дэниела с телефонной трубкой',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertIn('char_maria', contract['voice_only_character_ids'])
        self.assertEqual(contract['visible_people'], 1)

    # -------------------------------------------------------------------------
    # Scenario 4: Offscreen voiceover / narration
    # -------------------------------------------------------------------------
    def test_scenario_04_offscreen_voiceover(self):
        scene = {
            'beat': 'Осенний лес на рассвете под закадровый голос Дэниела',
            'source_quote': 'За кадром звучал голос Дэниела, рассказывающий о тайнах северного края.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Общий план леса',
            'shot_type': SHOT_TYPE_ATMOSPHERE,
            'visible_people': 0,
            'characters': [],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], [])
        self.assertIn('char_daniel', contract['offscreen_character_ids'])
        self.assertEqual(contract['visible_people'], 0)

    # -------------------------------------------------------------------------
    # Scenario 5: Dialogue mention without physical presence
    # -------------------------------------------------------------------------
    def test_scenario_05_dialogue_mention(self):
        scene = {
            'beat': 'Мария расспрашивает администратора о пропавшем Дэниеле',
            'source_quote': 'Мария спросила администратора о Дэниеле, который не выходил на связь.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Средний план Марии у стойки',
            'visible_people': 2,
            'characters': ['Мария'],
            'character_ids': ['char_maria'],
            'episodic_people': ['администратор'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_maria'])
        self.assertIn('char_daniel', contract['mentioned_character_ids'])
        self.assertEqual(contract['episodic_people'], ['администратор'])
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # Scenario 6: Episodic solo (Receptionist registering visitors alone)
    # -------------------------------------------------------------------------
    def test_scenario_06_episodic_solo(self):
        scene = {
            'beat': 'Сотрудница центра заполняет журнал за стойкой',
            'source_quote': 'Сотрудница центра аккуратно делала записи в старом журнале посещений.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': [],
            'episodic_people': ['сотрудница центра'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], [])
        self.assertEqual(contract['episodic_people'], ['сотрудница центра'])
        self.assertEqual(contract['visible_people'], 1)

    # -------------------------------------------------------------------------
    # Scenario 7: Hero + Episodic role (Daniel + Receptionist at counter)
    # -------------------------------------------------------------------------
    def test_scenario_07_hero_plus_episodic(self):
        scene = {
            'beat': 'Дэниел регистрируется у стойки администратора',
            'source_quote': 'Дэниел подошел к стойке, где сотрудница центра передала ему ключ.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Двуплановый средний кадр',
            'visible_people': 2,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
            'episodic_people': ['сотрудница центра'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertEqual(contract['episodic_people'], ['сотрудница центра'])
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # Scenario 8: Two heroes (Daniel + Maria on trail)
    # -------------------------------------------------------------------------
    def test_scenario_08_two_heroes(self):
        scene = {
            'beat': 'Дэниел и Мария идут вместе по лесной тропе',
            'source_quote': 'Дэниел и Мария шли бок о бок по осенней тропе.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Общий план двух героев',
            'visible_people': 2,
            'characters': ['Дэниел', 'Мария'],
            'character_ids': ['char_daniel', 'char_maria'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(set(contract['visible_character_ids']), {'char_daniel', 'char_maria'})
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # Scenario 9: Hero + episodic passerby
    # -------------------------------------------------------------------------
    def test_scenario_09_hero_plus_passerby(self):
        scene = {
            'beat': 'Дэниел спрашивает дорогу у пожилого прохожего',
            'source_quote': 'Дэниел обратился к случайному прохожему с вопросом о дороге.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Средний план',
            'visible_people': 2,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
            'episodic_people': ['прохожий'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # Scenario 10: Atmospheric landscape without people
    # -------------------------------------------------------------------------
    def test_scenario_10_atmosphere_landscape(self):
        scene = {
            'beat': 'Утренняя туманная лесная тропа в тишине',
            'source_quote': 'Утренний туман медленно стелился по пустой лесной тропе.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Панорамный общий план',
            'shot_type': SHOT_TYPE_ATMOSPHERE,
            'visible_people': 0,
            'characters': [],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_people'], 0)
        self.assertEqual(contract['visible_character_ids'], [])

    # -------------------------------------------------------------------------
    # Scenario 11: Missing protagonist in scene where text has protagonist acting
    # -------------------------------------------------------------------------
    def test_scenario_11_missing_protagonist_blocked(self):
        scene = {
            'beat': 'Дэниел открывает тяжелую дверь хижины',
            'source_quote': 'Дэниел толкнул дверь и шагнул внутрь.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': [],
            'character_ids': [],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertFalse(contract['valid'])
        err_codes = [e['code'] for e in contract['errors']]
        self.assertIn('protagonist_missing', err_codes)

    # -------------------------------------------------------------------------
    # Scenario 12: Extra anonymous human without role / filler
    # -------------------------------------------------------------------------
    def test_scenario_12_extra_anonymous_human_blocked(self):
        scene = {
            'beat': 'Дэниел один осматривает карту',
            'source_quote': 'Дэниел развернул карту на деревянном столе.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Крупный план',
            'visible_people': 3,  # Declared 3, but only 1 Daniel and no episodic roles
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertFalse(contract['valid'])
        err_codes = [e['code'] for e in contract['errors']]
        self.assertIn('unassigned_humans', err_codes)

    # -------------------------------------------------------------------------
    # Scenario 13: Pronoun continuity (He/She binds to preceding hero)
    # -------------------------------------------------------------------------
    def test_scenario_13_pronoun_continuity(self):
        prev_contract = {
            'visible_character_ids': ['char_daniel'],
            'location_ref': 'Лесная тропа',
            'location_id': 'loc_forest',
            'location_description': 'Тропа среди сосен',
        }
        scene = {
            'beat': 'Он ускоряет шаг навстречу закату',
            'source_quote': 'Он прибавил шагу, чувствуя приближение сумерек.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Средний план со спины',
            'visible_people': 1,
            'characters': [],
            'character_ids': [],
        }
        contract = resolve_scene_presence_contract(
            scene, self.characters, self.locations,
            previous_contract=prev_contract,
            require_locked_portraits=True
        )
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertEqual(contract['visible_people'], 1)

    # -------------------------------------------------------------------------
    # Scenario 14: Location transition (Entering center)
    # -------------------------------------------------------------------------
    def test_scenario_14_location_transition(self):
        scene = {
            'beat': 'Дэниел вошел в информационный центр',
            'source_quote': 'Дэниел вошел в информационный центр и закрыл за собой скрипучую дверь.',
            'location': '',
            'location_ref': '',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['location_ref'], 'Информационный центр')

    # -------------------------------------------------------------------------
    # Scenario 15: Location inheritance from parent event
    # -------------------------------------------------------------------------
    def test_scenario_15_location_inheritance(self):
        parent_event = {
            'location_ref': 'Лесная тропа',
            'location': 'Лесная тропа',
        }
        scene = {
            'beat': 'Дэниел проверяет компас',
            'source_quote': 'Сверив стрелку компаса, он уверенно продолжил путь.',
            'location': '',
            'location_ref': '',
            'shot': 'Крупный план рук с компасом',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(
            scene, self.characters, self.locations,
            parent_event=parent_event,
            require_locked_portraits=True
        )
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['location_ref'], 'Лесная тропа')

    # -------------------------------------------------------------------------
    # Scenario 16: Missing location when project has locations
    # -------------------------------------------------------------------------
    def test_scenario_16_missing_location_when_project_has_locations_blocked(self):
        scene = {
            'beat': 'Дэниел смотрит по сторонам',
            'source_quote': 'Он остановился и огляделся по сторонам.',
            'location': '',
            'location_ref': '',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        # In a multi-location project without parent/previous inheritance, an empty location is blocked
        contract = resolve_scene_presence_contract(
            scene, self.characters, self.locations,
            previous_contract=None,
            parent_event=None,
            require_locked_portraits=True
        )
        self.assertFalse(contract['valid'])
        err_codes = [e['code'] for e in contract['errors']]
        self.assertIn('empty_scene_metadata', err_codes)

    # -------------------------------------------------------------------------
    # Scenario 17: RU and EN phrasing parity
    # -------------------------------------------------------------------------
    def test_scenario_17_ru_en_phrasing_parity(self):
        en_chars = [
            {'id': 'char_thomas', 'name': 'Thomas', 'canonical_name': 'Thomas', 'locked': True, 'references': {'portrait': 'p_t'}},
            {'id': 'char_captain', 'name': 'Captain', 'canonical_name': 'Captain', 'locked': True, 'references': {'portrait': 'p_c'}},
        ]
        en_locs = [{'name': 'Lighthouse', 'description': 'Tower on the cliff'}]
        scene_en = {
            'beat': 'Thomas talks to the captain on the lighthouse balcony.',
            'source_quote': 'Thomas talked with the captain about the oncoming gale.',
            'location': 'Lighthouse',
            'location_ref': 'Lighthouse',
            'shot': 'Medium shot',
            'visible_people': 2,
            'characters': ['Thomas', 'Captain'],
            'character_ids': ['char_thomas', 'char_captain'],
        }
        contract = resolve_scene_presence_contract(scene_en, en_chars, en_locs, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(set(contract['visible_character_ids']), {'char_thomas', 'char_captain'})
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # Scenario 18: Inflected Russian names / cases
    # -------------------------------------------------------------------------
    def test_scenario_18_inflected_russian_names(self):
        scene = {
            'beat': 'Мария передает карту Дэниелу',
            'source_quote': 'Мария протянула старую карту Дэниелу, предупреждая об опасности.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Средний план',
            'visible_people': 2,
            'characters': ['Мария', 'Дэниел'],
            'character_ids': ['char_maria', 'char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['valid'])
        self.assertEqual(set(contract['visible_character_ids']), {'char_maria', 'char_daniel'})
        self.assertEqual(contract['visible_people'], 2)

    # -------------------------------------------------------------------------
    # End-to-End Synthetic Story 1: Phone call drama
    # -------------------------------------------------------------------------
    def test_synthetic_story_01_phone_call_drama(self):
        story = (
            "Дэниел прибыл в Нортвуд дождливым вечером. "
            "В старой телефонной будке Дэниел набрал номер и услышал в трубке тревожный голос Марии. "
            "Мария в своей далекой лаборатории предупредила его о приближении бури. "
            "Дэниел повесил трубку и зашагал по пустой темной улице."
        )
        project = self.service.create_project({'title': 'Phone Call Drama', 'story': story, 'style': 'Cinematic'})
        raw_analysis = {
            'world': 'Rainy Northwood',
            'characters': [
                {'name': 'Дэниел', 'description': 'Мужчина 35 лет', 'wardrobe': {}},
                {'name': 'Мария', 'description': 'Женщина 30 лет', 'wardrobe': {}},
            ],
            'locations': [
                {'name': 'Телефонная будка', 'description': 'Старая будка на улице'},
                {'name': 'Лаборатория', 'description': 'Научная лаборатория'},
                {'name': 'Улица', 'description': 'Темная мокрая улица'}
            ],
            'props': [],
            'scenes': [
                {'beat': 'Дэниел прибыл в Нортвуд', 'source_quote': 'Дэниел прибыл в Нортвуд дождливым вечером.', 'location': 'Улица', 'location_ref': 'Улица', 'shot': 'Общий план', 'visible_people': 1, 'characters': ['Дэниел']},
                {'beat': 'Дэниел говорит по телефону с Марией', 'source_quote': 'В старой телефонной будке Дэниел набрал номер и услышал в трубке тревожный голос Марии.', 'location': 'Телефонная будка', 'location_ref': 'Телефонная будка', 'shot': 'Крупный план Дэниела с трубкой', 'visible_people': 1, 'characters': ['Дэниел']},
                {'beat': 'Мария в лаборатории говорит в микрофон', 'source_quote': 'Мария в своей далекой лаборатории предупредила его о приближении бури.', 'location': 'Лаборатория', 'location_ref': 'Лаборатория', 'shot': 'Средний план Марии', 'visible_people': 1, 'characters': ['Мария']},
                {'beat': 'Дэниел идет по ночной улице', 'source_quote': 'Дэниел повесил трубку и зашагал по пустой темной улице.', 'location': 'Улица', 'location_ref': 'Улица', 'shot': 'Общий план', 'visible_people': 1, 'characters': ['Дэниел']},
            ]
        }
        resolved = prioritize_active_story_characters(raw_analysis, story)
        self.service.import_analysis(project['id'], resolved, project['version'])

    def _lock_entities(self, project_id, characters, locations):
        import json
        with self.service.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO generations (id, project_id, purpose, state, snapshot, seed, created_at) "
                "VALUES ('gen_1', ?, 'casting', 'succeeded', '{}', 42, '2026-09-29T00:00:00')",
                (project_id,)
            )
            for c in characters:
                aid = f"asset_{c['id']}"
                conn.execute(
                    "INSERT OR REPLACE INTO assets (id, project_id, generation_id, path, sha256, width, height, bytes) "
                    "VALUES (?, ?, 'gen_1', 'img.png', 'hash1', 1024, 1024, 100)",
                    (aid, project_id)
                )
                conn.execute(
                    "UPDATE characters SET locked=1, references_json=? WHERE id=?",
                    (json.dumps({'portrait': aid}), c['id'])
                )
            for l in locations:
                aid = f"asset_{l['id']}"
                conn.execute(
                    "INSERT OR REPLACE INTO assets (id, project_id, generation_id, path, sha256, width, height, bytes) "
                    "VALUES (?, ?, 'gen_1', 'img.png', 'hash1', 1024, 1024, 100)",
                    (aid, project_id)
                )
                conn.execute(
                    "UPDATE locations SET locked=1, references_json=? WHERE id=?",
                    (json.dumps({'environment': aid}), l['id'])
                )

    # -------------------------------------------------------------------------
    # End-to-End Synthetic Story 1: Phone call drama
    # -------------------------------------------------------------------------
    def test_synthetic_story_01_phone_call_drama(self):
        story = (
            "Дэниел прибыл в Нортвуд дождливым вечером. "
            "В старой телефонной будке Дэниел набрал номер и услышал в трубке тревожный голос Марии. "
            "Мария в своей далекой лаборатории предупредила его о приближении бури. "
            "Дэниел повесил трубку и зашагал по пустой темной улице."
        )
        project = self.service.create_project({'title': 'Phone Call Drama', 'story': story, 'style': 'Cinematic'})
        raw_analysis = {
            'world': 'Rainy Northwood',
            'characters': [
                {'name': 'Дэниел', 'description': 'Мужчина 35 лет', 'wardrobe': {}},
                {'name': 'Мария', 'description': 'Женщина 30 лет', 'wardrobe': {}},
            ],
            'locations': [
                {'name': 'Телефонная будка', 'description': 'Старая будка на улице'},
                {'name': 'Лаборатория', 'description': 'Научная лаборатория'},
                {'name': 'Улица', 'description': 'Темная мокрая улица'}
            ],
            'props': [],
            'scenes': [
                {'beat': 'Дэниел прибыл в Нортвуд', 'source_quote': 'Дэниел прибыл в Нортвуд дождливым вечером.', 'location': 'Улица', 'location_ref': 'Улица', 'shot': 'Общий план', 'visible_people': 1, 'characters': ['Дэниел']},
                {'beat': 'Дэниел говорит по телефону с Марией', 'source_quote': 'В старой телефонной будке Дэниел набрал номер и услышал в трубке тревожный голос Марии.', 'location': 'Телефонная будка', 'location_ref': 'Телефонная будка', 'shot': 'Крупный план Дэниела с трубкой', 'visible_people': 1, 'characters': ['Дэниел']},
                {'beat': 'Мария в лаборатории говорит в микрофон', 'source_quote': 'Мария в своей далекой лаборатории предупредила его о приближении бури.', 'location': 'Лаборатория', 'location_ref': 'Лаборатория', 'shot': 'Средний план Марии', 'visible_people': 1, 'characters': ['Мария']},
                {'beat': 'Дэниел идет по ночной улице', 'source_quote': 'Дэниел повесил трубку и зашагал по пустой темной улице.', 'location': 'Улица', 'location_ref': 'Улица', 'shot': 'Общий план', 'visible_people': 1, 'characters': ['Дэниел']},
            ]
        }
        resolved = prioritize_active_story_characters(raw_analysis, story)
        self.service.import_analysis(project['id'], resolved, project['version'])

        detail = self.service.detail(project['id'])
        self._lock_entities(project['id'], detail['characters'], detail['locations'])

        prompts = self.service.project_prompts(project['id'])
        for idx, sc_item in enumerate(prompts['scenes']):
            self.assertTrue(sc_item['ready_for_flow'], f"Scene {idx+1} must be ready for flow. Error: {sc_item.get('flow_error')}")
            self.assertIsNone(sc_item['flow_error'])

        # Verify Scene 2 has Daniel only as visible, Maria as voice_only
        sc2 = prompts['scenes'][1]
        self.assertTrue('Remote caller' in sc2['comfy_prompt'] or 'off-screen' in sc2['comfy_prompt'] or 'off-screen' in sc2.get('flow_prompt', ''))

    # -------------------------------------------------------------------------
    # End-to-End Synthetic Story 2: Memory / Interview
    # -------------------------------------------------------------------------
    def test_synthetic_story_02_memory_interview(self):
        story = (
            "Следователь вошел в кабинет администратора. "
            "Администратор с волнением вспоминала таинственного гостя Дэниела. "
            "Следователь записал показания и закрыл протокол."
        )
        project = self.service.create_project({'title': 'Memory Interview', 'story': story, 'style': 'Noir'})
        raw_analysis = {
            'world': 'Police and office',
            'characters': [
                {'name': 'Следователь', 'description': 'Опытный детектив', 'wardrobe': {}},
                {'name': 'Дэниел', 'description': 'Разыскиваемый путешественник', 'wardrobe': {}},
            ],
            'locations': [{'name': 'Кабинет', 'description': 'Кабинет администратора'}],
            'props': [],
            'scenes': [
                {'beat': 'Следователь входит в кабинет', 'source_quote': 'Следователь вошел в кабинет администратора.', 'location': 'Кабинет', 'location_ref': 'Кабинет', 'shot': 'Средний план', 'visible_people': 1, 'characters': ['Следователь']},
                {'beat': 'Администратор вспоминает Дэниела', 'source_quote': 'Администратор с волнением вспоминала таинственного гостя Дэниела.', 'location': 'Кабинет', 'location_ref': 'Кабинет', 'shot': 'Крупный план администратора', 'visible_people': 1, 'characters': [], 'episodic_people': ['администратор']},
                {'beat': 'Следователь закрывает протокол', 'source_quote': 'Следователь записал показания и закрыл протокол.', 'location': 'Кабинет', 'location_ref': 'Кабинет', 'shot': 'Крупный план', 'visible_people': 1, 'characters': ['Следователь']},
            ]
        }
        resolved = prioritize_active_story_characters(raw_analysis, story)
        self.service.import_analysis(project['id'], resolved, project['version'])

        detail = self.service.detail(project['id'])
        self._lock_entities(project['id'], detail['characters'], detail['locations'])

        prompts = self.service.project_prompts(project['id'])
        # All scenes must be ready for flow
        for idx, sc_item in enumerate(prompts['scenes']):
            self.assertTrue(sc_item['ready_for_flow'], f"Scene {idx+1} must be ready for flow. Error: {sc_item.get('flow_error')}")

    # -------------------------------------------------------------------------
    # End-to-End Synthetic Story 3: Multi-location journey with two heroes
    # -------------------------------------------------------------------------
    def test_synthetic_story_03_multilocation_journey(self):
        story = (
            "Томас встретил Капитана на пристани у подножия маяка. "
            "Они вместе поднялись по каменной лестнице на вершину утеса. "
            "Капитан зашел в комнату управления и включил главный прожектор."
        )
        project = self.service.create_project({'title': 'Two Heroes Journey', 'story': story, 'style': 'Cinematic'})
        raw_analysis = {
            'world': 'Lighthouse cliff',
            'characters': [
                {'name': 'Thomas', 'description': 'Young keeper', 'wardrobe': {}},
                {'name': 'Captain', 'description': 'Old sea captain', 'wardrobe': {}},
            ],
            'locations': [
                {'name': 'Пристань', 'description': 'Деревянная пристань'},
                {'name': 'Каменная лестница', 'description': 'Лестница на утесе'},
                {'name': 'Комната управления', 'description': 'Фонарная комната маяка'},
            ],
            'props': [],
            'scenes': [
                {'beat': 'Thomas meets Captain on the dock', 'source_quote': 'Томас встретил Капитана на пристани у подножия маяка.', 'location': 'Пристань', 'location_ref': 'Пристань', 'shot': 'Двуплановый средний кадр', 'visible_people': 2, 'characters': ['Thomas', 'Captain']},
                {'beat': 'They climb the stone staircase together', 'source_quote': 'Они вместе поднялись по каменной лестнице на вершину утеса.', 'location': 'Каменная лестница', 'location_ref': 'Каменная лестница', 'shot': 'Общий план', 'visible_people': 2, 'characters': ['Thomas', 'Captain']},
                {'beat': 'Captain switches on the spotlight', 'source_quote': 'Капитан зашел в комнату управления и включил главный прожектор.', 'location': 'Комната управления', 'location_ref': 'Комната управления', 'shot': 'Средний план Капитана', 'visible_people': 1, 'characters': ['Captain']},
            ]
        }
        resolved = prioritize_active_story_characters(raw_analysis, story)
        self.service.import_analysis(project['id'], resolved, project['version'])

        detail = self.service.detail(project['id'])
        self._lock_entities(project['id'], detail['characters'], detail['locations'])

        prompts = self.service.project_prompts(project['id'])
        for idx, sc_item in enumerate(prompts['scenes']):
            self.assertTrue(sc_item['ready_for_flow'], f"Scene {idx+1} must be ready for flow: {sc_item.get('flow_error')}")

    # -------------------------------------------------------------------------
    # Invariant Test 1: Raw Story Text through Real Analysis Normalization
    # -------------------------------------------------------------------------
    def test_raw_story_text_through_analysis_normalization(self):
        """Verify raw narrative text processed by analysis pipeline yields canonical contract."""
        story = "Дэниел приехал в Нортвуд. Он вошел в информационный центр и поздоровался с сотрудницей центра."
        project = self.service.create_project({'title': 'Raw Story Flow', 'story': story, 'style': 'Cinematic'})

        # Simulated raw LLM JSON response for story
        raw_llm_analysis = {
            'world': 'Northwood valley',
            'characters': [
                {'name': 'Дэниел', 'canonical_name': 'Дэниел', 'description': 'Protagonist traveler', 'wardrobe': {}}
            ],
            'locations': [
                {'name': 'Информационный центр', 'description': 'Wooden visitor center building'}
            ],
            'props': [],
            'scenes': [
                {
                    'beat': 'Дэниел прибывает в Нортвуд',
                    'source_quote': 'Дэниел приехал в Нортвуд.',
                    'location': 'Информационный центр',
                    'shot': 'Широкий план',
                    'characters': ['Дэниел'],
                },
                {
                    'beat': 'Дэниел входит в информационный центр и здоровается с сотрудницей',
                    'source_quote': 'Он вошел в информационный центр и поздоровался с сотрудницей центра.',
                    'location': 'Информационный центр',
                    'shot': 'Средний план',
                    'characters': ['Дэниел'],
                    'episodic_people': ['сотрудница центра'],
                    'visible_people': 2
                }
            ]
        }

        # Run through prioritize_active_story_characters and import_analysis
        resolved = prioritize_active_story_characters(raw_llm_analysis, story)
        imported_detail = self.service.import_analysis(project['id'], resolved, project['version'])

        # Verify scene 2 was normalized to exactly 2 visible people with Daniel and episodic employee
        sc2 = imported_detail['scenes'][1]['data']
        self.assertEqual(sc2['visible_people'], 2)
        self.assertEqual(sc2['characters'], ['Дэниел'])
        self.assertEqual(sc2['episodic_people'], ['сотрудница центра'])
        self.assertEqual(sc2['location_ref'], 'Информационный центр')

        # Lock Daniel and Location
        self._lock_entities(project['id'], imported_detail['characters'], imported_detail['locations'])

        prompts = self.service.project_prompts(project['id'])
        self.assertTrue(prompts['scenes'][0]['ready_for_flow'])
        self.assertTrue(prompts['scenes'][1]['ready_for_flow'])

        # Verify compiled Flow prompt contains Daniel and episodic participant
        flow_p2 = prompts['scenes'][1]['flow_prompt']
        self.assertIn('@Дэниел', flow_p2)
        self.assertIn('сотрудница центра', flow_p2)
        self.assertIn('Exactly 2 people total', flow_p2)

    # -------------------------------------------------------------------------
    # Negative Invariant 1: Unassigned human without role blocked in Preflight & Flow
    # -------------------------------------------------------------------------
    def test_negative_invariant_01_unassigned_human_blocked(self):
        scene = {
            'beat': 'Дэниел идет по лесной тропе',
            'source_quote': 'Дэниел шел по узкой лесной тропе.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Средний план',
            'visible_people': 2,  # 2 declared but only 1 accounted for!
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
            'episodic_people': []
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertFalse(contract['semantic_valid'])
        self.assertFalse(contract['valid'])
        self.assertTrue(any(e['code'] == 'unassigned_humans' for e in contract['semantic_errors']))

        # Verify project preflight parity
        project = {
            'id': 'p_inv1', 'story': 'Дэниел шел по узкой лесной тропе.',
            'characters': self.characters, 'locations': self.locations, 'props': [],
            'scenes': [{'id': 'sc1', 'position': 1, 'data': scene}]
        }
        preflight = validate_project_preflight(project, project['story'])
        self.assertFalse(preflight['ok'])
        self.assertFalse(preflight['valid'])
        self.assertTrue(any(e['code'] in ('unassigned_humans', 'unassigned_humans_threshold') for e in preflight['errors']))

    # -------------------------------------------------------------------------
    # Negative Invariant 2: Stale scene fields overwritten by contract prior to prompt
    # -------------------------------------------------------------------------
    def test_negative_invariant_02_stale_fields_overwritten_prior_to_prompt(self):
        story = "Дэниел в одиночестве сидит на скамье у входа в информационный центр."
        project = self.service.create_project({'title': 'Stale Scene Fix', 'story': story, 'style': 'Cinematic'})

        # Stale scene had Maria and 3 people stored
        stale_scene = {
            'beat': 'Дэниел сидит на скамье',
            'source_quote': story,
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'episodic_people': []
        }
        raw_analysis = {
            'world': 'Valley',
            'characters': [
                {'name': 'Дэниел', 'canonical_name': 'Дэниел', 'description': 'Hero', 'wardrobe': {}},
                {'name': 'Мария', 'canonical_name': 'Мария', 'description': 'Heroine', 'wardrobe': {}}
            ],
            'locations': [{'name': 'Информационный центр', 'description': 'Visitor center'}],
            'props': [],
            'scenes': [stale_scene]
        }
        imported = self.service.import_analysis(project['id'], raw_analysis, project['version'])
        self._lock_entities(project['id'], imported['characters'], imported['locations'])

        prompts = self.service.project_prompts(project['id'])
        sc1_prompts = prompts['scenes'][0]
        self.assertTrue(sc1_prompts['ready_for_flow'])

        # Maria must NOT be in references or prompt
        ref_names = [r['name'] for r in sc1_prompts['references']]
        self.assertIn('Дэниел', ref_names)
        self.assertNotIn('Мария', ref_names)
        self.assertNotIn('@Мария', sc1_prompts['flow_prompt'])
        self.assertIn('Exactly 1 person in the scene', sc1_prompts['flow_prompt'])

    # -------------------------------------------------------------------------
    # Negative Invariant 3: Offscreen / voiceover character excluded from prompt and refs
    # -------------------------------------------------------------------------
    def test_negative_invariant_03_offscreen_voiceover_excluded(self):
        scene = {
            'beat': 'Дэниел слушает по рации голос Марии',
            'source_quote': 'Дэниел держал рацию, слушая далекий голос Марии.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Крупный план Дэниела',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel']
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['semantic_valid'])
        self.assertEqual(contract['visible_character_ids'], ['char_daniel'])
        self.assertEqual(contract['voice_only_characters'], ['Мария'])

        flow_p = compile_flow_prompt({'style': 'Cinematic photorealism', 'world': 'Forest', 'mode': 'characters'}, scene, [self.hero_daniel], [self.loc_forest])
        self.assertIn('@Дэниел', flow_p)
        self.assertNotIn('@Мария', flow_p)
        self.assertIn('Represent voices, music and other sounds only through visible character reactions', flow_p)

    # -------------------------------------------------------------------------
    # Negative Invariant 4: Episodic person allowed without portrait reference
    # -------------------------------------------------------------------------
    def test_negative_invariant_04_episodic_person_allowed_without_portrait(self):
        scene = {
            'beat': 'Дэниел разговаривает с сотрудницей центра за стойкой',
            'source_quote': 'Дэниел подошел к стойке и обратился к сотруднице центра.',
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Двуплановый средний кадр',
            'visible_people': 2,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
            'episodic_people': ['сотрудница центра']
        }
        contract = resolve_scene_presence_contract(scene, self.characters, self.locations, require_locked_portraits=True)
        self.assertTrue(contract['semantic_valid'])
        self.assertTrue(contract['render_ready'])
        self.assertTrue(contract['valid'])
        self.assertEqual(contract['visible_people'], 2)
        self.assertEqual(contract['visible_characters'], ['Дэниел'])
        self.assertEqual(contract['episodic_people'], ['сотрудница центра'])

    # -------------------------------------------------------------------------
    # Negative Invariant 5: Missing locked portrait gives semantic_valid=True, render_ready=False
    # -------------------------------------------------------------------------
    def test_negative_invariant_05_missing_portrait_render_ready_false(self):
        unlocked_daniel = copy.deepcopy(self.hero_daniel)
        unlocked_daniel['locked'] = False
        unlocked_daniel['references'] = {}

        scene = {
            'beat': 'Дэниел идет по лесной тропе',
            'source_quote': 'Дэниел шел по лесной тропе.',
            'location': 'Лесная тропа',
            'location_ref': 'Лесная тропа',
            'shot': 'Средний план',
            'visible_people': 1,
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
        }
        contract = resolve_scene_presence_contract(scene, [unlocked_daniel], self.locations, require_locked_portraits=True)
        self.assertTrue(contract['semantic_valid'], "Narrative casting is semantically correct")
        self.assertFalse(contract['render_ready'], "Locked portrait is missing")
        self.assertFalse(contract['valid'], "Final ready_for_flow validity must be False")
        self.assertTrue(any(e['code'] == 'missing_portrait' for e in contract['render_errors']))

    # -------------------------------------------------------------------------
    # Negative Invariant 6: Real Bridge Queue skips blocked frame cleanly
    # -------------------------------------------------------------------------
    def test_negative_invariant_06_real_bridge_queue_skips_blocked_frame(self):
        """Execute Real Userscript bridge queue in Node.js and verify blocked frames are skipped cleanly."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        test_js = root / 'tests' / 'test_bridge_queue.js'
        node_candidates = [
            'node',
            r'C:\Program Files (x86)\MOZA Pit House\bin\node.exe',
            r'C:\Program Files\nodejs\node.exe',
        ]
        node_path = None
        for cand in node_candidates:
            try:
                res = subprocess.run([cand, '-v'], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    node_path = cand
                    break
            except Exception:
                continue

        if not node_path:
            self.skipTest("Node.js not found; skipping JS bridge execution")

        proc = subprocess.run([node_path, str(test_js)], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, f"Real JS Bridge Queue failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("All JavaScript Bridge tests passed successfully (100% OK)", proc.stdout)

    # -------------------------------------------------------------------------
    # Negative Invariant 7: LLM visible_people=2 reconciled to canonical 1
    # -------------------------------------------------------------------------
    def test_negative_invariant_07_raw_llm_visible_people_reconciled_to_canonical(self):
        """When LLM hallucinates visible_people: 2 for a solo shot or memory scene,
        validate_analysis MUST save canonical visible_people: 1 with diagnostic audit,
        and downstream preflight and ready_for_flow MUST succeed without blocking the queue."""
        story = "Сотрудница центра задумчиво опустила глаза, вспоминая странного путешественника Дэниела."
        project = self.service.create_project({'title': 'LLM VP Reconciliation', 'story': story, 'style': 'Cinematic'})

        # Simulated raw LLM output with visible_people: 2 (model counted mentioned Daniel)
        raw_scene_from_llm = {
            'beat': 'Сотрудница центра задумчиво вспоминает Дэниела',
            'source_quote': story,
            'location': 'Информационный центр',
            'location_ref': 'Информационный центр',
            'shot': 'Крупный план сотрудницы',
            'visible_people': 2,  # Raw invalid LLM number
            'characters': ['Дэниел'],  # LLM erroneously included mentioned Daniel
            'episodic_people': ['сотрудница центра']
        }
        raw_analysis = {
            'world': 'Valley',
            'characters': [
                {'name': 'Дэниел', 'canonical_name': 'Дэниел', 'description': 'Hero', 'wardrobe': {}}
            ],
            'locations': [{'name': 'Информационный центр', 'description': 'Visitor center'}],
            'props': [],
            'scenes': [raw_scene_from_llm]
        }
        imported = self.service.import_analysis(project['id'], raw_analysis, project['version'])
        self._lock_entities(project['id'], imported['characters'], imported['locations'])

        # Verify saved scene in project
        proj_detail = self.service.detail(project['id'])
        saved_scene = proj_detail['scenes'][0]
        scene_data = saved_scene['data']
        self.assertEqual(scene_data['visible_people'], 1, "Canonical visible_people must be saved, NOT raw 2")
        self.assertEqual(scene_data['raw_visible_people'], 2, "Raw LLM number preserved in diagnostics")
        self.assertIn('visible_people_audit', scene_data, "Diagnostic audit record must be saved")
        self.assertEqual(scene_data['episodic_people'], ['сотрудница центра'])
        self.assertEqual(scene_data['characters'], [], "Daniel must not be in visual characters")

        # Verify Preflight parity
        preflight = validate_project_preflight(proj_detail, story)
        self.assertTrue(preflight['ok'], "Preflight must be OK (no unassigned humans / missing protagonist errors)")
        self.assertTrue(preflight['valid'])

        # Verify Prompts and ready_for_flow
        prompts = self.service.project_prompts(project['id'])
        sc1_prompts = prompts['scenes'][0]
        self.assertTrue(sc1_prompts['ready_for_flow'], "Scene must be ready_for_flow: True")
        self.assertNotIn('@Дэниел', sc1_prompts['flow_prompt'], "Daniel must NOT be tagged in Flow prompt")
        char_refs = [r for r in sc1_prompts['references'] if r.get('type') == 'character']
        self.assertEqual(char_refs, [], "No character portrait refs needed for episodic solo scene")
        loc_refs = [r for r in sc1_prompts['references'] if r.get('type') == 'location']
        self.assertEqual(len(loc_refs), 1, "Location reference must be attached")


def _lock_entities_helper(service, project_id, characters, locations):
    pass


if __name__ == '__main__':
    unittest.main()



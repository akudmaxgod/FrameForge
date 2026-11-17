"""Comprehensive regression tests for global scene presence analysis contract.

Covers all 31 specific test scenarios and the end-to-end control story fixture
specified in TASK_GLOBAL_SCENE_PRESENCE_FIX.md.
"""
import shutil
import tempfile
import unittest
from imagegen.domain import (
    resolve_scene_presence_contract,
    extract_scene_episodic_roles,
    extract_scene_staging_count,
    generate_episodic_role_id,
    validate_project_preflight,
    validate_analysis,
    compile_flow_prompt,
    DomainError,
)
from imagegen.service import Service
from imagegen.cast import classify_candidate_role


class TestScenePresenceGlobalRegression(unittest.TestCase):
    def setUp(self):
        self.char_wife = {
            'id': 'char_wife',
            'canonical_name': 'Жена',
            'name': 'Жена',
            'role': 'Главная героиня',
            'description': 'Женщина 30 лет, каштановые волосы, выразительные глаза.',
            'wardrobe': {'default': 'Пальто и шарф'},
            'locked': True,
            'references': {'portrait': 'asset_wife_ref'}
        }
        self.char_husband = {
            'id': 'char_husband',
            'canonical_name': 'Муж',
            'name': 'Муж',
            'role': 'Муж главной героини',
            'description': 'Мужчина 35 лет, темные короткие волосы, спортивное телосложение.',
            'wardrobe': {'default': 'Куртка и джинсы'},
            'locked': True,
            'references': {'portrait': 'asset_husband_ref'}
        }
        self.char_hero = {
            'id': 'char_hero',
            'canonical_name': 'Герой',
            'name': 'Герой',
            'role': 'Главный герой',
            'description': 'Мужчина 28 лет, высокий, в кожаной куртке.',
            'wardrobe': {'default': 'Кожаная куртка'},
            'locked': True,
            'references': {'portrait': 'asset_hero_ref'}
        }
        self.loc_room = {
            'id': 'loc_living_room',
            'name': 'Гостиная',
            'description': 'Просторная светлая гостиная с окнами во двор.'
        }
        self.loc_center = {
            'id': 'loc_visitor_center',
            'name': 'Центр для посетителей',
            'description': 'Административное здание с деревянной стойкой регистрации.'
        }

    # =========================================================================
    # Группа 1: Постоянные персонажи (Тесты 1-4)
    # =========================================================================

    def test_01_wife_alone_in_room(self):
        """1. Жена одна в комнате -> visible_people: 1, visible_characters: ['Жена']"""
        scene = {
            'source_quote': 'Жена сидела в гостиной в полной тишине.',
            'beat': 'Жена сидит на диване в гостиной в задумчивости.',
            'shot': 'Medium shot of Wife sitting on sofa',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], [])
        self.assertEqual(contract['voice_only_characters'], [])
        self.assertEqual(contract['depicted_people'], [])
        self.assertTrue(contract['valid'])

    def test_02_wife_and_husband_physically_together(self):
        """2. Жена и муж физически рядом -> visible_people: 2, visible_characters: ['Жена', 'Муж']"""
        scene = {
            'source_quote': 'Жена и муж стояли у окна и смотрели на улицу.',
            'beat': 'Жена и муж стоят рядом возле окна.',
            'shot': 'Two-shot of Wife and Husband standing by the window',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 2)
        self.assertCountEqual(contract['visible_characters'], ['Жена', 'Муж'])
        self.assertEqual(contract['episodic_people'], [])
        self.assertTrue(contract['valid'])

    def test_03_two_characters_talking_both_visible(self):
        """3. Два героя разговаривают, оба видимы -> visible_people: 2"""
        scene = {
            'source_quote': 'Герой и жена обсуждали случившееся.',
            'beat': 'Герой и Жена оживленно разговаривают в комнате.',
            'shot': 'Two-shot showing both characters in dialogue',
            'characters': ['Герой', 'Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_hero, self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 2)
        self.assertCountEqual(contract['visible_characters'], ['Герой', 'Жена'])
        self.assertTrue(contract['valid'])

    def test_04_dialogue_participant_excluded_by_close_up(self):
        """4. Один участник диалога исключён крупным планом -> visible_people: 1, offscreen: ['Муж']"""
        scene = {
            'source_quote': '— Я всё объясню, — сказал муж. Жена слушала его с замиранием сердца.',
            'beat': 'Жена слушает слова мужа с напряженным выражением лица.',
            'shot': 'Close-up on Wife reacting to offscreen dialogue',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertIn('Муж', contract['offscreen_characters'])
        self.assertTrue(contract['valid'])

    # =========================================================================
    # Группа 2: Эпизодические роли (Тесты 5-10)
    # =========================================================================

    def test_05_hero_and_center_staff_member(self):
        """5. Герой и сотрудница центра -> visible_people: 2, episodic_people: ['сотрудница центра']"""
        scene = {
            'source_quote': 'Герой подошел к стойке, где его встретила сотрудница центра.',
            'beat': 'Герой разговаривает с сотрудницей центра за стойкой.',
            'shot': 'Two-shot of Hero and female employee at the counter',
            'characters': ['Герой'],
            'location_ref': 'Центр для посетителей'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_hero], [self.loc_center])
        self.assertEqual(contract['visible_people'], 2)
        self.assertEqual(contract['visible_characters'], ['Герой'])
        self.assertEqual(contract['episodic_people'], ['сотрудница центра'])
        self.assertTrue(contract['valid'])

    def test_06_husband_beside_another_woman(self):
        """6. Муж рядом с другой женщиной -> visible_people: 2, episodic_people: ['другая женщина']"""
        scene = {
            'source_quote': 'Муж стоял рядом с другой женщиной.',
            'beat': 'Муж стоит в комнате рядом с другой незнакомой женщиной.',
            'shot': 'Two-shot of Husband and another woman',
            'characters': ['Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_husband, self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 2)
        self.assertEqual(contract['visible_characters'], ['Муж'])
        self.assertEqual(contract['episodic_people'], ['другая женщина'])
        self.assertTrue(contract['valid'])

    def test_07_three_people_in_wide_shot(self):
        """7. Три человека в широком плане -> visible_people: 3"""
        scene = {
            'source_quote': 'В комнате стояли жена, муж и другая женщина.',
            'beat': 'Жена замирает в дверях, глядя на мужа и другую женщину.',
            'shot': 'Wide shot showing three figures in the room: Wife, Husband, and another woman',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 3)
        self.assertCountEqual(contract['visible_characters'], ['Жена', 'Муж'])
        self.assertEqual(contract['episodic_people'], ['другая женщина'])
        self.assertTrue(contract['valid'])

    def test_08_episodic_person_remains_in_background(self):
        """8. Эпизодический человек остаётся в фоне следующего кадра -> visible_people: 3"""
        prev_scene = {
            'event_id': 'evt_culmination',
            'source_quote': 'Жена увидела мужа и другую женщину.',
            'beat': 'Жена входит в комнату к мужу и другой женщине.',
            'shot': 'Wide shot showing three figures',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        prev_contract = resolve_scene_presence_contract(prev_scene, [self.char_wife, self.char_husband], [self.loc_room])

        cur_scene = {
            'event_id': 'evt_culmination',
            'source_quote': 'Муж шагнул навстречу жене. Другая женщина оставалась на фоне.',
            'beat': 'Муж делает шаг к Жене, пока другая женщина остается на заднем плане.',
            'shot': 'Medium shot of Husband and Wife, with the other woman visible in the background',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        cur_contract = resolve_scene_presence_contract(
            cur_scene, [self.char_wife, self.char_husband], [self.loc_room],
            previous_contract=prev_contract
        )
        self.assertEqual(cur_contract['visible_people'], 3)
        self.assertIn('другая женщина', cur_contract['episodic_people'])
        self.assertTrue(cur_contract['valid'])

    def test_09_episodic_role_preserves_stable_role_id(self):
        """9. Эпизодическая роль сохраняет одинаковый role_id внутри события"""
        role_id1 = generate_episodic_role_id('evt_culmination', 'другая женщина')
        role_id2 = generate_episodic_role_id('evt_culmination', 'другая женщина')
        self.assertEqual(role_id1, role_id2)
        self.assertTrue(role_id1.startswith('episodic_role_evt_culmination_'))

    def test_10_recurring_episodic_role_promotion_awareness(self):
        """10. Определение стабильного идентификатора роли для межсценарного использования"""
        role_id = generate_episodic_role_id('evt_02', 'сотрудница центра')
        self.assertEqual(role_id, 'episodic_role_evt_02_sotrudnitsa_tsentra')

    # =========================================================================
    # Группа 3: Coreference (Тесты 11-14)
    # =========================================================================

    def test_11_wife_arrived_woman_got_out_single_person(self):
        """11. 'Жена приехала. Женщина вышла из машины.' -> один человек (Жена)"""
        scene = {
            'source_quote': 'Жена приехала к дому. Женщина вышла из машины и направилась к двери.',
            'beat': 'Жена выходит из автомобиля и идет к крыльцу.',
            'shot': 'Medium shot of Wife getting out of car',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], [])
        # Coreference audit check
        coref = contract['visible_people_audit']['coreference_resolutions']
        self.assertTrue(any(c.get('surface_form') == 'женщина' and c.get('resolved_as') == 'Жена' for c in coref))

    def test_12_husband_entered_man_sat_single_person(self):
        """12. 'Муж вошёл. Мужчина сел.' -> один человек (Муж)"""
        scene = {
            'source_quote': 'Муж вошел в комнату. Мужчина сел в кресло.',
            'beat': 'Муж входит и садится в глубокое кресло.',
            'shot': 'Medium shot of Husband sitting down in armchair',
            'characters': ['Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Муж'])
        self.assertEqual(contract['episodic_people'], [])
        coref = contract['visible_people_audit']['coreference_resolutions']
        self.assertTrue(any(c.get('surface_form') == 'мужчина' and c.get('resolved_as') == 'Муж' for c in coref))

    def test_13_wife_saw_another_woman_two_distinct_people(self):
        """13. 'Жена увидела другую женщину.' -> два разных человека"""
        scene = {
            'source_quote': 'Жена открыла дверь и увидела другую женщину.',
            'beat': 'Жена видит перед собой другую женщину.',
            'shot': 'Two-shot of Wife and another woman',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 2)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], ['другая женщина'])

    def test_14_she_approached_him_pronoun_resolution(self):
        """14. 'Она подошла к нему.' -> корректное разрешение двух местоимений"""
        prev_scene = {
            'source_quote': 'Жена и Муж встретились в холле.',
            'beat': 'Жена и Муж смотрят друг на друга.',
            'shot': 'Two-shot of Wife and Husband',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        prev_contract = resolve_scene_presence_contract(prev_scene, [self.char_wife, self.char_husband], [self.loc_room])

        cur_scene = {
            'source_quote': 'Она подошла к нему и тихо заговорила.',
            'beat': 'Она подходит к нему вплотную.',
            'shot': 'Two-shot of Wife approaching Husband',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        cur_contract = resolve_scene_presence_contract(
            cur_scene, [self.char_wife, self.char_husband], [self.loc_room],
            previous_contract=prev_contract
        )
        self.assertEqual(cur_contract['visible_people'], 2)
        self.assertCountEqual(cur_contract['visible_characters'], ['Жена', 'Муж'])

    # =========================================================================
    # Группа 4: Голоса и упоминания (Тесты 15-18)
    # =========================================================================

    def test_15_husband_voice_behind_door_voice_only(self):
        """15. Голос мужа слышен за дверью -> муж voice_only, visible_people: 1 (только Жена)"""
        scene = {
            'source_quote': 'Жена замерла на лестнице, услышав голос мужа из-за закрытой двери.',
            'beat': 'Жена стоит на лестнице и прислушивается к доносящемуся голосу мужа.',
            'shot': 'Medium shot of Wife on stairs listening to offscreen voice',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['voice_only_characters'], ['Муж'])
        self.assertEqual(contract['episodic_people'], [])
        self.assertTrue(contract['valid'])

    def test_16_husband_speaks_on_phone_from_remote_location(self):
        """16. Муж говорит по телефону из другой локации -> не видим (voice_only)"""
        scene = {
            'source_quote': 'Жена прижала трубку к уху. В трубке звучал голос мужа.',
            'beat': 'Жена слушает голос мужа в телефонной трубке.',
            'shot': 'Close-up of Wife on phone listening to caller',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['voice_only_characters'], ['Муж'])

    def test_17_wife_remembers_husband_mentioned_only(self):
        """17. Жена вспоминает мужа -> муж mentioned_only"""
        scene = {
            'source_quote': 'Оставшись одна, жена вспомнила своего мужа и их прошлый разговор.',
            'beat': 'Жена сидит у окна и думает о муже.',
            'shot': 'Solo shot of Wife in deep thought',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['mentioned_characters'], ['Муж'])

    def test_18_unknown_message_sender_no_card_created(self):
        """18. Рассказ о неизвестном отправителе -> карточка не создаётся, visible_people: 1"""
        scene = {
            'source_quote': 'Жена получила странное сообщение от неизвестного отправителя.',
            'beat': 'Жена смотрит на экран телефона со входящим сообщением.',
            'shot': 'Close-up of Wife reading message on phone screen',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], [])

    # =========================================================================
    # Группа 5: Фото и экраны (Тесты 19-21)
    # =========================================================================

    def test_19_husband_in_phone_photograph_depicted_people(self):
        """19. Муж показан на фотографии телефона -> depicted_people, visible_people: 1 (только Жена)"""
        scene = {
            'source_quote': 'Жена открыла в телефоне фотографию мужа.',
            'beat': 'Жена смотрит на фотографию мужа на экране своего смартфона.',
            'shot': 'Close-up of Wife holding phone displaying photo of Husband',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['depicted_people'], ['Муж'])
        self.assertNotIn('Муж', contract['visible_characters'])
        self.assertTrue(contract['valid'])

    def test_20_person_on_tv_screen_does_not_increase_visible_people(self):
        """20. Человек на экране телевизора не увеличивает visible_people"""
        scene = {
            'source_quote': 'Жена сидела в кресле. На экране телевизора диктор читал новости.',
            'beat': 'Жена сидит перед включенным телевизором.',
            'shot': 'Medium shot of Wife watching TV broadcast',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])

    def test_21_portrait_on_wall_does_not_add_physical_participant(self):
        """21. Портрет на стене не добавляет физического участника"""
        scene = {
            'source_quote': 'В пустой комнате висел большой портрет основателя дома.',
            'beat': 'Старинный портрет основателя на каменной стене пустой залы.',
            'shot': 'Static shot of wall portrait in empty room',
            'characters': [],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [], [self.loc_room])
        self.assertEqual(contract['visible_people'], 0)
        self.assertEqual(contract['visible_characters'], [])
        self.assertEqual(contract['episodic_people'], [])

    # =========================================================================
    # Группа 6: Потеря субъекта и активные глаголы (Тесты 22-24)
    # =========================================================================

    def test_22_she_entered_and_froze_wife_stays_visible(self):
        """22. 'Она вошла и застыла' -> жена остаётся видимой (visible_people: 1)"""
        prev_scene = {
            'event_id': 'evt_house_arrival',
            'source_quote': 'Жена подошла к дому.',
            'beat': 'Жена подходит к входной двери дома.',
            'shot': 'Medium shot of Wife',
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        prev_contract = resolve_scene_presence_contract(prev_scene, [self.char_wife], [self.loc_room])

        cur_scene = {
            'event_id': 'evt_house_arrival',
            'source_quote': 'Она вошла — и застыла.',
            'beat': 'Она замирает в дверном проеме, пораженная увиденным.',
            'shot': 'Medium shot of female protagonist frozen in doorway',
            'characters': [],  # LLM omitted explicit name
            'location_ref': 'Гостиная'
        }
        cur_contract = resolve_scene_presence_contract(
            cur_scene, [self.char_wife], [self.loc_room],
            previous_contract=prev_contract
        )
        self.assertEqual(cur_contract['visible_people'], 1)
        self.assertEqual(cur_contract['visible_characters'], ['Жена'])
        self.assertTrue(cur_contract['valid'])

    def test_23_he_turned_and_left_husband_stays_visible(self):
        """23. 'Он повернулся и ушёл' -> муж остаётся видимым (visible_people: 1)"""
        prev_scene = {
            'event_id': 'evt_exit',
            'source_quote': 'Муж стоял в коридоре.',
            'beat': 'Муж стоит в коридоре.',
            'shot': 'Medium shot of Husband',
            'characters': ['Муж'],
            'location_ref': 'Гостиная'
        }
        prev_contract = resolve_scene_presence_contract(prev_scene, [self.char_husband], [self.loc_room])

        cur_scene = {
            'event_id': 'evt_exit',
            'source_quote': 'Он повернулся и молча вышел.',
            'beat': 'Он поворачивается спиной и выходит из комнаты.',
            'shot': 'Medium shot of male character walking away',
            'characters': [],
            'location_ref': 'Гостиная'
        }
        cur_contract = resolve_scene_presence_contract(
            cur_scene, [self.char_husband], [self.loc_room],
            previous_contract=prev_contract
        )
        self.assertEqual(cur_contract['visible_people'], 1)
        self.assertEqual(cur_contract['visible_characters'], ['Муж'])
        self.assertTrue(cur_contract['valid'])

    def test_24_ambiguous_antecedent_blocks_scene_with_error(self):
        """24. Местоимение без однозначного антецедента блокирует сцену с диагностикой"""
        char_john = {
            'id': 'char_john',
            'canonical_name': 'Джон',
            'name': 'Джон',
            'role': 'Гость',
            'description': 'Мужчина 30 лет.',
            'locked': True,
            'references': {'portrait': 'ref_john'}
        }
        char_bob = {
            'id': 'char_bob',
            'canonical_name': 'Боб',
            'name': 'Боб',
            'role': 'Гость',
            'description': 'Мужчина 32 года.',
            'locked': True,
            'references': {'portrait': 'ref_bob'}
        }
        scene = {
            'source_quote': 'Он резко встал из-за стола.',
            'beat': 'Он встает из-за стола в гневе.',
            'shot': 'Medium shot of man standing up',
            'characters': [],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [char_john, char_bob], [self.loc_room])
        self.assertTrue(any(e.get('code') == 'ambiguous_antecedent' for e in contract['errors']))
        self.assertFalse(contract['semantic_valid'])

    # =========================================================================
    # Группа 7: Противоречия и аудит (Тесты 25-28)
    # =========================================================================

    def test_25_shot_three_figures_contradiction_detected(self):
        """25. shot: 'three figures', но контракт только на 2 -> обнаруживается противоречие"""
        scene = {
            'source_quote': 'В комнате находились двое.',
            'beat': 'Жена и муж сидят за столом.',
            'shot': 'Wide shot showing three figures in the room',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertTrue(any(e.get('code') == 'composition_contradiction' for e in contract['errors']))
        self.assertFalse(contract['valid'])

    def test_26_shot_alone_with_multiple_people_detected(self):
        """26. 'alone', но заявлено 2 человека -> обнаруживается противоречие"""
        scene = {
            'source_quote': 'Жена была одна в комнате.',
            'beat': 'Жена и муж сидят вместе.',
            'shot': 'Solo shot of Wife alone in the room',
            'characters': ['Жена', 'Муж'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertTrue(any(e.get('code') == 'composition_contradiction' for e in contract['errors']))
        self.assertFalse(contract['valid'])

    def test_27_staging_count_extractor_accuracy(self):
        """27. Проверка extract_scene_staging_count на ключевые фразы"""
        self.assertEqual(extract_scene_staging_count({'shot': 'wide shot showing three figures'}), 3)
        self.assertEqual(extract_scene_staging_count({'beat': 'трое людей стояли в комнате'}), 3)
        self.assertEqual(extract_scene_staging_count({'shot': 'two-shot of characters'}), 2)
        self.assertEqual(extract_scene_staging_count({'beat': 'двое стоят у окна'}), 2)
        self.assertEqual(extract_scene_staging_count({'shot': 'solo shot of protagonist alone'}), 1)
        self.assertEqual(extract_scene_staging_count({'shot': 'empty room, uninhabited'}), 0)

    def test_28_raw_visible_people_audit_reconciliation(self):
        """28. Сырое visible_people модели пересчитывается с записью в audit"""
        scene = {
            'source_quote': 'Жена услышала голос мужа за дверью.',
            'beat': 'Жена прислушивается к голосу мужа за дверью.',
            'shot': 'Solo shot of Wife',
            'characters': ['Жена'],
            'visible_people': 2,  # Raw LLM falsely counted voice as visible person
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['raw_visible_people'], 2)
        audit = contract['visible_people_audit']
        self.assertEqual(audit['raw_visible_people'], 2)
        self.assertEqual(audit['canonical_visible_people'], 1)
        self.assertEqual(audit['voice_only_characters'], ['Муж'])
        self.assertTrue(bool(audit['reconciliation_reason']))

    # =========================================================================
    # Группа 8: Очередь и Preflight (Тесты 29-31)
    # =========================================================================

    def test_29_preflight_disjointness_invariants_pass(self):
        """29. Preflight валидирует инварианты непересекаемости категорий присутствия"""
        project_data = {
            'story': 'Жена пришла домой. Муж был там.',
            'characters': [self.char_wife, self.char_husband],
            'locations': [self.loc_room],
            'scenes': [
                {
                    'source_quote': 'Жена пришла домой.',
                    'beat': 'Жена входит в дом.',
                    'shot': 'Medium shot of Wife',
                    'characters': ['Жена'],
                    'location_ref': 'Гостиная',
                    'visible_people': 1
                },
                {
                    'source_quote': 'Муж был там.',
                    'beat': 'Муж встречает жену в комнате.',
                    'shot': 'Two-shot of Wife and Husband',
                    'characters': ['Жена', 'Муж'],
                    'location_ref': 'Гостиная',
                    'visible_people': 2
                }
            ]
        }
        res = validate_project_preflight(project_data)
        self.assertTrue(res['valid'], msg=f"Errors: {res.get('errors')}")

    def test_30_preflight_blocks_unassigned_human_with_diagnostic(self):
        """30. Preflight блокирует кадр с нераспознанным человеком без роли"""
        project_data = {
            'story': 'В комнате кто-то был.',
            'characters': [self.char_wife],
            'locations': [self.loc_room],
            'scenes': [
                {
                    'source_quote': 'В комнате кто-то был.',
                    'beat': 'В комнате находится неизвестный силуэт без роли.',
                    'shot': 'Medium shot',
                    'characters': [],
                    'location_ref': 'Гостиная',
                    'visible_people': 2  # No bound character or valid episodic role
                }
            ]
        }
        res = validate_project_preflight(project_data)
        self.assertFalse(res['valid'])
        self.assertTrue(any(e.get('code') in ('unassigned_humans', 'unassigned_humans_threshold') for e in res['errors']))

    def test_31_compile_flow_prompt_presence_exclusions(self):
        """31. compile_flow_prompt формирует промпт с исключениями голоса и фото"""
        scene = {
            'source_quote': 'Жена посмотрела на фотографию мужа.',
            'beat': 'Жена смотрит на фото мужа на экране телефона.',
            'shot': 'Close-up of Wife holding smartphone',
            'characters': ['Жена'],
            'location_ref': 'Гостиная',
            'visible_people': 1,
            'depicted_people': ['Муж'],
            'voice_only_characters': [],
            'offscreen_characters': []
        }
        prompt = compile_flow_prompt({'style': 'cinematic'}, scene, characters=[self.char_wife], locations=[self.loc_room])
        self.assertIn('Solo shot featuring only @Жена', prompt)
        self.assertIn('A photograph or media depiction of Муж may appear only on a phone screen', prompt)
        self.assertIn('Do not place Муж physically in the room', prompt)

    # =========================================================================
    # Сквозной контрольный тест (Section 15)
    # =========================================================================

    def test_control_story_end_to_end_fixture(self):
        """Сквозной тест на контрольный сюжет: Жена, Муж, неизвестный отправитель, другая женщина."""
        story_text = (
            "Жена получила сообщение с незнакомого номера. На прикрепленной фотографии был ее муж с другой женщиной. "
            "Жена приехала к загородному дому и поднялась по лестнице. За закрытой дверью звучал голос мужа. "
            "Она вошла — и застыла. В просторной гостиной перед ней стояли муж и другая женщина. "
            "Муж сделал шаг навстречу, а другая женщина оставалась на фоне у окна."
        )

        project_data = {
            'story': story_text,
            'characters': [self.char_wife, self.char_husband],
            'locations': [self.loc_room],
            'scenes': [
                # Кадр 1: Получение сообщения и просмотр фото
                {
                    'position': 1,
                    'event_id': 'evt_01',
                    'source_quote': 'Жена получила сообщение с незнакомого номера. На прикрепленной фотографии был ее муж с другой женщиной.',
                    'beat': 'Жена сидит в машине и в шоке смотрит на экран телефона с фотографией мужа.',
                    'shot': 'Close-up of Wife looking at smartphone screen',
                    'characters': ['Жена'],
                    'location_ref': 'Гостиная',
                    'visible_people': 1
                },
                # Кадр 2: Подъем по лестнице и голос мужа за дверью
                {
                    'position': 2,
                    'event_id': 'evt_02',
                    'source_quote': 'Жена приехала к загородному дому и поднялась по лестнице. За закрытой дверью звучал голос мужа.',
                    'beat': 'Жена стоит на лестничной площадке и слушает голос мужа из-за двери.',
                    'shot': 'Medium shot of Wife on staircase listening to offscreen voice',
                    'characters': ['Жена'],
                    'location_ref': 'Гостиная',
                    'visible_people': 1
                },
                # Кадр 3: Вход в комнату (пропуск имени, активный глагол)
                {
                    'position': 3,
                    'event_id': 'evt_03',
                    'source_quote': 'Она вошла — и застыла.',
                    'beat': 'Она замирает в дверном проеме комнаты.',
                    'shot': 'Medium shot of female protagonist frozen in doorway',
                    'characters': ['Жена'],
                    'location_ref': 'Гостиная',
                    'visible_people': 1
                },
                # Кадр 4: Кульминация (широкий план, 3 фигуры)
                {
                    'position': 4,
                    'event_id': 'evt_03',
                    'source_quote': 'В просторной гостиной перед ней стояли муж и другая женщина.',
                    'beat': 'Жена смотрит на мужа и другую женщину в гостиной.',
                    'shot': 'Wide shot showing three figures in the room: Wife, Husband, and another woman',
                    'characters': ['Жена', 'Муж'],
                    'location_ref': 'Гостиная',
                    'visible_people': 3
                },
                # Кадр 5: Финал события (другая женщина в фоне)
                {
                    'position': 5,
                    'event_id': 'evt_03',
                    'source_quote': 'Муж сделал шаг навстречу, а другая женщина оставалась на фоне у окна.',
                    'beat': 'Муж делает шаг к жене, другая женщина остается на заднем плане у окна.',
                    'shot': 'Medium shot of Husband approaching Wife, with the other woman visible in the background',
                    'characters': ['Жена', 'Муж'],
                    'location_ref': 'Гостиная',
                    'visible_people': 3
                }
            ]
        }

        # 1. Validate complete analysis
        validated = validate_analysis(project_data, story_text)
        self.assertEqual(len(validated['scenes']), 5)

        s1, s2, s3, s4, s5 = validated['scenes']

        # Проверка кадра 1 (Фото)
        self.assertEqual(s1['visible_people'], 1)
        self.assertEqual(s1['characters'], ['Жена'])
        c1 = resolve_scene_presence_contract(s1, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(c1['depicted_people'], ['Муж'])
        self.assertEqual(c1['visible_characters'], ['Жена'])

        # Проверка кадра 2 (Голос за дверью)
        self.assertEqual(s2['visible_people'], 1)
        self.assertEqual(s2['characters'], ['Жена'])
        c2 = resolve_scene_presence_contract(s2, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(c2['voice_only_characters'], ['Муж'])

        # Проверка кадра 3 (Вход)
        self.assertEqual(s3['visible_people'], 1)
        self.assertEqual(s3['characters'], ['Жена'])

        # Проверка кадра 4 (Кульминационный широкий план 3 фигуры)
        self.assertEqual(s4['visible_people'], 3)
        self.assertCountEqual(s4['characters'], ['Жена', 'Муж'])
        self.assertIn('другая женщина', s4['episodic_people'])

        # Проверка кадра 5 (Фон / непрерывность эпизодической роли)
        self.assertEqual(s5['visible_people'], 3)
        self.assertCountEqual(s5['characters'], ['Жена', 'Муж'])
        self.assertIn('другая женщина', s5['episodic_people'])

        # 2. Preflight validation
        preflight_res = validate_project_preflight(validated, story_text)
        self.assertTrue(preflight_res['valid'], msg=f"Preflight errors: {preflight_res.get('errors')}")

        # 3. Flow prompt generation checks
        p1 = compile_flow_prompt({'style': 'cinematic'}, s1, characters=[self.char_wife], locations=[self.loc_room])
        self.assertIn('Solo shot featuring only @Жена', p1)
        self.assertIn('photograph or media depiction', p1)

        p2 = compile_flow_prompt({'style': 'cinematic'}, s2, characters=[self.char_wife], locations=[self.loc_room])
        self.assertIn('Solo shot featuring only @Жена', p2)

        p4 = compile_flow_prompt({'style': 'cinematic'}, s4, characters=[self.char_wife, self.char_husband], locations=[self.loc_room])
        self.assertIn('Exactly 3 people total', p4)
        self.assertIn('другая женщина', p4)

    # =========================================================================
    # Группа 8: Дополнительные тесты контракта присутствия (Тесты 33-39)
    # =========================================================================

    def test_33_arbitrary_physical_people_counts(self):
        """33. Поддержка произвольного числа людей (4, 5, 10) без ограничения min(3, ...)."""
        char_c1 = {'id': 'c1', 'canonical_name': 'Алексей', 'name': 'Алексей', 'role': 'Герой', 'locked': True}
        char_c2 = {'id': 'c2', 'canonical_name': 'Борис', 'name': 'Борис', 'role': 'Друг', 'locked': True}
        char_c3 = {'id': 'c3', 'canonical_name': 'Виктор', 'name': 'Виктор', 'role': 'Коллега', 'locked': True}
        char_c4 = {'id': 'c4', 'canonical_name': 'Дмитрий', 'name': 'Дмитрий', 'role': 'Помощник', 'locked': True}
        char_c5 = {'id': 'c5', 'canonical_name': 'Елена', 'name': 'Елена', 'role': 'Специалист', 'locked': True}
        all_chars = [char_c1, char_c2, char_c3, char_c4, char_c5]

        # 4 человека
        scene_4 = {
            'source_quote': 'Алексей, Борис, Виктор и Дмитрий сидели за круглым столом.',
            'beat': 'Алексей, Борис, Виктор и Дмитрий сидят за столом в переговорной.',
            'shot': 'Group shot of four participants seated around table',
            'characters': ['Алексей', 'Борис', 'Виктор', 'Дмитрий'],
            'location_ref': 'Гостиная'
        }
        c4 = resolve_scene_presence_contract(scene_4, all_chars, [self.loc_room])
        self.assertEqual(c4['visible_people'], 4)
        self.assertEqual(len(c4['visible_characters']), 4)
        self.assertTrue(c4['valid'])

        # 5 человек
        scene_5 = {
            'source_quote': 'Алексей, Борис, Виктор, Дмитрий и Елена собрались в центре комнаты.',
            'beat': 'Алексей, Борис, Виктор, Дмитрий и Елена стоят вместе в гостиной.',
            'shot': 'Five people standing together in the room',
            'characters': ['Алексей', 'Борис', 'Виктор', 'Дмитрий', 'Елена'],
            'location_ref': 'Гостиная'
        }
        c5 = resolve_scene_presence_contract(scene_5, all_chars, [self.loc_room])
        self.assertEqual(c5['visible_people'], 5)
        self.assertEqual(len(c5['visible_characters']), 5)
        self.assertTrue(c5['valid'])

        # 10 человек
        scene_10 = {
            'source_quote': 'Десять человек стояли в зале.',
            'beat': 'Десять участников стоят в зале.',
            'shot': 'Wide shot of ten figures in the hall',
            'composition': 'ten people in room',
            'location_ref': 'Гостиная',
            'episodic_people': [f'участник {i}' for i in range(1, 11)]
        }
        c10 = resolve_scene_presence_contract(scene_10, [], [self.loc_room])
        self.assertEqual(c10['visible_people'], 10)
        self.assertEqual(len(c10['episodic_people']), 10)
        self.assertTrue(c10['valid'])

    def test_34_unknown_people_without_episodic_role_blocks_scene(self):
        """34. unknown_people без подтверждённой роли блокируют кадр ошибкой unassigned_humans."""
        scene = {
            'source_quote': 'Жена стояла в комнате, а позади неё был кто-то неясный.',
            'beat': 'Жена стоит посреди комнаты, сзади некая неопознанная фигура.',
            'shot': 'Medium shot of Wife with mysterious presence behind her',
            'raw_visible_people': 2,
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], [])
        self.assertFalse(contract['semantic_valid'])
        error_codes = [e['code'] for e in contract['errors']]
        self.assertIn('unassigned_humans', error_codes)

    def test_35_merge_explicit_and_text_extracted_episodic_roles(self):
        """35. Объединение episodic_people, background_roles и ролей, извлечённых из текста/композиции."""
        scene = {
            'source_quote': 'Сотрудница центра и другой мужчина стояли у регистрационной стойки.',
            'beat': 'Сотрудница центра и другой мужчина обсуждают маршрут у входа.',
            'shot': 'Two-shot of staff member and another man at the counter',
            'composition': 'two figures at the counter',
            'episodic_people': ['сотрудница центра'],
            'location_ref': 'Центр для посетителей'
        }
        contract = resolve_scene_presence_contract(scene, [], [self.loc_center])
        self.assertEqual(contract['visible_people'], 2)
        self.assertIn('сотрудница центра', contract['episodic_people'])
        self.assertIn('другой мужчина', contract['episodic_people'])
        self.assertTrue(contract['semantic_valid'])

    def test_36_coreference_inflated_count_reconciliation(self):
        """36. Завышенный LLM счёт из-за кореференции ('Жена' + 'Женщина') согласуется и проходит."""
        scene = {
            'source_quote': 'Жена подошла к окну. Женщина посмотрела во двор.',
            'beat': 'Жена смотрит в окно во двор.',
            'shot': 'Medium shot of Wife looking through the window',
            'raw_visible_people': 2,
            'characters': ['Жена'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['episodic_people'], [])
        self.assertTrue(contract['semantic_valid'])
        self.assertTrue(contract['valid'])
        self.assertTrue(len(contract['visible_people_audit']['coreference_resolutions']) >= 1)

    def test_37_voice_only_priority_over_declared_characters(self):
        """37. Приоритет voice_only: 'поднялась по лестнице и услышала голос мужа' -> муж только голос, не рисуется."""
        scene = {
            'source_quote': 'Она поднялась по лестнице и услышала голос мужа за закрытой дверью.',
            'beat': 'Жена стоит на лестничной площадке и слушает голос мужа из-за двери.',
            'shot': 'Medium shot of Wife listening on the stairs',
            'characters': ['Жена', 'Муж'],
            'character_ids': ['char_wife', 'char_husband'],
            'location_ref': 'Гостиная'
        }
        contract = resolve_scene_presence_contract(scene, [self.char_wife, self.char_husband], [self.loc_room])
        self.assertEqual(contract['visible_people'], 1)
        self.assertEqual(contract['visible_characters'], ['Жена'])
        self.assertEqual(contract['voice_only_characters'], ['Муж'])
        self.assertTrue(contract['semantic_valid'])

        prompt = compile_flow_prompt(
            {'style': 'cinematic'},
            {
                'source_quote': scene['source_quote'],
                'beat': scene['beat'],
                'shot': scene['shot'],
                'characters': contract['visible_characters'],
                'voice_only_characters': contract['voice_only_characters'],
                'visible_people': contract['visible_people']
            },
            characters=[self.char_wife, self.char_husband],
            locations=[self.loc_room]
        )
        self.assertIn('Solo shot featuring only @Жена', prompt)
        self.assertIn('Муж is heard offscreen and must not be shown', prompt)

    def test_38_episodic_role_promotion_to_secondary_character(self):
        """38. Продвижение эпизодической роли до постоянного персонажа при появлении в нескольких событиях с диалогом."""
        cand = {
            'canonical_name': 'Кузнец',
            'aliases': [],
            'evidence': [
                {'quote': '— Держи свой клинок, — сказал кузнец.', 'start': 10, 'end': 45},
                {'quote': 'Кузнец вернулся на следующее утро и принёс заказ.', 'start': 120, 'end': 170}
            ]
        }
        story = (
            "Сцена 1: В кузнице мастер завершил работу. — Держи свой клинок, — сказал кузнец.\n\n"
            "Сцена 2: Наступило утро. Кузнец вернулся на следующее утро и принёс заказ."
        )
        role, reason = classify_candidate_role(cand, story)
        self.assertEqual(role, 'key')
        self.assertIn('Кузнец', reason)

    def test_39_end_to_end_saved_project_check_via_service_project_prompts(self):
        """39. Сквозная проверка сохранённого проекта через Service.project_prompts на контрольной истории."""
        temp_dir = tempfile.mkdtemp()
        try:
            service = Service(temp_dir)
            story_text = (
                "Жена сидела в комнате и смотрела на старую фотографию мужа в телефоне. "
                "Жена приехала к загородному дому и поднялась по лестнице. За закрытой дверью звучал голос мужа. "
                "Она вошла — и застыла. "
                "В просторной гостиной перед ней стояли муж и другая женщина. "
                "Муж сделал шаг навстречу, а другая женщина оставалась на фоне у окна."
            )
            proj = service.create_project({
                'title': 'Контрольная история присутствия',
                'story': story_text,
                'style': 'cinematic film still, photorealistic'
            })
            project_id = proj['id']

            # Создаём анализ и импортируем его
            analysis_data = {
                'world': 'Гостиная загородного дома',
                'characters': [
                    {
                        'name': 'Жена',
                        'canonical_name': 'Жена',
                        'role': 'Главная героиня',
                        'description': 'Женщина 30 лет с каштановыми волосами.',
                        'wardrobe': {'default': 'Пальто и шарф'}
                    },
                    {
                        'name': 'Муж',
                        'canonical_name': 'Муж',
                        'role': 'Муж главной героини',
                        'description': 'Мужчина 35 лет, темные короткие волосы.',
                        'wardrobe': {'default': 'Куртка и джинсы'}
                    }
                ],
                'locations': [
                    {
                        'name': 'Гостиная',
                        'description': 'Просторная светлая гостиная загородного дома.'
                    }
                ],
                'scenes': [
                    {
                        'source_quote': 'Жена сидела в комнате и смотрела на старую фотографию мужа в телефоне.',
                        'beat': 'Жена сидит на диване и смотрит на фотографию мужа на экране телефона.',
                        'shot': 'Close-up of Wife looking down at the phone screen',
                        'characters': ['Жена'],
                        'location_ref': 'Гостиная'
                    },
                    {
                        'source_quote': 'Жена приехала к загородному дому и поднялась по лестнице. За закрытой дверью звучал голос мужа.',
                        'beat': 'Жена стоит на лестничной площадке и слушает голос мужа из-за двери.',
                        'shot': 'Medium shot of Wife on staircase listening to offscreen voice',
                        'characters': ['Жена'],
                        'location_ref': 'Гостиная'
                    },
                    {
                        'source_quote': 'Она вошла — и застыла.',
                        'beat': 'Она замирает в дверном проеме комнаты.',
                        'shot': 'Medium shot of female protagonist frozen in doorway',
                        'characters': ['Жена'],
                        'location_ref': 'Гостиная'
                    },
                    {
                        'source_quote': 'В просторной гостиной перед ней стояли муж и другая женщина.',
                        'beat': 'Жена смотрит на мужа и другую женщину в гостиной.',
                        'shot': 'Wide shot showing three figures in the room: Wife, Husband, and another woman',
                        'characters': ['Жена', 'Муж'],
                        'location_ref': 'Гостиная'
                    },
                    {
                        'source_quote': 'Муж сделал шаг навстречу, а другая женщина оставалась на фоне у окна.',
                        'beat': 'Муж делает шаг к жене, другая женщина остается на заднем плане у окна.',
                        'shot': 'Medium shot of Husband approaching Wife, with the other woman visible in the background',
                        'characters': ['Жена', 'Муж'],
                        'location_ref': 'Гостиная'
                    }
                ]
            }

            # Scene 2 in raw LLM analysis: quote contains "услышала знакомый голос мужа",
            # raw analysis mistakenly lists ['Жена', 'Муж'] in characters.
            analysis_data['scenes'][1] = {
                'source_quote': 'Она поднялась по лестнице и услышала знакомый голос мужа.',
                'beat': 'Она поднимается по лестнице и слушает знакомый голос мужа.',
                'shot': 'Medium shot of Wife on staircase listening to offscreen voice',
                'characters': ['Жена', 'Муж'],
                'visible_people': 1,
                'location_ref': 'Гостиная'
            }

            service.import_analysis(project_id, analysis_data, proj['version'])
            proj_detail = service.detail(project_id)

            c_wife = next(c for c in proj_detail['characters'] if c['name'] == 'Жена')
            c_husband = next(c for c in proj_detail['characters'] if c['name'] == 'Муж')
            loc_room = next(l for l in proj_detail['locations'] if l['name'] == 'Гостиная')

            # Создаём фейковые ассеты и блокируем референсы
            asset_w_id = 'asset_w_portrait'
            asset_h_id = 'asset_h_portrait'
            asset_loc_id = 'asset_loc_living'
            (service.root / 'wife.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00fake')
            (service.root / 'husband.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00fake')
            (service.root / 'loc.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00fake')
            with service.db.transaction() as conn:
                gw = service.insert_generation(conn, project_id, 'character', {'prompt': 'wife'}, character_id=c_wife['id'], character_revision=c_wife['revision'])
                gh = service.insert_generation(conn, project_id, 'character', {'prompt': 'husband'}, character_id=c_husband['id'], character_revision=c_husband['revision'])
                gl = service.insert_generation(conn, project_id, 'location', {'prompt': 'loc'}, location_id=loc_room['id'], location_revision=loc_room['revision'])
                conn.execute(
                    "INSERT OR REPLACE INTO assets(id,project_id,generation_id,path,sha256,width,height,bytes) VALUES(?,?,?,?,?,?,?,?)",
                    (asset_w_id, project_id, gw, 'wife.png', 'sha_wife', 768, 432, 1000)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO assets(id,project_id,generation_id,path,sha256,width,height,bytes) VALUES(?,?,?,?,?,?,?,?)",
                    (asset_h_id, project_id, gh, 'husband.png', 'sha_husband', 768, 432, 1000)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO assets(id,project_id,generation_id,path,sha256,width,height,bytes) VALUES(?,?,?,?,?,?,?,?)",
                    (asset_loc_id, project_id, gl, 'loc.png', 'sha_loc', 768, 432, 1000)
                )
                # Reference locking validates a finished generation and its
                # selected asset; a queued placeholder is not a ready variant.
                for generation_id, asset_id in ((gw, asset_w_id), (gh, asset_h_id), (gl, asset_loc_id)):
                    conn.execute("UPDATE generations SET state='succeeded',asset_id=? WHERE id=?", (asset_id, generation_id))

            service.lock(c_wife['id'], {'portrait': asset_w_id}, c_wife['revision'])
            service.lock(c_husband['id'], {'portrait': asset_h_id}, c_husband['revision'])
            service.lock_location(loc_room['id'], {'environment': asset_loc_id}, loc_room['revision'])

            prompts_res = service.project_prompts(project_id)
            self.assertEqual(len(prompts_res['scenes']), 5)

            for i, sc in enumerate(prompts_res['scenes'], 1):
                self.assertTrue(sc['semantic_valid'], msg=f"Scene {i} semantic error: {sc.get('flow_error')}")
                self.assertTrue(sc['render_ready'], msg=f"Scene {i} render not ready: {sc.get('flow_error')}")
                self.assertTrue(sc['ready_for_flow'], msg=f"Scene {i} not ready for Flow: {sc.get('flow_error')}")

            # Кадр 1: Только референс Жены
            ref_names_1 = [r['name'] for r in prompts_res['scenes'][0]['references']]
            self.assertIn('Жена', ref_names_1)
            self.assertNotIn('Муж', ref_names_1)
            self.assertIn('Solo shot featuring only @Жена', prompts_res['scenes'][0]['flow_prompt'])

            # Кадр 2: Только референс Жены, Муж — голос за кадром (даже если был ошибочно указан в raw characters)
            ref_names_2 = [r['name'] for r in prompts_res['scenes'][1]['references']]
            self.assertIn('Жена', ref_names_2)
            self.assertNotIn('Муж', ref_names_2)
            self.assertIn('Solo shot featuring only @Жена', prompts_res['scenes'][1]['flow_prompt'])
            self.assertIn('Муж is heard offscreen and must not be shown', prompts_res['scenes'][1]['flow_prompt'])
            self.assertNotIn('Featuring @Муж', prompts_res['scenes'][1]['flow_prompt'])
            self.assertNotIn('Featuring @Жена, @Муж', prompts_res['scenes'][1]['flow_prompt'])
            self.assertNotIn('Featuring @Муж, @Жена', prompts_res['scenes'][1]['flow_prompt'])

            # Кадр 4: Жена и Муж в референсах, другая женщина в промпте
            ref_names_4 = [r['name'] for r in prompts_res['scenes'][3]['references']]
            self.assertIn('Жена', ref_names_4)
            self.assertIn('Муж', ref_names_4)
            self.assertIn('другая женщина', prompts_res['scenes'][3]['flow_prompt'])
            self.assertIn('Exactly 3 people total', prompts_res['scenes'][3]['flow_prompt'])

            # Кадр 5: Жена и Муж в референсах, другая женщина в фоне
            ref_names_5 = [r['name'] for r in prompts_res['scenes'][4]['references']]
            self.assertIn('Жена', ref_names_5)
            self.assertIn('Муж', ref_names_5)
            self.assertIn('другая женщина', prompts_res['scenes'][4]['flow_prompt'])
            self.assertIn('Exactly 3 people total', prompts_res['scenes'][4]['flow_prompt'])

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_40_arbitrary_numeric_staging_counts(self):
        """40. Поддержка произвольных чисел в staging_count (11 people, 25 figures, 11 человек, 25 участников)."""
        sc_11_en = extract_scene_staging_count({'shot': 'Wide shot of 11 people in the great hall'})
        self.assertEqual(sc_11_en, 11)

        sc_25_en = extract_scene_staging_count({'shot': '25 figures gathered in the courtyard', 'beat': 'Action'})
        self.assertEqual(sc_25_en, 25)

        sc_11_ru = extract_scene_staging_count({'beat': 'В зале находилось 11 человек в строгих костюмах'})
        self.assertEqual(sc_11_ru, 11)

        sc_25_ru = extract_scene_staging_count({'composition': 'Общий план, 25 участников церемонии'})
        self.assertEqual(sc_25_ru, 25)

    def test_41_episodic_role_promotion_integration(self):
        """41. Сквозная интеграция продвижения эпизодической роли в ключевые персонажи при диалогах в нескольких событиях."""
        from imagegen.cast import CastResolver
        story = (
            "Странник вошел в темную кузницу на краю деревни. "
            "Старый кузнец отложил молот и сказал: «Я ждал тебя много лет, путник». "
            "Странник показал сломанный клинок и попросил о помощи. "
            "На следующий день странник вернулся в кузницу за готовым оружием. "
            "Кузнец вручил ему сверкающий меч и произнес: «Береги его, клинок хранит древнюю силу»."
        )
        resolver = CastResolver(story)
        bible = resolver.resolve([])

        smith = next(c for c in bible['characters'] if 'кузнец' in c['canonical_name'].lower())
        self.assertEqual(smith['status'], 'active')
        self.assertIn('char_', smith['cast_id'])
        self.assertTrue(len(smith['evidence']) >= 2)

    def test_42_shot_sound_ripple_phrase_removal(self):
        """42. Нормализация shot удаляет фразы о визуализации звука/волн/ряби, чтобы не противоречить запрету звуковых волн."""
        project = {'style': 'cinematic film still'}
        scene = {
            'shot': 'Medium shot of Wife on staircase listening to offscreen voice, sound visualized as a subtle ripple in the air',
            'beat': 'Wife stops on staircase listening to offscreen voice, sound visualized as a subtle ripple in the air',
            'characters': ['Жена'],
            'visible_people': 1
        }
        flow_prompt = compile_flow_prompt(project, scene, characters=[self.char_wife], locations=[self.loc_room])
        self.assertNotIn('sound visualized as a subtle ripple in the air', flow_prompt)
        self.assertNotIn('ripple in the air', flow_prompt)
        self.assertIn('Medium shot of Wife on staircase listening to offscreen voice', flow_prompt)
        self.assertIn('Represent voices, music and other sounds only through visible character reactions', flow_prompt)


if __name__ == '__main__':
    unittest.main()

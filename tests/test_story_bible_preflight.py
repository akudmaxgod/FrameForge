"""Tests for Two-Level Story Event Planning, Global Story Bible, and Preflight Validation."""
import unittest
from imagegen.domain import (
    build_global_story_bible,
    validate_project_preflight,
    SHOT_TYPE_ESTABLISHING,
    SHOT_TYPE_MAIN_ACTION,
    SHOT_TYPE_PROTAGONIST_REACTION,
    SHOT_TYPE_PROP_DETAIL,
    SHOT_TYPE_ACTION_AFTERMATH,
    SHOT_TYPE_ATMOSPHERE,
    VALID_SHOT_TYPES,
    BANNED_FILLER_NAMES,
)
from imagegen.providers import expand_storyboard_scenes, attach_exact_sources
from imagegen.cast import CharacterEntityResolver, resolve_scene_pronouns_and_cast


class TestTwoLevelPlanningAndStoryBible(unittest.TestCase):
    def setUp(self):
        self.story = (
            "Дэниел приехал в таёжный посёлок Нортвуд поздней осенью. "
            "Он вошёл в старый деревянный дом и осмотрел пыльную гостиную. "
            "— Здесь кто-нибудь есть? — крикнул он в пустоту. "
            "Никто не ответил, лишь метель шумела за окном. "
            "На следующее утро Дэниел нашёл старинный ключ в ящике стола. "
            "Он взял ключ и открыл запертый сундук."
        )
        self.characters = [
            {
                'id': 'char_daniel',
                'name': 'Дэниел',
                'canonical_name': 'Дэниел',
                'aliases': ['Дэниел', 'Дэн'],
                'role': 'Главный герой, исследователь',
                'description': 'Мужчина 35 лет с короткими темными волосами и проницательными карими глазами.',
                'wardrobe': {'winter': 'Тёплая куртка с капюшоном'}
            }
        ]

    def test_global_story_bible_construction(self):
        bible = build_global_story_bible(self.story, self.characters)
        self.assertIn('protagonist', bible)
        self.assertEqual(bible['protagonist']['canonical_name'], 'Дэниел')
        self.assertIn('world', bible)
        self.assertEqual(bible['world']['setting'], 'Нортвуд')
        self.assertEqual(bible['world']['season'], 'autumn')
        self.assertIn('timeline', bible)
        self.assertTrue(len(bible['timeline']) >= 2)
        # Check day progression
        days = [t['story_day'] for t in bible['timeline']]
        self.assertIn('Day 1', days)
        self.assertIn('Day 2', days)
        self.assertIn('events', bible)
        self.assertTrue(len(bible['events']) >= 4)
        for ev in bible['events']:
            self.assertTrue(ev['event_id'].startswith('evt_'))
            self.assertTrue(bool(ev['source_quote']))

    def test_structured_shot_expansion_types_and_inheritance(self):
        raw_scenes = [
            {
                'event_id': 'evt_1',
                'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                'beat': 'Дэниел выходит из машины на въезде в Нортвуд.',
                'characters': ['Дэниел'],
                'character_ids': ['char_daniel'],
                'visible_people': 1,
                'location': 'Нортвуд',
                'location_ref': 'Нортвуд',
                'time': 'Поздняя осень, вечер',
                'story_day': 'Day 1',
                'season': 'autumn',
                'weather': 'clear',
                'wardrobe': {'Дэниел': 'Тёплая куртка'},
                'props': [],
            },
            {
                'event_id': 'evt_2',
                'source_quote': 'Он взял ключ и открыл запертый сундук.',
                'beat': 'Дэниел открывает старинный сундук медным ключом.',
                'characters': ['Дэниел'],
                'character_ids': ['char_daniel'],
                'visible_people': 1,
                'location': 'Старый дом',
                'location_ref': 'Старый дом',
                'time': 'Утро',
                'story_day': 'Day 2',
                'season': 'autumn',
                'weather': 'snowy',
                'wardrobe': {'Дэниел': 'Тёплая куртка'},
                'props': [{'name': 'ключ', 'state': 'held', 'holder': 'Дэниел'}],
            }
        ]

        result = {'scenes': raw_scenes}
        # Expand 2 events into 10 shots
        expanded = expand_storyboard_scenes(result, 10, camera_mode='cinema')
        scenes = expanded['scenes']
        self.assertEqual(len(scenes), 10)

        # Check shot types are valid
        for s in scenes:
            self.assertIn(s['shot_type'], VALID_SHOT_TYPES)
            self.assertTrue(s['event_id'] in ('evt_1', 'evt_2'))

        # Check atmosphere shot properties (visible_people=0, characters=[])
        atmosphere_shots = [s for s in scenes if s['shot_type'] == SHOT_TYPE_ATMOSPHERE]
        self.assertTrue(len(atmosphere_shots) >= 1)
        for atm in atmosphere_shots:
            self.assertEqual(atm['visible_people'], 0)
            self.assertEqual(atm['characters'], [])
            self.assertEqual(atm['wardrobe'], {})

        # Check inheritance across shots of evt_1
        evt_1_shots = [s for s in scenes if s['event_id'] == 'evt_1']
        for s in evt_1_shots:
            self.assertEqual(s['story_day'], 'Day 1')
            self.assertEqual(s['season'], 'autumn')
            self.assertEqual(s['location'], 'Нортвуд')
            if s['shot_type'] != SHOT_TYPE_ATMOSPHERE:
                self.assertEqual(s['characters'], ['Дэниел'])
                self.assertEqual(s['wardrobe'], {'Дэниел': 'Тёплая куртка'})

    def test_strict_semantic_character_binding_and_pronouns(self):
        cast_bible = {
            'characters': self.characters
        }
        scenes = [
            {
                'source_quote': 'Он вошёл в старый деревянный дом и осмотрел пыльную гостиную.',
                'beat': 'Осмотр гостиной.',
                'shot': 'Medium action shot',
                'visible_people': 1
            },
            {
                'source_quote': '— Здесь кто-нибудь есть? — крикнул он в пустоту.',
                'beat': 'Вопрос в темноту дома.',
                'shot': 'Close-up speech',
                'visible_people': 1
            }
        ]
        resolved = resolve_scene_pronouns_and_cast(scenes, cast_bible, self.story)
        self.assertEqual(resolved[0]['characters'], ['Дэниел'])
        self.assertEqual(resolved[1]['characters'], ['Дэниел'])

    def test_preflight_validation_blocking_rules(self):
        # 1. Valid project passes preflight
        valid_project = {
            'story': self.story,
            'characters': self.characters,
            'scenes': [
                {
                    'event_id': 'evt_1',
                    'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                    'beat': 'Приезд Дэниела в Нортвуд.',
                    'characters': ['Дэниел'],
                    'visible_people': 1,
                    'location': 'Нортвуд',
                    'location_ref': 'Нортвуд',
                    'time': 'Вечер',
                    'story_day': 'Day 1',
                    'season': 'autumn',
                    'wardrobe': {'Дэниел': 'Тёплая куртка'}
                }
            ]
        }
        report = validate_project_preflight(valid_project, self.story)
        self.assertTrue(report['valid'])
        self.assertEqual(len(report['errors']), 0)
        self.assertEqual(report['stats']['protagonist'], 'Дэниел')

        # 2. Missing protagonist in active scene triggers error
        bad_project_missing_protagonist = {
            'story': self.story,
            'characters': self.characters,
            'scenes': [
                {
                    'event_id': 'evt_1',
                    'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                    'beat': 'Приезд в Нортвуд.',
                    'characters': [], # Missing protagonist!
                    'visible_people': 1,
                    'location': 'Нортвуд',
                    'time': 'Вечер',
                }
            ]
        }
        report = validate_project_preflight(bad_project_missing_protagonist, self.story)
        self.assertFalse(report['valid'])
        error_codes = [e['code'] for e in report['errors']]
        self.assertIn('protagonist_missing', error_codes)

        # 3. Unassigned humans without episodic role triggers error
        bad_project_unassigned = {
            'story': self.story,
            'characters': [],
            'scenes': [
                {
                    'event_id': 'evt_1',
                    'source_quote': 'В тишине раздались шаги.',
                    'beat': 'Шаги в доме.',
                    'characters': [],
                    'visible_people': 2, # Ghost humans!
                    'location': 'Дом',
                    'time': 'Ночь',
                }
            ]
        }
        report = validate_project_preflight(bad_project_unassigned, self.story)
        self.assertFalse(report['valid'])
        error_codes = [e['code'] for e in report['errors']]
        self.assertIn('unassigned_humans', error_codes)

        # 4. Abstract filler role triggers error
        bad_project_filler = {
            'story': self.story,
            'characters': [
                {'name': 'странник', 'canonical_name': 'странник', 'description': 'Неизвестный путник'}
            ],
            'scenes': [
                {
                    'event_id': 'evt_1',
                    'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                    'beat': 'Приезд.',
                    'characters': ['странник'],
                    'visible_people': 1,
                    'location': 'Нортвуд',
                    'time': 'Вечер',
                }
            ]
        }
        report = validate_project_preflight(bad_project_filler, self.story)
        self.assertFalse(report['valid'])
        error_codes = [e['code'] for e in report['errors']]
        self.assertIn('abstract_filler_roles', error_codes)

        # 5. Intra-event conflict triggers error
        bad_project_conflict = {
            'story': self.story,
            'characters': self.characters,
            'scenes': [
                {
                    'event_id': 'evt_1',
                    'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                    'beat': 'Приезд.',
                    'characters': ['Дэниел'],
                    'visible_people': 1,
                    'location': 'Нортвуд',
                    'location_ref': 'Нортвуд',
                    'time': 'Вечер',
                    'story_day': 'Day 1',
                    'season': 'autumn',
                    'wardrobe': {'Дэниел': 'Тёплая куртка'}
                },
                {
                    'event_id': 'evt_1',
                    'source_quote': 'Дэниел приехал в таёжный посёлок Нортвуд поздней осенью.',
                    'beat': 'Приезд - крупный план.',
                    'characters': ['Дэниел'],
                    'visible_people': 1,
                    'location': 'Нортвуд',
                    'location_ref': 'Нортвуд',
                    'time': 'Вечер',
                    'story_day': 'Day 2', # Conflict with first shot Day 1!
                    'season': 'autumn',
                    'wardrobe': {'Дэниел': 'Тёплая куртка'}
                }
            ]
        }
        report = validate_project_preflight(bad_project_conflict, self.story)
        self.assertFalse(report['valid'])
        error_codes = [e['code'] for e in report['errors']]
        self.assertIn('intra_event_conflict', error_codes)

    def test_large_scale_expansion_up_to_2000(self):
        raw_scenes = [
            {
                'event_id': f'evt_{i}',
                'source_quote': f'Событие {i} в Нортвуде.',
                'beat': f'Дэниел исследует локацию {i}.',
                'characters': ['Дэниел'],
                'visible_people': 1,
                'location': 'Нортвуд',
                'time': 'День',
                'story_day': 'Day 1',
                'season': 'autumn',
                'weather': 'clear',
                'wardrobe': {'Дэниел': 'Куртка'},
            }
            for i in range(1, 11)
        ]
        result = {'scenes': raw_scenes}
        expanded = expand_storyboard_scenes(result, 2000, camera_mode='cinema')
        scenes = expanded['scenes']
        self.assertEqual(len(scenes), 2000)
        # Every scene must have an event_id and valid shot type
        for s in scenes:
            self.assertTrue(s['event_id'].startswith('evt_'))
            self.assertIn(s['shot_type'], VALID_SHOT_TYPES)
            if s['shot_type'] == SHOT_TYPE_ATMOSPHERE:
                self.assertEqual(s['visible_people'], 0)
                self.assertEqual(len(s['characters']), 0)
            else:
                self.assertEqual(s['characters'], ['Дэниел'])

    def test_toponym_northwood_not_character(self):
        resolver = CharacterEntityResolver(self.story)
        bible = resolver.resolve([{'name': 'Дэниел'}, {'name': 'Нортвуд'}])
        char_names = [c['canonical_name'] for c in bible.get('characters', [])]
        self.assertIn('Дэниел', char_names)
        self.assertNotIn('Нортвуд', char_names)


if __name__ == '__main__':
    unittest.main()

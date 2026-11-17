"""Unit and integration tests for prop importance classification and continuity invariants.

Tests all 12 scenarios from TASK_PROP_IMPORTANCE_CLASSIFICATION.md.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from imagegen.domain import (
    DomainError,
    compile_flow_prompt,
    compile_prompt,
    normalize_prop_continuity,
    validate_analysis,
)
from imagegen.providers import (
    classify_prop_candidate,
    normalize_analysis,
)
from imagegen.service import Service


class TestPropImportanceClassification(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = Service(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Пальто как гардероб: «Алексей входит в своём тёмном пальто»
    def test_1_coat_as_wardrobe_not_in_props(self):
        story = "Алексей входит в своём тёмном пальто. Он осматривает тихий холл."
        raw_analysis = {
            'world': 'Modern detective',
            'characters': [{'name': 'Алексей', 'description': 'Детектив в возрасте 40 лет', 'wardrobe': {}}],
            'locations': [{'name': 'Холл', 'description': 'Просторный холл'}],
            'props': [
                {'name': 'Тёмное пальто', 'description': 'Длинное шерстяное тёмное пальто', 'importance': 'story', 'owner': 'Алексей'}
            ],
            'scenes': [
                {
                    'beat': 'Алексей входит в своём тёмном пальто в холл',
                    'source_quote': 'Алексей входит в своём тёмном пальто.',
                    'location': 'Холл',
                    'location_ref': 'Холл',
                    'time': 'День',
                    'shot': 'Medium shot',
                    'emotion': 'calm',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'characters': ['Алексей'],
                    'wardrobe': {},
                    'props': [{'name': 'Тёмное пальто', 'state': 'worn', 'holder': 'Алексей'}]
                }
            ]
        }
        normalized = normalize_analysis(copy.deepcopy(raw_analysis))
        # Coat must NOT be in props
        prop_names = [p['name'] for p in normalized['props']]
        self.assertNotIn('Тёмное пальто', prop_names)
        self.assertEqual(len(normalized['props']), 0)
        # Coat must be integrated into Alexei's wardrobe
        alexei = next(c for c in normalized['characters'] if c['name'] == 'Алексей')
        self.assertTrue(any('пальто' in desc.lower() for desc in alexei['wardrobe'].values()))
        # Scene props must not contain the coat
        scene = normalized['scenes'][0]
        self.assertEqual(len(scene['props']), 0)

    # 2. Пустая комната: «На столе светится ноутбук», людей нет
    def test_2_empty_room_no_phantom_person_in_prompt_or_references(self):
        project = {
            'id': 'p2',
            'style': 'Cinematic photography',
            'world': 'Office interior',
        }
        laptop_prop = {
            'id': 'prop-laptop',
            'name': 'Ноутбук',
            'description': 'Черный рабочий ноутбук с светящимся экраном',
            'importance': 'story',
            'locked': True,
            'references': {'prop': 'asset-laptop-1'},
            'references_json': json.dumps({'prop': 'asset-laptop-1'})
        }
        scene = {
            'beat': 'На столе в пустой комнате светится ноутбук',
            'source_quote': 'На столе светится ноутбук.',
            'location': 'Кабинет',
            'location_ref': 'Кабинет',
            'visible_people': 0,
            'characters': [],
            'props': [
                {'name': 'Ноутбук', 'state': 'placed', 'holder': '', 'visible': True}
            ]
        }
        flow_prompt = compile_flow_prompt(project, scene, characters=(), locations=(), props=[laptop_prop])
        # Prompt must have uninhabited negative guard
        self.assertIn('Uninhabited scene, empty setting, no people', flow_prompt)
        self.assertIn('no empty clothes in body shape', flow_prompt)
        self.assertNotIn('@', flow_prompt)
        # Check comfy prompt as well
        comfy_prompt, refs = compile_prompt(project, scene, characters=(), locations=(), props=[laptop_prop])
        self.assertIn('UNINHABITED SCENE', comfy_prompt)
        self.assertIn('no empty floating or standing clothing in human shape', comfy_prompt)
        # Ref for placed laptop is allowed, but no character refs
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['prop_id'], 'prop-laptop')

    # 3. Снятое пальто: «Алексей снимает пальто и вешает на крючок» (holder='')
    def test_3_removed_coat_placed_without_holder(self):
        scenes = [
            {'beat': 'Алексей входит в комнату', 'source_quote': 'Алексей входит в комнату.'},
            {'beat': 'Алексей снимает пальто и вешает на крючок', 'source_quote': 'Алексей снимает пальто и вешает на крючок. В кармане лежит ключ.'},
            {'beat': 'Марина находит в кармане ключ', 'source_quote': 'Марина находит в кармане ключ.'}
        ]
        classification = classify_prop_candidate('Снятое пальто', 'Пальто, повешенное на крючок', scenes=scenes)
        self.assertEqual(classification, 'story')

        # Continuity test: removed coat becomes placed, holder is cleared
        data = {
            'props': [{'name': 'Пальто', 'importance': 'story', 'first_appearance': 2}],
            'characters': [{'name': 'Алексей'}],
            'scenes': [
                {
                    'beat': 'Алексей в комнате',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': [{'name': 'Пальто', 'state': 'worn', 'holder': 'Алексей'}]
                },
                {
                    'beat': 'Алексей снял пальто и повесил на крючок',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': [{'name': 'Пальто', 'state': 'placed', 'holder': ''}]
                },
                {
                    'beat': 'Пустая прихожая, пальто висит на крючке',
                    'characters': [],
                    'visible_people': 0,
                    'props': [{'name': 'Пальто', 'state': 'placed', 'holder': ''}]
                }
            ]
        }
        res = normalize_prop_continuity(data)
        # In scene 3 (empty room), placed coat is visible and holder is empty
        s3_coat = next(p for p in res['scenes'][2]['props'] if p['name'] == 'Пальто')
        self.assertEqual(s3_coat['state'], 'placed')
        self.assertEqual(s3_coat['holder'], '')
        self.assertTrue(s3_coat['visible'])

    # 4. Кастрюля: контрольная история сохраняет прежнее правильное поведение
    def test_4_cooking_pot_story_continuity_lifecycle(self):
        classification = classify_prop_candidate('Кастрюля', 'Старая алюминиевая кастрюля с двумя ручками')
        self.assertEqual(classification, 'story')

        data = {
            'props': [
                {'name': 'Кастрюля', 'importance': 'story', 'first_appearance': 2, 'owner': 'Алексей'}
            ],
            'characters': [{'name': 'Алексей'}],
            'scenes': [
                {
                    'beat': 'Алексей идёт по двору',
                    'source_quote': 'Алексей идёт по двору.',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': []
                },
                {
                    'beat': 'Алексей находит кастрюлю и надевает себе на голову',
                    'source_quote': 'Алексей находит кастрюлю и надевает себе на голову.',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': [{'name': 'Кастрюля', 'state': 'worn_on_head', 'holder': 'Алексей'}]
                },
                {
                    'beat': 'Алексей с кастрюлей на голове входит в подъезд',
                    'source_quote': 'Алексей с кастрюлей на голове входит в подъезд.',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': []  # Continuity should propagate worn_on_head
                }
            ]
        }
        res = normalize_prop_continuity(data)
        # Scene 1: before appearance -> absent
        self.assertEqual(len(res['scenes'][0]['props']), 0)
        # Scene 2: worn_on_head
        p_s2 = next(p for p in res['scenes'][1]['props'] if p['name'] == 'Кастрюля')
        self.assertEqual(p_s2['state'], 'worn_on_head')
        self.assertEqual(p_s2['holder'], 'Алексей')
        self.assertTrue(p_s2['visible'])
        # Scene 3: propagated worn_on_head
        p_s3 = next(p for p in res['scenes'][2]['props'] if p['name'] == 'Кастрюля')
        self.assertEqual(p_s3['state'], 'worn_on_head')
        self.assertEqual(p_s3['holder'], 'Алексей')
        self.assertTrue(p_s3['visible'])
        self.assertEqual(p_s3['continuity'], 'continues')

    # 5. Одноразовая кружка: упомянута один раз без последствий -> incidental
    def test_5_incidental_single_use_mug(self):
        scenes = [{'beat': 'Марина пьет кофе из керамической кружки', 'source_quote': 'Марина пьет кофе из кружки.'}]
        classification = classify_prop_candidate('Кружка', 'Обычная белая керамическая кружка', scenes=scenes)
        self.assertEqual(classification, 'incidental')

        analysis = {
            'characters': [{'name': 'Марина', 'description': 'Девушка 25 лет', 'wardrobe': {}}],
            'locations': [{'name': 'Кафе', 'description': 'Уютное кафе'}],
            'props': [{'name': 'Кружка', 'description': 'Белая кружка', 'importance': 'incidental'}],
            'scenes': [
                {
                    'beat': 'Марина пьет кофе',
                    'source_quote': 'Марина пьет кофе из кружки.',
                    'location': 'Кафе',
                    'location_ref': 'Кафе',
                    'characters': ['Марина'],
                    'visible_people': 1,
                    'props': [{'name': 'Кружка', 'state': 'held', 'holder': 'Марина'}]
                }
            ]
        }
        normalized = normalize_analysis(analysis)
        self.assertEqual(len(normalized['props']), 0)
        self.assertEqual(len(normalized['scenes'][0]['props']), 0)

    # 6. Сюжетный ключ: появляется один раз, но открывает сейф -> story
    def test_6_story_key_unlocks_safe(self):
        scenes = [{'beat': 'Алексей достает старинный ключ и открывает сейф', 'source_quote': 'Алексей открывает ключом тяжелый сейф.'}]
        classification = classify_prop_candidate('Старинный ключ', 'Бронзовый ключ с гравировкой', scenes=scenes)
        self.assertEqual(classification, 'story')

    def test_plot_critical_wearable_is_story_not_plain_wardrobe(self):
        scenes = [
            {'source_quote': 'The hero finds a magical iron helmet.'},
            {'source_quote': 'The magical helmet grants him the power of invisibility.'},
        ]
        classification = classify_prop_candidate(
            'Iron helmet', 'An enchanted helmet that grants invisibility', scenes=scenes)
        self.assertEqual(classification, 'story')

    # 7. Экранное содержимое: Messenger window не создаётся как физический prop
    def test_7_messenger_window_is_incidental_not_physical_prop(self):
        scenes = [{'beat': 'На экране ноутбука открыто окно мессенджера', 'source_quote': 'На экране светится окно мессенджера.'}]
        classification = classify_prop_candidate('Messenger window', 'Chat window with unread messages', scenes=scenes)
        self.assertEqual(classification, 'incidental')
        classification_ru = classify_prop_candidate('Окно мессенджера', 'Всплывающее окно переписки', scenes=scenes)
        self.assertEqual(classification_ru, 'incidental')

    # 8. Несколько активных предметов: «Марина держит чашку» не переводит ноутбук в held
    def test_8_multiple_active_items_cue_held_is_item_specific(self):
        data = {
            'props': [
                {'name': 'Ноутбук', 'importance': 'story', 'first_appearance': 1},
                {'name': 'Чашка', 'importance': 'story', 'first_appearance': 1}
            ],
            'characters': [{'name': 'Марина'}],
            'scenes': [
                {
                    'beat': 'Марина сидит за столом. На столе стоит ноутбук.',
                    'source_quote': 'На столе стоит ноутбук.',
                    'characters': ['Марина'],
                    'visible_people': 1,
                    'props': [{'name': 'Ноутбук', 'state': 'placed', 'holder': ''}]
                },
                {
                    'beat': 'Марина держит чашку горячего чая',
                    'source_quote': 'Марина держит чашку.',
                    'characters': ['Марина'],
                    'visible_people': 1,
                    'props': []  # Cue held should only affect чашка, NOT ноутбук
                }
            ]
        }
        res = normalize_prop_continuity(data)
        s2 = res['scenes'][1]
        laptop = next((p for p in s2['props'] if p['name'] == 'Ноутбук'), None)
        cup = next((p for p in s2['props'] if p['name'] == 'Чашка'), None)

        self.assertIsNotNone(cup)
        self.assertEqual(cup['state'], 'held')
        self.assertEqual(cup['holder'], 'Марина')

        # Laptop must NOT be held!
        if laptop:
            self.assertNotEqual(laptop['state'], 'held')

    # 9. Владелец вне кадра: worn/held + visible=true без владельца -> visible=false
    def test_9_holder_out_of_frame_suppressed_visibility(self):
        data = {
            'props': [{'name': 'Кастрюля', 'importance': 'story', 'first_appearance': 1}],
            'characters': [{'name': 'Алексей'}, {'name': 'Марина'}],
            'scenes': [
                {
                    'beat': 'Алексей надел кастрюлю',
                    'characters': ['Алексей'],
                    'visible_people': 1,
                    'props': [{'name': 'Кастрюля', 'state': 'worn_on_head', 'holder': 'Алексей'}]
                },
                {
                    'beat': 'Марина одна на кухне',
                    'characters': ['Марина'],  # Alexei is NOT in this scene!
                    'visible_people': 1,
                    'props': []
                },
                {
                    'beat': 'Пустая комната',
                    'characters': [],
                    'visible_people': 0,
                    'props': []
                }
            ]
        }
        res = normalize_prop_continuity(data)
        # In scene 2, Alexei's pot cannot be visible while Alexei is absent
        s2_props = res['scenes'][1]['props']
        s2_pot = next((p for p in s2_props if p['name'] == 'Кастрюля'), None)
        if s2_pot:
            self.assertFalse(s2_pot['visible'])

        # In scene 3 (uninhabited), worn_on_head is strictly visible=False
        s3_props = res['scenes'][2]['props']
        s3_pot = next((p for p in s3_props if p['name'] == 'Кастрюля'), None)
        if s3_pot:
            self.assertFalse(s3_pot['visible'])

    # 10. Старый проект: значение 'standard' / 'recurring' нормализуется в 'story'
    def test_10_legacy_project_importance_normalization(self):
        analysis = {
            'characters': [{'name': 'Алексей', 'description': 'Герой', 'wardrobe': {}}],
            'locations': [{'name': 'Комната', 'description': 'Комната'}],
            'props': [
                {'name': 'Кастрюля', 'description': 'Кастрюля', 'importance': 'standard'},
                {'name': 'Ключ', 'description': 'Ключ', 'importance': 'recurring'}
            ],
            'scenes': [
                {
                    'beat': 'Алексей находит кастрюлю',
                    'source_quote': 'Алексей находит кастрюлю.',
                    'characters': ['Алексей'],
                    'wardrobe': {},
                    'location': 'Комната',
                    'location_ref': 'Комната',
                    'time': 'День',
                    'shot': 'Medium shot',
                    'emotion': 'calm',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'props': [{'name': 'Кастрюля', 'state': 'held', 'holder': 'Алексей'}]
                }
            ]
        }
        validated = validate_analysis(analysis, 'Алексей находит кастрюлю.')
        for p in validated['props']:
            self.assertEqual(p['importance'], 'story')

    # 11. Очередь Flow: генерируются только story props, и только видимые референсы передаются в сцену
    def test_11_flow_project_prompts_only_story_props_and_visible_refs(self):
        project = self.service.create_project({
            'title': 'Flow Test Project',
            'story': 'Алексей держит кастрюлю. В комнате стоит стол.',
            'style': 'Cinematic',
            'world': 'Realism',
            'mode': 'characters'
        })
        p_id = project['id']

        # Add characters, locations, and props directly
        with self.service.db.transaction() as conn:
            conn.execute(
                "INSERT INTO characters(id, project_id, name, description, wardrobe, references_json, locked, revision) "
                "VALUES('c1', ?, 'Алексей', 'Мужчина 35 лет', '{}', ?, 1, 1)",
                (p_id, json.dumps({'portrait': 'asset-char'}))
            )
            conn.execute(
                "INSERT INTO locations(id, project_id, name, description, references_json, locked, revision) "
                "VALUES('l1', ?, 'Комната', 'Интерьер комнаты', ?, 1, 1)",
                (p_id, json.dumps({'environment': 'asset-loc'}))
            )
            # 1 story prop (locked), 1 wardrobe item (not story), 1 set dressing (not story)
            conn.execute(
                "INSERT INTO props(id, project_id, name, description, importance, first_appearance, owner, default_state, references_json, locked, revision) "
                "VALUES('p1', ?, 'Кастрюля', 'Кастрюля', 'key', 1, 'Алексей', 'held', ?, 1, 1)",
                (p_id, json.dumps({'prop': 'asset-pot'}))
            )
            conn.execute(
                "INSERT INTO props(id, project_id, name, description, importance, first_appearance, owner, default_state, references_json, locked, revision) "
                "VALUES('p2', ?, 'Пальто', 'Пальто', 'wardrobe', 1, 'Алексей', 'worn', '{}', 0, 1)",
                (p_id,)
            )
            conn.execute(
                "INSERT INTO props(id, project_id, name, description, importance, first_appearance, owner, default_state, references_json, locked, revision) "
                "VALUES('p3', ?, 'Стол', 'Стол', 'set_dressing', 1, '', 'placed', '{}', 0, 1)",
                (p_id,)
            )

            # Scene 1: Alexei holding pot
            scene1_data = {
                'beat': 'Алексей держит кастрюлю',
                'location': 'Комната',
                'location_ref': 'Комната',
                'characters': ['Алексей'],
                'visible_people': 1,
                'props': [
                    {'name': 'Кастрюля', 'state': 'held', 'holder': 'Алексей', 'visible': True},
                    {'name': 'Пальто', 'state': 'worn', 'holder': 'Алексей', 'visible': True}
                ]
            }
            conn.execute("INSERT INTO scenes(id, project_id, position, current_revision) VALUES('s1', ?, 1, 1)", (p_id,))
            conn.execute("INSERT INTO scene_revisions(scene_id, revision, data, created_at) VALUES('s1', 1, ?, 'now')", (json.dumps(scene1_data),))

        prompts_res = self.service.project_prompts(p_id)

        # Props returned by project_prompts must only contain 'Кастрюля'
        returned_props = prompts_res['props']
        self.assertEqual(len(returned_props), 1)
        self.assertEqual(returned_props[0]['name'], 'Кастрюля')
        self.assertEqual(returned_props[0]['importance'], 'story')

        # Locked props in project_prompts must only be 'Кастрюля'
        self.assertEqual(len(prompts_res['locked_props']), 1)
        self.assertEqual(prompts_res['locked_props'][0]['name'], 'Кастрюля')

        # Scene 1 references must include char, loc, pot — but NOT coat or table!
        s1_refs = prompts_res['scenes'][0]['references']
        ref_names = [r['name'] for r in s1_refs]
        self.assertIn('Алексей', ref_names)
        self.assertIn('Комната', ref_names)
        self.assertIn('Кастрюля', ref_names)
        self.assertNotIn('Пальто', ref_names)
        self.assertNotIn('Стол', ref_names)

    # 12. UI / Batch generation not blocked by non-story props
    def test_12_batch_generation_not_blocked_by_non_story_props(self):
        project = self.service.create_project({
            'title': 'Batch Test',
            'story': 'Алексей находит ключ.',
            'style': 'Cinematic',
            'world': 'Realism',
            'mode': 'characters'
        })
        p_id = project['id']

        with self.service.db.transaction() as conn:
            conn.execute(
                "INSERT INTO characters(id, project_id, name, description, wardrobe, references_json, locked, revision) "
                "VALUES('c1', ?, 'Алексей', 'Мужчина 35 лет', '{}', ?, 1, 1)",
                (p_id, json.dumps({'portrait': 'asset-char'}))
            )
            conn.execute(
                "INSERT INTO locations(id, project_id, name, description, references_json, locked, revision) "
                "VALUES('l1', ?, 'Комната', 'Интерьер', ?, 1, 1)",
                (p_id, json.dumps({'environment': 'asset-loc'}))
            )
            # 1 story prop (locked)
            conn.execute(
                "INSERT INTO props(id, project_id, name, description, importance, first_appearance, owner, default_state, references_json, locked, revision) "
                "VALUES('p1', ?, 'Ключ', 'Ключ', 'story', 1, 'Алексей', 'held', ?, 1, 1)",
                (p_id, json.dumps({'prop': 'asset-key'}))
            )
            # 1 UNLOCKED non-story prop (incidental)
            conn.execute(
                "INSERT INTO props(id, project_id, name, description, importance, first_appearance, owner, default_state, references_json, locked, revision) "
                "VALUES('p2', ?, 'Чашка', 'Чашка', 'incidental', 1, '', 'placed', '{}', 0, 1)",
                (p_id,)
            )
            scene_data = {
                'beat': 'Алексей держит ключ',
                'location': 'Комната',
                'location_ref': 'Комната',
                'characters': ['Алексей'],
                'visible_people': 1,
                'props': [{'name': 'Ключ', 'state': 'held', 'holder': 'Алексей', 'visible': True}]
            }
            conn.execute("INSERT INTO scenes(id, project_id, position, current_revision) VALUES('s1', ?, 1, 1)", (p_id,))
            conn.execute("INSERT INTO scene_revisions(scene_id, revision, data, created_at) VALUES('s1', 1, ?, 'now')", (json.dumps(scene_data),))

        # Batch generation must SUCCEED because all characters, locations, and STORY props are locked.
        # The unlocked incidental prop ('Чашка') must NOT block the batch!
        batch_res = self.service.batch(p_id, 'idempotency-key-test-12')
        self.assertIsNotNone(batch_res)
        self.assertEqual(batch_res['project_id'], p_id)


if __name__ == '__main__':
    unittest.main()

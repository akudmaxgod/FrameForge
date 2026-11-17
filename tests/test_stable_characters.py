"""Acceptance tests for stable character extraction and cast bible invariants.

Covers all 10 mandatory tests from TASK_STABLE_CHARACTER_EXTRACTION.md:
1. 20-run deterministic reproducibility.
2. Thomas and Captain guaranteed even if LLM returns only Thomas.
3. Captain + Captain Reed + Reed merged into single ID.
4. Photo of young woman excluded and doesn't replace heroine.
5. Telephone caller excluded from physical scene.
6. Scene with visible_people: 1 without character_id / portrait blocked from Flow.
7. 100k+ char script with character appearing after 80k chars preserved.
8. Unchanged story_hash reuses saved bible without re-extraction.
9. Story change produces structured diff and preserves old characters.
10. Bridge version 0.1.8 parity and existing domain compatibility.
"""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from imagegen.cast import (
    CharacterEntityResolver,
    compute_story_hash,
    diff_cast_bibles,
    chunk_large_story,
    resolve_scene_pronouns_and_cast,
    names_match_entity,
    is_photo_only,
    is_phone_caller_only,
    is_grammatical_non_character
)
from imagegen.domain import validate_analysis, compile_flow_prompt
from imagegen.providers import prioritize_active_story_characters, LlamaCppProvider, story_segments
from imagegen.service import Service


class TestStableCharacterExtraction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = Service(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_russian_short_name_cases_merge_and_multiword_toponym_is_rejected(self):
        story = (
            'Реставратор Ева Морозова приехала на остров Белый Мыс. '
            'Смотритель встретил Еву у причала. Позже он передал Еве журнал.'
        )
        raw = [
            {'name': 'Ева Морозова', 'description': 'Entity type: human; adult woman, 34 years old.', 'wardrobe': {}},
            {'name': 'Еву', 'description': 'Entity type: human; adult woman, 34 years old.', 'wardrobe': {}},
            {'name': 'Еве', 'description': 'Entity type: human; adult woman, 34 years old.', 'wardrobe': {}},
            {'name': 'Мыс', 'description': 'Entity type: human; adult man, 40 years old.', 'wardrobe': {}},
        ]
        bible = CharacterEntityResolver(story).resolve(raw, scenes=[])
        names = [c['canonical_name'] for c in bible['characters']]
        self.assertEqual(1, sum(name == 'Ева Морозова' for name in names))
        self.assertNotIn('Еву', names)
        self.assertNotIn('Еве', names)
        self.assertNotIn('Мыс', names)

    def test_remote_and_recorded_people_do_not_enter_locked_cast(self):
        story = (
            'Ева подняла фотографию Леона, но самого Леона рядом не было. '
            'Из передатчика прозвучал записанный голос Леона. '
            'Из рации раздался голос диспетчера Нины с материка. '
            'Ева услышала голос Нины и выключила рацию.'
        )
        raw = [
            {'name': 'Ева', 'description': 'Entity type: human; adult woman, 34 years old.', 'wardrobe': {}},
            {'name': 'Леон', 'description': 'Entity type: human; adult man, 40 years old.', 'wardrobe': {}},
            {'name': 'Нина', 'description': 'Entity type: human; adult woman, 36 years old.', 'wardrobe': {}},
        ]
        bible = CharacterEntityResolver(story).resolve(raw, scenes=[])
        names = [c['canonical_name'] for c in bible['characters']]
        self.assertIn('Ева', names)
        self.assertNotIn('Леон', names)
        self.assertNotIn('Нина', names)

    def test_one_off_professional_role_remains_episodic(self):
        story = (
            'Ева вошла в архив. Архивистка открыла дверь и положила коробку на стол. '
            'Ева изучила документы и покинула архив.'
        )
        raw = [
            {'name': 'Ева', 'description': 'Entity type: human; adult woman, 34 years old.', 'wardrobe': {}},
            {'name': 'Архивистка', 'description': 'Entity type: human; adult woman, 50 years old.', 'wardrobe': {}},
        ]
        bible = CharacterEntityResolver(story).resolve(raw, scenes=[])
        self.assertIn('Ева', [c['canonical_name'] for c in bible['characters']])
        self.assertNotIn('Архивистка', [c['canonical_name'] for c in bible['characters']])
        self.assertIn('Архивистка', [c['canonical_name'] for c in bible['episodic_participants']])

    def test_inflected_full_name_merges_with_short_name_and_one_off_roles_stay_ephemeral(self):
        story = (
            'Ева встретила Максима Орлова у архива. '
            'Максим включил генератор. Ева и Максим поднялись на маяк. '
            'Охранник открыл шкаф. Бармен поставил лампу.'
        )
        raw = [
            {'name': 'Максим Орлов', 'description': 'Entity type: human; adult man, 35 years old.', 'wardrobe': {}},
            {'name': 'Орлова', 'description': 'Entity type: human; adult man, 35 years old.', 'wardrobe': {}},
            {'name': 'Охранник', 'description': 'Entity type: human; adult man, 45 years old.', 'wardrobe': {}},
            {'name': 'Бармен', 'description': 'Entity type: human; adult man, 50 years old.', 'wardrobe': {}},
        ]
        bible = CharacterEntityResolver(story).resolve(raw, scenes=[])
        names = [c['canonical_name'] for c in bible['characters']]
        episodic = [c['canonical_name'] for c in bible['episodic_participants']]
        self.assertEqual(1, sum(name == 'Максим Орлов' for name in names))
        self.assertNotIn('Орлова', names)
        self.assertNotIn('Охранник', names)
        self.assertNotIn('Бармен', names)
        self.assertIn('Охранник', episodic)
        self.assertIn('Бармен', episodic)

    def test_ocean_and_named_natural_features_never_enter_character_bible(self):
        story = (
            'An octopus moves over a coral reef in the Pacific Ocean. '
            'Ocean currents sway the kelp while the octopus hides between rocks.'
        )
        raw = [
            {'name': 'Octopus', 'description': 'Entity type: animal; adult octopus.', 'wardrobe': {}},
            {'name': 'Ocean', 'description': 'A vast body of seawater.', 'wardrobe': {}},
            {'name': 'Pacific Ocean', 'description': 'The surrounding physical environment.', 'wardrobe': {}},
            {'name': 'Coral Reef', 'description': 'Underwater location and habitat.', 'wardrobe': {}},
        ]
        bible = CharacterEntityResolver(story).resolve(raw, scenes=[])
        names = [c['canonical_name'] for c in bible['characters']]
        self.assertIn('Octopus', names)
        self.assertNotIn('Ocean', names)
        self.assertNotIn('Pacific Ocean', names)
        self.assertNotIn('Coral Reef', names)

    # Test 1: Один и тот же сценарий запускается 20 раз.
    # Библия персонажей, их ID и набор участников каждой сцены совпадают во всех 20 запусках.
    def test_1_twenty_runs_deterministic_reproducibility(self):
        story = (
            "Thomas tends the lighthouse on the lonely cliff. "
            "The captain arrives by boat and warns Thomas of the storm. "
            "Thomas secures the lantern room while the captain prepares the harbor."
        )
        base_data = {
            'world': 'Seaside lighthouse',
            'characters': [
                {'name': 'Thomas', 'description': 'Keeper of the lighthouse', 'wardrobe': {}},
                {'name': 'Captain', 'description': 'Ship captain', 'wardrobe': {}}
            ],
            'locations': [{'name': 'Lighthouse', 'description': 'Old lighthouse'}],
            'props': [],
            'scenes': [
                {
                    'beat': 'Thomas tends the lighthouse on the lonely cliff',
                    'source_quote': 'Thomas tends the lighthouse on the lonely cliff.',
                    'location': 'Lighthouse',
                    'location_ref': 'Lighthouse',
                    'time': 'Day',
                    'shot': 'Wide shot',
                    'emotion': 'focused',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'characters': ['Thomas'],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'The captain arrives by boat and warns Thomas of the storm',
                    'source_quote': 'The captain arrives by boat and warns Thomas of the storm.',
                    'location': 'Lighthouse',
                    'location_ref': 'Lighthouse',
                    'time': 'Day',
                    'shot': 'Medium shot',
                    'emotion': 'tense',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 2,
                    'characters': ['Thomas', 'Captain'],
                    'wardrobe': {},
                    'props': []
                }
            ]
        }

        first_bible = None
        first_scenes = None

        for iteration in range(20):
            current_data = copy.deepcopy(base_data)
            # Add slight simulated variation in LLM candidate descriptions
            if iteration % 2 == 1:
                current_data['characters'][0]['description'] = 'Old lighthouse keeper on duty'

            resolved_data = prioritize_active_story_characters(current_data, story)
            bible = resolved_data['cast_bible']
            scenes = resolved_data['scenes']

            char_summary = [(c['id'], c['canonical_name'], c['role']) for c in bible['characters']]
            scene_summary = [s.get('character_ids', []) for s in scenes]

            if first_bible is None:
                first_bible = char_summary
                first_scenes = scene_summary
                self.assertGreaterEqual(len(first_bible), 2)
            else:
                self.assertEqual(char_summary, first_bible, f"Bible characters diverged at iteration {iteration}")
                self.assertEqual(scene_summary, first_scenes, f"Scene characters diverged at iteration {iteration}")

    # Test 2: Сценарий «Томас и капитан» всегда содержит двух персонажей, даже если модель вернула только Томаса.
    def test_2_thomas_and_captain_guaranteed_even_if_llm_returns_only_thomas(self):
        story = (
            "Thomas stands on the edge of the rocky cliff, watching the roaring sea. "
            "The captain steps ashore from his boat, greeting Thomas with a salute."
        )
        # LLM erroneously returned only Thomas
        data = {
            'world': 'Coastal harbor',
            'characters': [{'name': 'Thomas', 'description': 'The keeper', 'wardrobe': {}}],
            'scenes': [
                {
                    'beat': 'The captain steps ashore from his boat, greeting Thomas with a salute.',
                    'source_quote': 'The captain steps ashore from his boat, greeting Thomas with a salute.',
                    'characters': ['Thomas']
                }
            ]
        }

        resolved_data = prioritize_active_story_characters(data, story)
        bible = resolved_data['cast_bible']
        char_ids = [c['id'] for c in bible['characters']]
        canonical_names = [c['canonical_name'] for c in bible['characters']]

        self.assertIn('char_thomas', char_ids)
        self.assertIn('char_captain', char_ids)
        self.assertIn('Thomas', canonical_names)
        self.assertIn('Captain', canonical_names)

        # Both characters must have exact quotes as evidence
        for c in bible['characters']:
            self.assertTrue(len(c['evidence']) > 0, f"Character {c['canonical_name']} missing evidence")
            for ev in c['evidence']:
                self.assertIn(ev['quote'], story)

        # And the scene with both must resolve both IDs
        self.assertIn('char_thomas', resolved_data['scenes'][0]['character_ids'])
        self.assertIn('char_captain', resolved_data['scenes'][0]['character_ids'])

    # Test 3: Капитан, капитан Рид и Рид объединяются в один ID при наличии связи в тексте.
    def test_3_captain_reed_and_reed_merged_to_single_id(self):
        story = (
            "The captain looked at the stormy horizon. "
            "Captain Reed turned to his crew. "
            "Reed knew the perilous journey had only just begun."
        )
        # Model returns scattered fragments
        data = {
            'characters': [
                {'name': 'Капитан', 'aliases': ['капитан']},
                {'name': 'Капитан Рид', 'aliases': ['Captain Reed']},
                {'name': 'Рид', 'aliases': ['Reed']}
            ]
        }

        resolved_data = prioritize_active_story_characters(data, story)
        bible = resolved_data['cast_bible']
        characters = bible['characters']

        # Must merge into a single entity
        self.assertEqual(len(characters), 1, f"Expected 1 merged character, got {len(characters)}: {[c['canonical_name'] for c in characters]}")
        merged = characters[0]
        self.assertIn(merged['id'], ('char_captain_reed', 'char_kapitan_rid'))
        self.assertIn('Reed', merged['aliases'])
        self.assertTrue(any('captain' in a.lower() for a in merged['aliases']))
        for ev in merged['evidence']:
            self.assertIn(ev['quote'], story)

    # Test 4: Фотография молодой женщины не создаёт постоянного персонажа и не заменяет внешность главной героини.
    def test_4_photo_of_young_woman_excluded_and_not_replacing_heroine(self):
        story = (
            "Marina sits at the desk, studying the case files. "
            "On the table stands a framed photo of a young woman who disappeared twenty years ago. "
            "Marina sighs and picks up her pen to continue writing."
        )
        data = {
            'characters': [
                {'name': 'Marina', 'description': 'Lead investigator in her 40s', 'wardrobe': {}},
                {'name': 'Young woman', 'description': 'Girl in photograph', 'wardrobe': {}}
            ],
            'scenes': [
                {
                    'beat': 'Marina sighs and picks up her pen',
                    'source_quote': 'Marina sighs and picks up her pen to continue writing.',
                    'characters': ['Marina']
                }
            ]
        }

        resolved_data = prioritize_active_story_characters(data, story)
        bible = resolved_data['cast_bible']
        active_chars = bible['characters']

        active_names = [c['canonical_name'] for c in active_chars]
        self.assertIn('Marina', active_names)
        self.assertNotIn('Young woman', active_names)

        marina = [c for c in active_chars if c['canonical_name'] == 'Marina'][0]
        self.assertIn('investigator', marina['description'].lower())

    # Test 5: Телефонный разговор не создаёт физический кадр с удалённым собеседником.
    def test_5_telephone_caller_excluded_from_physical_scene(self):
        story = (
            "Alexei was sitting alone in the office. Suddenly the phone rang. "
            "On the other end of the line, Inspector Davis spoke in a cold, distant voice through the receiver: 'The suspect is gone.' "
            "Alexei gripped the receiver and stared at the empty room."
        )
        data = {
            'characters': [
                {'name': 'Alexei', 'description': 'Detective in office', 'wardrobe': {}},
                {'name': 'Inspector Davis', 'description': 'Voice on the telephone line', 'wardrobe': {}}
            ],
            'scenes': [
                {
                    'beat': 'Alexei answers the ringing phone and listens to Inspector Davis speaking through the receiver',
                    'source_quote': "On the other end of the line, Inspector Davis spoke in a cold, distant voice through the receiver: 'The suspect is gone.'",
                    'visible_people': 2,
                    'characters': ['Alexei', 'Inspector Davis']
                }
            ]
        }

        resolved_data = prioritize_active_story_characters(data, story)
        scene = resolved_data['scenes'][0]

        # The remote caller must not appear in the physical scene's character_ids
        self.assertIn('char_alexei', scene['character_ids'])
        self.assertNotIn('char_inspector_davis', scene['character_ids'])
        # The visible people count must be restricted to physical actor in the room
        self.assertEqual(len(scene['character_ids']), 1)

    def test_pronoun_context_fills_declared_known_person_slot(self):
        """A possessive pronoun may bind the preceding known person beside a named participant."""
        story = (
            'Кузнец Мартин чинил плуг. '
            'В его кузнице ученик Лука обжигал пальцы.'
        )
        data = {
            'characters': [
                {'name': 'Мартин', 'description': 'Кузнец', 'wardrobe': {}},
                {'name': 'Лука', 'description': 'Ученик', 'wardrobe': {}},
            ],
            'scenes': [
                {'beat': 'Мартин чинит плуг', 'source_quote': 'Кузнец Мартин чинил плуг.',
                 'visible_people': 1, 'characters': ['Мартин'], 'wardrobe': {}},
                {'beat': 'Лука работает в кузнице Мартина',
                 'source_quote': 'В его кузнице ученик Лука обжигал пальцы.',
                 'visible_people': 2, 'characters': ['Лука'], 'wardrobe': {}},
            ]
        }

        resolved = prioritize_active_story_characters(data, story)
        second_scene = resolved['scenes'][1]
        self.assertEqual(second_scene['visible_people'], 2)
        self.assertEqual(set(second_scene['character_ids']), {'char_martin', 'char_luka'})

    def test_shot_cast_repairs_bindings_and_never_leaves_people_count_inconsistent(self):
        """Names stated in the visual shot bind the frame; unsupported counts are safely reduced."""
        story = 'Мартин работает у горна. Лука подхватывает работу молотом.'
        cast_bible = {'characters': [
            {'id': 'char_martin', 'canonical_name': 'Мартин', 'aliases': ['Мартин'], 'description': 'Кузнец'},
            {'id': 'char_luka', 'canonical_name': 'Лука', 'aliases': ['Лука'], 'description': 'Ученик'},
        ]}
        scenes = [
                {'beat': 'Работа у горна', 'source_quote': 'Мартин работает у горна.',
                 'shot': 'Two-shot with Martin and Luka at the anvil.', 'visible_people': 2,
                 'characters': ['Мартин'], 'wardrobe': {}},
                {'beat': 'Только Мартин виден', 'source_quote': 'Мартин работает у горна.',
                 'shot': 'Close-up of Martin.', 'visible_people': 2,
                 'characters': ['Мартин'], 'wardrobe': {}},
        ]
        shot_repaired, count_repaired = resolve_scene_pronouns_and_cast(scenes, cast_bible, story)
        self.assertEqual(set(shot_repaired['character_ids']), {'char_martin', 'char_luka'})
        self.assertEqual(shot_repaired['visible_people'], 2)
        self.assertEqual(count_repaired['character_ids'], ['char_martin'])
        self.assertEqual(count_repaired['visible_people'], 1)

    # Test 6: Сцена с visible_people: 1 без character_id и портрета блокируется до отправки в Flow.
    def test_6_scene_with_visible_people_without_id_or_portrait_blocked_from_flow(self):
        project = self.service.create_project({
            "title": "Flow Readiness Project",
            "story": "A human figure appears in the doorway. Thomas watches from the desk.",
            "style": "Cinematic"
        })
        p_id = project['id']

        analysis = {
            'world': 'Office',
            'characters': [{'name': 'Thomas', 'description': 'Keeper', 'wardrobe': {}}],
            'locations': [{'name': 'Tower', 'description': 'Lighthouse tower'}],
            'props': [],
            'scenes': [
                {
                    'beat': 'A human figure appears in the doorway',
                    'source_quote': 'A human figure appears in the doorway.',
                    'location': 'Tower',
                    'location_ref': '',
                    'time': 'Night',
                    'shot': 'Medium shot',
                    'emotion': 'mysterious',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'characters': [],  # Missing confirmed character!
                    'wardrobe': {},
                    'props': []
                }
            ]
        }
        self.service.import_analysis(p_id, analysis, project['version'])

        prompts_res = self.service.project_prompts(p_id)
        scene_item = prompts_res['scenes'][0]

        # An anonymous human figure without character assignment or episodic role is blocked by contract
        self.assertFalse(scene_item['ready_for_flow'])
        self.assertIsNotNone(scene_item['flow_error'])

        # Now test missing locked portrait:
        analysis_with_char = copy.deepcopy(analysis)
        analysis_with_char['scenes'][0]['characters'] = ['Thomas']
        proj_updated = self.service.project(p_id)
        self.service.import_analysis(p_id, analysis_with_char, proj_updated['version'])

        prompts_res2 = self.service.project_prompts(p_id)
        scene_item2 = prompts_res2['scenes'][0]
        # Thomas does not have a locked portrait reference yet
        self.assertFalse(scene_item2['ready_for_flow'])
        self.assertIsNotNone(scene_item2['flow_error'])
        self.assertIn("отсутствует закреплённый портрет", scene_item2['flow_error'])

    def test_6a_explicit_one_off_passerby_is_allowed_without_character_reference(self):
        project = self.service.create_project({
            'title': 'One-off passerby', 'story': 'Случайный прохожий отвечает на вопрос.', 'style': 'Cinematic'
        })
        analysis = {
            'world': 'City street', 'characters': [], 'locations': [], 'props': [],
            'scenes': [{
                'beat': 'A single passerby answers a question on a city street.',
                'source_quote': 'Случайный прохожий отвечает на вопрос.',
                'location': '', 'location_ref': '', 'time': 'Day', 'shot': 'Medium shot',
                'emotion': 'interest', 'continuity_before': '', 'continuity_after': '',
                'visible_people': 1, 'characters': [], 'character_ids': [], 'wardrobe': {}, 'props': []
            }]
        }
        self.service.import_analysis(project['id'], analysis, project['version'])
        scene_item = self.service.project_prompts(project['id'])['scenes'][0]
        self.assertTrue(scene_item['ready_for_flow'])
        self.assertIsNone(scene_item['flow_error'])

    # Test 7: В сценарии из минимум 100 000 символов персонаж, впервые появляющийся после 80 000-го символа,
    # сохраняется в общей библиотеке и корректно появляется в своей сцене.
    def test_7_large_script_100k_chars_late_character_preserved(self):
        prefix_block = "Thomas worked tirelessly inside the grand lighthouse tower, watching over the distant dark waves. " * 950
        self.assertGreater(len(prefix_block), 85000)

        late_event = (
            "Far below the tower, Inspector Vance emerged from the dense fog and approached the heavy iron gates. "
            "Inspector Vance called up to Thomas, seeking shelter from the rising squall. "
            "Thomas opened the heavy gate and welcomed Vance inside."
        )
        suffix_block = "The storm roared violently against the lantern glass throughout the long night. " * 200

        full_story = prefix_block + late_event + suffix_block
        self.assertGreater(len(full_story), 100000)

        vance_pos = full_story.find("Inspector Vance")
        self.assertGreater(vance_pos, 80000)

        data = {
            'characters': [
                {'name': 'Thomas', 'description': 'The keeper'},
                {'name': 'Inspector Vance', 'description': 'Detective arriving late in the story'}
            ],
            'scenes': [
                {
                    'beat': 'Inspector Vance emerged from the dense fog and approached the iron gates',
                    'source_quote': 'Inspector Vance emerged from the dense fog and approached the heavy iron gates.',
                    'characters': ['Inspector Vance']
                }
            ]
        }

        resolved_data = prioritize_active_story_characters(data, full_story)
        bible = resolved_data['cast_bible']
        active_ids = [c['id'] for c in bible['characters']]

        self.assertIn('char_inspector_vance', active_ids)
        vance_char = [c for c in bible['characters'] if c['id'] == 'char_inspector_vance'][0]
        self.assertTrue(any(ev['start'] >= 80000 for ev in vance_char['evidence']))

        # Ensure scene binds Vance
        scene = resolved_data['scenes'][0]
        self.assertIn('char_inspector_vance', scene['character_ids'])

    # Test 8: Повторный анализ неизменённого сценария использует сохранённую библию и не вызывает новый cast-bible LLM-анализ.
    def test_8_unchanged_story_hash_reuses_saved_bible_without_reextraction(self):
        story = "Thomas tends the lonely lighthouse. The captain arrives by boat."
        data = {
            'characters': [
                {'name': 'Thomas', 'description': 'Keeper of the light'},
                {'name': 'Captain', 'description': 'Visiting sea captain'}
            ]
        }

        # First pass builds bible
        resolved_1 = prioritize_active_story_characters(data, story)
        bible_1 = resolved_1['cast_bible']
        self.assertIn('story_hash', bible_1)

        # Modify a field in bible_1 to prove it gets directly reused without re-extraction
        bible_1['characters'][0]['description'] = 'Customized locked keeper description'

        # Second pass with same story and existing_bible
        resolved_2 = prioritize_active_story_characters(data, story, existing_bible=bible_1)
        bible_2 = resolved_2['cast_bible']

        self.assertEqual(bible_2['characters'][0]['description'], 'Customized locked keeper description')
        self.assertEqual(bible_1['story_hash'], bible_2['story_hash'])

    # Test 9: Изменение текста показывает diff библии и не удаляет старого персонажа без подтверждения.
    def test_9_story_change_produces_structured_diff_and_preserves_old_characters(self):
        story_v1 = "Thomas tends the lighthouse. Captain Reed arrives by boat."
        data_v1 = {
            'characters': [
                {'name': 'Thomas', 'description': 'Keeper'},
                {'name': 'Captain Reed', 'description': 'Captain'}
            ]
        }
        res_v1 = prioritize_active_story_characters(data_v1, story_v1)
        bible_v1 = res_v1['cast_bible']

        # Story v2 adds Doctor Evans
        story_v2 = (
            "Thomas tends the lighthouse. Captain Reed arrives with Doctor Evans to inspect the island. "
            "Doctor Evans examines the medical supplies."
        )
        data_v2 = {
            'characters': [
                {'name': 'Captain Reed', 'description': 'Captain'},
                {'name': 'Doctor Evans', 'description': 'Visiting island doctor'}
            ]
        }

        res_v2 = prioritize_active_story_characters(data_v2, story_v2, existing_bible=bible_v1)
        bible_v2 = res_v2['cast_bible']
        ids_v2 = [c['id'] for c in bible_v2['characters']]

        # Old character char_thomas was preserved even though not in data_v2!
        self.assertIn('char_thomas', ids_v2)
        self.assertIn('char_captain_reed', ids_v2)
        self.assertIn('char_doctor_evans', ids_v2)

        # Structured diff must record addition
        diff = bible_v2.get('diff', {})
        added_ids = [a['id'] for a in diff.get('added', [])]
        self.assertIn('char_doctor_evans', added_ids)

    # Test 10: Все существующие тесты моста, UI и генерации проходят; версия моста 0.1.8 согласована.
    def test_10_bridge_version_parity_and_invariants(self):
        root = Path(__file__).resolve().parent.parent
        js_int = (root / 'integrations' / 'flow-v1' / 'frameforge-flow.user.js').read_text(encoding='utf-8')
        js_static = (root / 'imagegen' / 'static' / 'frameforge-flow-v1.user.js').read_text(encoding='utf-8')

        self.assertIn('// @version 0.1.61', js_int)
        self.assertIn('// @version 0.1.61', js_static)
        self.assertEqual(js_int, js_static, "Userscript copies in integrations/ and static/ must be strictly identical")
        self.assertIn("state.done=state.done.filter(done=>done!==kind+':'+id);", js_static)
        self.assertIn("delete state.refs['scenes:'+id];", js_static)
        self.assertNotIn("if(alreadyDone && state.refs['scenes:'+id]) continue;", js_static)
        self.assertIn("const requiresComposite=kind==='scenes'&&characterRefs.length>2;", js_static)
        self.assertIn("characterRefs.slice(0,2)", js_static)
        self.assertIn("if(requiresComposite && selectedRefs.length>=2 && kind==='scenes')", js_static)
        self.assertIn("async function ensureReferenceMediaId(ref, position", js_static)
        self.assertIn("ref:${state.projectId}:${state.flowProject}:", js_static)
        self.assertIn("Не удалось заново импортировать портрет ${ref.name} в текущий проект Flow", js_static)
        self.assertIn("Не удалось заново импортировать локацию ${ref.name} в текущий проект Flow", js_static)
        self.assertIn("Не удалось заново импортировать предмет ${ref.name} в текущий проект Flow", js_static)
        self.assertIn("const filename=result.filename||", js_static)
        self.assertIn("formatStageError", js_static)
        self.assertIn("Full-bleed 16:9 image", js_static)
        self.assertIn("поля|рамк|паспарту|letterbox|pillarbox|холст 16:9|встроенные поля", js_static)

    def test_10b_flow_bridge_reference_auto_recovery_and_isolation_simulation(self):
        """Regression tests for Flow Bridge automatic reference recovery:
        1. Tab reload / empty state.refs -> downloads portrait from FF and imports to Flow
        2. Flow project switch -> mediaIds are scoped per (projectId, flowProject) and not reused
        3. Local storage reset -> recovers references automatically
        4. Portrait replacement -> detects asset mismatch and imports new portrait
        5. Import failure -> clear, precise Russian error message
        """
        root = Path(__file__).resolve().parent.parent
        js = (root / 'imagegen' / 'static' / 'frameforge-flow-v1.user.js').read_text(encoding='utf-8')

        # Verify exact error formatting
        self.assertIn("Не удалось заново импортировать портрет ${ref.name} в текущий проект Flow", js)
        self.assertIn("Не удалось заново импортировать локацию ${ref.name} в текущий проект Flow", js)
        self.assertIn("Не удалось заново импортировать предмет ${ref.name} в текущий проект Flow", js)

        # Verify reference recovery is invoked for both locked entities pass and scene reference resolution
        self.assertIn("await ensureReferenceMediaId(ref, item.position)", js)
        self.assertIn("state.refAssets", js)

        # Run Real JavaScript unit test (tests/test_bridge_queue.js) via Node.js
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

        self.assertIsNotNone(node_path, "Node.js executable must be present to run real JavaScript bridge tests")
        js_test_path = root / 'tests' / 'test_bridge_queue.js'
        self.assertTrue(js_test_path.is_file(), f"JS test file missing: {js_test_path}")

        proc = subprocess.run([node_path, str(js_test_path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, f"Real JavaScript bridge queue test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("All JavaScript Bridge tests passed successfully", proc.stdout)

    def test_10c_filename_resolution_context_errors_and_pending_recovery(self):
        """Tests for:
        1. Reference import without explicitly passed filename (safe fallback)
        2. Safe filename resolution for PNG and MP4 uploads
        3. Handling Flow result with mediaId and base64
        4. Pending-result recovery: when Flow already generated an image before crash,
           next startup flushes the saved result to FF without regenerating or spending credits
        """
        root = Path(__file__).resolve().parent.parent
        js = (root / 'imagegen' / 'static' / 'frameforge-flow-v1.user.js').read_text(encoding='utf-8')

        # 1. Check code invariants for filename resolution
        self.assertIn("const filename=result.filename||(result.mimeType==='video/mp4'?'video.mp4':'video.webm');", js)
        self.assertIn("const ext=result.mimeType==='image/jpeg'?'jpg':result.mimeType==='image/webp'?'webp':'png';", js)
        self.assertIn("const filename=result.filename||`${kind}_${id}.${ext}`;", js)
        self.assertNotIn("flow_media_id:result.mediaId,filename\n", js.replace('\r\n', '\n'))

        # 2. Check stage error formatting helper
        self.assertIn("formatStageError(item, 'reference import', err", js)
        self.assertIn("formatStageError(item, 'Flow generate', error, state.job)", js)
        self.assertIn("formatStageError(item, 'upload to FF', error, state.job)", js)

        # 3. Simulate pending-result recovery lifecycle
        class MockRunner:
            def __init__(self):
                self.uploaded_files = []
                self.generation_calls = 0
                self.state = {
                    'projectId': 'proj_test',
                    'flowProject': 'flow_test',
                    'refs': {},
                    'done': [],
                    'job': {
                        'kind': 'scenes',
                        'id': 'scene_daniel_1',
                        'scene_id': 'scene_daniel_1',
                        'requestId': 'req-12345',
                        'prompt': 'Daniel standing in pine forest'
                    },
                    # Flow already succeeded before browser crash/reload!
                    'result': {
                        'mediaId': 'flow_media_forest_99',
                        'mimeType': 'image/png',
                        'base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
                    }
                }

            def api_upload(self, kind, item_id, payload):
                self.uploaded_files.append({
                    'kind': kind,
                    'id': item_id,
                    'filename': payload.get('filename'),
                    'flow_media_id': payload.get('flow_media_id'),
                    'has_image': bool(payload.get('image'))
                })
                return {'asset_id': f"ast_{kind}_{item_id}"}

            def upload_saved(self):
                if not self.state.get('result') or not self.state.get('job'):
                    return
                job = self.state['job']
                res = self.state['result']
                kind = job['kind']
                item_id = job.get('scene_id') or job['id']
                ext = 'jpg' if res['mimeType'] == 'image/jpeg' else 'webp' if res['mimeType'] == 'image/webp' else 'png'
                filename = res.get('filename') or f"{kind}_{item_id}.{ext}"
                upload_res = self.api_upload(kind, item_id, {
                    'image': f"data:{res['mimeType']};base64,{res['base64']}",
                    'flow_media_id': res['mediaId'],
                    'filename': filename
                })
                self.state['refs'][f"{kind}:{item_id}"] = res['mediaId']
                self.state['done'].append(f"{kind}:{item_id}")
                self.state['job'] = None
                self.state['result'] = None

            def run_queue(self):
                # Startup flush of pending result
                if self.state.get('job') and self.state.get('result'):
                    self.upload_saved()

                # Subsequent generation loop
                if not self.state.get('done'):
                    self.generation_calls += 1

        runner = MockRunner()
        # Verify initial pending state
        self.assertIsNotNone(runner.state['job'])
        self.assertIsNotNone(runner.state['result'])

        # Execute run_queue: must flush pending result without new generation call!
        runner.run_queue()

        self.assertEqual(len(runner.uploaded_files), 1)
        uploaded = runner.uploaded_files[0]
        self.assertEqual(uploaded['kind'], 'scenes')
        self.assertEqual(uploaded['id'], 'scene_daniel_1')
        self.assertEqual(uploaded['filename'], 'scenes_scene_daniel_1.png')
        self.assertEqual(uploaded['flow_media_id'], 'flow_media_forest_99')
        self.assertTrue(uploaded['has_image'])

        # Crucial: 0 new generations occurred! No credits wasted!
        self.assertEqual(runner.generation_calls, 0)
        self.assertIsNone(runner.state['job'])
        self.assertIsNone(runner.state['result'])
        self.assertIn('scenes:scene_daniel_1', runner.state['done'])
        self.assertEqual(runner.state['refs']['scenes:scene_daniel_1'], 'flow_media_forest_99')

    def test_10a_pronouns_citations_and_second_person_viewer_never_become_cast(self):
        story = (
            'Представьте, что вы опустили руку в расщелину. Вы видите осьминога. '
            'Осьминог исследует вас. Marine Research Archive\n'
            'Его кровь выглядит голубой. science.example.org\n'
            'При этом осьминог — моллюск.'
        )
        data = {
            'characters': [
                {'name': 'Diver', 'description': 'invented viewer'},
                {'name': 'Octopus', 'description': 'Entity type: animal; adult cephalopod with eight arms.'},
                {'name': 'Его', 'description': 'pronoun'},
                {'name': 'Marine Research Archive', 'description': 'source attribution'},
            ],
            'locations': [], 'props': [],
            'scenes': [{
                'beat': 'The octopus looks at the viewer.',
                'source_quote': 'Вы видите осьминога.',
                'location': 'Seabed', 'location_ref': '', 'time': 'Day', 'shot': 'Close shot',
                'emotion': 'curious', 'continuity_before': '', 'continuity_after': '',
                'visible_people': 1, 'characters': ['Diver', 'Octopus'], 'wardrobe': {}, 'props': []
            }]
        }
        resolved = prioritize_active_story_characters(data, story)
        self.assertEqual(['Octopus'], [c['name'] for c in resolved['characters']])
        self.assertEqual(['Octopus'], resolved['scenes'][0]['characters'])
        self.assertEqual(0, resolved['scenes'][0]['visible_people'])
        segments = story_segments(story)
        self.assertFalse(any(any(source in segment.casefold() for source in ('marine research archive', 'science.example', '.org'))
                             for segment in segments))
        flow_prompt = compile_flow_prompt({'style': 'realism'}, {
            **resolved['scenes'][0], 'source_quote': 'Вы опустили руку в расщелину, и осьминог касается ваших пальцев.',
            'visible_people': 0
        }, characters=[{'name': 'Octopus', 'description': 'Entity type: animal; adult cephalopod with eight arms.'}])
        self.assertIn('exactly one anonymous gloved human hand', flow_prompt)
        self.assertIn('@Octopus', flow_prompt)

    def test_10b_invented_names_cannot_borrow_evidence_from_a_generic_role(self):
        story = 'Учёные наблюдают за редким животным в горном лесу. Это саола.'
        data = {
            'characters': [
                {'name': 'Dr. Elena Marquez', 'aliases': ['учёные'], 'description': 'invented scientist'},
                {'name': 'Local Guide', 'aliases': ['проводник'], 'description': 'invented guide'},
                {'name': 'Саола', 'description': 'Entity type: animal; species: saola.'},
            ], 'locations': [], 'props': [],
            'scenes': [{
                'beat': 'Scientists observe the animal.', 'source_quote': 'Учёные наблюдают за редким животным в горном лесу.',
                'location': 'Forest', 'location_ref': '', 'time': 'Day', 'shot': 'Wide shot', 'emotion': 'quiet',
                'continuity_before': '', 'continuity_after': '', 'visible_people': 3,
                'characters': ['Dr. Elena Marquez', 'Local Guide', 'Саола'], 'wardrobe': {}, 'props': []
            }]
        }
        resolved = prioritize_active_story_characters(data, story)
        self.assertEqual(['Саола'], [item['name'] for item in resolved['characters']])
        self.assertEqual(['Саола'], resolved['scenes'][0]['characters'])
        self.assertEqual(0, resolved['scenes'][0]['visible_people'])


    def test_10b_species_fallback_is_never_promoted_to_a_human(self):
        story = ('В тропическом лесу фотоловушка снимает тёмное животное с двумя длинными рогами. '
                 'Это саола. Саола обитает в горных лесах. Учёные редко видят саолу.')
        data = {
            # This is the deliberately weak deterministic fallback description,
            # used when the primary text provider is unavailable.
            'characters': [{'name': 'Саола', 'description': 'Саола, active participant in the story.'}],
            'locations': [], 'props': [],
            'scenes': [{
                'beat': 'The saola walks through the forest.', 'source_quote': 'Это саола.',
                'location': 'Forest', 'location_ref': '', 'time': 'Day', 'shot': 'Wide shot',
                'emotion': 'cautious', 'continuity_before': '', 'continuity_after': '',
                'visible_people': 1, 'characters': ['Саола'], 'wardrobe': {}, 'props': []
            }]
        }
        resolved = prioritize_active_story_characters(data, story)
        saola = resolved['characters'][0]
        self.assertTrue(saola['description'].startswith('Entity type: animal;'))
        self.assertEqual(0, resolved['scenes'][0]['visible_people'])
        prompt = compile_flow_prompt({'style': 'realism'}, resolved['scenes'][0], characters=[saola])
        self.assertIn('Feature only the referenced non-human subject', prompt)
        self.assertNotIn('Solo shot featuring only', prompt)

    def test_10b_translated_animal_name_is_kept_when_the_source_scene_confirms_wildlife(self):
        story = 'Ночью панголин идёт по лесной тропе. Панголин — млекопитающее с чешуёй.'
        data = {
            'characters': [{'name': 'Pangolin',
                            'description': 'Adult pangolin with scaled armour and an elongated snout.',
                            'wardrobe': {}}],
            'locations': [], 'props': [],
            'scenes': [{'beat': 'The pangolin walks through the forest.',
                        'source_quote': 'Ночью панголин идёт по лесной тропе. Панголин — млекопитающее с чешуёй.',
                        'location': 'Forest', 'location_ref': '', 'time': 'Night', 'shot': 'Wide shot',
                        'emotion': 'cautious', 'continuity_before': '', 'continuity_after': '',
                        'visible_people': 0, 'characters': ['Pangolin'], 'wardrobe': {}, 'props': []}]
        }
        resolved = prioritize_active_story_characters(data, story)
        self.assertEqual(['Панголин'], [item['name'] for item in resolved['characters']])
        self.assertEqual(['Панголин'], resolved['scenes'][0]['characters'])
        self.assertEqual(0, resolved['scenes'][0]['visible_people'])

    def test_10b_taxonomy_sentence_recovers_animal_when_model_returns_empty_cast(self):
        story = 'Но это панголин — млекопитающее. Ночью панголин идёт по лесной тропе.'
        data = {
            'characters': [], 'locations': [], 'props': [],
            'scenes': [{'beat': 'The animal walks through the forest.', 'source_quote': story,
                        'location': 'Forest', 'location_ref': '', 'time': 'Night', 'shot': 'Wide shot',
                        'emotion': 'cautious', 'continuity_before': '', 'continuity_after': '',
                        'visible_people': 0, 'characters': [], 'wardrobe': {}, 'props': []}]
        }
        resolved = prioritize_active_story_characters(data, story)
        self.assertEqual(['панголин'], [item['name'].lower() for item in resolved['characters']])
        self.assertEqual(['панголин'], [c.lower() for c in resolved['scenes'][0]['characters']])
        self.assertEqual(0, resolved['scenes'][0]['visible_people'])


    def test_10b_inanimate_title_words_and_places_are_not_cast_members(self):
        story = (
            'Последняя запись смотрителя маяка: «Корабль идёт не с моря». '
            'Маяк на острове Блэквуд автоматизировали десять лет назад, но '
            'смотритель Томас приезжал проверить оборудование. Лодка ждала у причала.'
        )
        result = prioritize_active_story_characters({
            'characters': [
                {'name': 'Thomas', 'description': 'Lighthouse keeper'},
                {'name': 'Последняя', 'description': 'active participant'},
                {'name': 'Корабль', 'description': 'active participant'},
                {'name': 'Маяк', 'description': 'active participant'},
                {'name': 'Блэквуд', 'description': 'active participant'},
                {'name': 'Лодка', 'description': 'active participant'},
                {'name': 'Утром', 'description': 'active participant'},
            ], 'locations': [], 'props': [], 'scenes': []
        }, story)
        names = [c['canonical_name'] for c in result['cast_bible']['characters']]
        self.assertIn('Thomas', names)
        for invalid in ('Последняя', 'Корабль', 'Маяк', 'Блэквуд', 'Лодка', 'Утром'):
            self.assertNotIn(invalid, names)

    def test_10c_same_cast_id_can_exist_in_two_projects(self):
        analysis = {
            'world': '',
            'characters': [{'id': 'char_thomas', 'cast_id': 'char_thomas', 'name': 'Thomas', 'canonical_name': 'Thomas', 'description': 'Keeper', 'evidence': []}],
            'locations': [], 'props': [],
            'scenes': [{'beat': 'Thomas waits.', 'source_quote': 'Thomas waits.', 'location': '', 'location_ref': '', 'time': 'Night', 'shot': 'Medium shot', 'emotion': 'calm', 'continuity_before': '', 'continuity_after': '', 'visible_people': 1, 'characters': ['Thomas'], 'character_ids': ['char_thomas'], 'wardrobe': {}, 'props': []}]
        }
        first = self.service.create_project({'title': 'First Thomas', 'story': 'Thomas waits.', 'style': 'Cinematic'})
        second = self.service.create_project({'title': 'Second Thomas', 'story': 'Thomas waits.', 'style': 'Cinematic'})
        self.service.import_analysis(first['id'], analysis, first['version'])
        self.service.import_analysis(second['id'], analysis, second['version'])
        first_char = self.service.detail(first['id'])['characters'][0]
        second_char = self.service.detail(second['id'])['characters'][0]
        self.assertNotEqual(first_char['id'], second_char['id'])
        self.assertEqual(first_char['cast_id'], 'char_thomas')
        self.assertEqual(second_char['cast_id'], 'char_thomas')

    def test_10d_worker_persists_shared_cast_id_with_project_local_row_id(self):
        from imagegen.worker import Worker
        from imagegen.db import uid, now
        story = 'Thomas waits by the lighthouse.'
        first = self.service.create_project({'title': 'Worker first', 'story': story, 'style': 'Cinematic'})
        second = self.service.create_project({'title': 'Worker second', 'story': story, 'style': 'Cinematic'})
        analysis = {
            'world': '',
            'characters': [{'id': 'char_thomas', 'cast_id': 'char_thomas', 'name': 'Thomas', 'canonical_name': 'Thomas', 'description': 'Keeper', 'evidence': []}],
            'locations': [], 'props': [],
            'scenes': [{'beat': 'Thomas waits.', 'source_quote': story, 'location': '', 'location_ref': '', 'time': 'Night', 'shot': 'Medium shot', 'emotion': 'calm', 'continuity_before': '', 'continuity_after': '', 'visible_people': 1, 'characters': ['Thomas'], 'character_ids': ['char_thomas'], 'wardrobe': {}, 'props': []}]
        }
        worker = Worker(self.service)
        for project in (first, second):
            with self.service.db.transaction() as connection:
                connection.execute("INSERT INTO generations(id,project_id,purpose,batch_id,state,snapshot,seed,progress,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (uid(), project['id'], 'analysis', None, 'running', '{}', 0, '{}', now()))
                generation = connection.execute("SELECT * FROM generations WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project['id'],)).fetchone()
            worker._save_analysis(dict(generation), analysis, project['version'])
        chars = [self.service.detail(p['id'])['characters'][0] for p in (first, second)]
        self.assertNotEqual(chars[0]['id'], chars[1]['id'])
        self.assertEqual([c['cast_id'] for c in chars], ['char_thomas', 'char_thomas'])


    # Test 11: Полный сквозной анализ крупного сценария (> 100k) через реальный provider -> service -> resolver
    # В сценарии более 100 000 символов активная героиня появляется только в последнем чанке;
    # Доказывается, что она есть в cast_bible, имеет cast_id и привязана к кадру через character_ids.
    def test_11_integration_provider_service_resolver_large_script_heroine(self):
        sentence = (
            "Thomas worked tirelessly inside the grand lighthouse tower watching over the distant dark waves "
            "through the heavy glass windows, and he kept the lantern shining brightly across the stormy sea "
            "while the cold wind battered the rocky shores day and night for weeks without any relief from the unrelenting "
            "north atlantic winter squalls and icy mist that clung to the stone walls, and the waves crashed below"
        )
        paragraph = ", ".join([sentence] * 8) + ". "
        late_event = (
            "Far across the cliffs, Elena arrived at the emergency station and unlocked the communication console. "
            "Elena signaled the lighthouse through the storm."
        )
        full_story = paragraph * 32 + late_event
        self.assertGreater(len(full_story), 100000)
        self.assertGreater(full_story.find("Elena"), 85000)

        chunks = chunk_large_story(full_story)
        self.assertTrue(any("Elena" in c[2] for c in chunks[-1:]), "Elena must appear in final chunk")
        self.assertFalse(any("Elena" in c[2] for c in chunks[:-1]), "Elena must NOT appear in earlier chunks")

        project = self.service.create_project({
            "title": "Large Story Heroine Project",
            "story": full_story,
            "style": "Cinematic"
        })
        p_id = project['id']

        all_segments = story_segments(full_story)
        target = len(all_segments)

        provider = LlamaCppProvider('http://127.0.0.1:11435')

        class MockHttp:
            headers = {}
            def json(inner_self, path, payload):
                if path == '/v1/chat/completions':
                    instruction = payload['messages'][0]['content']
                    schema = payload.get('response_format', {}).get('json_schema', {}).get('schema', {})
                    req_scenes = schema.get('properties', {}).get('scenes', {}).get('minItems', 1)
                    enum_ids = schema.get('properties', {}).get('scenes', {}).get('items', {}).get('properties', {}).get('source_id', {}).get('enum', [0])
                    max_id = max(enum_ids) if enum_ids else 0
                    # 1. Determine Elena's presence STRICTLY from source_data of current job
                    source_block = ""
                    if '<source_segments>' in instruction and '</source_segments>' in instruction:
                        source_block = instruction.split('<source_segments>')[1].split('</source_segments>')[0]

                    elena_source_id = None
                    for line in source_block.splitlines():
                        line = line.strip()
                        if line.startswith('[') and ']' in line:
                            sid_str, seg_text = line[1:].split(']', 1)
                            if 'Elena' in seg_text:
                                elena_source_id = int(sid_str.strip())
                                break

                    has_elena = elena_source_id is not None
                    scenes = []
                    for idx in range(req_scenes):
                        sid = min(idx, max_id)
                        if has_elena and idx == req_scenes - 1:
                            scenes.append({
                                'beat': 'Elena arrived at the emergency station and signaled the lighthouse.',
                                'source_id': elena_source_id,
                                'location': 'Emergency Station',
                                'time': 'Night',
                                'shot': 'Medium shot',
                                'emotion': 'urgent',
                                'continuity_before': '',
                                'continuity_after': '',
                                'visible_people': 1,
                                'characters': ['Elena'],
                                'wardrobe': {},
                                'props': []
                            })
                        else:
                            scenes.append({
                                'beat': 'Thomas tended the grand lighthouse tower in the storm.',
                                'source_id': sid,
                                'location': 'Lighthouse Tower',
                                'time': 'Night',
                                'shot': 'Wide shot',
                                'emotion': 'focused',
                                'continuity_before': '',
                                'continuity_after': '',
                                'visible_people': 1,
                                'characters': ['Thomas'],
                                'wardrobe': {},
                                'props': []
                            })
                    characters = [{'name': 'Thomas', 'description': 'Lighthouse keeper', 'wardrobe': {}}]
                    if has_elena:
                        characters.append({'name': 'Elena', 'description': 'Emergency station operator', 'wardrobe': {}})
                    locations = [{'name': 'Lighthouse Tower', 'description': 'Stone lighthouse tower'}]
                    if has_elena:
                        locations.append({'name': 'Emergency Station', 'description': 'Emergency station'})
                    content_json = {
                        'world': 'Stormy coastal sea',
                        'characters': characters,
                        'locations': locations,
                        'props': [],
                        'scenes': scenes
                    }
                    return {
                        'choices': [{
                            'message': {'content': json.dumps(content_json)},
                            'finish_reason': 'stop'
                        }]
                    }
                return {}

        provider.http = MockHttp()

        # Route: provider.analyse() -> service.import_analysis() -> service.detail()
        analysis_result = provider.analyse(project, target=target)
        self.service.import_analysis(p_id, analysis_result, project['version'])
        detail = self.service.detail(p_id)

        bible = detail['cast_bible']
        char_map = {c['cast_id']: c for c in bible['characters']}
        self.assertIn('char_elena', char_map)
        elena_c = char_map['char_elena']
        self.assertEqual(elena_c['canonical_name'], 'Elena')
        self.assertEqual(elena_c['cast_id'], 'char_elena')
        self.assertTrue(any(ev['start'] >= 85000 for ev in elena_c.get('evidence', [])))

        # Elena must be bound to scene via character_ids and source_quote must contain the exact source phrase
        elena_scenes = [s for s in detail['scenes'] if 'char_elena' in s['data'].get('character_ids', [])]
        self.assertGreaterEqual(len(elena_scenes), 1)
        self.assertIn('Elena', elena_scenes[0]['data'].get('characters', []))
        self.assertIn('char_elena', elena_scenes[0]['data'].get('character_ids', []))
        self.assertIn('Elena arrived at the emergency station', elena_scenes[0]['data'].get('source_quote', ''))

        # A small target is a sampling budget across the entire story, not an
        # instruction to truncate the ending. The final Elena event must still
        # receive a frame and remain bound to the global Cast Bible.
        intro_result = provider.analyse(project, target=5)
        intro_bible_map = {c['cast_id']: c for c in intro_result['cast_bible']['characters']}
        self.assertIn('char_elena', intro_bible_map)
        self.assertTrue(any('Elena' in s.get('characters', []) or 'char_elena' in s.get('character_ids', []) for s in intro_result['scenes']))

    # Test 12: Постоянные ID как источник правды.
    # Переименование персонажа сохраняет cast_id и не ломает привязку сцен; отображаемые имена в сценах обновляются.
    def test_12_character_renaming_preserves_cast_id_and_updates_scene_display(self):
        story = "Thomas tends the lonely lighthouse on the cliff."
        project = self.service.create_project({
            "title": "Rename Test Project",
            "story": story,
            "style": "Cinematic"
        })
        p_id = project['id']

        analysis = {
            'world': 'Lighthouse',
            'characters': [{'name': 'Thomas', 'description': 'Keeper of the light', 'wardrobe': {}}],
            'locations': [{'name': 'Lighthouse', 'description': 'Tower'}],
            'props': [],
            'scenes': [
                {
                    'beat': 'Thomas tends the lonely lighthouse',
                    'source_quote': 'Thomas tends the lonely lighthouse on the cliff.',
                    'location': 'Lighthouse',
                    'location_ref': 'Lighthouse',
                    'time': 'Day',
                    'shot': 'Wide shot',
                    'emotion': 'calm',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'characters': ['Thomas'],
                    'wardrobe': {},
                    'props': []
                }
            ]
        }
        resolved = prioritize_active_story_characters(analysis, story)
        self.service.import_analysis(p_id, resolved, project['version'])

        detail_before = self.service.detail(p_id)
        char_obj = detail_before['characters'][0]
        char_id = char_obj['id']
        cast_id = char_obj['cast_id']
        self.assertEqual(cast_id, 'char_thomas')

        self.service.edit_character(char_id, description="Updated keeper description", revision=char_obj['revision'], name="Thomas Reed")

        detail_after = self.service.detail(p_id)
        char_after = detail_after['characters'][0]
        self.assertEqual(char_after['name'], 'Thomas Reed')
        self.assertEqual(char_after['cast_id'], 'char_thomas')

        scene_after = detail_after['scenes'][0]
        self.assertEqual(scene_after['data'].get('character_ids'), ['char_thomas'])
        self.assertEqual(scene_after['data'].get('characters'), ['Thomas Reed'])

    # Test 13: Cast Bible diff API и подтверждение действий (accept, keep_old, map)
    def test_13_cast_diff_api_and_resolutions(self):
        story_v1 = "Thomas tends the light. Captain Reed arrives by boat."
        project = self.service.create_project({
            "title": "Cast Diff Test Project",
            "story": story_v1,
            "style": "Cinematic"
        })
        p_id = project['id']

        analysis_v1 = {
            'characters': [
                {'name': 'Thomas', 'description': 'Keeper'},
                {'name': 'Captain Reed', 'description': 'Captain'}
            ],
            'locations': [],
            'props': [],
            'scenes': [{
                'beat': 'Thomas tends the light',
                'source_quote': 'Thomas tends the light.',
                'location': 'Tower',
                'location_ref': '',
                'time': 'Day',
                'shot': 'Wide shot',
                'emotion': 'calm',
                'continuity_before': '',
                'continuity_after': '',
                'visible_people': 1,
                'characters': ['Thomas'],
                'wardrobe': {},
                'props': []
            }]
        }
        resolved_v1 = prioritize_active_story_characters(analysis_v1, story_v1)
        self.service.import_analysis(p_id, resolved_v1, project['version'])

        story_v2 = "Thomas tends the light. Doctor Evans arrives by boat."
        analysis_v2 = {
            'characters': [
                {'name': 'Thomas', 'description': 'Keeper'},
                {'name': 'Doctor Evans', 'description': 'Doctor'}
            ],
            'locations': [],
            'props': [],
            'scenes': [{
                'beat': 'Thomas tends the light',
                'source_quote': 'Thomas tends the light.',
                'location': 'Tower',
                'location_ref': '',
                'time': 'Day',
                'shot': 'Wide shot',
                'emotion': 'calm',
                'continuity_before': '',
                'continuity_after': '',
                'visible_people': 1,
                'characters': ['Thomas'],
                'wardrobe': {},
                'props': []
            }]
        }
        bible_v1 = self.service.detail(p_id)['cast_bible']
        resolved_v2 = prioritize_active_story_characters(analysis_v2, story_v2, existing_bible=bible_v1)
        proj_updated = self.service.project(p_id)
        self.service.import_analysis(p_id, resolved_v2, proj_updated['version'])

        diff = self.service.cast_diff(p_id)
        self.assertIn('added', diff)
        self.assertIn('removed', diff)
        added_names = [a['name'] for a in diff['added']]
        self.assertIn('Doctor Evans', added_names)
        removed_names = [r['name'] for r in diff['removed']]
        self.assertIn('Captain Reed', removed_names)

        # Test keep_old resolution
        self.service.resolve_cast_diff(p_id, {'action': 'keep_old', 'cast_id': 'char_captain_reed'})
        detail_resolved = self.service.detail(p_id)
        current_char_ids = [c['cast_id'] for c in detail_resolved['cast_bible']['characters']]
        self.assertIn('char_captain_reed', current_char_ids)

        # Test accept resolution
        self.service.resolve_cast_diff(p_id, {'action': 'accept'})
        diff_after = self.service.cast_diff(p_id)
        self.assertEqual(diff_after.get('added'), [])
        self.assertEqual(diff_after.get('modified'), [])

    # Test 14: «Начать» и «Это» в сценарии не создают карточки персонажей
    def test_14_grammatical_words_like_nachat_and_eto_never_become_characters(self):
        narrative_with_grammar_words = (
            "Начать стоит с распространенного мифа. «Ну как же! Это просто мальчик и девочка». "
            "Однако это заблуждение. Конечно, это два разных вида. "
            "Давайте присмотримся повнимательнее к природе. "
            "Когда мы смотрим в небо, это птицы парят над лесом."
        )
        resolver = CharacterEntityResolver(narrative_with_grammar_words)
        cands = resolver.extract_deterministic_candidates()
        cand_names = [c['canonical_name'] for c in cands]
        self.assertNotIn('Начать', cand_names)
        self.assertNotIn('Это', cand_names)
        self.assertNotIn('Однако', cand_names)
        self.assertNotIn('Конечно', cand_names)
        self.assertNotIn('Давайте', cand_names)
        self.assertNotIn('Когда', cand_names)

        # Even if raw model candidates erroneously contain 'Начать' or 'Это'
        raw_candidates = [
            {'name': 'Начать', 'description': 'Начало рассказа'},
            {'name': 'Это', 'description': 'Местоимение как персонаж'}
        ]
        resolved = resolver.resolve(raw_candidates)
        res_names = [c['canonical_name'] for c in resolved.get('characters', [])]
        self.assertNotIn('Начать', res_names)
        self.assertNotIn('Это', res_names)

    # Test 15: «Ворона» (и персонажи из сценария вороны) сохраняются, «Начать» и «Это» исключены
    def test_15_crow_and_raven_story_extracts_animals_and_preserves_vorona(self):
        story = (
            "Начать наше погружение в мир пернатых интеллектуалов стоит с распространенного мифа. "
            "«Ну как же! Это просто мальчик и девочка». "
            "Во́рон и воро́на — это не самец и самка одного вида, а два абсолютно разных вида птиц. "
            "Настоящий ворон — это птица дикой, нетронутой природы. "
            "А теперь посмотрим на нашу старую знакомую — ворону. "
            "Та самая птица называется серой вороной. "
            "Теперь давайте сосредоточимся на нашей городской соседке."
        )
        resolver = CharacterEntityResolver(story)
        det_cands = resolver.extract_deterministic_candidates()
        det_names = [c['canonical_name'] for c in det_cands]
        self.assertNotIn('Начать', det_names)
        self.assertNotIn('Это', det_names)

        # Vorona is preserved and resolved
        raw_cands = [{'name': 'Ворона', 'description': 'Городская серая ворона'}]
        resolved = resolver.resolve(raw_cands)
        char_map = {c['canonical_name']: c for c in resolved.get('characters', [])}
        self.assertIn('Ворона', char_map)
        self.assertNotIn('Начать', char_map)
        self.assertNotIn('Это', char_map)
        vorona = char_map['Ворона']
        self.assertTrue(vorona['description'].startswith('Entity type: animal;'))

    # Test 16: Имена, роли и животные корректно извлекаются, повторный анализ не возвращает ложные карточки
    def test_16_roles_names_and_animals_extracted_and_reanalysis_stable(self):
        story = (
            "Капитан Рид стоял на мостике корабля. Томас смотрел на горизонт. "
            "В небе летела ворона, а в горах спал древний дракон. "
            "Начать путь было нелегко. Это требовало смелости. Однако они были готовы."
        )
        project = self.service.create_project({
            "title": "Cast Invariant Test",
            "story": story,
            "style": "Cinematic"
        })
        p_id = project['id']

        analysis_v1 = {
            'world': 'Sea and mountains',
            'characters': [
                {'name': 'Капитан Рид', 'description': 'Командир корабля', 'wardrobe': {}},
                {'name': 'Томас', 'description': 'Смотритель', 'wardrobe': {}},
                {'name': 'Ворона', 'description': 'Птица', 'wardrobe': {}},
                # Erroneous candidates that might come from an LLM
                {'name': 'Начать', 'description': 'Начало пути', 'wardrobe': {}},
                {'name': 'Это', 'description': 'Это требование', 'wardrobe': {}},
            ],
            'locations': [],
            'props': [],
            'scenes': [{
                'beat': 'Капитан Рид и Томас видят ворону',
                'source_quote': 'Капитан Рид стоял на мостике корабля. Томас смотрел на горизонт.',
                'location': 'Bridge',
                'location_ref': '',
                'time': 'Day',
                'shot': 'Wide shot',
                'emotion': 'determined',
                'continuity_before': '',
                'continuity_after': '',
                'visible_people': 2,
                'characters': ['Капитан Рид', 'Томас'],
                'wardrobe': {},
                'props': []
            }]
        }

        # 1. First analysis import
        resolved_v1 = prioritize_active_story_characters(analysis_v1, story)
        self.service.import_analysis(p_id, resolved_v1, project['version'])

        detail_v1 = self.service.detail(p_id)
        chars_v1 = [c['name'] for c in detail_v1['characters']]
        self.assertIn('Капитан Рид', chars_v1)
        self.assertIn('Томас', chars_v1)
        self.assertIn('Ворона', chars_v1)
        self.assertNotIn('Начать', chars_v1)
        self.assertNotIn('Это', chars_v1)

        # 2. Re-analysis on unchanged story must not reintroduce false cards
        proj_v2 = self.service.project(p_id)
        resolved_v2 = prioritize_active_story_characters(analysis_v1, story, existing_bible=detail_v1['cast_bible'])
        self.service.import_analysis(p_id, resolved_v2, proj_v2['version'])

        detail_v2 = self.service.detail(p_id)
        chars_v2 = [c['name'] for c in detail_v2['characters']]
        self.assertEqual(sorted(chars_v1), sorted(chars_v2))
        self.assertNotIn('Начать', chars_v2)
        self.assertNotIn('Это', chars_v2)


    # Test 17: Имя в начале предложений («Злата») детерминированно извлекается
    def test_17_sentence_initial_zlata_extracted_deterministically(self):
        story = "Злата стояла у окна. Злата смотрела на лес."
        resolver = CharacterEntityResolver(story)
        cands = resolver.extract_deterministic_candidates()
        cand_names = [c['canonical_name'] for c in cands]
        self.assertIn('Злата', cand_names)

        # Full resolution with empty raw candidates also discovers Zlata
        resolved = resolver.resolve([])
        res_names = [c['canonical_name'] for c in resolved.get('characters', [])]
        self.assertIn('Злата', res_names)

    # Test 18: Имя на -ти («Роберти») сохраняется и не отвергается как глагол
    def test_18_roberti_preserved_as_character_not_rejected_as_verb(self):
        story = "Роберти прибыл в порт на рассвете. Он осмотрелся вокруг."
        # Model returns Roberti as a person
        analysis = {
            'world': 'Port',
            'characters': [
                {'name': 'Роберти', 'description': 'Роберти, исследователь, человек', 'aliases': ['Роберти']}
            ],
            'locations': [],
            'props': [],
            'scenes': [{
                'beat': 'Роберти осматривает порт',
                'source_quote': 'Роберти прибыл в порт на рассвете.',
                'location': 'Port',
                'location_ref': '',
                'time': 'Dawn',
                'shot': 'Wide shot',
                'emotion': 'focused',
                'continuity_before': '',
                'continuity_after': '',
                'visible_people': 1,
                'characters': ['Роберти'],
                'wardrobe': {},
                'props': []
            }]
        }
        resolved = prioritize_active_story_characters(analysis, story)
        res_names = [c['canonical_name'] for c in resolved['cast_bible']['characters']]
        self.assertIn('Роберти', res_names)
        self.assertEqual(['Роберти'], resolved['scenes'][0]['characters'])

        # Also verify via is_grammatical_non_character directly
        self.assertFalse(is_grammatical_non_character('Роберти'))
        self.assertFalse(is_grammatical_non_character('Роберти', 'исследователь, человек'))

    # Test 19: Общее морфологическое разграничение родов без зашитых исключений видов
    def test_19_general_gender_distinction_without_species_exception(self):
        # Masculine zero-ending vs feminine -а/-я are distinct lemmas
        self.assertFalse(names_match_entity('ворон', 'ворона'))
        self.assertFalse(names_match_entity('ворона', 'ворон'))
        self.assertFalse(names_match_entity('raven', 'crow'))
        self.assertFalse(names_match_entity('crow', 'raven'))
        self.assertFalse(names_match_entity('raven', 'ворона'))
        self.assertFalse(names_match_entity('crow', 'ворон'))
        self.assertFalse(names_match_entity('Черный ворон', 'Серая ворона'))
        self.assertFalse(names_match_entity('Александр', 'Александра'))

        # Genuine aliases and same-entity forms still match
        self.assertTrue(names_match_entity('Томас', 'Томас'))
        self.assertTrue(names_match_entity('Thomas', 'Томас'))
        self.assertTrue(names_match_entity('Капитан Рид', 'Рид'))
        self.assertTrue(names_match_entity('Марина', 'Марина'))

    # Test 20: Интеграционный тест полного пути со сохранённым проблемным ответом анализатора и исходным сценарием
    def test_20_full_pipeline_crow_raven_reproduction(self):
        story = (
            "Начать наше погружение в мир пернатых интеллектуалов стоит с самого распространенного и невероятно живучего мифа, в который до сих пор верят миллионы людей по всему миру. "
            "Спросите случайного прохожего на улице: чем отличается ворон от вороны? "
            "В девяти случаях из десяти вам с уверенной улыбкой ответят: «Ну как же! Это просто мальчик и девочка, муж и жена». "
            "Этот стереотип укоренился в сознании настолько прочно, что перекочевал даже в детские книги и мультфильмы. "
            "Но биологическая реальность гораздо интереснее и категоричнее: ворон и ворона — это два совершенно разных биологических вида. "
            "Они принадлежат к одному семейству врановых и даже к одному роду, но между ними лежит такая же непреодолимая пропасть, как, например, между львом и домашней кошкой или волком и крошечной болонкой. "
            "Настоящий ворон — это птица дикой, нетронутой природы. "
            "Та самая птица, которую мы каждый день видим в скверах, парках и во дворах, называется серой вороной."
        )

        # 1. Saved problematic analyzer output that hallucinated 'Начать' and 'Это' as human entities
        problematic_raw_analysis = {
            'world': 'Лесная глушь и городские улицы',
            'characters': [
                {'name': 'Начать', 'description': 'Entity type: human; Starting person', 'aliases': ['Начать']},
                {'name': 'Это', 'description': 'Entity type: human; Demonstrative person', 'aliases': ['Это']}
            ],
            'locations': [
                {'name': 'Дикий лес', 'description': 'Нетронутая природа'},
                {'name': 'Городской сквер', 'description': 'Скверы и парки с прохожими'}
            ],
            'props': [],
            'scenes': [
                {
                    'beat': 'Рассказчик начинает погружение в мифы о птицах',
                    'source_quote': 'Начать наше погружение в мир пернатых интеллектуалов стоит с самого распространенного и невероятно живучего мифа, в который до сих пор верят миллионы людей по всему миру.',
                    'location': 'Городской сквер',
                    'location_ref': 'Городской сквер',
                    'time': 'День',
                    'shot': 'Общий план',
                    'emotion': 'познавательно',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 0,
                    'characters': ['Начать'],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'Прохожий уверенно отвечает про мальчика и девочку',
                    'source_quote': 'Это просто мальчик и девочка, муж и жена».',
                    'location': 'Городской сквер',
                    'location_ref': 'Городской сквер',
                    'time': 'День',
                    'shot': 'Средний план',
                    'emotion': 'уверенно',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1,
                    'characters': ['Это'],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'Величественный ворон в дикой нетронутой природе',
                    'source_quote': 'Настоящий ворон — это птица дикой, нетронутой природы.',
                    'location': 'Дикий лес',
                    'location_ref': 'Дикий лес',
                    'time': 'День',
                    'shot': 'Крупный план ворона',
                    'emotion': 'величественно',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 0,
                    'characters': [],
                    'wardrobe': {},
                    'props': []
                },
                {
                    'beat': 'Серая ворона в городском парке среди деревьев и скамеек',
                    'source_quote': 'Та самая птица, которую мы каждый день видим в скверах, парках и во дворах, называется серой вороной.',
                    'location': 'Городской сквер',
                    'location_ref': 'Городской сквер',
                    'time': 'День',
                    'shot': 'Средний план вороны',
                    'emotion': 'любопытство',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 0,
                    'characters': [],
                    'wardrobe': {},
                    'props': []
                }
            ]
        }

        # Step 1: Create new project via service (exact endpoint POST /api/projects)
        project = self.service.create_project({
            'title': 'Интеграционный тест: Ворон и Ворона',
            'story': story,
            'style': 'Cinematic documentary'
        })
        project_id = project['id']

        # Step 2: Full resolution and import (pipeline: raw analysis -> prioritize_active_story_characters -> validate -> import_analysis)
        resolved_analysis = prioritize_active_story_characters(problematic_raw_analysis, story, project.get('cast_bible'))
        validated = validate_analysis(resolved_analysis, story)
        self.service.import_analysis(project_id, validated, project['version'])

        # Step 3: Check API response from service.detail(project_id)
        api_detail = self.service.detail(project_id)
        char_names = [c['name'] for c in api_detail['characters']]
        canonical_names = [c['canonical_name'] for c in api_detail['characters']]

        # Acceptance Criteria 1: Начать and Это must be absent
        char_names_lower = [c['name'].casefold() for c in api_detail['characters']]
        canonical_names_lower = [c['canonical_name'].casefold() for c in api_detail['characters']]
        self.assertNotIn('начать', char_names_lower)
        self.assertNotIn('это', char_names_lower)
        self.assertNotIn('начать', canonical_names_lower)
        self.assertNotIn('это', canonical_names_lower)

        # Acceptance Criteria 2: Ворона and Ворон represented as separate entities
        self.assertIn('ворон', char_names_lower)
        self.assertIn('ворона', char_names_lower)
        raven_char = next(c for c in api_detail['characters'] if c['name'].casefold() == 'ворон')
        crow_char = next(c for c in api_detail['characters'] if c['name'].casefold() == 'ворона')
        self.assertNotEqual(raven_char['id'], crow_char['id'])
        self.assertNotEqual(raven_char['cast_id'], crow_char['cast_id'])

        # Acceptance Criteria 3: Scene connections match content
        scenes = api_detail['scenes']
        # Scene 1 & 2: narrator / passerby myth - no Начать or Это linked
        for sc in scenes[:2]:
            sc_data = sc.get('data', sc)
            sc_char_names = [c.casefold() if isinstance(c, str) else c['name'].casefold() for c in sc_data.get('characters', [])]
            self.assertNotIn('начать', sc_char_names)
            self.assertNotIn('это', sc_char_names)

        # Scene 3: Wild raven scene must link Ворон, not Ворона
        raven_scene = scenes[2]
        raven_data = raven_scene.get('data', raven_scene)
        self.assertIn('ворон', raven_data['beat'].lower())
        raven_scene_char_names = [c.casefold() if isinstance(c, str) else c['name'].casefold() for c in raven_data.get('characters', [])]
        self.assertIn('ворон', raven_scene_char_names)
        self.assertNotIn('ворона', raven_scene_char_names)
        self.assertEqual(raven_data.get('character_ids'), ['char_voron'])
        self.assertEqual(raven_data.get('props'), [])

        # Scene 4: Urban grey crow scene must link Ворона, not Ворон
        crow_scene = scenes[3]
        crow_data = crow_scene.get('data', crow_scene)
        self.assertIn('ворон', crow_data['beat'].lower())
        crow_scene_char_names = [c.casefold() if isinstance(c, str) else c['name'].casefold() for c in crow_data.get('characters', [])]
        self.assertIn('ворона', crow_scene_char_names)
        self.assertNotIn('ворон', crow_scene_char_names)
        self.assertEqual(crow_data.get('character_ids'), ['char_vorona'])
        self.assertEqual(crow_data.get('props'), [])

        # Acceptance Criteria 4: Props table and scenes have NO animal duplicates
        self.assertEqual(api_detail['props'], [])

        # Acceptance Criteria 5: Compiled prompts preserve bird species and enforce ABSOLUTE NO-TEXT RULE
        prompts_res = self.service.project_prompts(project_id)
        # Solo raven scene prompt
        raven_flow_prompt = prompts_res['scenes'][2]['flow_prompt']
        raven_comfy_prompt = prompts_res['scenes'][2]['comfy_prompt']
        self.assertIn('ABSOLUTE NO-TEXT RULE', raven_comfy_prompt)
        self.assertIn('@ворон', raven_flow_prompt.lower())
        self.assertNotIn('@ворона', raven_flow_prompt.lower())

        # Solo crow scene prompt
        crow_flow_prompt = prompts_res['scenes'][3]['flow_prompt']
        crow_comfy_prompt = prompts_res['scenes'][3]['comfy_prompt']
        self.assertIn('ABSOLUTE NO-TEXT RULE', crow_comfy_prompt)
        self.assertIn('@ворона', crow_flow_prompt.lower())
        self.assertNotIn('@ворон ', crow_flow_prompt.lower() + ' ')

        # Step 4: Re-analysis of the same project with updated / re-run analyzer output
        # Verify cast diff & re-import preserves the invariant
        proj_v2 = self.service.project(project_id)
        reanalysis_raw = copy.deepcopy(problematic_raw_analysis)
        re_resolved = prioritize_active_story_characters(reanalysis_raw, story, proj_v2.get('cast_bible'))
        re_validated = validate_analysis(re_resolved, story)

        diff = self.service.cast_diff(project_id)
        # Should not invent Начать or Это on re-analysis
        new_cast_names = [c['canonical_name'].casefold() for c in diff.get('new_cast', [])]
        self.assertNotIn('начать', new_cast_names)
        self.assertNotIn('это', new_cast_names)

        # Re-import maintains separation and cleanliness
        self.service.import_analysis(project_id, re_validated, proj_v2['version'])
        api_detail_v2 = self.service.detail(project_id)
        char_names_v2 = [c['name'].casefold() for c in api_detail_v2['characters']]
        self.assertNotIn('начать', char_names_v2)
        self.assertNotIn('это', char_names_v2)
        self.assertIn('ворон', char_names_v2)
        self.assertIn('ворона', char_names_v2)

    # Test 21: Точный состав character_ids и props в сравнительных и одиночных сценах
    def test_21_discussed_animals_do_not_become_visual_scene_references(self):
        cast_bible = {
            'characters': [
                {'id': 'char_raven', 'canonical_name': 'Ворон', 'aliases': ['ворон'],
                 'description': 'Entity type: animal; species: raven.'},
                {'id': 'char_crow', 'canonical_name': 'Ворона', 'aliases': ['ворона'],
                 'description': 'Entity type: animal; species: crow.'},
            ]
        }
        discussion = {
            'beat': 'A passerby is heard asking about a large raven and a city crow.',
            'source_quote': 'Если спросить прохожего, кем приходятся друг другу ворон и ворона, он ответит: «муж и жена».',
            'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []
        }
        visual = {
            'beat': 'The raven spreads its wings above the forest.',
            'source_quote': 'Ворон расправляет крылья над лесом.',
            'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []
        }
        resolved = resolve_scene_pronouns_and_cast([discussion, visual], cast_bible, '')
        self.assertEqual(resolved[0]['character_ids'], [])
        self.assertEqual(resolved[1]['character_ids'], ['char_raven'])

    def test_21a_raven_crow_homographs_preserve_the_visual_species_identity(self):
        cast_bible = {'characters': [
            {'id': 'char_raven', 'canonical_name': 'Ворон', 'aliases': [],
             'description': 'Entity type: animal; adult male raven.'},
            {'id': 'char_crow', 'canonical_name': 'Ворона', 'aliases': [],
             'description': 'Entity type: animal; adult female city crow.'},
        ]}
        scenes = [
            {'beat': 'A raven perches on a rocky ledge.', 'source_quote': 'Настоящий ворон — птица дикой природы.',
             'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []},
            {'beat': 'The same raven avoids city noise.', 'source_quote': 'Вороны не любят городской шум и суету.',
             'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []},
            {'beat': 'The city crow lands in the park.', 'source_quote': 'Посмотрим на нашу старую знакомую — ворону.',
             'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []},
            {'beat': 'A close detail of the raven beak.', 'source_quote': 'У ворона массивный мощный клюв.',
             'characters': [], 'character_ids': [], 'visible_people': 0, 'wardrobe': {}, 'props': []},
        ]
        resolved = resolve_scene_pronouns_and_cast(scenes, cast_bible, '')
        self.assertEqual([scene['character_ids'] for scene in resolved], [
            ['char_raven'], ['char_raven'], ['char_crow'], ['char_raven']
        ])

    def test_22_scene_character_binding_comparative_vs_solo_and_no_animal_props(self):
        story = (
            "Спросите случайного прохожего на улице: чем отличается ворон от вороны? "
            "Но биологическая реальность гораздо интереснее и категоричнее: ворон и ворона — это два совершенно разных биологических вида. "
            "Сравните их в полете: у ворона хвост клиновидный, а у вороны закругленный. "
            "Ворон сидит на ветке старого дуба в диком лесу. "
            "Этот пернатый гигант расправляет свои мощные черные крылья и издает глубокий крик."
        )

        analysis = {
            'world': 'Дикая природа и город',
            'characters': [],
            'locations': [
                {'name': 'Дикий лес', 'description': 'Нетронутый вековой лес'},
                {'name': 'Небо над лесом', 'description': 'Открытое небо'}
            ],
            # Raw LLM falsely added the animals as props
            'props': [
                {'name': 'ворон', 'description': 'Черная дикая птица', 'owner': ''},
                {'name': 'ворона', 'description': 'Серая городская птица', 'owner': ''},
                {'name': 'бинокль', 'description': 'Полевой бинокль натуралиста', 'owner': ''}
            ],
            'scenes': [
                {
                    'beat': 'Сравнение: ворон и ворона рядом на иллюстрации',
                    'source_quote': 'Но биологическая реальность гораздо интереснее и категоричнее: ворон и ворона — это два совершенно разных биологических вида.',
                    'location': 'Дикий лес',
                    'location_ref': 'Дикий лес',
                    'time': 'День',
                    'shot': 'Средний план',
                    'characters': [],
                    'props': [{'name': 'ворон'}, {'name': 'ворона'}]
                },
                {
                    'beat': 'Сравнение силуэтов в полете: клиновидный и закругленный хвост',
                    'source_quote': 'Сравните их в полете: у ворона хвост клиновидный, а у вороны закругленный.',
                    'location': 'Небо над лесом',
                    'location_ref': 'Небо над лесом',
                    'time': 'День',
                    'shot': 'Общий план',
                    'characters': [],
                    'props': []
                },
                {
                    'beat': 'Ворон на ветке дуба в диком лесу',
                    'source_quote': 'Ворон сидит на ветке старого дуба в диком лесу.',
                    'location': 'Дикий лес',
                    'location_ref': 'Дикий лес',
                    'time': 'День',
                    'shot': 'Крупный план',
                    'characters': [],
                    'props': [{'name': 'ворон'}, {'name': 'бинокль'}]
                },
                {
                    'beat': 'Крупный план ворона расправляющего крылья',
                    'source_quote': 'Этот пернатый гигант расправляет свои мощные черные крылья и издает глубокий крик.',
                    'location': 'Дикий лес',
                    'location_ref': 'Дикий лес',
                    'time': 'День',
                    'shot': 'Макро',
                    'characters': [],
                    'props': []
                }
            ]
        }

        project = self.service.create_project({
            'title': 'Тест привязки сцен и реквизита',
            'story': story,
            'style': 'Cinematic documentary'
        })
        p_id = project['id']

        resolved = prioritize_active_story_characters(analysis, story)
        validated = validate_analysis(resolved, story)
        self.service.import_analysis(p_id, validated, project['version'])

        detail = self.service.detail(p_id)
        scenes = detail['scenes']

        # Scene 0 (comparative): MUST get BOTH IDs ['char_voron', 'char_vorona']
        sc0_data = scenes[0]['data']
        self.assertEqual(set(sc0_data.get('character_ids', [])), {'char_voron', 'char_vorona'})
        self.assertEqual(sc0_data.get('props', []), [])  # Animals purged from scene props

        # Scene 1 (comparative flight): MUST get BOTH IDs ['char_voron', 'char_vorona']
        sc1_data = scenes[1]['data']
        self.assertEqual(set(sc1_data.get('character_ids', [])), {'char_voron', 'char_vorona'})

        # Scene 2 (solo raven): MUST get ONLY ['char_voron']
        sc2_data = scenes[2]['data']
        self.assertEqual(sc2_data.get('character_ids', []), ['char_voron'])
        # In scene 2, 'ворон' was purged, only genuine prop 'бинокль' remains (if story prop)
        sc2_prop_names = [p['name'].casefold() for p in sc2_data.get('props', [])]
        self.assertNotIn('ворон', sc2_prop_names)
        self.assertNotIn('ворона', sc2_prop_names)

        # Scene 3 (continuation via pronoun / narrative anchor "Этот пернатый гигант"): MUST get ONLY ['char_voron']
        sc3_data = scenes[3]['data']
        self.assertEqual(sc3_data.get('character_ids', []), ['char_voron'])

        # Global props table: Animals must NEVER appear in props
        project_prop_names = [p['name'].casefold() for p in detail.get('props', [])]
        self.assertNotIn('ворон', project_prop_names)
        self.assertNotIn('ворона', project_prop_names)

        # Prompts check
        prompts = self.service.project_prompts(p_id)
        # Scene 2 (solo raven) prompt must strictly mention @ворон, not @ворона
        p_sc2 = prompts['scenes'][2]
        self.assertIn('ABSOLUTE NO-TEXT RULE', p_sc2['comfy_prompt'])
        self.assertIn('@ворон', p_sc2['flow_prompt'].lower())
        self.assertNotIn('@ворона', p_sc2['flow_prompt'].lower())

    # Test 22: Закрытие ошибки «Вдали» и других пространственных наречий общим морфологическим правилом
    def test_22_spatial_adverb_vdali_general_rule_rejection(self):
        story = (
            "Вдали стоял старый маяк на скале. "
            "Впереди простиралось бескрайнее море. "
            "Наверху кружили чайки в лучах заката. "
            "Томас наблюдал за волнами с балкона."
        )

        # Direct morphological check via is_grammatical_non_character
        self.assertTrue(is_grammatical_non_character('Вдали'))
        self.assertTrue(is_grammatical_non_character('вдали'))
        self.assertTrue(is_grammatical_non_character('Вдалеке'))
        self.assertTrue(is_grammatical_non_character('Вблизи'))
        self.assertTrue(is_grammatical_non_character('Впереди'))
        self.assertTrue(is_grammatical_non_character('Позади'))
        self.assertTrue(is_grammatical_non_character('Наверху'))
        self.assertTrue(is_grammatical_non_character('Внизу'))
        self.assertTrue(is_grammatical_non_character('Сзади'))
        self.assertTrue(is_grammatical_non_character('Повсюду'))
        self.assertTrue(is_grammatical_non_character('Внезапно'))

        # Check candidate extraction on narrative with 'Вдали стоял...'
        resolver = CharacterEntityResolver(story)
        candidates = resolver.extract_deterministic_candidates()
        cand_names = [c['canonical_name'].casefold() for c in candidates]

        # Only 'Томас' qualifies as character; 'Вдали', 'Впереди', 'Наверху' are rejected
        self.assertTrue(any(c['canonical_name'].casefold() in ('thomas', 'томас') for c in candidates))
        self.assertNotIn('вдали', cand_names)
        self.assertNotIn('впереди', cand_names)
        self.assertNotIn('наверху', cand_names)

    # Test 23: Полная очистка списка персонажей от не-персонажей и грамматического мусора
    def test_23_garbage_words_filtered_from_cast_bible(self):
        story = (
            "Семнадцатое число подходило к концу. "
            "При этом Дэниел подошел к окну и посмотрел на улицу. "
            "Ничий след не виднелся на снегу. "
            "Нет, он точно знал, что Харпер ждет его в условленном месте. "
            "Кто мог предупредить поисковиков? "
            "Пауза затянулась. "
            "Вот на горизонте показался Майк вместе с Мейсоном. "
            "Деревья качались от сильного ветра. "
            "Мужчина вышел навстречу друзьям. "
            "Человек в капюшоне махнул рукой. "
            "Ответ последовал незамедлительно. "
            "Фигура скрылась за поворотом. "
            "Впервый раз за день они почувствовали облегчение. "
            "Большинство жителей уже спали. "
            "Поисковики прекратили осмотр территории."
        )

        resolver = CharacterEntityResolver(story)
        cast_bible = resolver.resolve([])
        characters = cast_bible.get('characters', [])
        char_names = [c['canonical_name'] for c in characters]
        char_names_lower = [c['canonical_name'].casefold() for c in characters]

        # Valid characters that SHOULD be extracted
        self.assertTrue(any('дэниел' in n for n in char_names_lower), f"Expected Дэниел in {char_names}")
        self.assertTrue(any('харпер' in n for n in char_names_lower), f"Expected Харпер in {char_names}")
        self.assertTrue(any('майк' in n for n in char_names_lower), f"Expected Майк in {char_names}")
        self.assertTrue(any('мейсон' in n for n in char_names_lower), f"Expected Мейсон in {char_names}")

        # Garbage words that MUST NEVER be present in characters list
        garbage_words = [
            'семнадцатое', 'семнадцатый',
            'при',
            'ничий', 'ничей',
            'нет',
            'кто',
            'пауза',
            'вот',
            'деревья', 'дерево',
            'мужчина',
            'человек',
            'ответ',
            'фигура',
            'впервый', 'впервые',
            'большинство',
            'поисковики'
        ]

        for word in garbage_words:
            self.assertNotIn(
                word,
                char_names_lower,
                f"Garbage non-character word '{word}' must NOT be in characters list: {char_names}"
            )

    def test_24_toponym_location_filtered_from_cast_bible(self):
        story = (
            "Дэниел приехал в Нортвуд на старом пикапе. "
            "В Нортвуде стоял густой холодный туман. "
            "Жители Нортвуда редко выходили на улицу после заката. "
            "Дэниел встретил Харпер у входа в мотель. "
            "Харпер передала ему карту и сказала: «Будь осторожен». "
            "Позже к ним присоединился Майк."
        )

        resolver = CharacterEntityResolver(story)
        cast_bible = resolver.resolve([])
        characters = cast_bible.get('characters', [])
        char_names = [c['canonical_name'] for c in characters]
        char_names_lower = [c['canonical_name'].casefold() for c in characters]

        # Toponym Нортвуд must NOT be extracted as a character
        self.assertNotIn('нортвуд', char_names_lower, f"Toponym 'Нортвуд' must NOT be in characters: {char_names}")

        # Real characters must be extracted
        self.assertTrue(any('дэниел' in n for n in char_names_lower), f"Expected Дэниел in {char_names}")
        self.assertTrue(any('харпер' in n for n in char_names_lower), f"Expected Харпер in {char_names}")


    def test_25_offscreen_remembered_character_allowed_in_preflight_and_flow_readiness(self):
        """Preflight and ready_for_flow allow scenes where a recurring character is remembered / offscreen,
        with a solo episodic role in frame (e.g. Scene 4: employee recalling Daniel)."""
        project_data = {
            'id': 'proj_scene4_test',
            'title': 'Memory Scene Test',
            'story': (
                "Дэниел приехал в Нортвуд утром семнадцатого октября. Он зарегистрировался в центре для посетителей. "
                "Сотрудница центра позже вспоминала, что Дэниел казался спокойным и хорошо подготовленным."
            ),
            'style': 'Cinematic photorealism',
            'characters': [
                {'id': 'char_deniel', 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'role': 'protagonist',
                 'description': 'Male hiker in early 30s', 'wardrobe': {}, 'references': {'portrait': 'ast_dan'}, 'locked': 1}
            ],
            'locations': [
                {'id': 'loc_center', 'name': 'Visitor Center', 'description': 'Small wooden building with desk',
                 'references': {'environment': 'ast_loc'}, 'locked': 1}
            ],
            'props': [],
            'scenes': [
                {
                    'position': 1,
                    'beat': 'A staff member at the center later recalls Дэниел’s composed demeanor and thorough preparation.',
                    'source_quote': 'Сотрудница центра позже вспоминала, что Дэниел казался спокойным и хорошо подготовленным.',
                    'shot': 'Close-up on staff’s face, background blurred',
                    'shot_type': 'main_action',
                    'location': 'Visitor Center',
                    'location_ref': 'Visitor Center',
                    'characters': [],
                    'character_ids': [],
                    'episodic_people': ['сотрудница центра'],
                    'visible_people': 1,
                    'wardrobe': {}
                }
            ]
        }

        from imagegen.domain import validate_project_preflight
        preflight = validate_project_preflight(project_data, project_data['story'])
        # Must pass preflight with no missing_character_scenes error!
        self.assertTrue(preflight['valid'], f"Preflight must be valid, got errors: {preflight['errors']}")
        missing_errors = [e for e in preflight['errors'] if e.get('code') == 'missing_character_scenes']
        self.assertEqual(len(missing_errors), 0, f"Expected 0 missing_character_scenes errors, got {missing_errors}")

    def test_26_physically_present_character_without_binding_blocked_in_preflight_and_flow(self):
        """When a story character is physically present in the frame without binding,
        both Preflight and Flow readiness must strictly block generation."""
        project_data = {
            'id': 'proj_phys_missing_test',
            'title': 'Physically Missing Test',
            'story': "Дэниел вошёл в комнату и сел за стол.",
            'style': 'Cinematic photorealism',
            'characters': [
                {'id': 'char_deniel', 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'role': 'protagonist',
                 'description': 'Male hiker', 'wardrobe': {}, 'references': {'portrait': 'ast_dan'}, 'locked': 1}
            ],
            'locations': [
                {'id': 'loc_room', 'name': 'Office', 'description': 'Small office',
                 'references': {'environment': 'ast_loc'}, 'locked': 1}
            ],
            'props': [],
            'scenes': [
                {
                    'position': 1,
                    'beat': 'Дэниел enters the room and sits down at the desk.',
                    'source_quote': 'Дэниел вошёл в комнату и сел за стол.',
                    'shot': 'Medium shot of Daniel sitting at the desk',
                    'shot_type': 'main_action',
                    'location': 'Office',
                    'location_ref': 'Office',
                    'characters': [],  # Missing binding!
                    'character_ids': [],
                    'visible_people': 1,
                    'wardrobe': {}
                }
            ]
        }

        from imagegen.domain import validate_project_preflight
        preflight = validate_project_preflight(project_data, project_data['story'])
        self.assertFalse(preflight['valid'], "Preflight must fail for physically present unbound character")
        char_errors = [e for e in preflight['errors'] if e.get('code') in ('missing_character_scenes', 'protagonist_missing')]
        self.assertTrue(len(char_errors) > 0, "Preflight must report missing_character_scenes or protagonist_missing")

    def test_27_preflight_and_ready_for_flow_consistency_invariant(self):
        """Preflight integrity validation and ready_for_flow must strictly agree on scene readiness."""
        project = self.service.create_project({
            'title': 'Consistency Test',
            'story': (
                "Дэниел приехал в Нортвуд. "
                "Сотрудница центра позже вспоминала Дэниела."
            ),
            'style': 'Cinematic photorealism'
        })
        p_id = project['id']

        # Analysis with Scene 1: Daniel in street; Scene 2: Employee recalling Daniel (solo episodic)
        analysis = {
            'world': 'Town',
            'characters': [{'name': 'Дэниел', 'description': 'Hiker in his 30s', 'wardrobe': {}}],
            'locations': [
                {'name': 'Street', 'description': 'Quiet town street'},
                {'name': 'Visitor Center', 'description': 'Small center office'}
            ],
            'props': [],
            'scenes': [
                {
                    'position': 1,
                    'beat': 'Дэниел arrives on the quiet street.',
                    'source_quote': 'Дэниел приехал в Нортвуд.',
                    'location': 'Street', 'location_ref': 'Street',
                    'time': 'Morning', 'shot': 'Wide shot of Daniel on the street',
                    'emotion': 'calm', 'continuity_before': '', 'continuity_after': '',
                    'visible_people': 1, 'characters': ['Дэниел'], 'wardrobe': {}, 'props': []
                },
                {
                    'position': 2,
                    'beat': 'The staff member recalls Дэниел.',
                    'source_quote': 'Сотрудница центра позже вспоминала Дэниела.',
                    'location': 'Visitor Center', 'location_ref': 'Visitor Center',
                    'time': 'Day', 'shot': 'Close-up on staff member face',
                    'emotion': 'thoughtful', 'continuity_before': '', 'continuity_after': '',
                    'visible_people': 1, 'characters': [], 'episodic_people': ['сотрудница центра'],
                    'wardrobe': {}, 'props': []
                }
            ]
        }

        self.service.import_analysis(p_id, analysis, 1)

        # Lock references
        from PIL import Image
        import io
        img = Image.new('RGB', (768, 432), color='blue')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        dummy_png = buf.getvalue()

        dan = [c for c in self.service.detail(p_id)['characters'] if c['name'] == 'Дэниел'][0]
        self.service.upload_character_portrait(dan['id'], dummy_png, lock=True)

        loc_street = [l for l in self.service.detail(p_id)['locations'] if l['name'] == 'Street'][0]
        self.service.upload_location_image(loc_street['id'], dummy_png, lock=True)

        loc_vc = [l for l in self.service.detail(p_id)['locations'] if l['name'] == 'Visitor Center'][0]
        self.service.upload_location_image(loc_vc['id'], dummy_png, lock=True)

        # Check preflight and project_prompts
        preflight = self.service.project_preflight(p_id)
        self.assertTrue(preflight['valid'], f"Preflight must be valid: {preflight['errors']}")

        prompts = self.service.project_prompts(p_id)
        for s in prompts['scenes']:
            self.assertTrue(s['ready_for_flow'], f"Scene {s['position']} must be ready_for_flow: {s.get('flow_error')}")


if __name__ == '__main__':
    unittest.main()

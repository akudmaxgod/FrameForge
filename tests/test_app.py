import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw
from imagegen.db import uid, dump, now
from imagegen.domain import (DomainError, MAX_SCENES, validate_analysis, compile_prompt,
                             compile_flow_prompt, age_constraint, gender_constraint,
                             scene_has_explicit_unbound_person, scene_anonymous_people_count,
                             scene_over_shoulder_anonymous_people_count, normalize_prop_continuity)
from imagegen.service import Service
from imagegen.providers import (build_workflow, ProviderError, ComfyProvider, GeminiTextProvider, GoogleNanoBananaProvider, GroqProvider,
                                story_segments, story_character_context, attach_exact_sources, normalize_analysis,
                                analysis_schema_for_segments, chunk_segments,
                                allocate_scene_counts, subdivide_analysis_jobs,
                                expand_storyboard_scenes, prioritize_active_story_characters, LlamaCppProvider,
                                camera_director_prompt)
from imagegen.worker import Worker, ResourceSafetyError, ContentRejectedError
from imagegen.server import Application, make_server
from imagegen.runtime import RuntimeManager
from imagegen.cast import CharacterEntityResolver


def analysis(count=2, characters=True):
    names = ['Ada', 'Ben'] if characters else []
    return {'world': 'Space station', 'characters': [
        {'name': name, 'description': 'Distinct character ' + name, 'wardrobe': {}} for name in names],
        'scenes': [{'beat': f'Event {i}', 'source_quote': f'Event {i}.', 'characters': names,
                    'wardrobe': {}, 'location': 'Station', 'time': 'Day', 'shot': 'Wide',
                    'continuity_before': 'Ada holds compass', 'continuity_after': 'Ben holds compass'}
                   for i in range(count)]}


def png(width=1024, height=576):
    result = io.BytesIO()
    Image.new('RGB', (width, height), '#526880').save(result, format='PNG')
    return result.getvalue()


def panel_png(width=1024, height=576):
    image = Image.new('RGB', (width, height), '#526880')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width // 2 - 4, height // 2 - 4), fill='#31597a')
    draw.rectangle((width // 2 + 3, 0, width, height // 2 - 4), fill='#806040')
    draw.rectangle((0, height // 2 + 3, width // 2 - 4, height), fill='#35623a')
    draw.rectangle((width // 2 + 3, height // 2 + 3, width, height), fill='#71435a')
    draw.line((width // 2, 0, width // 2, height), fill='white', width=6)
    draw.line((0, height // 2, width, height // 2), fill='white', width=6)
    result = io.BytesIO()
    image.save(result, format='PNG')
    return result.getvalue()


def split_png(width=1024, height=576):
    image = Image.new('RGB', (width, height), '#526880')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width // 2 - 4, height), fill='#31597a')
    draw.rectangle((width // 2 + 3, 0, width, height), fill='#806040')
    draw.line((width // 2, 0, width // 2, height), fill='white', width=6)
    result = io.BytesIO()
    image.save(result, format='PNG')
    return result.getvalue()


class AnalysisNormalizationTests(unittest.TestCase):
    def test_acoustic_label_never_creates_a_cast_card(self):
        story = 'Дэн услышал голос Майка в лесу. Майк был его старшим братом.'
        cast = CharacterEntityResolver(story).resolve([
            {'name': 'Голос Майка', 'description': 'Offscreen voice.', 'wardrobe': {}}
        ])
        names = [item['canonical_name'] for item in cast['characters']]
        self.assertNotIn('Голос Майка', names)

    def test_acoustic_label_never_stays_in_scene_cast(self):
        story = 'Дэн услышал голос Майка в лесу. Майк был его старшим братом.'
        result = prioritize_active_story_characters({
            'characters': [{'name': 'Голос Майка', 'description': 'Offscreen voice.', 'wardrobe': {}}],
            'scenes': [{
                'characters': ['Голос Майка'],
                'characters_present': ['Дэн'],
                'source_quote': 'Дэн услышал голос Майка в лесу.',
                'beat': 'Дэн слышит голос.',
            }],
        }, story)
        self.assertNotIn('Голос Майка', result['scenes'][0]['characters'])
        self.assertEqual([], result['scenes'][0]['character_ids'])

    def test_over_shoulder_employee_is_a_visible_second_person(self):
        scene = {'shot': 'Over-the-shoulder shot of Сотрудница центра looking toward Дэниел’s backpack.'}
        self.assertEqual(1, scene_over_shoulder_anonymous_people_count(scene, ['Дэниел']))

    def test_age_constraint_understands_exact_and_decade_ages(self):
        self.assertEqual({'minimum': 30, 'maximum': 39, 'label': '30–39 years old'},
                         age_constraint('A woman in her 30s.'))
        self.assertEqual({'minimum': 42, 'maximum': 42, 'label': '42 years old'},
                         age_constraint('Женщина 42 года, спокойная.'))
        self.assertEqual({'minimum': 8, 'maximum': 8, 'label': '8 years old'},
                         age_constraint('A girl, 8 years old.'))
        self.assertIsNone(age_constraint('Adult woman with dark hair.'))

    def test_age_constraint_understands_written_russian_age(self):
        self.assertEqual({'minimum': 25, 'maximum': 25, 'label': '25 years old'},
                         age_constraint('Молодой человек двадцати пяти лет, худощавый.'))

    def test_demographics_understand_roles_and_young_adult(self):
        self.assertEqual({'minimum': 25, 'maximum': 35, 'label': '25–35 years old'},
                         age_constraint('A young woman with dark hair.'))
        self.assertEqual({'value': 'male', 'label': 'man'}, gender_constraint('A quiet person', 'Her husband'))
        self.assertEqual({'value': 'female', 'label': 'woman'}, gender_constraint('A young woman', 'Daughter'))

    def test_active_children_replace_deceased_character_slots_and_link_to_their_events(self):
        story = ('Анна живёт одна. Её муж умер три года назад. Дочь пришла к Анне и сказала, '
                 'что она с Андреем, сыном Анны, приняла решение.')
        data = {'world': 'Modern town', 'locations': [], 'characters': [
                    {'name': 'Anna', 'description': 'Woman, 72 years old.', 'wardrobe': {}},
                    {'name': 'Husband', 'description': 'Man who passed away three years ago.', 'wardrobe': {}},
                    {'name': 'Daughter', 'description': 'Woman in her 30s, Anna’s daughter.', 'wardrobe': {}}],
                'scenes': [
                    {'source_quote': 'Её муж умер три года назад.', 'beat': 'Anna remembers her husband.',
                     'characters': ['Anna', 'Husband'], 'wardrobe': {'Husband': 'coat'}, 'visible_people': 1},
                    {'source_quote': 'Дочь пришла к Анне и сказала, что она с Андреем, сыном Анны, приняла решение.',
                     'beat': 'The daughter speaks with Anna and Andrei.', 'characters': ['Anna', 'Daughter'],
                     'wardrobe': {}, 'visible_people': 2}]}
        result = prioritize_active_story_characters(data, story)
        self.assertEqual(['Анна', 'Daughter', 'Son'], [item['name'] for item in result['characters']])
        self.assertEqual(['Анна'], result['scenes'][0]['characters'])
        self.assertEqual(['Анна', 'Daughter', 'Son'], result['scenes'][1]['characters'])
        self.assertEqual(3, result['scenes'][1]['visible_people'])

    def test_active_captain_is_stable_when_analyser_omits_the_role(self):
        story = 'Thomas tends the lighthouse at night. The captain arrives by boat and warns Thomas of the storm.'
        data = {'world': 'Stormy coast', 'locations': [],
                'characters': [{'name': 'Thomas', 'description': 'Adult man, lighthouse keeper.', 'wardrobe': {}}],
                'scenes': [
                    {'source_quote': 'Thomas tends the lighthouse at night.', 'beat': 'Thomas tends the lighthouse.',
                     'characters': ['Thomas'], 'wardrobe': {}, 'visible_people': 1},
                    {'source_quote': 'The captain arrives by boat and warns Thomas of the storm.',
                     'beat': 'The captain warns Thomas.', 'characters': ['Thomas'], 'wardrobe': {}, 'visible_people': 1}]}
        result = prioritize_active_story_characters(data, story)
        self.assertEqual(['Thomas', 'Captain'], [item['name'] for item in result['characters']])
        self.assertEqual(['Thomas', 'Captain'], result['scenes'][1]['characters'])
        self.assertEqual(2, result['scenes'][1]['visible_people'])

    def test_portrait_for_a_thirty_something_forbids_unrequested_grey_hair(self):
        service = Service.__new__(Service)
        snapshot = service.casting_snapshot(
            {'mode': 'characters', 'style': 'realism'},
            {'name': 'Daughter', 'description': 'A woman in her 30s, wearing a business suit.'},
            profile={'unet': 'test'})
        self.assertIn('Do not show grey, silver, white or grey-streaked hair', snapshot['prompt'])

    def test_location_reference_is_readable_as_bank_or_living_room_without_text(self):
        service = Service.__new__(Service)
        project = {'style': 'realism'}
        bank = service.location_snapshot(project, {'name': 'Bank Vault', 'description': 'Secure bank room.'},
                                         profile={'unet': 'test'})
        room = service.location_snapshot(project, {'name': 'Living room', 'description': 'Family room at home.'},
                                         profile={'unet': 'test'})
        self.assertIn('service counter', bank['prompt'])
        self.assertIn('safe-deposit drawers', bank['prompt'])
        self.assertIn('sofa, armchair, coffee table', room['prompt'])
        self.assertIn('do not substitute an exterior facade',
                      ' COMPOSITION OVERRIDE: ' + room['location_anchor'] + ' Keep this exact type of interior; do not substitute an exterior facade.')

    def test_runtime_api_key_survives_application_restart(self):
        with tempfile.TemporaryDirectory() as root:
            first = RuntimeManager(root).text_key
            second = RuntimeManager(root).text_key
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)

    def test_source_ids_become_exact_quotes_in_story_order(self):
        story = 'First event happened. Second event happened! Third event followed?'
        segments = story_segments(story)
        data = {'scenes': [{'source_id': 2}, {'source_id': 0}, {'source_id': 1}]}
        result = attach_exact_sources(data, segments)
        self.assertEqual([scene['source_quote'] for scene in result['scenes']], segments)
        self.assertTrue(all('source_id' not in scene for scene in result['scenes']))

    def test_markdown_project_notes_are_context_not_storyboard_scenes(self):
        story = '''# Кастрюля семи огней
Жанр: городское фэнтези
Визуальный стиль: cinematic
## Главный герой
Антон — молодой человек двадцати пяти лет, с короткими тёмными волосами.
## Сценарий
### Сцена 1
Антон входит в тёмный двор.
### Сцена 2
Он замечает старую кастрюлю у стены.'''
        self.assertEqual(
            ['Антон входит в тёмный двор.', 'Он замечает старую кастрюлю у стены.'],
            story_segments(story))
        context = story_character_context(story)
        self.assertIn('Антон — молодой человек двадцати пяти лет', context)
        self.assertNotIn('Жанр:', context)
        self.assertNotIn('Визуальный стиль:', context)

    def test_flow_prompt_keeps_worn_story_prop_and_adds_visual_detail(self):
        scene = {'beat': 'Anton discovers that the pot gives him strength.',
                 'source_quote': 'Антон надевает кастрюлю и чувствует прилив силы.',
                 'characters': ['Anton'], 'visible_people': 1,
                 'location_ref': 'Courtyard', 'time': 'Night', 'shot': 'Low-angle medium shot',
                 'emotion': 'astonished confidence',
                 'continuity_before': 'Anton lifts the pot from the ground.',
                 'continuity_after': 'He keeps the pot on his head.',
                 'props': [{'name': 'Magic pot', 'state': 'worn_on_head',
                            'holder': 'Anton', 'visible': True}]}
        prompt = compile_flow_prompt(
            {'style': 'cinematic photography'}, scene,
            characters=[{'name': 'Anton'}],
            locations=[{'name': 'Courtyard', 'description': 'Wet brick walls under a flickering lamp.'}],
            props=[{'name': 'Magic pot', 'description': 'Dented copper pot', 'importance': 'story'}])
        self.assertIn('Environment: Wet brick walls under a flickering lamp.', prompt)
        self.assertIn('Visual continuity:', prompt)
        self.assertIn('foreground, midground and background', prompt)
        self.assertIn('Keep referenced headwear exactly as specified', prompt)
        self.assertNotIn('bare head', prompt)
        self.assertNotIn('strictly no hat', prompt)

    def test_flow_prompt_allows_story_required_in_world_text_without_dialogue_overlays(self):
        scene = {'beat': 'Emma opens the letter.', 'source_quote': 'Emma reads a letter from her father.',
                 'characters': ['Emma'], 'visible_people': 1, 'location': 'Study',
                 'props': [{'name': 'Letter', 'state': 'held', 'holder': 'Emma', 'visible': True}]}
        prompt = compile_flow_prompt({'style': 'cinematic'}, scene,
                                     characters=[{'name': 'Emma'}], locations=[],
                                     props=[{'name': 'Letter', 'importance': 'story', 'description': 'Handwritten letter.'}])
        self.assertIn('In-world text is allowed only when the source explicitly calls for it', prompt)
        self.assertIn('physically printed, handwritten or engraved on that named object', prompt)
        self.assertIn('Never render words as floating, glowing or superimposed typography', prompt)
        self.assertIn('Never draw sound waves, voice trails', prompt)
        self.assertIn('Never add subtitles, captions, narration overlays, speech bubbles, dialogue balloons, dialogue windows', prompt)
        self.assertNotIn('No readable text anywhere', prompt)
        self.assertNotIn('show it closed, turned away, blank or too out of focus to read', prompt)

    def test_flow_prompt_requires_a_complete_recognisable_animal(self):
        scene = {'beat': 'The saola crosses the rainforest platform.',
                 'source_quote': 'Фотоловушка снимает саолу в тропическом лесу.',
                 'characters': ['Saola'], 'visible_people': 0,
                 'location': 'Rainforest platform', 'shot': 'Wide shot'}
        prompt = compile_flow_prompt(
            {'style': 'cinematic wildlife photography'}, scene,
            characters=[{'name': 'Saola', 'description': 'Entity type: animal; species: saola.'}])
        self.assertIn('head and eyes visible, full torso', prompt)
        self.assertIn('Never show only legs, hindquarters, torso, a cropped body fragment', prompt)
        self.assertIn('one intact continuous physically plausible form', prompt)

    def test_flow_prompt_never_hides_a_named_scene_character_as_remote_caller(self):
        scene = {'beat': 'The captain calls from the boat and Thomas answers him.',
                 'source_quote': 'Captain Reed calls Thomas from the boat.',
                 'characters': ['Thomas', 'Captain Reed'], 'visible_people': 2,
                 'location': 'Boat dock', 'shot': 'Two-shot'}
        prompt = compile_flow_prompt({'style': 'mystery illustration'}, scene,
                                     characters=[{'name': 'Thomas'}, {'name': 'Captain Reed'}])
        self.assertIn('Featuring @Thomas, @Captain Reed', prompt)
        self.assertNotIn('remote caller or speaker is heard only off-screen', prompt)
        self.assertIn('One single full-frame image only', prompt)

    def test_flow_prompt_requires_complete_body_for_wide_movement_shot(self):
        scene = {'beat': 'Thomas climbs the lighthouse stairs.',
                 'source_quote': 'Thomas walks onto the balcony.',
                 'characters': ['Thomas'], 'visible_people': 1,
                 'location': 'Lighthouse', 'shot': 'Wide shot'}
        prompt = compile_flow_prompt({'style': 'mystery illustration'}, scene,
                                     characters=[{'name': 'Thomas'}])
        self.assertIn('Full-body composition', prompt)
        self.assertIn('two complete legs and both feet', prompt)
        self.assertIn('building cross-section', prompt)

    def test_flow_prompt_never_requests_a_person_without_cast_or_explicit_role(self):
        scene = {'beat': 'His face shows fatigue.', 'source_quote': 'The documents lie on the desk.',
                 'characters': [], 'visible_people': 1, 'location': 'Study'}
        prompt = compile_flow_prompt({'style': 'cinematic'}, scene, characters=[], locations=[])
        self.assertIn('Uninhabited scene, empty setting, no people', prompt)
        self.assertNotIn('Solo shot, exactly 1 person', prompt)

    def test_pronoun_without_cast_cannot_create_an_unknown_person(self):
        scene = {'source_quote': 'The laptop screen flickers on the table.',
                 'beat': 'His face shows fatigue as he looks around.',
                 'characters': [], 'wardrobe': {}, 'visible_people': 1}
        self.assertFalse(scene_has_explicit_unbound_person(scene, ['Alexey']))
        result = normalize_analysis({'characters': [{'name': 'Alexey', 'description': 'Man', 'wardrobe': {}}],
                                     'locations': [], 'props': [], 'scenes': [scene]})
        self.assertEqual([], result['scenes'][0]['characters'])
        self.assertEqual(0, result['scenes'][0]['visible_people'])

    def test_explicit_one_off_role_remains_allowed_without_reference_cast(self):
        scene = {'source_quote': 'A guard opens the gate.',
                 'beat': 'A guard opens the gate.', 'characters': [], 'wardrobe': {}}
        self.assertTrue(scene_has_explicit_unbound_person(scene, ['Alexey']))

    def test_episodic_roles_keep_human_scene_contract_without_portrait_cards(self):
        scene = {
            'source_quote': 'Деревенский плотник вырезал рукояти, кожевник шил ножны.',
            'beat': 'Плотник и кожевник готовят детали в кузнице.',
            'characters': [], 'visible_people': 0, 'location': 'Кузница', 'shot': 'Medium two-shot',
        }
        self.assertEqual(2, scene_anonymous_people_count(scene, ['Мартин', 'Лука']))
        flow = compile_flow_prompt({'style': 'medieval illustration'},
                                   {**scene, 'visible_people': 2}, characters=[])
        self.assertNotIn('Uninhabited scene', flow)
        self.assertIn('Exactly 2 anonymous source-described participants', flow)

    def test_anonymous_group_role_uses_bounded_people_contract(self):
        scene = {
            'source_quote': 'К первому снегу солдаты приехали за оружием.',
            'beat': 'Солдаты заходят в кузницу.', 'characters': [], 'visible_people': 0,
        }
        self.assertEqual(3, scene_anonymous_people_count(scene, ['Мартин', 'Лука']))
        single = {'source_quote': 'A soldier waits at the gate.', 'beat': 'A soldier waits.'}
        self.assertEqual(1, scene_anonymous_people_count(single, []))

    def test_prop_cannot_appear_before_its_source_backed_first_appearance(self):
        scenes = []
        for index in range(1, 8):
            quote = 'She remembers the years in her home.'
            if index == 7:
                quote = 'She begins packing boxes for the move.'
            scenes.append({'source_quote': quote, 'beat': quote, 'characters': ['Emma'],
                           'visible_people': 1, 'location': 'Home', 'location_ref': 'Home',
                           'props': [{'name': 'Box', 'state': 'placed', 'visible': True}]})
        data = {'props': [{'name': 'Box', 'description': 'Plain moving box.', 'importance': 'story',
                           'first_appearance': 1, 'default_state': 'absent'}], 'scenes': scenes}
        result = normalize_prop_continuity(data)
        self.assertEqual([], result['scenes'][1]['props'])
        self.assertEqual('placed', result['scenes'][6]['props'][0]['state'])
        self.assertTrue(result['scenes'][6]['props'][0]['visible'])

    def test_collapsed_source_selection_is_spread_across_whole_story(self):
        segments = [f'Part {index}.' for index in range(8)]
        data = {'scenes': [{'source_id': 3} for _ in range(4)]}
        result = attach_exact_sources(data, segments)
        self.assertEqual(['Part 0.', 'Part 2.', 'Part 5.', 'Part 7.'],
                         [scene['source_quote'] for scene in result['scenes']])

    def test_repeated_final_story_moment_is_replaced_with_ordered_coverage(self):
        segments = [f'Part {index}.' for index in range(8)]
        data = {'scenes': [{'source_id': source_id, 'beat': 'The woman walks away from the house.',
                            'location': 'Street', 'time': 'Dusk', 'shot': 'Wide',
                            'continuity_before': '', 'continuity_after': '',
                            'characters': [], 'wardrobe': {}}
                           for source_id in (0, 1, 2, 3, 6, 6)]}
        result = attach_exact_sources(data, segments)
        self.assertEqual(['Part 0.', 'Part 1.', 'Part 3.', 'Part 4.', 'Part 6.', 'Part 7.'],
                         [scene['source_quote'] for scene in result['scenes']])
        self.assertEqual('Visually depict this exact story event: Part 4.', result['scenes'][3]['beat'])

    def test_surplus_frames_are_distributed_in_order_instead_of_looping_from_the_final_scene(self):
        segments = ['First.', 'Second.', 'Third.']
        data = {'scenes': [{'source_id': 2, 'beat': f'Recycled event {index}',
                            'location': 'Wrong place', 'time': 'Wrong time', 'shot': 'Wrong shot',
                            'continuity_before': 'Wrong', 'continuity_after': 'Wrong'}
                           for index in range(7)]}
        result = attach_exact_sources(data, segments)
        self.assertEqual(['First.', 'First.', 'First.', 'Second.', 'Second.', 'Third.', 'Third.'],
                         [scene['source_quote'] for scene in result['scenes']])
        self.assertEqual('Visually depict this exact story event: First.', result['scenes'][0]['beat'])
        self.assertIn('different from the preceding frame', result['scenes'][2]['beat'])

    def test_attach_exact_sources_preserves_consecutive_scenes_from_start(self):
        segments = [f'Sentence {index}.' for index in range(20)]
        data = {'scenes': [{'source_id': index, 'beat': f'Beat {index}',
                            'location': 'Room', 'time': 'Day', 'shot': 'Medium',
                            'continuity_before': '', 'continuity_after': '',
                            'characters': [], 'wardrobe': {}}
                           for index in range(5)]}
        result = attach_exact_sources(data, segments)
        self.assertEqual([f'Sentence {index}.' for index in range(5)],
                         [scene['source_quote'] for scene in result['scenes']])

    def test_exact_wardrobe_description_is_mapped_to_declared_key(self):
        data = {'characters': [{'name': 'Mira', 'wardrobe': {'coat': 'Long blue coat'}}],
                'scenes': [{'characters': ['Mira'], 'wardrobe': {'Mira': 'Long blue coat'}}]}
        self.assertEqual(normalize_analysis(data)['scenes'][0]['wardrobe'], {'Mira': 'coat'})

    def test_empty_wardrobe_placeholder_is_removed_only_when_bible_is_empty(self):
        data = {'characters': [{'name': 'Cat', 'wardrobe': {}}],
                'scenes': [{'characters': ['Cat'], 'wardrobe': {'Cat': '{}'}}]}
        self.assertEqual(normalize_analysis(data)['scenes'][0]['wardrobe'], {})

    def test_outfit_assignment_restores_omitted_scene_character(self):
        data = {'world': 'Modern city',
                'characters': [{'name': 'Anton', 'description': 'Young man',
                                'wardrobe': {'daily': 'Blue jacket'}}],
                'scenes': [{'beat': 'Anton finds a pot', 'source_quote': 'Anton finds a pot.',
                            'characters': [], 'wardrobe': {'Anton': 'daily'},
                            'location': 'Courtyard', 'time': 'Morning', 'shot': 'Wide',
                            'continuity_before': '', 'continuity_after': ''}]}
        result = normalize_analysis(data)
        self.assertEqual(result['scenes'][0]['characters'], ['Anton'])
        self.assertEqual(result['scenes'][0]['wardrobe'], {'Anton': 'daily'})
        self.assertIs(validate_analysis(result, 'Anton finds a pot.'), result)

    def test_object_only_scene_drops_analyser_phantom_person(self):
        quote = 'На журнальном столике мерцает экран забытого ноутбука.'
        data = {'world': 'A dim apartment',
                'characters': [{'name': 'Alexey', 'description': 'A man', 'wardrobe': {}}],
                'locations': [{'name': 'Living room', 'description': 'A dark living room'}],
                'scenes': [{'beat': 'A faint glow flickers from the laptop screen on the coffee table.',
                            'source_quote': quote, 'characters': [], 'wardrobe': {},
                            'location': 'Living room', 'location_ref': 'Living room',
                            'time': 'Night', 'shot': 'Wide', 'emotion': 'quiet',
                            'continuity_before': 'The room is dim.',
                            'continuity_after': 'Alexey bursts through the doorway.',
                            'visible_people': 1}]}
        result = normalize_analysis(data)
        self.assertEqual(0, result['scenes'][0]['visible_people'])
        self.assertIs(validate_analysis(result, quote), result)
        prompt = compile_flow_prompt({'style': 'cinematic'}, result['scenes'][0],
                                     characters=[], locations=result['locations'])
        self.assertIn('Uninhabited scene, empty setting, no people, no characters', prompt)

    def test_scene_character_names_are_canonicalized_and_deduplicated(self):
        data = {'world': 'Modern city',
                'characters': [{'name': 'Anton', 'description': 'Young man',
                                'wardrobe': {'daily': 'Blue jacket'}}],
                'scenes': [{'beat': 'Anton meets a passerby',
                            'source_quote': 'Anton meets a passerby.',
                            'characters': [' anton ', 'Anton', 'ANTON', 'Passerby'],
                            'wardrobe': {' anton ': 'daily', 'Passerby': 'street'},
                            'location': 'Street', 'time': 'Morning', 'shot': 'Wide',
                            'continuity_before': '', 'continuity_after': ''}]}
        result = normalize_analysis(data)
        self.assertEqual(result['scenes'][0]['characters'], ['Anton'])
        self.assertEqual(result['scenes'][0]['wardrobe'], {'Anton': 'daily'})
        self.assertIs(validate_analysis(result, 'Anton meets a passerby.'), result)

    def test_validation_recovers_repeated_animal_subject_in_a_scene(self):
        story = 'Панголин ночью идёт по лесной тропе.'
        data = {
            'characters': [{'name': 'Панголин', 'description': 'Entity type: animal; scaled mammal.', 'wardrobe': {}}],
            'locations': [], 'props': [],
            'scenes': [{'beat': 'The pangolin walks along the forest trail.', 'source_quote': story,
                        'characters': ['панголин', 'Панголин', ' Панголин '], 'wardrobe': {},
                        'location': '', 'time': 'Night', 'shot': 'Wide', 'emotion': 'cautious',
                        'continuity_before': '', 'continuity_after': ''}]
        }
        self.assertIs(validate_analysis(data, story), data)
        self.assertEqual(['Панголин'], data['scenes'][0]['characters'])

    def test_duplicate_character_bible_entries_are_merged(self):
        data = {'characters': [
                    {'name': ' Mira ', 'description': 'Woman', 'wardrobe': {'day': 'Blue coat'}},
                    {'name': 'MIRA', 'description': 'Woman', 'wardrobe': {'night': 'Black coat'}}],
                'scenes': [{'characters': ['mira'], 'wardrobe': {'MIRA': 'night'}}]}
        result = normalize_analysis(data)
        self.assertEqual(len(result['characters']), 1)
        self.assertEqual(result['characters'][0]['name'], 'Mira')
        self.assertEqual(result['scenes'][0]['characters'], ['Mira'])
        self.assertEqual(result['scenes'][0]['wardrobe'], {'Mira': 'night'})

    def test_concrete_scene_location_missing_from_bible_is_added_and_linked(self):
        data = {'locations': [{'name': 'Family house', 'description': 'Old family home.'}],
                'scenes': [{'location': 'Hospital room', 'location_ref': '',
                            'beat': 'The woman recovers after surgery.', 'characters': [], 'wardrobe': {}}]}
        result = normalize_analysis(data)
        self.assertEqual(['Family house', 'Hospital room'], [item['name'] for item in result['locations']])
        self.assertEqual('Hospital room', result['scenes'][0]['location_ref'])

    def test_bank_vault_and_safe_deposit_box_are_one_location(self):
        data = {'locations': [{'name': 'Bank Vault', 'description': 'Secure bank room.'}],
                'scenes': [{'location': 'Bank Safe Deposit Box', 'location_ref': '',
                            'beat': 'She opens the deposit box.', 'characters': [], 'wardrobe': {}}]}
        result = normalize_analysis(data)
        self.assertEqual(['Bank Vault'], [item['name'] for item in result['locations']])
        self.assertEqual('Bank Vault', result['scenes'][0]['location_ref'])

    def test_separate_clothing_items_become_one_complete_outfit(self):
        data = {'characters': [{'name': 'Anna', 'wardrobe': {
                    'blue_coat': 'long blue wool coat', 'red_scarf': 'red knitted scarf',
                    'dark_pants': 'dark practical trousers', 'leather_bag': 'brown leather shoulder bag'}}],
                'scenes': [{'characters': ['Anna'], 'wardrobe': {
                    'Anna': 'blue_coat, red_scarf, dark_pants, leather_bag'}}]}
        result = normalize_analysis(data)
        outfit = result['scenes'][0]['wardrobe']['Anna']
        self.assertTrue(outfit.startswith('look_blue_coat_red_scarf_dark_pants_leather_bag_'))
        self.assertIn('long blue wool coat', result['characters'][0]['wardrobe'][outfit])

    def test_large_item_inventory_is_pruned_to_used_complete_outfits(self):
        items = {f'item_{index}': f'garment {index}' for index in range(16)}
        first = ', '.join(f'item_{index}' for index in range(8))
        second = ', '.join(f'item_{index}' for index in range(8, 16))
        data = {'characters': [{'name': 'Anna', 'description': 'Explorer', 'wardrobe': items}],
                'scenes': [
                    {'characters': ['Anna'], 'wardrobe': {'Anna': first}},
                    {'characters': ['Anna'], 'wardrobe': {'Anna': second}}]}
        result = normalize_analysis(data)
        self.assertEqual(2, len(result['characters'][0]['wardrobe']))
        self.assertTrue(all(key.startswith('look_') for key in result['characters'][0]['wardrobe']))

    def test_excess_scene_outfits_are_compacted_to_twelve(self):
        wardrobe = {f'look_{index}': f'blue travel outfit version {index}' for index in range(13)}
        scenes = [{'characters': ['Anna'], 'wardrobe': {'Anna': f'look_{index}'}}
                  for index in range(13)]
        data = {'characters': [{'name': 'Anna', 'wardrobe': wardrobe}], 'scenes': scenes}
        result = normalize_analysis(data)
        self.assertEqual(12, len(result['characters'][0]['wardrobe']))
        self.assertTrue(all(scene['wardrobe']['Anna'] in result['characters'][0]['wardrobe']
                            for scene in result['scenes']))

    def test_requested_scene_count_is_constrained_by_schema(self):
        scenes = analysis_schema_for_segments(4, 3)['properties']['scenes']
        self.assertEqual((scenes['minItems'], scenes['maxItems']), (3, 3))

    def test_long_story_chunks_and_scene_allocation_are_bounded(self):
        segments = ['x' * 8000 + '.' for _ in range(8)]
        chunks = chunk_segments(segments)
        counts = allocate_scene_counts(chunks, 20)
        self.assertTrue(all(sum(len(value) for value in chunk) <= 24003 for chunk in chunks))
        self.assertEqual(sum(counts), 20)
        self.assertTrue(all(value >= 1 for value in counts))

    def test_large_storyboard_analysis_stays_within_local_model_scene_limit(self):
        chunks = [[f'Beat {index}.' for index in range(80)]]
        jobs = subdivide_analysis_jobs(chunks, [240])
        self.assertEqual(240, sum(count for _, count in jobs))
        self.assertTrue(all(count <= 16 for _, count in jobs))
        self.assertTrue(all(part for part, _ in jobs))

    def test_compact_analysis_expands_to_ordered_video_shots(self):
        result = analysis(2, characters=False)
        result['scenes'][0]['source_quote'] = 'Event 0.'
        result['scenes'][1]['source_quote'] = 'Event 1.'
        expanded = expand_storyboard_scenes(result, 240)
        self.assertEqual(240, len(expanded['scenes']))
        quotes = [scene['source_quote'] for scene in expanded['scenes']]
        self.assertEqual(quotes, sorted(quotes))
        self.assertIn('Close meaningful detail', [scene['shot'] for scene in expanded['scenes']])

    def test_validation_supports_storyboards_above_one_hundred_scenes(self):
        story = ' '.join(f'Event {index}.' for index in range(101))
        data = analysis(101, characters=False)
        self.assertEqual(MAX_SCENES, 2000)
        self.assertIs(validate_analysis(data, story), data)

    def test_chunk_merge_resolves_wardrobe_key_collision(self):
        base = {'world': 'World', 'characters': [{'name': 'Mira', 'description': 'Woman',
                'wardrobe': {'travel': 'Blue coat'}}], 'scenes': [
                {'beat': 'First', 'source_quote': 'First.', 'characters': ['Mira'],
                 'wardrobe': {'Mira': 'travel'}, 'location': '', 'time': '', 'shot': '',
                 'continuity_before': '', 'continuity_after': ''}]}
        later = copy.deepcopy(base)
        later['characters'][0]['wardrobe'] = {'travel': 'Red coat'}
        later['scenes'][0]['source_quote'] = 'Second.'
        merged = LlamaCppProvider._merge_analyses([base, later], 'First. Second.')
        self.assertEqual(merged['characters'][0]['wardrobe'],
                         {'travel': 'Blue coat', 'travel_2': 'Red coat'})
        self.assertEqual(merged['scenes'][1]['wardrobe']['Mira'], 'travel_2')

    def test_chunk_merge_keeps_locations_referenced_by_scenes(self):
        base = {'world': 'World', 'characters': [],
                'locations': [{'name': 'Hospital room',
                               'description': 'Quiet room with a single bed and a window.'}],
                'scenes': [{'beat': 'The room is quiet', 'source_quote': 'First.',
                            'characters': [], 'wardrobe': {}, 'location': 'Hospital room',
                            'location_ref': 'Hospital room', 'time': 'Night', 'shot': 'Wide',
                            'continuity_before': '', 'continuity_after': ''}]}
        later = copy.deepcopy(base)
        later['scenes'][0]['source_quote'] = 'Second.'
        merged = LlamaCppProvider._merge_analyses([base, later], 'First. Second.')
        self.assertEqual(['Hospital room'], [item['name'] for item in merged['locations']])
        self.assertEqual(['Hospital room', 'Hospital room'],
                         [scene['location_ref'] for scene in merged['scenes']])

    def test_chunk_merge_deduplicates_character_and_location_variations(self):
        base = {'world': 'World',
                'characters': [{'name': 'Commander', 'description': 'Adult pilot in his 40s.', 'wardrobe': {}},
                               {'name': 'Co-pilot', 'description': 'Adult co-pilot in his 30s.', 'wardrobe': {}}],
                'locations': [{'name': 'Cockpit', 'description': 'Flight deck of the plane.'}],
                'scenes': [{'beat': 'Commander checks radar', 'source_quote': 'First.',
                            'characters': ['Commander', 'Co-pilot'], 'wardrobe': {},
                            'location': 'Cockpit', 'location_ref': 'Cockpit',
                            'time': 'Night', 'shot': 'Medium', 'continuity_before': '', 'continuity_after': ''}]}
        later = {'world': 'World',
                 'characters': [{'name': 'Commander Alexei', 'description': 'Adult pilot Alexei.', 'wardrobe': {}}],
                 'locations': [{'name': 'Airplane cockpit', 'description': 'Cockpit of the airplane.'}],
                 'scenes': [{'beat': 'Commander Alexei spots lights', 'source_quote': 'Second.',
                             'characters': ['Commander Alexei'], 'wardrobe': {},
                             'location': 'Airplane cockpit', 'location_ref': 'Airplane cockpit',
                             'time': 'Night', 'shot': 'Close', 'continuity_before': '', 'continuity_after': ''}]}
        merged = LlamaCppProvider._merge_analyses([base, later], 'First. Second.')
        self.assertEqual(['Commander', 'Co-pilot'], [c['name'] for c in merged['characters']])
        self.assertEqual(['Cockpit'], [loc['name'] for loc in merged['locations']])
        self.assertEqual(['Commander', 'Co-pilot'], merged['scenes'][0]['characters'])
        self.assertEqual(['Commander'], merged['scenes'][1]['characters'])
        self.assertEqual('Cockpit', merged['scenes'][0]['location_ref'])
        self.assertEqual('Cockpit', merged['scenes'][1]['location_ref'])
        self.assertEqual('Cockpit', merged['scenes'][1]['location'])

    def test_normalize_analysis_deduplicates_alias_characters_and_locations(self):
        data = {
            'world': 'Aviation mystery',
            'characters': [
                {'name': 'Commander', 'description': 'Pilot', 'wardrobe': {}},
                {'name': 'Commander Alexei', 'description': 'Detailed description of Commander Alexei', 'wardrobe': {}},
                {'name': 'Co-pilot', 'description': 'First officer', 'wardrobe': {}}
            ],
            'locations': [
                {'name': 'Cockpit', 'description': 'Flight deck'},
                {'name': 'Airplane cockpit', 'description': 'Enclosed cockpit with dials'}
            ],
            'scenes': [
                {'beat': 'First', 'source_quote': 'First.', 'characters': ['Commander Alexei', 'Co-pilot'],
                 'location': 'Airplane cockpit', 'location_ref': 'Airplane cockpit', 'wardrobe': {}}
            ]
        }
        result = normalize_analysis(data)
        self.assertEqual(['Commander', 'Co-pilot'], [c['name'] for c in result['characters']])
        self.assertEqual(['Cockpit'], [l['name'] for l in result['locations']])
        self.assertEqual(['Commander', 'Co-pilot'], result['scenes'][0]['characters'])
        self.assertEqual('Cockpit', result['scenes'][0]['location_ref'])
        self.assertEqual('Cockpit', result['scenes'][0]['location'])

    def test_illustration_mode_removes_reference_subjects(self):
        provider = LlamaCppProvider('http://127.0.0.1:1')
        provider._analyse_segments = lambda project, segments, target: analysis(1)
        result = provider.analyse({'story': 'Event 0.', 'style': 'painting', 'idea': '',
                                   'mode': 'illustration'}, 1)
        self.assertEqual([], result['characters'])
        self.assertEqual([], result['scenes'][0]['characters'])
        self.assertEqual({}, result['scenes'][0]['wardrobe'])

    def test_long_story_frame_budget_samples_through_the_ending(self):
        provider = LlamaCppProvider('http://127.0.0.1:1')
        captured = []

        def analyse_intro(_project, segments, target):
            captured.extend(segments)
            result = analysis(target, characters=False)
            for index, scene in enumerate(result['scenes']):
                scene['source_quote'] = segments[index]
            return result

        provider._analyse_segments = analyse_intro
        story = ' '.join(f'Event {index}.' for index in range(30))
        result = provider.analyse({'story': story, 'style': 'painting', 'idea': '',
                                   'mode': 'characters', 'analysis_evidence_budget': 20}, 20)
        self.assertEqual(20, len(result['scenes']))
        self.assertEqual('Event 0.', captured[0])
        self.assertEqual('Event 29.', captured[-1])
        self.assertEqual(20, len(captured))
        self.assertEqual(captured, [scene['source_quote'] for scene in result['scenes']])

    def test_object_mode_is_sent_to_the_analyser(self):
        class Capture:
            headers = {}
            def json(inner, path, payload):
                inner.payload = payload
                content = {'world': 'City', 'characters': [], 'scenes': [{
                    'beat': 'A landmark appears', 'source_id': 0, 'characters': [], 'wardrobe': {},
                    'location': 'City', 'time': 'Day', 'shot': 'Wide',
                    'continuity_before': '', 'continuity_after': ''}]}
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]}
        provider = LlamaCppProvider('http://127.0.0.1:1')
        provider.http = Capture()
        provider._analyse_segments({'story': 'A landmark appears.', 'style': 'realism',
                                    'idea': '', 'mode': 'objects'}, ['A landmark appears.'], 1)
        prompt = provider.http.payload['messages'][0]['content']
        self.assertIn('Object mode', prompt)
        self.assertIn('landmarks, vehicles or architectural structures', prompt)
        self.assertEqual(1800, provider.http.payload['max_tokens'])
        self.assertEqual({'enable_thinking': False}, provider.http.payload['chat_template_kwargs'])
        schema = provider.http.payload['response_format']['json_schema']['schema']
        self.assertEqual(600, schema['properties']['world']['maxLength'])
        self.assertEqual(0, schema['properties']['characters']['items']['properties']['wardrobe']['maxProperties'])
        self.assertFalse(schema['properties']['characters']['items']['properties']['wardrobe']['additionalProperties'])
        self.assertFalse(schema['properties']['scenes']['items']['properties']['wardrobe']['additionalProperties'])
        self.assertEqual(700, schema['properties']['scenes']['items']['properties']['beat']['maxLength'])
        self.assertIn('emotion', schema['properties']['scenes']['items']['required'])


class PromptSafetyTests(unittest.TestCase):
    def scene(self, **changes):
        data = {'beat': 'A quiet coast is shown at sunrise.', 'source_quote': 'A quiet coast is shown at sunrise.',
                'characters': [], 'wardrobe': {}, 'location': 'Rocky coast', 'time': 'Sunrise',
                'shot': 'Wide landscape', 'continuity_before': '', 'continuity_after': ''}
        data.update(changes)
        return data

    def test_comic_illustration_forbids_people_and_dialogue(self):
        project = {'style': 'Cinematic graphic novel comic art', 'world': 'Ancient coast', 'mode': 'illustration'}
        prompt, references = compile_prompt(project, self.scene(manual_prompt='Show the sea and lighthouse.'), [])
        self.assertEqual([], references)
        self.assertIn('UNINHABITED SCENE', prompt)
        self.assertIn('empty of inhabitants and observers', prompt)
        self.assertIn('no speech bubbles', prompt)
        self.assertIn('dialogue balloons', prompt)
        self.assertIn('readable words', prompt)
        self.assertIn('preserving the uninhabited composition', prompt)

    def test_empty_character_scene_forbids_undescribed_people_and_extra_limbs(self):
        project = {'style': 'Cinematic photorealism', 'world': 'Modern city', 'mode': 'characters'}
        prompt, references = compile_prompt(project, self.scene(), [])
        self.assertEqual([], references)
        self.assertIn('UNINHABITED SCENE', prompt)
        self.assertIn('empty of inhabitants and observers', prompt)
        self.assertNotIn('third arms', prompt)
        self.assertNotIn('human faces', prompt)

    def test_source_dialogue_is_never_sent_to_the_image_model(self):
        project = {'style': 'Cinematic photorealism', 'world': 'Modern house', 'mode': 'characters'}
        prompt, _ = compile_prompt(project, self.scene(
            source_quote='A message says: «Meet me at this address».\n— You promised to work late.',
            beat='The woman confronts her husband without showing written words.'), [])
        self.assertIn('VISUAL STORYTELLING ONLY', prompt)
        self.assertIn('SINGLE FULL-FRAME IMAGE ONLY', prompt)
        self.assertIn('split screen', prompt)
        self.assertIn('timestamps', prompt)
        self.assertNotIn('Meet me at this address', prompt)
        self.assertNotIn('You promised to work late', prompt)

    def test_prompt_keeps_non_dialogue_source_evidence_for_repaired_shots(self):
        project = {'style': 'Cinematic photorealism', 'world': 'Old house', 'mode': 'characters'}
        prompt, _ = compile_prompt(project, self.scene(
            source_quote='The woman opens a sealed envelope in her late husband\'s wardrobe.',
            beat='Visually depict this exact story event.'), [])
        self.assertIn('Story evidence for this image', prompt)
        self.assertIn('The woman opens a sealed envelope', prompt)

    def test_people_scene_gets_explicit_emotion_without_sad_default(self):
        project = {'style': 'Cinematic photorealism', 'world': 'Modern house', 'mode': 'characters'}
        subject = {'id': 'woman-1', 'name': 'Anna', 'description': 'Adult woman with short dark hair',
                   'locked': True, 'references_json': json.dumps({'portrait': 'asset-1'}),
                   'wardrobe': '{}', 'revision': 1}
        prompt, _ = compile_prompt(project, self.scene(characters=['Анна'], emotion='relieved and hopeful'), [subject])
        self.assertIn('Visible emotion: relieved and hopeful', prompt)
        self.assertIn('Do not default to sadness', prompt)

    def test_object_reference_is_not_described_as_a_person(self):
        project = {'style': 'Cinematic photorealism', 'world': 'Harbour', 'mode': 'objects'}
        subject = {'id': 'ship-1', 'name': 'Ferry', 'description': 'A red steel ferry with a white bridge',
                   'locked': True, 'references_json': json.dumps({'portrait': 'asset-1'}),
                   'wardrobe': '{}', 'revision': 3}
        prompt, references = compile_prompt(project, self.scene(), [subject])
        self.assertEqual('portrait', references[0]['role'])
        self.assertIn("Preserve this object's defining form", prompt)
        self.assertIn('do not turn it into a living figure', prompt)
        self.assertIn('UNINHABITED SCENE', prompt)
        self.assertNotIn('same facial structure', prompt)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.service.save_settings({'unet': 'flux-2-klein-4b-Q4.gguf', 'clip': 'qwen3-4b.gguf', 'vae': 'flux2-klein-vae.safetensors'})
        self.project = self.service.create_project({'title': 'Test', 'story': ' '.join(f'Event {i}.' for i in range(20)),
                                                    'style': 'Painting', 'idea': 'Space'})

    def import_story(self, count=2, characters=True):
        return self.service.import_analysis(self.project['id'], analysis(count, characters), 1)

    def complete(self, generation_id):
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        profile = json.loads(generation['snapshot'])['profile']
        worker = Worker(self.service)
        if generation['purpose'] == 'casting':
            worker._detected_face_count = lambda _image: 1
        worker.save_image(generation, png(profile['width'], profile['height']))
        return self.service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))

    def lock_characters(self, project):
        for character in project['characters']:
            ids = self.service.cast(character['id'], 1)['generation_ids']
            generation = self.complete(ids[0])
            self.service.lock(character['id'], {'portrait': generation['asset_id']}, character['revision'],
                              {'one_person': True, 'gender': True, 'age': True})

    def test_analysis_rejects_invented_source(self):
        data = analysis()
        data['scenes'][0]['source_quote'] = 'This is not in story'
        with self.assertRaises(DomainError):
            self.service.import_analysis(self.project['id'], data, 1)
        self.assertEqual([], self.service.detail(self.project['id'])['scenes'])

    def test_analysis_rejects_unknown_cast_and_wardrobe(self):
        for field, value in [('characters', ['Stranger']), ('wardrobe', {'Ada': 'unknown'})]:
            data = analysis()
            data['scenes'][0][field] = value
            with self.assertRaises(DomainError):
                validate_analysis(data, self.project['story'])

    def test_casting_lock_required(self):
        project = self.import_story()
        with self.assertRaises(DomainError):
            self.service.batch(project['id'], 'first')
        self.lock_characters(project)
        batch = self.service.batch(project['id'], 'first')
        self.assertEqual(batch['state'], 'running')

    def test_foreign_identity_reference_rejected(self):
        project = self.import_story()
        a, b = project['characters']
        generation = self.complete(self.service.cast(a['id'], 1)['generation_ids'][0])
        with self.assertRaises(DomainError):
            self.service.lock(b['id'], {'portrait': generation['asset_id']}, 1,
                              {'one_person': True, 'gender': True, 'age': True})

    def test_human_portrait_locks_immediately_after_selection(self):
        project = self.import_story()
        character = project['characters'][0]
        generation = self.complete(self.service.cast(character['id'], 1)['generation_ids'][0])
        locked = self.service.lock(character['id'], {'portrait': generation['asset_id']}, character['revision'])
        self.assertTrue(locked['locked'])

    def test_locked_identity_is_immutable(self):
        project = self.import_story()
        self.lock_characters(project)
        with self.assertRaises(DomainError):
            self.service.edit_character(project['characters'][0]['id'], 'New face', 1)

    def test_recurring_location_is_cast_locked_and_bound_to_its_scene(self):
        data = analysis(1)
        data['locations'] = [{'name': 'Anna house exterior',
                              'description': 'Two-storey brick house, blue front door, three windows on the upper floor.'}]
        data['scenes'][0]['location_ref'] = 'Anna house exterior'
        project = self.service.import_analysis(self.project['id'], data, 1)
        location = project['locations'][0]
        generation_id = self.service.cast_location(location['id'], 1)['generation_ids'][0]
        generation = self.complete(generation_id)
        self.service.lock_location(location['id'], {'environment': generation['asset_id']}, location['revision'])
        self.lock_characters(project)
        snapshot = self.service.scene_snapshot(self.service.scene(project['scenes'][0]['id']))
        self.assertEqual('environment', snapshot['references'][0]['role'])
        self.assertIn('Keep the architectural footprint, facade, door and window positions', snapshot['prompt'])

    def test_narrative_location_reference_does_not_render_its_people(self):
        project = self.service.project(self.project['id'])
        project['world'] = 'A story about an elderly woman and a notary.'
        snapshot = self.service.location_snapshot(project, {'name': 'Notary Office',
            'description': "Where Anna meets with a notary to learn about her husband's wishes."})
        self.assertEqual(12, snapshot['template_version'])
        self.assertNotIn('elderly woman', snapshot['prompt'])
        self.assertNotIn('Where Anna meets', snapshot['prompt'])
        self.assertIn('Ignore the narrative event', snapshot['prompt'])
        living = self.service.location_snapshot(project, {'name': 'Living room',
            'description': 'A dim room where the woman reads a letter.'})
        self.assertNotIn('where the woman reads', living['prompt'])
        self.assertEqual(12, living['template_version'])

    def test_default_cast_creates_two_variants(self):
        project = self.import_story(1)
        result = self.service.cast(project['characters'][0]['id'])
        self.assertEqual(2, len(result['generation_ids']))
        snapshot = json.loads(self.service.db.one('SELECT snapshot FROM generations WHERE id=?',
                                                  (result['generation_ids'][0],))['snapshot'])
        self.assertEqual((768, 432), (snapshot['profile']['width'], snapshot['profile']['height']))
        self.assertIn('Exactly one person', snapshot['prompt'])

    def test_casting_prompt_makes_explicit_age_non_negotiable(self):
        data = analysis(1)
        data['world'] = 'A story about a woman in her 70s and her adult daughter.'
        project = self.service.import_analysis(self.project['id'], data, 1)
        character = project['characters'][0]
        self.service.edit_character(character['id'], 'A woman in her 30s with dark hair.', character['revision'])
        changed = self.service.character(character['id'])
        snapshot = self.service.casting_snapshot(self.service.project(changed['project_id']), changed)
        self.assertEqual({'minimum': 30, 'maximum': 39, 'label': '30–39 years old'}, snapshot['age_constraint'])
        self.assertEqual({'value': 'female', 'label': 'woman'}, snapshot['gender_constraint'])
        self.assertIn('STRICT AGE REQUIREMENT', snapshot['prompt'])
        self.assertIn('STRICT GENDER REQUIREMENT', snapshot['prompt'])
        self.assertIn('older than 39', snapshot['prompt'])
        self.assertIn('AGE APPEARANCE GUARD', snapshot['prompt'])
        self.assertIn('person in their thirties', snapshot['prompt'])
        self.assertNotIn('woman in her 70s', snapshot['prompt'])

    def test_child_casting_uses_child_guard_not_thirties_guard(self):
        project = self.import_story(1)
        character = project['characters'][0]
        self.service.edit_character(character['id'], 'A girl, 8 years old, with dark hair.', character['revision'])
        changed = self.service.character(character['id'])
        snapshot = self.service.casting_snapshot(self.service.project(changed['project_id']), changed)
        self.assertEqual({'minimum': 8, 'maximum': 8, 'label': '8 years old'}, snapshot['age_constraint'])
        self.assertEqual({'value': 'female', 'label': 'girl'}, snapshot['gender_constraint'])
        self.assertIn('child or teenager matching the stated age range', snapshot['prompt'])
        self.assertIn('unmistakable girl', snapshot['prompt'])
        self.assertNotIn('person in their thirties', snapshot['prompt'])

    def test_completed_analysis_waits_for_explicit_reference_selection(self):
        project = self.service.project(self.project['id'])
        with self.service.db.transaction() as connection:
            generation_id = self.service.insert_generation(connection, project['id'], 'analysis',
                {'profile': self.service.profile(), 'project': project, 'target': 2})
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        Worker(self.service)._save_analysis(generation, analysis(2), project['version'])
        detail = self.service.detail(project['id'])
        casts = [item for item in detail['generations'] if item['purpose'] == 'casting']
        self.assertEqual([], casts)
        event = self.service.db.one(
            "SELECT data FROM events WHERE project_id=? AND kind='analysis_saved' ORDER BY rowid DESC LIMIT 1",
            (project['id'],),
        )
        self.assertEqual(0, json.loads(event['data'])['auto_casts'])

    def test_stale_cast_cannot_be_locked(self):
        project = self.import_story()
        character = project['characters'][0]
        generation = self.complete(self.service.cast(character['id'], 1)['generation_ids'][0])
        self.service.edit_character(character['id'], 'Changed face', 1)
        with self.assertRaises(DomainError):
            self.service.lock(character['id'], {'portrait': generation['asset_id']}, 2,
                              {'one_person': True, 'gender': True, 'age': True})

    def test_twenty_scenes_no_characters_export_order_and_metadata(self):
        project = self.import_story(20, False)
        batch = self.service.batch(project['id'], 'all')
        generations = self.service.db.all('SELECT * FROM generations WHERE batch_id=? ORDER BY rowid', (batch['id'],))
        self.assertEqual(20, len(generations))
        for generation in reversed(generations):
            self.complete(generation['id'])
            self.service.select_generation(generation['id'])
        payload = self.service.export(project['id'])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(list(range(1, 21)), [s['scene']['position'] for s in manifest['scenes']])
            with Image.open(io.BytesIO(archive.read('contact_sheet.png'))) as sheet:
                self.assertEqual((1280, 1050), sheet.size)
            for scene in manifest['scenes']:
                with Image.open(io.BytesIO(archive.read(scene['file']))) as im:
                    self.assertEqual((1024, 576), im.size)
                self.assertIn('metadata/' + scene['generation']['id'] + '.json', archive.namelist())
                with Image.open(io.BytesIO(archive.read(scene['thumbnail']))) as thumb:
                    self.assertEqual((320, 180), thumb.size)

    def test_idempotency_and_changed_request_conflict(self):
        project = self.import_story(characters=False)
        first = self.service.batch(project['id'], 'same')
        second = self.service.batch(project['id'], 'same')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(2, len(self.service.detail(project['id'])['generations']))
        with self.assertRaises(DomainError):
            self.service.batch(project['id'], 'same', [project['scenes'][0]['id']])

    def test_scene_edit_preserves_other_results_and_old_snapshot(self):
        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'all')
        generations = self.service.detail(project['id'])['generations']
        for generation in generations:
            self.complete(generation['id'])
            self.service.select_generation(generation['id'])
        target = project['scenes'][0]
        self.service.edit_scene(target['id'], {'manual_prompt': 'A new frame'}, 1)
        changed = self.service.detail(project['id'])
        self.assertTrue(changed['scenes'][0]['stale'])
        self.assertFalse(changed['scenes'][1]['stale'])
        self.assertEqual(generations[1]['id'], changed['scenes'][1]['active_generation_id'])
        self.assertNotEqual('A new frame', changed['generations'][0]['snapshot']['prompt'])
        with self.assertRaises(DomainError):
            self.service.select_generation(generations[0]['id'])
        with self.assertRaises(DomainError):
            self.service.export(project['id'])
        batch = self.service.batch(project['id'], 'single', [target['id']])
        new = self.service.db.all('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        self.assertEqual(1, len(new))
        self.assertTrue(json.loads(new[0]['snapshot'])['prompt'].endswith(
            'Additional visual direction, while preserving the uninhabited composition: A new frame'))

    def test_scene_revision_conflict(self):
        project = self.import_story(characters=False)
        scene = project['scenes'][0]
        self.service.edit_scene(scene['id'], {'manual_prompt': 'Updated'}, 1)
        with self.assertRaises(DomainError):
            self.service.edit_scene(scene['id'], {'manual_prompt': 'Lost update'}, 1)

    def test_cancel_queued_jobs_and_pause(self):
        project = self.import_story(characters=False)
        batch = self.service.batch(project['id'], 'all')
        self.service.control_batch(batch['id'], 'pause')
        Worker(self.service).tick()
        self.assertTrue(all(g['state'] == 'queued' for g in self.service.detail(project['id'])['generations']))
        self.service.control_batch(batch['id'], 'cancel')
        self.assertTrue(all(g['state'] == 'cancelled' for g in self.service.detail(project['id'])['generations']))

    def test_resume_refreshes_only_legacy_queued_scene_snapshots(self):
        project = self.import_story(characters=False)
        scene = self.service.scene(project['scenes'][0]['id'])
        scene['data']['source_quote'] = 'A woman enters the room.'
        with self.service.db.transaction() as connection:
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene['id'], 2, dump(scene['data']), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (2, scene['id']))
        batch = self.service.batch(project['id'], 'legacy-resume', [scene['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        legacy = json.loads(generation['snapshot'])
        legacy['template_version'] = 3
        legacy['uninhabited'] = True
        with self.service.db.transaction() as connection:
            connection.execute('UPDATE generations SET snapshot=? WHERE id=?', (dump(legacy), generation['id']))
        self.service.control_batch(batch['id'], 'pause')
        self.service.control_batch(batch['id'], 'resume')
        resumed = self.service.db.one('SELECT snapshot FROM generations WHERE id=?', (generation['id'],))
        snapshot = json.loads(resumed['snapshot'])
        self.assertEqual(8, snapshot['template_version'])
        self.assertFalse(snapshot['uninhabited'])

    def test_legacy_face_detailer_is_disabled_before_gpu_submission(self):
        class FakeRuntime:
            def ensure_comfy(self, _settings):
                return None
        class FakeComfy:
            graph = None
            def __init__(self, _url):
                pass
            def health(self):
                return {'ok': True}
            def validate(self, graph):
                type(self).graph = graph
            def submit(self, _graph, _client_id):
                return 'provider-job-1'

        project = self.import_story(characters=False)
        batch = self.service.batch(project['id'], 'legacy-detail', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        snapshot = json.loads(generation['snapshot'])
        snapshot['detail_faces'] = True
        snapshot['template_version'] = 1
        with self.service.db.transaction() as connection:
            connection.execute('UPDATE generations SET snapshot=? WHERE id=?', (dump(snapshot), generation['id']))
        worker = Worker(self.service, provider_factory=FakeComfy, runtime=FakeRuntime())
        worker._assert_gpu_safe = lambda: None
        worker.tick()
        stored = json.loads(self.service.db.one('SELECT snapshot FROM generations WHERE id=?', (generation['id'],))['snapshot'])
        self.assertFalse(stored['detail_faces'])
        self.assertNotIn('FaceDetailer', [item['class_type'] for item in FakeComfy.graph.values()])

    def test_gpu_safety_alarm_pauses_the_batch_before_submission(self):
        class FakeRuntime:
            def ensure_comfy(self, _settings):
                raise AssertionError('GPU backend must not start when the thermal gate is closed')
        project = self.import_story(count=2, characters=False)
        batch = self.service.batch(project['id'], 'thermal-gate')
        generation = self.service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        worker = Worker(self.service, runtime=FakeRuntime())
        worker._assert_gpu_safe = lambda: (_ for _ in ()).throw(ResourceSafetyError('GPU too hot'))
        worker.tick()
        self.assertEqual('resource_degraded', self.service.db.one('SELECT state FROM generations WHERE id=?', (generation['id'],))['state'])
        self.assertEqual('paused', self.service.db.one('SELECT state FROM batches WHERE id=?', (batch['id'],))['state'])

    def test_image_generation_reports_progress_through_submission(self):
        class FakeComfy:
            def __init__(self, _url):
                pass
            def health(self):
                return {'ok': True}
            def validate(self, _graph):
                return None
            def submit(self, _graph, _client_id):
                return 'provider-job-1'

        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'progress')
        Worker(self.service, provider_factory=FakeComfy).tick()
        generation = self.service.detail(project['id'])['generations'][0]
        self.assertEqual('running', generation['state'])
        self.assertEqual('В очереди ComfyUI', generation['progress']['stage'])
        self.assertEqual(32, generation['progress']['percent'])

    def test_unexpected_shutdown_pauses_queue_and_never_retries_active_frame(self):
        project = self.import_story(characters=False)
        batch = self.service.batch(project['id'], 'interrupted')
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (project['scenes'][0]['id'],))
        Worker(self.service).update(generation, state='running', progress={'stage': 'Генерация', 'percent': 54})
        Worker(self.service).recover_after_unexpected_shutdown()
        recovered = self.service.detail(project['id'])['generations'][0]
        self.assertEqual('resource_degraded', recovered['state'])
        self.assertIn('неожиданным выключением', recovered['error'])
        self.assertEqual('Прервано аварийным выключением', recovered['progress']['stage'])
        self.assertEqual('paused', self.service.db.one('SELECT state FROM batches WHERE id=?', (batch['id'],))['state'])

    def test_uninhabited_scene_with_detected_face_is_rejected_before_saving(self):
        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'face-check', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (project['scenes'][0]['id'],))
        assets_before = len(self.service.db.all('SELECT * FROM assets WHERE project_id=?', (project['id'],)))
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 1
        worker._detected_person_count = lambda _image: 0
        with self.assertRaisesRegex(DomainError, 'найден(о|а)? (лицо|человек)'):
            worker.save_image(generation, png())
        self.assertEqual([], self.service.db.all('SELECT * FROM assets'))

    def test_content_rejection_keeps_retrying_with_a_fresh_seed(self):
        class AcceptedByProvider:
            def __init__(self, _url):
                pass
            def status(self, _prompt_id):
                return {'state': 'succeeded', 'image': 'image-id'}
            def fetch(self, _image_id):
                return png()

        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'keep-retrying', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (project['scenes'][0]['id'],))
        Worker(self.service).update(generation, state='running', provider_id='prompt-id', attempts=2)
        worker = Worker(self.service, provider_factory=AcceptedByProvider)
        worker.save_image = lambda *_args: (_ for _ in ()).throw(ContentRejectedError('Rejected composition'))
        worker.monitor(self.service.db.one('SELECT * FROM generations WHERE id=?', (generation['id'],)))
        retried = self.service.db.one('SELECT * FROM generations WHERE id=?', (generation['id'],))
        self.assertEqual('queued', retried['state'])
        self.assertIsNone(retried['error'])
        self.assertIn('вариант №3', json.loads(retried['progress'])['stage'])

    def test_casting_reference_rejects_extra_people(self):
        project = self.import_story(1)
        generation_id = self.service.cast(project['characters'][0]['id'], 1)['generation_ids'][0]
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        profile = json.loads(generation['snapshot'])['profile']
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 2
        with self.assertRaisesRegex(DomainError, 'ровно одно лицо'):
            worker.save_image(generation, png(profile['width'], profile['height']))
        self.assertEqual([], self.service.db.all('SELECT * FROM assets'))

    def test_extra_whole_person_is_rejected_even_when_faces_are_hidden(self):
        project = self.import_story(1)
        self.lock_characters(project)
        batch = self.service.batch(project['id'], 'extra-silhouette', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 1
        worker._detected_person_count = lambda _image: 3
        worker._detected_hand_count = lambda _image: 0
        with self.assertRaisesRegex(DomainError, 'Количество людей'):
            worker.save_image(generation, png())

    def test_visible_people_contract_includes_anonymous_source_character(self):
        project = self.import_story(1)
        self.lock_characters(project)
        scene = self.service.scene(project['scenes'][0]['id'])
        scene['data'].update({'characters': ['Ada'], 'visible_people': 2,
                              'source_quote': 'Ada speaks with a stranger.', 'beat': 'Ada speaks with a stranger.'})
        with self.service.db.transaction() as connection:
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene['id'], 2, dump(scene['data']), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (2, scene['id']))
        snapshot = self.service.scene_snapshot(self.service.scene(scene['id']))
        self.assertEqual(2, snapshot['expected_people'])
        self.assertIn('Exactly 2 visible human subject(s) total', snapshot['prompt'])
        self.assertIn('exactly 1 anonymous supporting person(s)', snapshot['prompt'])

    def test_unlinked_human_scene_is_not_mistaken_for_an_empty_scene(self):
        project = self.import_story(characters=False)
        scene = self.service.scene(project['scenes'][0]['id'])
        scene['data']['source_quote'] = 'A woman walks into the room.'
        with self.service.db.transaction() as connection:
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene['id'], 2, dump(scene['data']), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (2, scene['id']))
        snapshot = self.service.scene_snapshot(self.service.scene(scene['id']))
        self.assertFalse(snapshot['uninhabited'])
        self.assertEqual(1, snapshot['expected_people'])
        self.assertNotIn('UNINHABITED SCENE', snapshot['prompt'])
        self.assertIn('no fixed identity reference is available', snapshot['prompt'])
        self.service.batch(project['id'], 'human-not-empty', [scene['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (scene['id'],))
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 1
        worker._detected_person_count = lambda _image: 0
        worker._detected_hand_count = lambda _image: 0
        worker.save_image(generation, png())
        self.assertEqual('succeeded', self.service.db.one('SELECT state FROM generations WHERE id=?', (generation['id'],))['state'])

    def test_contact_sheet_grid_is_rejected_before_saving(self):
        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'panel-check', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (project['scenes'][0]['id'],))
        with self.assertRaisesRegex(DomainError, 'сетк[аи] из нескольких панелей'):
            Worker(self.service).save_image(generation, panel_png())
        self.assertEqual([], self.service.db.all('SELECT * FROM assets'))

    def test_flow_upload_accepts_matching_character_face(self):
        project = self.import_story(1)
        self.lock_characters(project)
        scene_id = project['scenes'][0]['id']
        assets_before = len(self.service.db.all('SELECT * FROM assets WHERE project_id=?', (project['id'],)))
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 1
        worker._detected_person_count = lambda _image: 1
        worker._detected_hand_count = lambda _image: 0
        worker._identity_similarity = lambda _image, _ref: 0.85
        worker._identity_assignment_similarity = lambda _image, _refs: 0.85
        worker._identity_face_too_small = lambda _image, _exp=1: False
        candidate = io.BytesIO()
        Image.new('RGB', (1024, 576), '#31597a').save(candidate, format='PNG')
        self.service.upload_scene_image(
            scene_id, candidate.getvalue(), image_validator=worker.validate_external_scene_image
        )
        self.assertEqual(assets_before + 1, len(self.service.db.all('SELECT * FROM assets WHERE project_id=?', (project['id'],))))

    def test_flow_upload_rejects_mismatched_face(self):
        project = self.import_story(1)
        self.lock_characters(project)
        scene_id = project['scenes'][0]['id']
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 1
        worker._detected_person_count = lambda _image: 1
        worker._detected_hand_count = lambda _image: 0
        worker._identity_similarity = lambda _image, _ref: 0.12
        worker._identity_assignment_similarity = lambda _image, _refs: 0.12
        worker._identity_face_too_small = lambda _image, _exp=1: False
        candidate = io.BytesIO()
        Image.new('RGB', (1024, 576), '#31597a').save(candidate, format='PNG')
        with self.assertRaises(ContentRejectedError):
            self.service.upload_scene_image(
                scene_id, candidate.getvalue(), image_validator=worker.validate_external_scene_image
            )

    def test_flow_upload_rejects_even_a_two_panel_image_before_saving(self):
        project = self.import_story(characters=False)
        scene_id = project['scenes'][0]['id']
        worker = Worker(self.service)
        with self.assertRaisesRegex(DomainError, 'несколько сцен или панелей'):
            self.service.upload_scene_image(
                scene_id, split_png(), image_validator=worker.validate_external_scene_image
            )
        self.assertEqual([], self.service.db.all('SELECT * FROM assets'))

    def test_dark_frame_edges_are_not_mistaken_for_panel_dividers(self):
        image = Image.new('RGB', (1024, 576), '#526880')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 150, 575), fill='#101010')
        draw.rectangle((870, 0, 1023, 575), fill='#101010')
        self.assertFalse(Worker._has_panel_grid(image))

    def test_narrow_natural_shadow_does_not_count_as_a_panel_gutter(self):
        image = Image.new('RGB', (1024, 576), '#526880')
        draw = ImageDraw.Draw(image)
        # A dark trunk/shadow can extend through a shot, but the scenery on
        # both sides remains continuous. It is not a contact-sheet separator.
        draw.rectangle((509, 0, 514, 575), fill='#101010')
        self.assertFalse(Worker._has_panel_grid(image))

    def test_broad_natural_horizon_does_not_count_as_a_panel_gutter(self):
        image = Image.new('RGB', (1024, 576), '#526880')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 260, 1023, 340), fill='#101010')
        self.assertFalse(Worker._has_panel_grid(image))

    def test_extra_visible_faces_or_hands_are_rejected_before_saving(self):
        project = self.import_story(1)
        self.lock_characters(project)
        asset_count = len(self.service.db.all('SELECT * FROM assets'))
        self.service.batch(project['id'], 'anatomy-check', [project['scenes'][0]['id']])
        generation = self.service.db.one('SELECT * FROM generations WHERE scene_id=?', (project['scenes'][0]['id'],))
        worker = Worker(self.service, runtime=object())
        worker._detected_face_count = lambda _image: 3
        worker._detected_person_count = lambda _image: 0
        worker._detected_hand_count = lambda _image: 0
        with self.assertRaisesRegex(DomainError, 'Количество людей'):
            worker.save_image(generation, png())
        worker._detected_face_count = lambda _image: 2
        worker._detected_hand_count = lambda _image: 5
        with self.assertRaisesRegex(DomainError, 'больше рук'):
            worker.save_image(generation, png())
        self.assertEqual(asset_count, len(self.service.db.all('SELECT * FROM assets')))

    def test_bad_image_dimensions_do_not_succeed(self):
        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'all')
        generation = self.service.db.one('SELECT * FROM generations LIMIT 1')
        with self.assertRaises(DomainError):
            Worker(self.service).save_image(generation, png(512,512))
        self.assertEqual([], self.service.db.all('SELECT * FROM assets'))

    def test_cancel_race_does_not_select_image(self):
        project = self.import_story(characters=False)
        self.service.batch(project['id'], 'all')
        generation = self.service.db.one('SELECT * FROM generations LIMIT 1')
        self.service.cancel_generation(generation['id'])
        saved = self.complete(generation['id'])
        self.assertEqual('cancelled', saved['state'])
        self.assertIsNone(self.service.scene(generation['scene_id'])['active_generation_id'])

    def test_unknown_scene_and_non_loopback_settings_rejected(self):
        self.import_story(characters=False)
        with self.assertRaises(DomainError):
            self.service.batch(self.project['id'], 'bad', ['foreign-scene'])
        with self.assertRaises(DomainError):
            self.service.save_settings({'comfy_url': 'http://remote.example:8188'})

    def test_scene_clothing_uses_portrait_identity_without_clothing_reference(self):
        data = analysis(1)
        data['characters'][0]['wardrobe'] = {'space': 'White spacesuit'}
        data['scenes'][0]['wardrobe'] = {'Ada': 'space'}
        project = self.service.import_analysis(self.project['id'], data, 1)
        ada, ben = project['characters']
        portrait = self.complete(self.service.cast(ada['id'], 1)['generation_ids'][0])
        self.service.lock(ada['id'], {'portrait': portrait['asset_id']}, 1,
                          {'one_person': True, 'gender': True, 'age': True})
        ben_image = self.complete(self.service.cast(ben['id'], 1)['generation_ids'][0])
        self.service.lock(ben['id'], {'portrait': ben_image['asset_id']}, 1,
                          {'one_person': True, 'gender': True, 'age': True})
        snapshot = self.service.scene_snapshot(self.service.scene(project['scenes'][0]['id']))
        self.assertEqual(portrait['asset_id'], snapshot['references'][0]['asset_id'])
        self.assertEqual('portrait', snapshot['references'][0]['role'])
        self.assertIn('White spacesuit', snapshot['prompt'])
        self.assertIn('SINGLE FULL-FRAME IMAGE ONLY', snapshot['prompt'])
        self.assertIn('Visible emotion:', snapshot['prompt'])
        self.assertIn('no duplicate body parts', snapshot['prompt'])
        self.assertFalse(snapshot['detail_faces'])

    def test_separate_clothing_reference_generation_is_rejected(self):
        project = self.import_story(1)
        character = project['characters'][0]
        portrait = self.complete(self.service.cast(character['id'], 1)['generation_ids'][0])
        with self.assertRaises(DomainError):
            self.service.cast(character['id'], 1, portrait['asset_id'], 'unused')


class WorkflowTests(unittest.TestCase):
    def test_zero_one_two_three_references_preserve_order(self):
        profile = {'unet':'flux.gguf','clip':'clip.gguf','vae':'vae','width':1024,'height':576}
        for count in range(4):
            graph = build_workflow(profile,'prompt',123,[f'{i}.png' for i in range(count)],'prefix')
            inputs = [graph[str(20+i*4)]['inputs']['image'] for i in range(count)]
            self.assertEqual([f'{i}.png' for i in range(count)], inputs)
            self.assertEqual(4, graph['8']['inputs']['steps'])
            self.assertEqual('flux2', graph['2']['inputs']['type'])
            self.assertEqual('EmptyFlux2LatentImage', graph['5']['class_type'])
            if count:
                self.assertEqual(0.85, graph['21']['inputs']['megapixels'])

    def test_face_detailer_reuses_locked_portrait_conditioning(self):
        profile = {'unet':'flux.gguf','clip':'clip.gguf','vae':'vae','width':1024,'height':576}
        graph = build_workflow(profile, 'prompt', 123, ['portrait.png'], 'prefix', True)
        self.assertEqual('UltralyticsDetectorProvider', graph['14']['class_type'])
        self.assertEqual('bbox/face_yolov8m.pt', graph['14']['inputs']['model_name'])
        self.assertEqual(['23', 0], graph['15']['inputs']['positive'])
        self.assertEqual(['15', 0], graph['12']['inputs']['images'])
        self.assertEqual(0.22, graph['15']['inputs']['denoise'])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = Application(cls.temp.name)
        cls.server = make_server(cls.app, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self,path,data=None,headers=None):
        request=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
            headers=headers or {})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                return response.status,response.read()
        except urllib.error.HTTPError as error:
            return error.code,error.read()

    def test_static_and_session(self):
        status,body=self.request('/')
        self.assertEqual(200,status)
        self.assertIn(b'FrameForge',body)
        for path in ('/app.js', '/ui-config.mjs', '/presets/realism.png'):
            self.assertEqual(200, self.request(path)[0])
        status,body=self.request('/api/session')
        self.assertEqual(self.app.token,json.loads(body)['token'])

    def test_csrf_and_host_rejected(self):
        self.assertEqual(403,self.request('/api/projects',{})[0])
        self.assertEqual(403,self.request('/api/session',headers={'Host':'evil.example'})[0])
        self.assertEqual(403,self.request('/api/session',headers={'Origin':'http://evil.example'})[0])

    def test_create_and_read_roundtrip(self):
        status,body=self.request('/api/projects',{'title':'HTTP','story':'Real story.','style':'paint','mode':'dynamic'},
            {'X-Session-Token':self.app.token})
        self.assertEqual(200,status,body)
        project=json.loads(body)
        self.assertEqual('dynamic',project['mode'])
        self.assertEqual(200,self.request('/api/projects/'+project['id'])[0])

    def test_analysis_api_accepts_large_video_storyboard_target(self):
        status, body = self.request('/api/projects', {'title': 'Long video', 'story': 'Story.',
            'style': 'paint', 'mode': 'illustration'}, {'X-Session-Token': self.app.token})
        self.assertEqual(200, status, body)
        project = json.loads(body)
        status, body = self.request('/api/projects/' + project['id'] + '/analyse',
            {'target': MAX_SCENES}, {'X-Session-Token': self.app.token})
        self.assertEqual(200, status, body)
        generation_id = json.loads(body)['generation_id']
        snapshot = json.loads(self.app.service.db.one(
            'SELECT snapshot FROM generations WHERE id=?', (generation_id,))['snapshot'])
        self.assertEqual(MAX_SCENES, snapshot['target'])

    def test_unknown_project_mode_is_rejected(self):
        status,_=self.request('/api/projects',{'title':'HTTP','story':'Real story.',
            'style':'paint','mode':'unknown'}, {'X-Session-Token':self.app.token})
        self.assertEqual(400,status)

    def test_missing_and_traversal(self):
        self.assertEqual(404,self.request('/../pyproject.toml')[0])
        self.assertEqual(404,self.request('/api/assets/unknown')[0])


class GoogleNanoBananaTests(unittest.TestCase):
    def test_provider_requires_api_key(self):
        with self.assertRaises(DomainError):
            GoogleNanoBananaProvider('')

    def test_service_settings_accepts_google_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            updated = service.save_settings({
                'image_backend': 'google_nanobanana',
                'google_api_key': 'test_key_12345',
                'google_model': 'gemini-3.1-flash-image'
            })
            self.assertEqual('google_nanobanana', updated['image_backend'])
            self.assertEqual('test_key_12345', updated['google_api_key'])
            self.assertEqual('gemini-3.1-flash-image', updated['google_model'])

    def test_worker_generates_image_with_google_provider(self):
        import base64
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            service.save_settings({
                'image_backend': 'google_nanobanana',
                'google_api_key': 'mock_key',
                'google_model': 'gemini-3.1-flash-image'
            })
            project = service.create_project({'title': 'Test Cloud', 'story': 'Event 0.', 'style': 'Cinematic', 'mode': 'illustration'})
            service.import_analysis(project['id'], analysis(count=1, characters=False), 1)
            scenes = service.detail(project['id'])['scenes']
            batch = service.batch(project['id'], 'idem_google_1', [scenes[0]['id']])
            generation = service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
            gen_id = generation['id']

            test_png_bytes = png(1024, 576)
            mock_resp = {
                'candidates': [
                    {
                        'content': {
                            'parts': [
                                {
                                    'inlineData': {
                                        'mimeType': 'image/png',
                                        'data': base64.b64encode(test_png_bytes).decode('ascii')
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

            class MockResponse:
                def read(self):
                    return json.dumps(mock_resp).encode('utf-8')
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            worker = Worker(service)
            original_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = lambda req, timeout=60: MockResponse()
                worker.prepare(generation)
            finally:
                urllib.request.urlopen = original_urlopen

            updated_gen = service.db.one('SELECT * FROM generations WHERE id=?', (gen_id,))
            self.assertEqual('succeeded', updated_gen['state'])
            self.assertIsNotNone(updated_gen['asset_id'])
            asset = service.asset(updated_gen['asset_id'])
            self.assertTrue((service.root / asset['path']).exists())


class PollinationsTests(unittest.TestCase):
    def test_provider_generate_success(self):
        from imagegen.providers import PollinationsProvider
        provider = PollinationsProvider(model='flux-realism')
        test_png_bytes = png(1024, 576)

        class MockResponse:
            def read(self):
                return test_png_bytes
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        original_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=90: MockResponse()
            result = provider.generate('a cute cat', width=1024, height=576, seed=42)
            self.assertIsInstance(result, bytes)
            with Image.open(io.BytesIO(result)) as img:
                self.assertEqual((1024, 576), img.size)
        finally:
            urllib.request.urlopen = original_urlopen

    def test_service_pollinations_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            saved = service.save_settings({
                'image_backend': 'pollinations',
                'pollinations_model': 'flux-realism'
            })
            self.assertEqual('pollinations', saved['image_backend'])
            self.assertEqual('flux-realism', saved['pollinations_model'])
            self.assertEqual('pollinations', service.settings()['image_backend'])

    def test_worker_pollinations_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            service.save_settings({
                'image_backend': 'pollinations',
                'pollinations_model': 'flux'
            })
            project = service.create_project({'title': 'Test Pollinations', 'story': 'Event 0.', 'style': 'Cinematic', 'mode': 'illustration'})
            service.import_analysis(project['id'], analysis(count=1, characters=False), 1)
            scenes = service.detail(project['id'])['scenes']
            batch = service.batch(project['id'], 'idem_pollinations_1', [scenes[0]['id']])
            generation = service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
            gen_id = generation['id']

            test_png_bytes = png(1024, 576)

            class MockResponse:
                def read(self):
                    return test_png_bytes
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            worker = Worker(service)
            original_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = lambda req, timeout=90: MockResponse()
                worker.prepare(generation)
            finally:
                urllib.request.urlopen = original_urlopen

            updated_gen = service.db.one('SELECT * FROM generations WHERE id=?', (gen_id,))
            self.assertEqual('succeeded', updated_gen['state'])
            self.assertIsNotNone(updated_gen['asset_id'])
            asset = service.asset(updated_gen['asset_id'])
            self.assertTrue((service.root / asset['path']).exists())


class GoogleFlowIntegrationTests(unittest.TestCase):
    def test_flow_proxy_generation_waits_for_bridge_and_is_completed_by_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Service(temp_dir)
            service.save_settings({'image_backend': 'flow', 'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = service.create_project({'title': 'Flow story', 'story': ' '.join(f'Event {i}.' for i in range(5)), 'style': 'realism'})
            service.import_analysis(project['id'], analysis(1, characters=False), 1)
            scene = service.detail(project['id'])['scenes'][0]
            batch = service.batch(project['id'], 'flow-regenerate', [scene['id']])
            proxy = service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
            repeated = service.batch(project['id'], 'flow-regenerate-again', [scene['id']])
            self.assertEqual(batch['id'], repeated['id'])

            # The local worker must not claim or cancel browser-owned Flow work.
            Worker(service).tick()
            self.assertEqual('queued', service.db.one('SELECT state FROM generations WHERE id=?', (proxy['id'],))['state'])
            prompts = service.project_prompts(project['id'])
            self.assertFalse(prompts['scenes'][0]['has_image'])

            uploaded = service.upload_scene_image(scene['id'], png())
            self.assertEqual(proxy['id'], uploaded['generation_id'])
            completed = service.db.one('SELECT * FROM generations WHERE id=?', (proxy['id'],))
            self.assertEqual('succeeded', completed['state'])
            self.assertIsNotNone(completed['asset_id'])
            self.assertTrue(service.project_prompts(project['id'])['scenes'][0]['has_image'])

            Worker(service).tick()
            self.assertEqual('completed', service.db.one('SELECT state FROM batches WHERE id=?', (batch['id'],))['state'])

    def test_flow_upload_completes_duplicate_and_paused_proxy_without_sticking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Service(temp_dir)
            service.save_settings({'image_backend': 'flow', 'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = service.create_project({'title': 'Flow story', 'story': ' '.join(f'Event {i}.' for i in range(5)), 'style': 'realism'})
            service.import_analysis(project['id'], analysis(1, characters=False), 1)
            scene_id = service.detail(project['id'])['scenes'][0]['id']
            paused_batch = service.batch(project['id'], 'paused-flow', [scene_id])
            service.control_batch(paused_batch['id'], 'pause')
            # A paused attempt must not suppress a new requested regeneration.
            active_batch = service.batch(project['id'], 'active-flow', [scene_id])
            self.assertNotEqual(paused_batch['id'], active_batch['id'])
            self.assertFalse(service.project_prompts(project['id'])['scenes'][0]['has_image'])
            service.upload_scene_image(scene_id, png())
            for batch in (paused_batch, active_batch):
                proxy = service.db.one('SELECT state FROM generations WHERE batch_id=?', (batch['id'],))
                self.assertEqual('succeeded', proxy['state'])
            Worker(service).refresh_batches()
            for batch in (paused_batch, active_batch):
                self.assertEqual('completed', service.db.one('SELECT state FROM batches WHERE id=?', (batch['id'],))['state'])

    def test_upload_scene_image_updates_active_generation_and_creates_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Service(temp_dir)
            service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = service.create_project({'title': 'Story', 'story': ' '.join(f'Event {i}.' for i in range(5)), 'style': 'realism'})
            service.import_analysis(project['id'], analysis(2), 1)
            scenes = service.detail(project['id'])['scenes']
            scene = scenes[0]

            image_bytes = png(800, 450)
            res = service.upload_scene_image(scene['id'], image_bytes)
            self.assertEqual(scene['id'], res['scene_id'])

            detail = service.detail(project['id'])
            updated_scene = detail['scenes'][0]
            self.assertEqual(res['generation_id'], updated_scene['active_generation_id'])
            self.assertFalse(updated_scene['stale'])

            asset = service.asset(res['asset_id'])
            self.assertEqual(1024, asset['width'])
            self.assertEqual(576, asset['height'])
            self.assertTrue((service.root / asset['path']).exists())

    def test_upload_character_and_location_locks_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Service(temp_dir)
            service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = service.create_project({'title': 'Story', 'story': 'Ada and Ben in Room. Event 0.', 'style': 'realism'})
            data = analysis(1, characters=True)
            data['locations'] = [{'name': 'Room', 'description': 'Cosy room.'}]
            service.import_analysis(project['id'], data, 1)

            detail = service.detail(project['id'])
            char = detail['characters'][0]
            loc = detail['locations'][0]

            char_img = png(512, 512)
            char_res = service.upload_character_portrait(char['id'], char_img)
            self.assertTrue(char_res['locked'])
            self.assertIn('portrait', json.loads(char_res['references_json']))

            loc_img = png(768, 432)
            loc_res = service.upload_location_image(loc['id'], loc_img)
            self.assertTrue(loc_res['locked'])
            self.assertIn('environment', json.loads(loc_res['references_json']))

    def test_project_prompts_and_server_dispatch_upload(self):
        import base64
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Application(temp_dir)
            app.service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = app.service.create_project({'title': 'Story', 'story': ' '.join(f'Event {i}.' for i in range(5)), 'style': 'realism'})
            app.service.import_analysis(project['id'], analysis(2), 1)

            # Test project_prompts
            prompts = app.dispatch('GET', f'/api/projects/{project["id"]}/prompts', {})
            self.assertEqual(project['id'], prompts['project_id'])
            self.assertEqual(2, len(prompts['scenes']))
            self.assertIn('prompt', prompts['scenes'][0])
            self.assertIn('flow_prompt', prompts['scenes'][0])
            self.assertIn('references', prompts['scenes'][0])
            self.assertIn('locked_characters', prompts)
            self.assertIn('locked_locations', prompts)
            self.assertIn('known_hashes', prompts)

            # Test telemetry endpoint
            telem_res = app.dispatch('POST', '/api/telemetry', {'event': 'test_ping', 'info': 'ok'})
            self.assertEqual('ok', telem_res['status'])

            # Test server upload endpoint
            scene_id = prompts['scenes'][0]['scene_id']
            b64_str = base64.b64encode(png(1024, 576)).decode('utf-8')
            upload_res = app.dispatch('POST', f'/api/scenes/{scene_id}/upload', {'image': f'data:image/png;base64,{b64_str}'})
            self.assertEqual(scene_id, upload_res['scene_id'])

            detail = app.service.detail(project['id'])
            self.assertEqual(upload_res['generation_id'], detail['scenes'][0]['active_generation_id'])

            # Test deduplication rejection on second scene
            scene2_id = prompts['scenes'][1]['scene_id']
            with self.assertRaises(DomainError) as ctx:
                app.dispatch('POST', f'/api/scenes/{scene2_id}/upload', {'image': f'data:image/png;base64,{b64_str}'})
            self.assertEqual(409, ctx.exception.status)
            self.assertIn('дублирует', str(ctx.exception))

    def test_upload_scene_image_rejects_duplicate_of_location_reference(self):
        import base64
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Application(temp_dir)
            app.service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = app.service.create_project({'title': 'Story', 'story': 'Ada in Room. Event 0.', 'style': 'realism'})
            data = analysis(1, characters=True)
            data['locations'] = [{'name': 'Room', 'description': 'Cosy room.'}]
            app.service.import_analysis(project['id'], data, 1)

            # Upload location reference
            locations = app.service.detail(project['id'])['locations']
            loc_id = locations[0]['id']
            loc_img_bytes = png(768, 432)
            loc_b64 = base64.b64encode(loc_img_bytes).decode('utf-8')
            app.dispatch('POST', f'/api/locations/{loc_id}/upload', {'image': f'data:image/png;base64,{loc_b64}'})

            # Attempt to upload the same location image as a scene image
            scene_id = app.service.detail(project['id'])['scenes'][0]['id']
            with self.assertRaises(DomainError) as ctx:
                app.dispatch('POST', f'/api/scenes/{scene_id}/upload', {'image': f'data:image/png;base64,{loc_b64}'})
            self.assertEqual(409, ctx.exception.status)
            self.assertIn('Room', str(ctx.exception))


    def test_bridge_v1_status_and_project_scoped_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Application(temp_dir)
            project = app.dispatch('POST', '/api/projects', {'title': 'Bridge', 'story': ' '.join(f'Event {i}.' for i in range(5)), 'style': 'realism'})
            project_id = project['id']
            self.assertFalse(app.dispatch('GET', '/api/bridge/v1/status', {})['connected'])
            hello = {'project_id': project_id, 'flow_project_id': 'flow-project',
                     'adapter_connected': True, 'adapter_instance_id': 'active-adapter',
                     'claim_adapter': True, 'running': False, 'status': 'Готов',
                     'script_version': '0.1.43', 'scoped_scene_start': True}
            self.assertIsNone(app.dispatch('POST', '/api/bridge/v1/heartbeat', hello)['command'])
            status = app.dispatch('GET', '/api/bridge/v1/status', {})
            self.assertTrue(status['connected'])
            self.assertEqual(project_id, status['project_id'])
            self.assertEqual('0.1.43', status['script_version'])
            self.assertTrue(status['scoped_scene_start'])
            app.service.import_analysis(project_id, analysis(1, characters=False), 1)
            scene_id = app.service.detail(project_id)['scenes'][0]['id']
            scoped = app.dispatch('POST', '/api/bridge/v1/command',
                                  {'action': 'start', 'project_id': project_id, 'scene_ids': [scene_id]})
            scoped_command = app.dispatch('POST', '/api/bridge/v1/heartbeat', hello)['command']
            self.assertEqual(scoped['command_id'], scoped_command['id'])
            self.assertEqual([scene_id], scoped_command['scene_ids'])
            app.dispatch('POST', '/api/bridge/v1/heartbeat', {**hello, 'ack_command_id': scoped_command['id']})
            app.dispatch('POST', '/api/bridge/v1/heartbeat', {**hello, 'script_version': '0.1.99',
                                                              'scoped_scene_start': False})
            with self.assertRaises(DomainError):
                app.dispatch('POST', '/api/bridge/v1/command',
                             {'action': 'start', 'project_id': project_id, 'scene_ids': [scene_id]})
            app.dispatch('POST', '/api/bridge/v1/heartbeat', hello)
            with self.assertRaises(DomainError):
                app.dispatch('POST', '/api/bridge/v1/command', {'action': 'start', 'project_id': 'another'})
            sent = app.dispatch('POST', '/api/bridge/v1/command', {'action': 'start', 'project_id': project_id})
            # A second Flow tab can have its own adapter peer.  It still must not
            # consume a command claimed by the selected adapter tab.
            stale = {**hello, 'adapter_instance_id': 'stale-adapter',
                     'claim_adapter': False, 'status': 'Other adapter'}
            self.assertIsNone(app.dispatch('POST', '/api/bridge/v1/heartbeat', stale)['command'])
            command = app.dispatch('POST', '/api/bridge/v1/heartbeat', hello)['command']
            self.assertEqual(sent['command_id'], command['id'])
            self.assertEqual('start', command['action'])
            self.assertIsNone(app.dispatch('POST', '/api/bridge/v1/heartbeat',
                                           {**hello, 'ack_command_id': command['id']})['command'])

            # Checking the bridge is allowed even before its heartbeat has
            # reported the newly selected FrameForge project.  The live adapter
            # then receives the check command and claims that project.
            second = app.dispatch('POST', '/api/projects', {'title': 'Bridge second', 'story': 'Test second bridge.', 'style': 'realism'})
            second_id = second['id']
            checked = app.dispatch('POST', '/api/bridge/v1/command', {'action': 'check', 'project_id': second_id})
            rebound = app.dispatch('POST', '/api/bridge/v1/heartbeat', {**hello, 'project_id': second_id})['command']
            self.assertEqual(checked['command_id'], rebound['id'])
            self.assertEqual('check', rebound['action'])
            self.assertEqual(second_id, app.dispatch('GET', '/api/bridge/v1/status', {})['project_id'])

    def test_scene_accepts_same_location_with_small_visible_change(self):
        from PIL import ImageDraw
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Application(temp_dir)
            app.service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v'})
            project = app.service.create_project({'title': 'Story', 'story': 'Ada in Room. Event 0.', 'style': 'realism'})
            data = analysis(1, characters=True)
            data['locations'] = [{'name': 'Room', 'description': 'Cosy room.'}]
            app.service.import_analysis(project['id'], data, 1)
            detail = app.service.detail(project['id'])
            reference = png(1024, 576)
            app.service.upload_location_image(detail['locations'][0]['id'], reference)
            image = Image.open(io.BytesIO(reference)).convert('RGB')
            ImageDraw.Draw(image).rectangle((400, 200, 440, 280), fill='red')
            output = io.BytesIO()
            image.save(output, format='PNG')
            result = app.service.upload_scene_image(detail['scenes'][0]['id'], output.getvalue())
            self.assertEqual(result['scene_id'], detail['scenes'][0]['id'])


class GroqProviderTests(unittest.TestCase):
    def test_gemini_text_adapter_returns_openai_compatible_content(self):
        import unittest.mock
        provider = GeminiTextProvider('google_test', 'gemini-3.1-flash-lite')
        response = unittest.mock.MagicMock()
        response.read.return_value = json.dumps({
            'candidates': [{'content': {'parts': [{'text': '{"scenes":[]}'}]}, 'finishReason': 'STOP'}]
        }).encode()
        response.__enter__.return_value = response
        with unittest.mock.patch('urllib.request.urlopen', return_value=response) as mocked:
            result = provider._chat_completion({'messages': [{'role': 'user', 'content': 'Analyse this story'}], 'max_tokens': 777})
        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertIn('Analyse this story', payload['contents'][0]['parts'][0]['text'])
        self.assertEqual('application/json', payload['generationConfig']['responseMimeType'])
        self.assertEqual(777, payload['generationConfig']['maxOutputTokens'])
        self.assertEqual('{"scenes":[]}', result['choices'][0]['message']['content'])

    def test_groq_provider_requires_api_key(self):
        with self.assertRaises(DomainError):
            GroqProvider('')

    def test_service_settings_accepts_groq_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            updated = service.save_settings({
                'text_backend': 'groq',
                'groq_api_key': 'gsk_test1234567890',
                'groq_model': 'qwen/qwen3.8-27b'
            })
            self.assertEqual('groq', updated['text_backend'])
            self.assertEqual('gsk_test1234567890', updated['groq_api_key'])
            self.assertEqual('qwen/qwen3.8-27b', updated['groq_model'])

    def test_groq_health_success(self):
        import unittest.mock
        provider = GroqProvider('gsk_valid', 'openai/gpt-oss-120b')
        mock_response = json.dumps({'data': [{'id': 'openai/gpt-oss-120b'}, {'id': 'llama-3.1-8b-instant'}]}).encode()
        with unittest.mock.patch('urllib.request.urlopen') as mock_urlopen:
            mock_cm = unittest.mock.MagicMock()
            mock_cm.read.return_value = mock_response
            mock_cm.__enter__.return_value = mock_cm
            mock_urlopen.return_value = mock_cm
            health = provider.health()
            self.assertTrue(health['ok'])
            self.assertEqual(2, health['models_count'])
            self.assertEqual('openai/gpt-oss-120b', health['selected'])

    def test_groq_retries_on_429_rate_limit(self):
        import unittest.mock
        provider = GroqProvider('gsk_valid')
        error_body = json.dumps({'error': {'message': 'Rate limit reached. Please try again in 0.1s.', 'code': 'rate_limit_exceeded'}}).encode()
        err_429 = urllib.error.HTTPError('https://api.groq.com', 429, 'Too Many Requests', {}, io.BytesIO(error_body))
        success_response = unittest.mock.MagicMock()
        success_response.read.return_value = json.dumps({'choices': [{'message': {'content': '{"ok": true}'}}]}).encode()
        success_response.__enter__.return_value = success_response

        with unittest.mock.patch('urllib.request.urlopen', side_effect=[err_429, success_response]), \
             unittest.mock.patch('time.sleep') as mock_sleep:
            res = provider._chat_completion({'model': 'openai/gpt-oss-120b', 'messages': []})
            self.assertEqual('{"ok": true}', res['choices'][0]['message']['content'])
            self.assertTrue(mock_sleep.called)

    def test_groq_tries_every_key_on_120b_before_stopping(self):
        import unittest.mock
        from imagegen.providers import GroqRateLimitError

        keys = [f'gsk_test_{index}' for index in range(5)]
        provider = GroqProvider(keys, 'openai/gpt-oss-120b')
        requests = []

        def rate_limited(request, timeout):
            requests.append((request.get_header('Authorization'), json.loads(request.data)['model']))
            raise urllib.error.HTTPError(
                request.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'{"error":"rate limit"}'))

        with unittest.mock.patch('urllib.request.urlopen', side_effect=rate_limited):
            with self.assertRaises(GroqRateLimitError):
                provider._chat_completion({'model': 'openai/gpt-oss-120b', 'messages': []})

        self.assertEqual([f'Bearer {key}' for key in keys], [item[0] for item in requests])
        self.assertEqual(['openai/gpt-oss-120b'] * len(keys), [item[1] for item in requests])

    def test_worker_uses_groq_for_analysis(self):
        import unittest.mock
        with tempfile.TemporaryDirectory() as temp:
            app = Application(temp)
            app.service.save_settings({'unet': 'u', 'clip': 'c', 'vae': 'v', 'text_backend': 'groq', 'story_analyzer_version': 'v2', 'groq_api_key': 'gsk_mock'})
            project = app.service.create_project({'title': 'Story', 'story': 'Ada works in the room. Ada finishes the task.', 'style': 'realism'})
            generation_id = app.dispatch('POST', f'/api/projects/{project["id"]}/analyse', {'target': 2})['generation_id']
            gen = app.service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))

            mock_payload = {
                'choices': [{
                    'finish_reason': 'stop',
                    'message': {
                        'content': json.dumps({
                            'world': 'Test world',
                            'characters': [{'name': 'Ada', 'description': 'Adult woman, 30 years old', 'wardrobe': {}}],
                            'locations': [{'name': 'Room', 'description': 'A simple room'}],
                            'scenes': [
                                {
                                    'source_id': 0,
                                    'beat': 'First event',
                                    'characters': ['Ada'],
                                    'wardrobe': {},
                                    'location': 'Room',
                                    'location_ref': 'Room',
                                    'time': 'Day',
                                    'shot': 'Medium',
                                    'emotion': 'calm',
                                    'continuity_before': '',
                                    'continuity_after': '',
                                    'visible_people': 1
                                },
                                {
                                    'source_id': 1,
                                    'beat': 'Second event',
                                    'characters': ['Ada'],
                                    'wardrobe': {},
                                    'location': 'Room',
                                    'location_ref': 'Room',
                                    'time': 'Day',
                                    'shot': 'Close-up',
                                    'emotion': 'focused',
                                    'continuity_before': '',
                                    'continuity_after': '',
                                    'visible_people': 1
                                }
                            ]
                        })
                    }
                }]
            }
            with unittest.mock.patch.object(GroqProvider, '_chat_completion', return_value=mock_payload):
                app.worker.prepare(gen)

            project_detail = app.service.detail(project['id'])
            self.assertEqual(2, len(project_detail['scenes']))
            self.assertEqual('Ada', project_detail['characters'][0]['name'])

    def test_horror_preset_thumbnail_exists(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        thumbnail = root / 'imagegen' / 'static' / 'presets' / 'horror.png'
        self.assertTrue(thumbnail.is_file())
        self.assertGreater(thumbnail.stat().st_size, 1000)

    def test_groq_retries_on_400_json_validate_failed(self):
        import unittest.mock
        provider = GroqProvider('gsk_valid')
        error_body = json.dumps({
            'error': {
                'message': "Failed to validate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
                'type': 'invalid_request_error',
                'code': 'json_validate_failed',
                'failed_generation': ''
            }
        }).encode()
        err_400 = urllib.error.HTTPError('https://api.groq.com', 400, 'Bad Request', {}, io.BytesIO(error_body))
        success_response = unittest.mock.MagicMock()
        success_response.read.return_value = json.dumps({'choices': [{'message': {'content': '{"world": "Recovered"}'}}]}).encode()
        success_response.__enter__.return_value = success_response

        with unittest.mock.patch('urllib.request.urlopen', side_effect=[err_400, success_response]), \
             unittest.mock.patch('time.sleep') as mock_sleep:
            res = provider._chat_completion({
                'model': 'openai/gpt-oss-120b',
                'messages': [],
                'response_format': {'type': 'json_object'}
            })
            self.assertEqual('{"world": "Recovered"}', res['choices'][0]['message']['content'])
            self.assertTrue(mock_sleep.called)

    def test_groq_parses_markdown_wrapped_json(self):
        import unittest.mock
        provider = GroqProvider('gsk_valid')
        mock_analysis = {
            'world': 'Test world',
            'characters': [{'name': 'Ada', 'description': 'Adult woman, 30 years old', 'wardrobe': {}}],
            'locations': [{'name': 'Room', 'description': 'A simple room'}],
            'scenes': [
                {
                    'source_id': 0,
                    'beat': 'First event',
                    'characters': ['Ada'],
                    'wardrobe': {},
                    'location': 'Room',
                    'location_ref': 'Room',
                    'time': 'Day',
                    'shot': 'Medium',
                    'emotion': 'calm',
                    'continuity_before': '',
                    'continuity_after': '',
                    'visible_people': 1
                }
            ]
        }
        markdown_content = f"Here is your screenplay:\n```json\n{json.dumps(mock_analysis)}\n```\nHope you like it!"
        mock_resp = {
            'choices': [{
                'finish_reason': 'stop',
                'message': {'content': markdown_content}
            }]
        }
        with unittest.mock.patch.object(provider, '_chat_completion', return_value=mock_resp):
            project = {'mode': 'characters', 'style': 'realism', 'idea': '', 'story': 'Event 0.'}
            result = provider._analyse_segments(project, ['Event 0.'], 1)
            self.assertEqual(1, len(result['scenes']))
            self.assertEqual('Ada', result['characters'][0]['name'])

    def test_settings_defaults_to_local_text_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            settings = service.settings()
            self.assertEqual('local', settings['text_backend'])

    def test_scene_video_upload_and_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            project = service.create_project({'title': 'Video Test', 'story': 'Event 0. Event 1.', 'style': 'realism'})
            service.import_analysis(project['id'], analysis(2), 1)
            scenes = service.detail(project['id'])['scenes']
            scene = scenes[0]
            self.assertIsNone(scene.get('video_asset_id'))

            dummy_webm = b'\x1a\x45\xdf\xa3webm_dummy_bytes'
            result = service.upload_scene_video(scene['id'], dummy_webm, filename='test.webm')
            self.assertEqual(scene['id'], result['scene_id'])
            self.assertTrue(result['video_asset_id'])

            detail = service.detail(project['id'])
            updated_scene = detail['scenes'][0]
            self.assertEqual(result['video_asset_id'], updated_scene['video_asset_id'])

            prompts = service.project_prompts(project['id'])
            self.assertTrue(prompts['scenes'][0]['has_video'])
            self.assertEqual(result['video_asset_id'], prompts['scenes'][0]['video_asset_id'])

            delete_res = service.delete_scene_video(scene['id'])
            self.assertIsNone(delete_res['video_asset_id'])
            detail_after = service.detail(project['id'])
            self.assertIsNone(detail_after['scenes'][0]['video_asset_id'])

    def test_server_scene_video_endpoint_and_mime(self):
        import base64
        with tempfile.TemporaryDirectory() as temp:
            app = Application(temp)
            project = app.service.create_project({'title': 'HTTP Video Test', 'story': 'Event 0. Event 1.', 'style': 'realism'})
            app.service.import_analysis(project['id'], analysis(2), 1)
            scenes = app.service.detail(project['id'])['scenes']
            scene = scenes[0]

            dummy_mp4 = b'mp4_dummy_bytes'
            b64_data = base64.b64encode(dummy_mp4).decode()
            res = app.dispatch('POST', f'/api/scenes/{scene["id"]}/video', {'video': b64_data, 'filename': 'anim.mp4'})
            self.assertTrue(res['video_asset_id'])

            asset = app.service.asset(res['video_asset_id'])
            self.assertTrue(asset['path'].endswith('.mp4'))

            flow_res = app.dispatch('POST', '/api/flow/command', {'command': 'animate_scene', 'scene_id': scene['id']})
            self.assertEqual('animate_scene', flow_res['queued_command'])

    def test_camera_settings_migration_and_project_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(temp)
            p_default = service.create_project({'title': 'Def Cam', 'story': 'Event 0.', 'style': 'realism'})
            self.assertEqual('cinema', p_default.get('camera_mode'))
            self.assertEqual('steady', p_default.get('camera_pacing'))
            self.assertEqual('16:9', p_default.get('aspect_ratio'))

            p_custom = service.create_project({
                'title': 'Custom Cam', 'story': 'Event 0.', 'style': 'realism',
                'camera_mode': 'portrait', 'camera_pacing': 'fast', 'aspect_ratio': '21:9'
            })
            self.assertEqual('portrait', p_custom.get('camera_mode'))
            self.assertEqual('fast', p_custom.get('camera_pacing'))
            self.assertEqual('21:9', p_custom.get('aspect_ratio'))

            updated = service.update_camera_settings(p_default['id'], {
                'camera_mode': 'dynamic', 'camera_pacing': 'slow', 'aspect_ratio': '9:16'
            })
            self.assertEqual('dynamic', updated['camera_mode'])
            self.assertEqual('slow', updated['camera_pacing'])
            self.assertEqual('9:16', updated['aspect_ratio'])

    def test_server_analyse_action_updates_camera_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Application(temp)
            project = app.service.create_project({'title': 'Analyse Cam', 'story': 'Event 0. Event 1.', 'style': 'realism'})
            self.assertEqual('cinema', project['camera_mode'])
            res = app.dispatch('POST', f'/api/projects/{project["id"]}/analyse', {
                'target': 2, 'camera_mode': 'panorama', 'camera_pacing': 'slow', 'aspect_ratio': '21:9'
            })
            self.assertTrue(res.get('generation_id'))
            reloaded = app.service.project(project['id'])
            self.assertEqual('panorama', reloaded['camera_mode'])
            self.assertEqual('slow', reloaded['camera_pacing'])
            self.assertEqual('21:9', reloaded['aspect_ratio'])

    def test_camera_director_prompt_and_expand_storyboard(self):
        cinema_p = camera_director_prompt('cinema', 'steady')
        self.assertIn('Cinema', cinema_p)
        self.assertIn('Steady', cinema_p)

        portrait_p = camera_director_prompt('portrait', 'fast')
        self.assertIn('Portrait', portrait_p)
        self.assertIn('Fast', portrait_p)

        panorama_p = camera_director_prompt('panorama', 'slow')
        self.assertIn('Panorama', panorama_p)
        self.assertIn('Slow', panorama_p)

        dynamic_p = camera_director_prompt('dynamic', 'steady')
        self.assertIn('Dynamic', dynamic_p)

        base_res = {'scenes': [{'source_quote': 'Story text.', 'shot': 'Initial', 'beat': 'Initial'}]}
        exp_port = expand_storyboard_scenes(copy.deepcopy(base_res), 3, 'portrait')
        self.assertEqual(3, len(exp_port['scenes']))
        self.assertTrue(any('portrait' in s['shot'].lower() or 'close' in s['shot'].lower() for s in exp_port['scenes'][1:]))

        exp_pano = expand_storyboard_scenes(copy.deepcopy(base_res), 3, 'panorama')
        self.assertTrue(any('panoramic' in s['shot'].lower() or 'wide' in s['shot'].lower() for s in exp_pano['scenes'][1:]))

        exp_dyn = expand_storyboard_scenes(copy.deepcopy(base_res), 3, 'dynamic')
        self.assertTrue(any('dynamic' in s['shot'].lower() or 'dutch' in s['shot'].lower() for s in exp_dyn['scenes'][1:]))


if __name__ == '__main__':
    unittest.main()

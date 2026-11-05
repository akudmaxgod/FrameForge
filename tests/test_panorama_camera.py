import copy
import json
import unittest
from unittest.mock import patch
from collections import Counter

from imagegen.panorama_camera import build_plan, instruction, selection, WIDE, VERSION
from imagegen.camera_profiles import instruction as camera_instruction
from imagegen.domain import compile_prompt, compile_flow_prompt
from imagegen.generation_scene import compile_wire
from imagegen import frame_composition
from tests.test_generation_scene_wire import signed
from tests import test_veo_animation as fixtures


CAST = [
    {'id': 'a', 'name': 'Arin', 'description': 'A man.', 'entity_type': 'human'},
    {'id': 'b', 'name': 'Bela', 'description': 'A woman.', 'entity_type': 'human'},
    {'id': 'pet', 'name': 'Tom', 'description': 'A cat.', 'entity_type': 'animal', 'identity_scope': 'individual'},
]
PROJECT = {'camera_mode': 'panorama', 'camera_pacing': 'steady', 'mode': 'dynamic',
           'style': 'Cinematic', 'characters': CAST}


def scene(quote, actors=('Arin',), **changes):
    return {'source_quote': quote, 'beat': quote, 'characters': list(actors),
            'visible_people': len(actors), 'shot': 'medium shot', 'props': [],
            'location': 'Courtyard', 'location_ref': '', 'time': 'Day', **changes}


def rows(quotes):
    return [{'id': str(i), 'position': i + 1, 'current_revision': 1,
             'data': scene(quote, ('Arin', 'Bela'))} for i, quote in enumerate(quotes)]


class PanoramaPlanTests(unittest.TestCase):
    def test_place_only_metadata_does_not_hide_source_action(self):
        data = scene('Arin gives the sword to Bela.', ('Arin', 'Bela'),
                     visual_content={'place': 'Courtyard'})
        self.assertEqual(selection(PROJECT, data)['shot'], 'detail')

    def test_different_actions_at_same_index_change_the_camera(self):
        detail = selection(PROJECT, scene('Arin hands over a sword to Bela.', ('Arin', 'Bela')))
        landscape = selection(PROJECT, scene('A vast valley reaches the horizon.', ()))
        self.assertEqual(detail['shot'], 'detail')
        self.assertIn(landscape['shot'], WIDE)
        self.assertNotEqual(detail['angle'], landscape['angle'])

    def test_contextual_sequence_has_establishing_dialogue_detail_and_reaction(self):
        sequence = rows(['A vast city fills the valley.', 'Arin tells Bela about the journey.',
                         'Bela answers Arin.', 'Arin watches in surprise.',
                         'Arin gives the sword to Bela.', 'The troops attack the gate.'])
        plan = list(build_plan(PROJECT, sequence).values())
        self.assertIn(plan[0]['shot'], WIDE)
        self.assertTrue(any(p['shot'] in ('over_shoulder', 'medium') for p in plan[1:3]))
        self.assertEqual(plan[3]['category'], 'reaction')
        self.assertEqual(plan[4]['shot'], 'detail')
        self.assertEqual(plan[5]['category'], 'action')
        self.assertGreater(len({p['angle'] for p in plan}), 1)

    def test_wide_dominance_and_pacing_do_not_become_a_fixed_seven_frame_cycle(self):
        sequence = rows(['The current story moment continues.'] * 70)
        ratios = []
        for pace in ('fast', 'steady', 'slow'):
            plan = list(build_plan({**PROJECT, 'camera_pacing': pace}, sequence).values())
            ratio = sum(p['shot'] in WIDE for p in plan) / len(plan)
            self.assertGreaterEqual(ratio, .5)
            self.assertLess(ratio, .9)
            self.assertTrue(any(p['shot'] == 'medium' for p in plan))
            self.assertTrue(all(not (a['shot'] == b['shot'] == c['shot'])
                                for a, b, c in zip(plan, plan[1:], plan[2:])))
            ratios.append(ratio)
        self.assertGreater(ratios[2], ratios[0])

    def test_new_place_is_established_and_plans_are_deterministic(self):
        sequence = rows(['Arin explains the offer to Bela.'] * 4)
        sequence[2]['data']['location'] = 'Harbour'
        plan = build_plan(PROJECT, sequence)
        self.assertTrue(plan['2']['establishing'])
        self.assertIn(plan['2']['shot'], WIDE)
        self.assertEqual(plan, build_plan(PROJECT, sequence))

    def test_cat_and_hero_do_not_make_two_human_ots_partners(self):
        sequence = rows(['Arin talks to Tom.'] * 9)
        for row in sequence:
            row['data']['characters'] = ['Arin', 'Tom']
        plan = build_plan(PROJECT, sequence)
        self.assertTrue(all(p['human_count'] == 1 and p['shot'] != 'over_shoulder' for p in plan.values()))

    def test_object_and_nature_frames_add_no_human_observer(self):
        data = scene('A geyser steams above the valley.', (), visible_people=0, population_policy='object_only')
        text = instruction(PROJECT, data)
        self.assertIn('no added human scale figure', text)
        self.assertNotIn('over-the-shoulder', text)

    def test_medical_detail_is_not_hidden_by_landscape_preference(self):
        data = scene('На руках человека появилась сыпь.')
        plan = selection(PROJECT, data)
        self.assertEqual(plan['shot'], 'detail')
        self.assertIn('Source-required detail', instruction(PROJECT, data))

    def test_source_cast_state_and_manual_words_are_not_modified(self):
        data = scene('Arin refuses the crown.', state='wounded', period='winter',
                     manual_prompt='Keep the red cloth.', source_start=30, source_end=54)
        before = copy.deepcopy(data)
        project_before = copy.deepcopy(PROJECT)
        build_plan(PROJECT, [{'id': 'x', 'data': data}])
        instruction(PROJECT, data)
        self.assertEqual(before, data)
        self.assertEqual(project_before, PROJECT)

    def test_stale_or_arbitrary_plan_is_not_rendered(self):
        data = scene('Arin hands over a sword to Bela.', ('Arin', 'Bela'))
        old = selection(PROJECT, data)
        data['source_quote'] = 'The army attacks the gate.'
        data['panorama_camera'] = {**old, 'shot': 'invent-a-dragon'}
        self.assertNotIn('invent-a-dragon', instruction(PROJECT, data))
        self.assertEqual(selection(PROJECT, data)['category'], 'action')

    def test_other_camera_profiles_keep_their_original_instructions(self):
        data = scene('Arin tells Bela a story.', ('Arin', 'Bela'), camera_sequence_index=0)
        self.assertIn('extreme wide establishing shot', camera_instruction({**PROJECT, 'camera_mode': 'cinema'}, data))
        self.assertIn('medium shot', camera_instruction({**PROJECT, 'camera_mode': 'portrait'}, data))
        self.assertIn('CAMERA: My exact camera', camera_instruction({**PROJECT, 'camera_mode': 'custom', 'camera_custom': 'My exact camera'}, data))
        self.assertIsNone(selection({**PROJECT, 'camera_mode': 'cinema'}, data))


class PanoramaPromptTests(unittest.TestCase):
    def test_local_and_flow_omit_legacy_framing_and_keep_source_exact(self):
        data = scene('Arin hands over a sword to Bela.', ('Arin', 'Bela'))
        data['panorama_camera'] = selection(PROJECT, data)
        local, _ = compile_prompt(PROJECT, data, CAST[:2], allow_unlocked=True)
        flow = compile_flow_prompt(PROJECT, data, CAST[:2])
        for prompt in (local, flow):
            self.assertEqual(prompt.count('CAMERA:'), 1)
            self.assertIn('close-up of the source-described action detail', prompt)
            self.assertNotIn('Shot: medium shot', prompt)
            self.assertNotIn('Full-body composition:', prompt)
            self.assertNotIn('Never crop, fade', prompt)
        self.assertIn(data['source_quote'], local)
        self.assertIn('hands over a sword', flow)

    def test_source_sentence_containing_shot_words_is_not_stripped(self):
        data = scene('Arin reads the words medium shot from the letter.')
        local, _ = compile_prompt(PROJECT, data, CAST[:1], allow_unlocked=True)
        self.assertIn(data['source_quote'], local)

    def test_manual_composition_wins_over_panorama_without_camera_conflict(self):
        story = 'Алиса и Борис несут землю в корзинах.'
        data = frame_composition.attach(signed(story))
        data['shot'] = 'wide shot'
        data['frame_composition'] = frame_composition.choice(data['frame_facts'],
            {'shot_size': 'close', 'camera_angle': 'low', 'placement': 'thirds'})
        local, _ = compile_prompt(PROJECT, data, [], allow_unlocked=True)
        wire = compile_wire({**PROJECT, 'story': story}, data)
        self.assertIn('CAMERA CHOICE: close shot, low oblique camera', local)
        self.assertNotIn('CAMERA:', local)
        self.assertNotIn('Shot: wide shot', local)
        self.assertIn('CAMERA CHOICE:', wire['prompt'])
        self.assertNotIn('CAMERA:', wire['prompt'])

    def test_verified_scene_wire_receives_panorama_without_rewriting_approved_facts(self):
        story = 'Алиса и Борис несут землю в корзинах.'
        data = signed(story)
        before = copy.deepcopy(data)
        wire = compile_wire({**PROJECT, 'story': story}, data)
        self.assertIsNone(wire['error'])
        self.assertEqual(wire['prompt'].count('CAMERA:'), 1)
        self.assertIn('Two people carry baskets of earth.', wire['prompt'])
        self.assertEqual(before, data)


class PanoramaServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.pid = self.fixture._create_sample_project(12)
        with self.service.db.transaction() as db:
            db.execute("UPDATE projects SET camera_mode='panorama' WHERE id=?", (self.pid,))

    def test_single_snapshot_full_manifest_and_scoped_manifest_agree(self):
        detail = self.service.detail(self.pid)
        chosen = detail['scenes'][5]
        before = self.service.db.all('SELECT * FROM scene_revisions WHERE scene_id=?', (chosen['id'],))
        full = self.service.project_prompts(self.pid)
        scoped = self.service.project_prompts(self.pid, scene_ids=[chosen['id']])
        snapshot = self.service.scene_snapshot(chosen, allow_unlocked=True)
        expected = next(s for s in full['scenes'] if s['scene_id'] == chosen['id'])['camera_plan']
        self.assertEqual(expected, scoped['scenes'][0]['camera_plan'])
        self.assertEqual(expected, snapshot['camera_plan'])
        self.assertEqual(before, self.service.db.all('SELECT * FROM scene_revisions WHERE scene_id=?', (chosen['id'],)))
        self.assertFalse(self.service.db.all('SELECT id FROM generations'))

    def test_cache_invalidates_after_source_or_pacing_change(self):
        detail = self.service.detail(self.pid)
        plan = self.service._panorama_camera_plan(detail, detail['scenes'])
        self.assertIs(plan, self.service._panorama_camera_plan(detail, detail['scenes']))
        changed = copy.deepcopy(detail)
        changed['scenes'][0]['data']['source_quote'] = 'На руках появилась сыпь.'
        refreshed = self.service._panorama_camera_plan(changed, changed['scenes'])
        self.assertEqual(refreshed[changed['scenes'][0]['id']]['shot'], 'detail')
        self.assertIsNot(plan, refreshed)
        self.assertIsNot(refreshed, self.service._panorama_camera_plan({**changed, 'camera_pacing': 'slow'}, changed['scenes']))


if __name__ == '__main__':
    unittest.main()

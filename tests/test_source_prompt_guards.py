import unittest
from imagegen.forgotten_cities.prompts import build_prompt_packet
from imagegen.forgotten_cities.grounding import apply_packet, enforce_source_constraints
from imagegen.forgotten_cities.fidelity import apply_review
from imagegen.domain import resolve_scene_presence_contract, compile_flow_prompt

META = {'city_bible': {'canonical_name': 'Город'}, 'anchors': [
    {'anchor_id': 'eye', 'anchor_type': 'artifact', 'canonical_name': 'Искусственный глаз'}]}
STORY = ('Женщина носила искусственный глаз при жизни. Кто-то заметил её проблему. '
         'Подобрал материал. Сделал предмет, который должен был держаться на лице. '
         'Теперь показан древний город. Название «врата в преисподнюю» звучит естественно. Но это наш образ.')


class SourcePromptGuardTests(unittest.TestCase):
    def packet(self, quote):
        return build_prompt_packet(META, 'event', quote, 'Город', story_text=STORY,
                                   source_quote=quote, source_start=STORY.index(quote))

    def test_concrete_actions_survive_context_only_cached_plan_and_have_valid_roles(self):
        wrong_plan = {'status': 'pass', 'subject_state': 'artifact', 'subject': 'Vessel',
            'visual_direction': 'Pottery at excavation', 'expected_people': 0, 'roles': [],
            'required_artifacts': [], 'forbidden': [], 'time_layer': 'excavation', 'reason': 'Generic context'}
        for quote, count in [('Кто-то заметил её проблему.', 2), ('Подобрал материал.', 1),
                             ('Сделал предмет, который должен был держаться на лице.', 1)]:
            packet = apply_review(self.packet(quote), wrong_plan, META['anchors'])
            self.assertEqual(packet['source_binding'], 'artifact_action')
            self.assertEqual(packet['required_artifacts'], ['Искусственный глаз'])
            self.assertIn('Illustrative historical reconstruction', packet['visual_direction'])
            scene = apply_packet({'source_quote': quote, 'raw_visible_people': 9}, packet)
            self.assertEqual(scene['raw_visible_people'], count)
            self.assertEqual(len(scene['episodic_people']), count)
            contract = resolve_scene_presence_contract(scene, [], story=STORY)
            self.assertFalse(any(e['code'] == 'unassigned_humans' for e in contract['errors']))

    def test_artifact_does_not_carry_over_to_next_city_even_without_gemini(self):
        packet = enforce_source_constraints(self.packet('Теперь показан древний город.'), STORY)
        scene = apply_packet({'props': [{'name': 'Искусственный глаз', 'visible': True}]}, packet)
        self.assertEqual(scene['props'], [])
        self.assertEqual(packet['required_artifacts'], [])
        self.assertIn('Do not insert unrelated reference objects', packet['compiled_prompt'])

    def test_critical_direction_survives_flow_2200_character_cut(self):
        quote = 'Название «врата в преисподнюю» звучит естественно.'
        packet = enforce_source_constraints(self.packet(quote), STORY)
        scene = apply_packet({'beat': quote, 'location': 'Город'}, packet)
        prompt = compile_flow_prompt({'style': 'documentary', 'world': 'Large generic city context. '*300,
                                      'mode': 'objects', 'profile_type': 'forgotten_cities'}, scene)
        self.assertGreater(len(prompt), 2200)
        sent = prompt[:2200]
        self.assertTrue(sent.startswith('SELECTED SCENE'))
        self.assertIn('no invented portal', sent)
        self.assertIn('Do not insert unrelated reference objects', sent)
        self.assertIn(quote, sent)

    def test_reference_is_visible_in_artifact_view_and_pronoun_burial(self):
        packet = self.packet('Женщина носила искусственный глаз при жизни.')
        plan = {'status': 'pass', 'subject_state': 'artifact', 'subject': 'Eye',
            'required_artifacts': ['Искусственный глаз'], 'expected_people': 0, 'roles': [],
            'visual_direction': 'Show attachment holes.', 'forbidden': [], 'time_layer': 'museum_display'}
        scene = apply_packet({}, apply_review(packet, plan, META['anchors']))
        self.assertEqual(scene['props'][0]['state'], 'placed')
        story = 'Женщина носила искусственный глаз при жизни. А потом женщина была с ним похоронена.'
        quote = 'А потом женщина была с ним похоронена.'
        packet = build_prompt_packet(META, 'burial', quote, 'Город', story_text=story,
                                    source_quote=quote, source_start=story.index(quote))
        self.assertEqual(packet['source_binding'], 'buried_remains')
        self.assertEqual(packet['required_artifacts'], ['Искусственный глаз'])

    def test_metaphor_and_physical_scale_guards_are_in_generation_prompt(self):
        packet = enforce_source_constraints(self.packet('Название «врата в преисподнюю» звучит естественно.'), STORY)
        self.assertIn('not a literal structure', packet['compiled_prompt'])
        self.assertIn('no invented portal', packet['compiled_prompt'])
        artifact = enforce_source_constraints(self.packet('Женщина носила искусственный глаз при жизни.'), STORY)
        self.assertIn('real physical scale', artifact['compiled_prompt'])

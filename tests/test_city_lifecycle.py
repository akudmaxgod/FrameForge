import unittest
import json
import tempfile
from imagegen.db import dump, now, uid
from imagegen.service import Service
from tests.test_app import analysis
from imagegen.domain import compile_flow_prompt, compile_prompt, resolve_scene_presence_contract
from imagegen.forgotten_cities.prompts import build_prompt_packet
from imagegen.forgotten_cities.grounding import apply_packet, enforce_source_constraints
from imagegen.forgotten_cities.fidelity import apply_review
from imagegen.forgotten_cities.lifecycle import city_lifecycle


class CityLifecycleTests(unittest.TestCase):
    def test_past_archaeological_fieldwork_uses_its_period(self):
        quote='В тысяча девятьсот двадцать втором году археолог поднялся на вершину холма.'
        packet=self.packet(quote)
        self.assertEqual(packet['visual_time_layer'],'excavation')
        self.assertIn('nineteenth or twentieth-century',packet['visual_direction'])

    def test_fieldwork_date_carries_into_next_paragraph(self):
        first='В тысяча девятьсот двадцать втором году археолог начал раскопки.'
        quote='Археолог извлек из земли небольшую печать из стеатита.'
        packet=self.packet(quote,first+'\n\n'+quote)
        self.assertEqual(packet['visual_time_layer'],'excavation')

    def test_speculative_claim_is_a_publication_not_an_explosion(self):
        quote='Сторонники псевдонаучных теорий утверждали, что город погиб в результате ядерного взрыва.'
        packet=self.packet(quote)
        self.assertEqual(packet['visual_time_layer'],'abstract_explanation')
        self.assertIn('publications',packet['visual_direction'])

    def test_seal_animal_stays_an_engraving(self):
        first='Археолог нашёл печать из стеатита.'
        quote='На ней был выгравирован образ быка с единственным рогом.'
        packet=self.packet(quote,first+' '+quote)
        self.assertIn('carved into the flat artifact surface',packet['visual_direction'])
    def packet(self, quote, story=None):
        story = story or quote
        packet = build_prompt_packet({}, 'e', quote, 'Древний город', story_text=story,
                                     source_quote=quote, source_start=story.index(quote))
        return enforce_source_constraints(packet, story)

    def test_city_life_overrides_cached_empty_excavation_without_exact_headcount(self):
        quote = 'Для жителей они разделяли повседневные действия: здесь готовят, здесь хранят вещи, здесь работают.'
        packet = self.packet(quote)
        bad = {'status': 'pass', 'subject_state': 'architecture', 'subject': 'Ruins',
               'visual_direction': 'Empty excavation', 'expected_people': 0, 'roles': [],
               'required_artifacts': [], 'forbidden': [], 'time_layer': 'excavation'}
        packet = apply_review(packet, bad)
        scene = apply_packet({'source_quote': quote, 'raw_visible_people': 0}, packet)
        self.assertEqual(scene['time_layer'], 'historical_reconstruction')
        self.assertEqual(scene['population_policy'], 'inhabited_city')
        self.assertGreater(scene['visible_people'], 0)
        self.assertEqual(scene['raw_visible_people'], scene['visible_people'])
        presence = resolve_scene_presence_contract(scene, [], story=quote)
        self.assertFalse(any(e['code'] == 'unassigned_humans' for e in presence['errors']))
        project = {'mode': 'objects', 'profile_type': 'forgotten_cities', 'style': 'documentary'}
        for prompt in (compile_flow_prompt(project, scene), compile_prompt(project, scene, [])[0]):
            self.assertIn('no exact headcount', prompt.lower().replace('without an exact headcount', 'no exact headcount'))
            for contradiction in ('Uninhabited scene', 'Exactly 1 person', 'Solo shot', 'empty of inhabitants'):
                self.assertNotIn(contradiction, prompt)
        self.assertIn('complete, maintained', compile_flow_prompt(project, scene)[:2200])

    def test_reconstruction_carries_across_sentences_but_stops_at_today(self):
        first = 'Попробуйте мысленно убрать с этого плана пять тысяч лет.'
        second = 'У дверного проёма — дверь.'
        third = 'Сегодня перед нами только низкие стены.'
        story = first + ' У стены снова появляется высота. ' + second + '\n\n' + third
        self.assertEqual('historical_reconstruction', city_lifecycle(second, story))
        self.assertEqual('modern_ruins', city_lifecycle(third, story))
        self.assertEqual('historical_reconstruction', city_lifecycle('В третьем тысячелетии здесь развивался крупный городской центр.'))

    def test_modern_ruins_remove_stale_residents(self):
        quote = 'Сегодня здесь сухая земля, остатки стен и древние захоронения.'
        scene = apply_packet({'visible_people': 3, 'raw_visible_people': 3, 'episodic_people': ['жители']}, self.packet(quote))
        self.assertEqual(scene['time_layer'], 'modern_ruins')
        self.assertEqual(scene['visible_people'], 0)
        self.assertEqual(scene['episodic_people'], [])
        self.assertEqual(scene['raw_visible_people'], 0)

    def test_archaeologists_are_allowed_only_for_explicit_modern_activity(self):
        quote = 'Археологи расчищают остатки стен.'
        scene = apply_packet({}, self.packet(quote))
        self.assertEqual(scene['time_layer'], 'modern_ruins')
        self.assertEqual(scene['population_policy'], 'source_people')
        self.assertEqual(scene['episodic_people'], ['археологи'])
        self.assertNotIn('Uninhabited scene', compile_flow_prompt({'style': 'documentary'}, scene))

    def test_burial_and_artifact_detail_never_gain_city_residents(self):
        meta = {'anchors': [{'anchor_id': 'eye', 'anchor_type': 'artifact', 'canonical_name': 'Искусственный глаз'}]}
        for quote in ('Сегодня нашли женщину с искусственным глазом в погребении.', 'Искусственный глаз: от центра расходятся тонкие линии.'):
            packet = build_prompt_packet(meta, 'e', quote, 'Город', story_text=quote, source_quote=quote)
            scene = apply_packet({}, packet)
            self.assertEqual(scene['visible_people'], 0)
            self.assertNotIn('population_policy', scene)

    def test_history_in_later_parts_keeps_farming_fishing_and_building_activity(self):
        for quote, cue in [('Сначала люди создавали и переделывали свои постройки.', 'maintenance'),
                           ('Здесь выращивали злаки, держали овец, коз и крупный рогатый скот.', 'farming'),
                           ('Пользовались ресурсами озера Хамун: ловили рыбу, добывали водоплавающих птиц.', 'lakeshore')]:
            packet = self.packet(quote)
            self.assertEqual(packet['visual_time_layer'], 'historical_reconstruction')
            self.assertEqual(packet['city_population'], 'inhabited')
            self.assertIn(cue, packet['visual_direction'])

    def test_board_features_from_later_exact_source_are_preserved(self):
        quote = 'Старая игровая доска.'
        story = quote + ' В погребении нашли деревянную игровую доску. На доске — двадцать круглых игровых полей.'
        packet = {'required_artifacts': ['Игровая доска'], 'source_quote': quote,
                  'source_binding': 'artifact_detail', 'shot_contract': '',
                  'style_contract': '', 'city_contract': '', 'time_layer_contract': '', 'negative_contract': ''}
        packet = enforce_source_constraints(packet, story)
        self.assertIn('twenty circular playing fields', packet['compiled_prompt'])
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            channel = service.create_channel({'name': 'City', 'profile_type': 'forgotten_cities'})
            project = service.create_project({'title': 'Board', 'story': story, 'style': 'documentary',
                                             'channel_profile_id': channel['id']})
            snapshot = service.prop_snapshot(project, {'name': 'Игровая доска', 'description': 'Ancient game'},
                                             profile={'width': 768, 'height': 432})
            self.assertIn('twenty circular playing fields', snapshot['prompt'])

    def test_resume_refreshes_queued_legacy_prompt_and_keeps_finished_history(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            service.save_settings({'image_backend': 'flow', 'unet': 'u', 'clip': 'c', 'vae': 'v'})
            channel = service.create_channel({'name': 'City', 'profile_type': 'forgotten_cities'})
            quote = 'В третьем тысячелетии здесь развивался крупный городской центр.'
            project = service.create_project({'title': 'City', 'story': quote, 'style': 'documentary',
                'mode': 'objects', 'channel_profile_id': channel['id']})
            data = analysis(1, characters=False)
            data['scenes'][0].update(source_quote=quote, beat=quote)
            service.import_analysis(project['id'], data, 1)
            scene = service.detail(project['id'])['scenes'][0]
            old = {'template_version': 8, 'scene': {'visible_people': 0}}
            bid = uid()
            with service.db.transaction() as conn:
                conn.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)', (bid, project['id'], 'legacy', 'old', 'paused', now()))
                pending = service.insert_generation(conn, project['id'], 'scene', old,
                    batch_id=bid, scene_id=scene['id'], scene_revision=scene['current_revision'])
                finished = service.insert_generation(conn, project['id'], 'scene', old,
                    scene_id=scene['id'], scene_revision=scene['current_revision'])
                conn.execute("UPDATE generations SET state='succeeded' WHERE id=?", (finished,))
            service.control_batch(bid, 'resume')
            refreshed = json.loads(service.db.one('SELECT snapshot FROM generations WHERE id=?', (pending,))['snapshot'])
            self.assertEqual(9, refreshed['template_version'])
            self.assertEqual('inhabited_city', refreshed['scene']['population_policy'])
            self.assertEqual(dump(old), service.db.one('SELECT snapshot FROM generations WHERE id=?', (finished,))['snapshot'])

import io
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from imagegen.db import dump, uid, now
from imagegen.domain import compile_prompt, compile_flow_prompt, prop_is_story, validate_analysis, DomainError
from imagegen.service import Service
from imagegen.forgotten_cities.prompts import build_prompt_packet
from imagegen.forgotten_cities.grounding import apply_packet
from imagegen.analyzer_v3.models import DocumentIR, DocumentMode, EventIR, SourceSpan
from imagegen.analyzer_v3.planner_models import VisualEventPlan, SelectedEventIR
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service


STORY = ('В одном из древних погребений нашли женщину с искусственным глазом. '
         'Ему около пяти тысяч лет. На его поверхности обозначен зрачок. '
         'От центра расходятся тонкие линии. По краям — маленькие отверстия для крепления. '
         'Женщина носила искусственный глаз при жизни.')
QUOTES = ['В одном из древних погребений нашли женщину с искусственным глазом.',
          'На его поверхности обозначен зрачок.', 'От центра расходятся тонкие линии.',
          'По краям — маленькие отверстия для крепления.', 'Женщина носила искусственный глаз при жизни.']


def metadata():
    return {'city_bible': {'canonical_name': 'Город'},
            'anchors': [{'anchor_id': 'eye', 'anchor_type': 'artifact',
                         'canonical_name': 'Искусственный глаз', 'description': QUOTES[0]}],
            'event_contexts': [{'event_id': f'ev{i}', 'time_layer': 'historical_reconstruction',
                               'people_policy': 'anonymous_action' if i == 0 else 'none'} for i in range(5)],
            'sequences': [{'event_ids': [f'ev{i}' for i in range(5)], 'artifact_anchor_ids': ['eye']}]}


class SourceFidelityTests(unittest.TestCase):
    def packet(self, i, ordinal=1):
        return build_prompt_packet(metadata(), f'ev{i}', 'Wrong inferred city view', 'Город',
            ordinal=ordinal, story_text=STORY, source_quote=QUOTES[i], source_start=STORY.index(QUOTES[i]))

    def test_burial_is_not_a_living_portrait_in_either_backend(self):
        packet = self.packet(0)
        scene = apply_packet({'source_quote': QUOTES[0], 'beat': QUOTES[0], 'location': 'Город',
                              'visible_people': 1, 'episodic_people': ['женщина']}, packet)
        self.assertEqual(scene['subject_state'], 'buried_remains')
        self.assertEqual(scene['visible_people'], 0)
        project = {'mode': 'objects', 'profile_type': 'forgotten_cities', 'style': 'documentary'}
        for prompt in (compile_prompt(project, scene, [])[0], compile_flow_prompt(project, scene)):
            self.assertIn('deceased remains', prompt)
            self.assertIn('burial', prompt.lower())
            self.assertNotIn('source-described living event', prompt)
            self.assertNotIn('Solo shot featuring one', prompt)
            self.assertNotIn('no human body parts', prompt)

    def test_artifact_details_never_cycle_into_architecture(self):
        for i in (1, 2, 3):
            for ordinal in range(1, 13):
                packet = self.packet(i, ordinal)
                self.assertEqual(packet['source_binding'], 'artifact_detail')
                self.assertEqual(packet['shot_type'], 'prop_detail')
                self.assertEqual(packet['required_artifacts'], ['Искусственный глаз'])
                self.assertEqual(packet['binding_errors'], [])
                self.assertNotIn('Elevated', packet['visual_direction'])
                self.assertIn('surface markings', packet['visual_direction'])

    def test_life_scene_does_not_inherit_death_from_prior_sentence(self):
        packet = self.packet(4)
        scene = apply_packet({'source_quote': QUOTES[4], 'beat': QUOTES[4]}, packet)
        self.assertNotIn('subject_state', scene)
        self.assertEqual(scene['visible_people'], 1)
        self.assertEqual(packet['source_binding'], 'artifact_action')
        self.assertEqual(scene['props'][0]['state'], 'worn')

    def test_unknown_detail_blocks_instead_of_inventing_city(self):
        packet = build_prompt_packet(metadata(), 'missing', 'От центра расходятся тонкие линии.', 'Город',
                                     story_text='От центра расходятся тонкие линии.')
        self.assertTrue(packet['binding_errors'])
        # A bead is also a source-bound object, even without a casting anchor.
        packet = build_prompt_packet(metadata(), 'bead', 'Мы видим отверстие и гладкую поверхность.', 'Город',
            story_text='Маленькая бусина требует работы. Мы видим отверстие и гладкую поверхность.')
        self.assertEqual(packet['primary_subject'], 'бусина')
        self.assertEqual(packet['binding_errors'], [])
        self.assertEqual(packet['shot_type'], 'prop_detail')
        packet = build_prompt_packet(metadata(), 'bead', 'На поверхности бусины есть отверстие.', 'Город',
            story_text='Искусственный глаз. На поверхности бусины есть отверстие.')
        self.assertEqual(packet['primary_subject'], 'бусина')
        self.assertEqual(packet['required_artifacts'], [])

    def test_new_analysis_exports_literal_state_and_visible_artifact(self):
        spans = [SourceSpan(STORY.index(q), STORY.index(q) + len(q), q) for q in QUOTES]
        doc = DocumentIR('storyhash', events=[EventIR(f'ev{i}', 'section', span) for i, span in enumerate(spans)],
                         metadata={'forgotten_cities': metadata()})
        plan = VisualEventPlan('plan', 'storyhash', DocumentMode.NARRATIVE, 5,
            selected_events=[SelectedEventIR(f'sel{i}', f'ev{i}', i, 'section', span) for i, span in enumerate(spans)])
        result = StoryAnalyzerV3Service.export_to_app_analysis(doc, plan, STORY, mode='objects', profile_type='forgotten_cities')
        validate_analysis(result, STORY)
        self.assertEqual(result['props'][0]['importance'], 'story')
        self.assertEqual(result['scenes'][0]['subject_state'], 'buried_remains')
        for scene in result['scenes'][:4]:
            self.assertEqual(scene['props'][0]['name'], 'Искусственный глаз')
            self.assertEqual(scene['shot'], 'prop_detail')

    def test_grounding_replaces_stale_people_count_and_city_artifact_leak(self):
        from imagegen.forgotten_cities.fidelity import apply_review
        base = self.packet(1)
        grounded = apply_packet({'raw_visible_people': 1, 'visible_people': 1}, base)
        self.assertEqual(grounded['raw_visible_people'], 0)
        architecture = {'status': 'pass', 'subject_state': 'architecture', 'subject': 'City',
            'required_artifacts': ['Искусственный глаз'], 'expected_people': 0, 'roles': [],
            'visual_direction': 'View of the city ruins.', 'time_layer': 'excavation', 'forbidden': [], 'reason': 'City context'}
        city = build_prompt_packet(metadata(), 'city', 'Сегодня видны руины города.', 'Город',
                                  source_quote='Сегодня видны руины города.')
        packet = apply_review(city, architecture, metadata()['anchors'])
        grounded = apply_packet({'props': [{'name': 'Искусственный глаз', 'visible': True}]}, packet)
        self.assertEqual(packet['required_artifacts'], [])
        self.assertEqual(grounded['props'], [])
        self.assertEqual(architecture['required_artifacts'], ['Искусственный глаз'], 'Do not mutate cached input')
        # A literal artifact detail must survive a stale generic city plan.
        retained = apply_review(base, architecture, metadata()['anchors'])
        self.assertEqual(retained['required_artifacts'], ['Искусственный глаз'])

    def test_contradictory_cached_review_does_not_block_real_snapshot_or_flow_export(self):
        bad = {'status': 'pass', 'subject_state': 'artifact', 'subject': 'Eye alone',
               'required_artifacts': ['Искусственный глаз'], 'expected_people': 0,
               'visual_direction': 'Eye alone on a table', 'forbidden': [], 'roles': [],
               'time_layer': 'museum_display', 'reason': 'Incorrect interpretation'}
        with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.cached_plan',
                   side_effect=lambda scene, *_: bad if scene.get('source_quote') == QUOTES[0] else None):
            self.test_saved_scene_snapshot_and_flow_export_repair_stale_contracts()

    def test_saved_scene_snapshot_and_flow_export_repair_stale_contracts(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            profile = {**service.settings(), 'unet': 'test', 'clip': 'test', 'vae': 'test'}
            with patch.object(service, 'profile', return_value=profile):
                channel = service.create_channel({'name': 'Source fidelity', 'profile_type': 'forgotten_cities'})
                project = service.create_project({'title': 'Eye', 'story': STORY, 'style': 'documentary',
                    'mode': 'objects', 'channel_profile_id': channel['id']})
                pid = project['id']; prop_id = uid(); scene_ids = []
                with service.db.transaction() as conn:
                    conn.execute('UPDATE projects SET analysis_metadata=? WHERE id=?', (dump({'forgotten_cities': metadata()}), pid))
                    conn.execute('INSERT INTO props(id,project_id,name,description,importance) VALUES(?,?,?,?,?)',
                                 (prop_id, pid, 'Искусственный глаз', 'Small prosthetic eye', 'story_critical'))
                    for i, quote in enumerate(QUOTES[:3], 1):
                        sid = uid(); scene_ids.append(sid)
                        data = {'source_quote': quote, 'beat': quote, 'source_start': STORY.index(quote),
                                'location': 'Город', 'location_ref': '', 'shot': 'establishing',
                                'visible_people': 1 if i == 1 else 0, 'episodic_people': ['женщина'] if i == 1 else [],
                                'props': [], 'characters': [], 'wardrobe': {}, 'forgotten_event_id': f'ev{i-1}'}
                        conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (sid, pid, i))
                        conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (sid, 1, dump(data), now()))
                buf = io.BytesIO(); Image.new('RGB', (768, 432), '#536780').save(buf, 'PNG')
                service.upload_prop_image(prop_id, buf.getvalue())
                self.assertTrue(prop_is_story(service.prop(prop_id)))
                rejected_image = io.BytesIO()
                Image.new('RGB', (1024, 576), '#778899').save(rejected_image, 'PNG')
                before = len(service.db.all('SELECT id FROM generations WHERE project_id=?', (pid,)))
                with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer._request',
                    return_value={'status': 'fail', 'observed': 'Живая женщина.', 'reason': 'Цитата требует погребение.',
                                  'correction': 'Останки в погребении.'}):
                    with self.assertRaisesRegex(DomainError, 'Смысловая проверка отклонила'):
                        service.upload_scene_image(scene_ids[0], rejected_image.getvalue())
                self.assertEqual(len(service.db.all('SELECT id FROM generations WHERE project_id=?', (pid,))), before)
                for i, sid in enumerate(scene_ids):
                    snap = service.scene_snapshot(service.scene(sid))
                    self.assertEqual(snap['expected_people'], 0)
                    self.assertEqual(snap['references'][0]['prop_id'], prop_id)
                    if i == 0:
                        self.assertFalse(snap['uninhabited'])
                        self.assertNotIn('source-described living event', snap['prompt'])
                    else:
                        self.assertEqual(snap['scene']['shot'], 'prop_detail')
                        self.assertNotIn('location_id', snap['references'][0])
                    with service.db.transaction() as conn:
                        service.insert_generation(conn, pid, 'scene', snap, scene_id=sid, scene_revision=1)
                exported = service.project_prompts(pid)['scenes']
                self.assertEqual(len(exported), 3)
                self.assertIn('deceased remains', exported[0]['flow_prompt'])
                self.assertEqual(exported[0]['source_fidelity_retry']['attempts'], 1)
                self.assertTrue(exported[0]['flow_prompt'].startswith('SOURCE FIDELITY CORRECTION'))
                self.assertNotIn('source-described living event', exported[0]['flow_prompt'])
                for scene in exported:
                    self.assertEqual(scene['references'][0]['type'], 'prop')
                    self.assertEqual(scene['references'][0]['id'], prop_id)
                self.assertIn('surface markings', exported[2]['flow_prompt'])
                with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer._request',
                    return_value={'status': 'pass', 'observed': 'Крупный план глаза.', 'reason': 'Зрачок на поверхности глаза.'}):
                    accepted = service.upload_scene_image(scene_ids[1], rejected_image.getvalue())
                saved = service.db.one('SELECT snapshot,state FROM generations WHERE id=?', (accepted['generation_id'],))
                self.assertEqual(saved['state'], 'succeeded')
                self.assertEqual(json.loads(saved['snapshot'])['source_fidelity_review']['status'], 'pass')
                self.assertEqual(len(service.db.all("SELECT id FROM generations WHERE project_id=? AND purpose='scene'", (pid,))), 3)


if __name__ == '__main__':
    unittest.main()

"""Regression: selected references reach scene snapshots and Flow safely."""
import unittest

from imagegen.forgotten_cities.references import scene_locations
from tests.test_reference_casting_flow import ReferenceCastingFlowTests


class SceneReferenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ReferenceCastingFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        for kind, target in (('location', self.fixture.location), ('location', self.fixture.other_location),
                             ('prop', self.fixture.prop)):
            asset = self.fixture.finish(self.fixture.cast(kind, target))[0]
            self.fixture.post(f'/{kind}s/{target["id"]}/select', {'asset_id': asset, 'revision': 1})
            self.fixture.post(f'/{kind}s/{target["id"]}/lock',
                              {'references': {'environment' if kind == 'location' else 'prop': asset}, 'revision': 1})
        self.project = self.service.detail(self.fixture.pid)

    def resolve(self, quote, layer='historical_reconstruction', **packet):
        return scene_locations(self.project, {'source_quote': quote, 'time_layer': layer,
            'forgotten_cities_prompt_contract': {'source_quote': quote, 'visual_time_layer': layer,
                'source_binding': 'narrated_event', **packet}}, self.project['locations'])

    def test_grounded_scene_keeps_reference_without_changing_event_or_people(self):
        scene = self.project['scenes'][0]
        snapshot = self.service.scene_snapshot(scene, allow_unlocked=True)
        refs = snapshot['references']
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['asset_id'], self.project['locations'][0]['references']['environment'])
        self.assertEqual(refs[0]['scope'], 'material_context')
        self.assertEqual(snapshot['expected_people'], 0)
        self.assertEqual(snapshot['scene']['source_quote'], self.project['story'])
        self.assertIn('Do not copy people', snapshot['prompt'])
        self.assertNotIn('Preserve this exact place', snapshot['prompt'])

    def test_same_reference_reaches_flow_with_scope_and_source_setting(self):
        payload = self.service.project_prompts(self.fixture.pid, include_all_scenes=True)
        scene = payload['scenes'][0]
        self.assertEqual(scene['references'][0]['asset_id'], self.project['locations'][0]['references']['environment'])
        self.assertEqual(scene['references'][0]['scope'], 'material_context')
        self.assertIn('Do not copy people', scene['flow_prompt'])
        self.assertNotIn('Setting: Главный вид города', scene['flow_prompt'])

    def test_isolated_supplemental_shot_does_not_receive_optional_populated_context(self):
        import copy
        scene = copy.deepcopy(self.project['scenes'][0])
        scene['data']['environment_reference_policy'] = 'isolate_unbound_participants/1'
        snapshot = self.service.scene_snapshot(scene, allow_unlocked=True)
        self.assertEqual(snapshot['references'], [])
        self.assertEqual(snapshot['reference_binding']['environment_asset_ids'], [])
        self.assertEqual(snapshot['reference_binding']['omitted_context_location_ids'], [self.fixture.location['id']])
        self.assertEqual(snapshot['expected_people'], 0)
        self.assertEqual(snapshot['scene']['source_quote'], self.project['story'])

    def test_isolation_keeps_exact_architecture_and_bound_story_person(self):
        from imagegen.forgotten_cities.references import isolate_material_context
        scene = {'environment_reference_policy': 'isolate_unbound_participants/1'}
        context = {'id': 'context', 'reference_scope': 'material_context'}
        exact = {'id': 'exact', 'reference_scope': 'exact_location'}
        kept, omitted = isolate_material_context(scene, [context, exact])
        self.assertEqual(kept, [exact])
        self.assertEqual(omitted, [context])
        self.assertEqual(isolate_material_context(scene, [context], {'asset_id': 'owner'})[0], [context])

    def test_new_policy_does_not_change_completed_legacy_reference_binding(self):
        scene = self.project['scenes'][0]
        before = self.service.scene_snapshot(scene, allow_unlocked=True)
        from imagegen.forgotten_cities.references import isolate_material_context
        locations = scene_locations(self.project, before['scene'], self.project['locations'])
        self.assertEqual(isolate_material_context(before['scene'], locations), (locations, []))
        self.assertEqual(self.service.scene_snapshot(scene, allow_unlocked=True), before)

    def test_modern_and_documented_event_use_different_context_images(self):
        modern = self.resolve('Древний город: сегодня открыты подземные залы.', 'modern_ruins')
        historical = self.resolve('Древний город: хозяин идёт в коридор.', 'documented_event')
        self.assertEqual(modern[0]['id'], self.fixture.other_location['id'])
        self.assertEqual(historical[0]['id'], self.fixture.location['id'])
        self.assertNotEqual(modern[0]['references'], historical[0]['references'])

    def test_reference_scope_survives_actual_bridge_prompt_truncation(self):
        payload = self.service.project_prompts(self.fixture.pid, include_all_scenes=True)
        prompt = payload['scenes'][0]['flow_prompt']
        self.assertGreater(len(prompt), 2200)
        prefix = prompt[:2200]
        sentence_end = prefix.rfind('. ')
        sent = prefix[:sentence_end + 1] if sentence_end > 1200 else prefix.rsplit(' ', 1)[0]
        self.assertTrue(sent.startswith('SELECTED SCENE'))
        self.assertIn('Do not copy people, faces, clothing, tools, props', sent)
        self.assertIn('The selected source defines the actual place, structure, layout and epoch', sent)
        self.assertIn(self.project['story'], sent)
        self.assertIn('Visible living people: 0', sent)

    def test_unlocked_master_does_not_fall_back_to_modern_ruins(self):
        self.fixture.post(f'/locations/{self.fixture.location["id"]}/unlock', {})
        self.project = self.service.detail(self.fixture.pid)
        self.assertEqual(self.resolve('Древний город: каменные стены.'), [])

    def test_object_closeup_map_and_reported_claim_have_no_city_reference(self):
        for layer, packet in [('museum_display', {'source_binding': 'artifact_detail'}),
                              ('historical_reconstruction', {'source_binding': 'artifact_action'}),
                              ('map', {}), ('modern_ruins', {'source_topic': 'reported_claim'})]:
            with self.subTest(layer=layer, packet=packet):
                self.assertEqual(self.resolve('Древний город и каменная стена.', layer, **packet), [])

    def test_geological_event_does_not_receive_city_architecture(self):
        self.assertEqual(self.resolve('Извержение вулкана засыпало равнину пеплом.'), [])

    def test_different_named_place_does_not_inherit_primary_city(self):
        self.project['analysis_metadata']['forgotten_cities']['geography_mentions'] = [
            {'text': 'Соседний город', 'geography_type': 'SECONDARY_CITY'}]
        self.assertEqual(self.resolve('Соседний город: каменные стены.'), [])

    def test_stale_metadata_lock_is_not_authority_for_a_reference(self):
        for location in self.project['locations']:
            location['locked'] = False
        for anchor in self.project['analysis_metadata']['forgotten_cities']['anchors']:
            anchor.update(locked=True, reference_asset_id='stale-image')
        self.assertEqual(self.resolve('Древний город: каменные стены.'), [])

    def test_batch_snapshot_has_reference_and_current_revision(self):
        batch = self.fixture.post(f'/projects/{self.fixture.pid}/batch', {'idempotency_key': 'reference-regression'})
        job = self.service.db.one('SELECT snapshot FROM generations WHERE batch_id=?', (batch['id'],))
        import json
        snapshot = json.loads(job['snapshot'])
        self.assertEqual(snapshot['references'][0]['revision'], self.fixture.location['revision'])
        self.assertEqual(snapshot['reference_binding']['environment_asset_ids'],
                         [snapshot['references'][0]['asset_id']])

    def legacy_batch(self):
        import json
        from imagegen.db import dump
        batch = self.fixture.post(f'/projects/{self.fixture.pid}/batch', {'idempotency_key': 'legacy-reference-regression'})
        job = self.service.db.one('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        snapshot = json.loads(job['snapshot'])
        snapshot.pop('reference_binding')
        snapshot['references'] = []
        with self.service.db.transaction() as db:
            db.execute('UPDATE generations SET snapshot=? WHERE id=?', (dump(snapshot), job['id']))
        self.service.control_batch(batch['id'], 'pause')
        return batch, job

    def test_resume_refreshes_queued_legacy_jobs_without_creating_duplicates(self):
        import json
        batch, job = self.legacy_batch()
        self.service.control_batch(batch['id'], 'resume')
        rows = self.service.db.all('SELECT * FROM generations WHERE batch_id=?', (batch['id'],))
        self.assertEqual([r['id'] for r in rows], [job['id']])
        self.assertEqual(len(json.loads(rows[0]['snapshot'])['references']), 1)

    def test_resume_does_not_change_completed_generation(self):
        batch, job = self.legacy_batch()
        with self.service.db.transaction() as db:
            db.execute("UPDATE generations SET state='succeeded' WHERE id=?", (job['id'],))
        before = self.service.db.one('SELECT * FROM generations WHERE id=?', (job['id'],))
        self.service.control_batch(batch['id'], 'resume')
        self.assertEqual(self.service.db.one('SELECT * FROM generations WHERE id=?', (job['id'],)), before)

    def test_resume_does_not_rewrite_an_unresolved_flow_submission(self):
        from imagegen.flow_admission import FlowAdmission
        from imagegen.domain import DomainError
        batch, job = self.legacy_batch()
        with FlowAdmission(self.service).db() as db:
            db.execute('INSERT INTO admissions VALUES(?,?,?,?,?,?)',
                       ('pending-submit', self.fixture.pid, job['scene_id'], job['scene_revision'], 'old-prompt', 'reserved'))
        before = self.service.db.one('SELECT * FROM generations WHERE id=?', (job['id'],))
        with self.assertRaises(DomainError):
            self.service.control_batch(batch['id'], 'resume')
        self.assertEqual(self.service.db.one('SELECT * FROM generations WHERE id=?', (job['id'],)), before)

"""Reference casting API/worker/SQLite contracts; no external image generation."""
import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

from PIL import Image
from imagegen.db import dump, uid
from imagegen.domain import DomainError
from imagegen.server import Application
from imagegen.service import Service
from imagegen.analyzer_v3.models import SourceSpan
from imagegen.forgotten_cities.models import CityBibleIR, VisualAnchorIR, ForgottenCitiesDocumentIR


class ReferenceCastingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(self.tmp.name)
        self.service = self.app.service
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        profile = {**self.service.settings(), 'unet': 'test', 'clip': 'test', 'vae': 'test'}
        self.stack.enter_context(patch.object(self.service, 'profile', return_value=profile))
        for method in ('_has_panel_grid', '_has_embedded_frame_or_matte', '_has_embedded_text',
                       '_detected_person_count', '_detected_face_count', '_detected_hand_count'):
            self.stack.enter_context(patch.object(self.app.worker, method, return_value=0))
        channel = self.service.create_channel({'name': 'Casting test', 'profile_type': 'forgotten_cities'})
        self.project = self.service.create_project({'title': 'Casting', 'story': 'Древний город стоит у реки.',
            'style': 'documentary', 'mode': 'objects', 'channel_profile_id': channel['id']})
        self.pid = self.project['id']
        span = SourceSpan(0, len(self.project['story']), self.project['story'])
        anchors = [VisualAnchorIR('city', 'city_master', 'Главный вид города', 'Mud brick houses',
                                  'historical_reconstruction', reference_required=True),
                   VisualAnchorIR('ruins', 'landscape', 'Современные руины', 'Excavated mud bricks',
                                  'modern_ruins', reference_required=True),
                   VisualAnchorIR('eye', 'artifact', 'Глаз', 'Golden eye', 'museum_display',
                                  evidence_spans=[span], reference_required=True)]
        document = ForgottenCitiesDocumentIR(CityBibleIR('city', 'Древний город'), anchors)
        analysis = {'world': 'Ancient city', 'characters': [],
            'locations': [{'name': a.canonical_name, 'description': a.description} for a in anchors[:2]],
            'props': [{'name': 'Глаз', 'description': 'Golden eye', 'importance': 'story'}],
            'scenes': [{'beat': self.project['story'], 'source_quote': self.project['story'],
                        'characters': [], 'wardrobe': {}, 'props': [], 'location': anchors[0].canonical_name,
                        'time': 'Day', 'shot': 'Wide', 'continuity_before': '', 'continuity_after': ''}],
            'v3_metadata': {'forgotten_cities': document.to_dict()}}
        with self.service.db.transaction() as conn:
            gid = self.service.insert_generation(conn, self.pid, 'analysis', {'profile':profile,'project':self.project})
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
        self.app.worker._save_analysis(generation, analysis, self.project['version'])
        detail = self.service.detail(self.pid)
        self.location = detail['locations'][0]
        self.other_location = detail['locations'][1]
        self.prop = detail['props'][0]

    def post(self, path, body):
        return self.app.dispatch('POST', '/api' + path, body)

    def cast(self, kind, target):
        return self.post(f'/{kind}s/{target["id"]}/cast', {'count': 2})['generation_ids']

    def finish(self, ids):
        assets = []
        for gid in ids:
            data = io.BytesIO()
            Image.new('RGB', (768, 432), '#567890').save(data, 'PNG')
            result = self.post(f'/reference_jobs/{gid}/upload', data.getvalue())
            assets.append(result['asset_id'])
            self.assertEqual(self.post(f'/reference_jobs/{gid}/upload', data.getvalue()), result)
        return assets

    def test_analysis_and_reopening_never_enqueue_references_or_frames(self):
        for _ in range(2):
            self.app.dispatch('GET', f'/api/projects/{self.pid}', None)
            prompts = self.service.project_prompts(self.pid)
            self.assertEqual(prompts['reference_jobs'], [])
            self.assertEqual(prompts['scenes'], [])
        jobs = self.service.db.all('SELECT purpose FROM generations WHERE project_id=?', (self.pid,))
        self.assertEqual(jobs, [{'purpose': 'analysis'}])

    def test_cast_creates_exactly_two_jobs_for_only_selected_reference(self):
        for kind, target in (('location', self.location), ('prop', self.prop)):
            ids = self.cast(kind, target)
            self.assertEqual(len(ids), 2)
            for gid in ids:
                job = self.service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
                self.assertEqual(job['purpose'], kind)
                self.assertEqual(job[f'{kind}_id'], target['id'])
                self.assertIsNone(job['scene_id'])
                self.assertIsNone(job['batch_id'])
                self.assertEqual(json.loads(job['snapshot'])['generation_role'], 'reference')
        self.assertEqual(self.service.db.all("SELECT id FROM generations WHERE purpose='scene'"), [])
        self.assertEqual(self.service.db.all('SELECT * FROM batches'), [])
        self.assertEqual(self.service.db.all('SELECT id FROM generations WHERE location_id=?', (self.other_location['id'],)), [])

    def test_concurrent_repeated_click_and_partial_completion_reuse_pair(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.cast('location', self.location), range(3)))
        self.assertTrue(all(ids == results[0] for ids in results))
        self.finish(results[0][:1])
        self.assertEqual(self.cast('location', self.location), results[0])
        self.assertEqual(len(self.service.db.all("SELECT id FROM generations WHERE purpose='location'")), 2)
        with self.assertRaises(DomainError):
            self.post(f'/locations/{self.location["id"]}/select', {'asset_id': 'invalid', 'revision': 1})

    def test_selection_lock_and_reload_are_scoped_and_durable(self):
        for kind, target in (('location', self.location), ('prop', self.prop)):
            assets = self.finish(self.cast(kind, target))
            self.assertFalse(getattr(self.service, kind)(target['id'])['locked'])
            self.post(f'/{kind}s/{target["id"]}/select', {'asset_id': assets[1], 'revision': 1})
            reopened = Service(self.tmp.name)
            self.assertEqual(reopened.reference_cast(kind, target)['selected_asset_id'], assets[1])
            self.post(f'/{kind}s/{target["id"]}/lock', {'references': {kind if kind == 'prop' else 'environment': assets[1]}, 'revision': 1})
            locked = getattr(reopened, kind)(target['id'])
            self.assertTrue(locked['locked'])
            self.assertIn(assets[1], json.loads(locked['references_json']).values())
            self.assertFalse(self.service.location(self.other_location['id'])['locked'])
        self.assertEqual(self.service.db.all('SELECT * FROM batches'), [])

    def test_batch_requires_locked_references_and_only_explicit_batch_creates_frames(self):
        self.cast('location', self.location)
        self.assertIn('missing_required_reference', [e['code'] for e in self.service.project_preflight(self.pid)['errors']])
        with self.assertRaises(DomainError):
            self.post(f'/projects/{self.pid}/batch', {'idempotency_key': uid()})
        for kind, target in (('location', self.location), ('location', self.other_location), ('prop', self.prop)):
            assets = self.finish(self.cast(kind, target))
            self.post(f'/{kind}s/{target["id"]}/select', {'asset_id': assets[0], 'revision': 1})
            self.post(f'/{kind}s/{target["id"]}/lock', {'references': {kind if kind == 'prop' else 'environment': assets[0]}, 'revision': 1})
        self.assertEqual(self.service.db.all("SELECT id FROM generations WHERE purpose='scene'"), [])
        # Stale LOCK flags in analysis cannot authorize frames after unlocking.
        metadata = self.service.detail(self.pid)['analysis_metadata']
        for anchor in metadata['forgotten_cities']['anchors']:
            anchor.update(locked=True, reference_asset_id='legacy_metadata')
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE projects SET analysis_metadata=? WHERE id=?', (dump(metadata), self.pid))
        refs = json.loads(self.service.location(self.other_location['id'])['references_json'])
        self.post(f'/locations/{self.other_location["id"]}/unlock', {})
        with self.assertRaises(DomainError):
            self.post(f'/projects/{self.pid}/batch', {'idempotency_key': uid()})
        self.post(f'/locations/{self.other_location["id"]}/lock', {'references': refs, 'revision': 1})
        self.post(f'/projects/{self.pid}/batch', {'idempotency_key': uid()})
        self.assertEqual(len(self.service.db.all("SELECT id FROM generations WHERE purpose='scene'")), 1)

    def test_idle_bridge_remains_connected_and_accepts_next_pair(self):
        ids = self.cast('location', self.location)
        self.service.active_project_id = self.pid
        self.app.flow_bridge_v1.update(last_seen=time.time()-45, adapter_connected=True,
            adapter_instance_id='test', project_id=self.pid, reference_casting=True, running=False)
        status = self.app.dispatch('GET', '/api/bridge/v1/status', None)
        self.assertTrue(status['connected'])
        self.assertFalse(status['running'])
        self.post('/bridge/v1/command', {'action': 'start', 'project_id': self.pid, 'generation_ids': ids})
        self.assertEqual(self.app.flow_bridge_v1['command']['generation_ids'], ids)
        self.app.flow_bridge_v1['last_seen'] = time.time()-120
        self.assertFalse(self.app.dispatch('GET', '/api/bridge/v1/status', None)['connected'])

    def test_flow_scoped_command_rejects_old_bridge_and_foreign_pair(self):
        ids = self.cast('location', self.location)
        self.service.active_project_id = self.pid
        self.app.flow_bridge_v1.update(last_seen=time.time(), adapter_connected=True,
            adapter_instance_id='test', project_id=self.pid, reference_casting=False)
        body = {'action': 'start', 'project_id': self.pid, 'generation_ids': ids}
        with self.assertRaises(DomainError):
            self.post('/bridge/v1/command', body)
        self.app.flow_bridge_v1['reference_casting'] = True
        self.post('/bridge/v1/command', body)
        self.assertEqual(self.app.flow_bridge_v1['command']['generation_ids'], ids)
        with self.assertRaises(DomainError):
            self.post('/bridge/v1/command', {**body, 'generation_ids': [uid(), uid()]})
        self.assertEqual(self.service.db.all('SELECT * FROM batches'), [])


if __name__ == '__main__':
    unittest.main()

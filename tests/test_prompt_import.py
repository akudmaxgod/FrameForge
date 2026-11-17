import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

from imagegen.prompt_import import create, parse, VERSION, BASE_BUDGET
from imagegen.service import Service
from imagegen.domain import DomainError, fingerprint
from imagegen.server import Application
from imagegen.final_prompt_review import check
from imagegen.generation_scene import RETRY


class PromptImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.service = Service(self.folder.name)
        defaults = self.service.settings()
        self.service.settings = lambda: {**defaults, 'source_contract_enabled': True,
            'google_queue_enabled': True, 'image_backend': 'flow'}
        self.data = {'title': 'Арал — импорт', 'story': 'Вода ушла. Корабль остался.',
            'prompts_text': 'Ржавый корабль на песке.\n\nРыбак смотрит на старый причал.',
            'prompts_filename': 'aral.txt', 'story_filename': 'story.txt', 'seconds_per_scene': 5}

    def project(self, **overrides):
        return create(self.service, {**self.data, **overrides})

    def test_import_preserves_prompt_order_and_creates_no_jobs_or_references(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('No external analysis')):
            project = self.project()
            prompts = self.service.project_prompts(project['id'])
        original = parse(self.data['prompts_text'])
        self.assertEqual([s['data']['manual_prompt'] for s in project['scenes']], original)
        self.assertEqual([s['flow_prompt'] for s in prompts['scenes']], original)
        self.assertTrue(all(s['ready_for_flow'] for s in prompts['scenes']))
        self.assertEqual(project['generations'], [])
        self.assertEqual(project['characters']+project['locations']+project['props'], [])
        self.assertTrue(project['preflight']['ok'])
        self.assertFalse(project['preflight']['semantic_verified'])
        self.assertEqual(project['analysis_metadata']['prompt_import']['duration_seconds'], 10)
        self.assertEqual(project['scenes'][1]['data']['prompt_timing']['start_seconds'], 5)
        self.assertNotIn('source_quote', project['scenes'][0]['data'])

    def test_wrapped_paragraph_crlf_bom_and_blank_lines(self):
        self.assertEqual(parse('\ufeffFirst line\r\nwrapped line\r\n \r\n\r\nSecond paragraph.\r\n'),
            ['First line\nwrapped line', 'Second paragraph.'])
        self.assertEqual(parse('One\n\nTwo\nThree', 'lines'), ['One','Two','Three'])
        self.assertEqual(parse('One prompt with normal spaces.'), ['One prompt with normal spaces.'])

    def test_empty_binary_invalid_separator_and_over_limit_rejected(self):
        for text, mode in [('', 'paragraphs'), ('\x00bad', 'lines'), ('�bad', 'paragraphs'),
            ('valid', 'space'), ('x'*(BASE_BUDGET+1), 'paragraphs'), ('\n\n'.join(['x']*2001), 'paragraphs')]:
            with self.subTest(mode=mode), self.assertRaises(DomainError): parse(text, mode)

    def test_invalid_request_leaves_database_unchanged(self):
        for changes in ({'prompts_text': 'x'*(BASE_BUDGET+1)}, {'seconds_per_scene': True},
            {'seconds_per_scene': float('nan')}, {'seconds_per_scene': 0}, {'story': ''}):
            with self.subTest(changes=changes), self.assertRaises(DomainError): self.project(**changes)
        self.assertEqual(self.service.db.one('SELECT count(*) AS n FROM projects')['n'], 0)
        self.assertEqual(self.service.db.one('SELECT count(*) AS n FROM scenes')['n'], 0)

    def test_failed_insert_rolls_back_project_and_all_scenes(self):
        with self.service.db.transaction() as db:
            db.execute("CREATE TRIGGER fail_second_scene BEFORE INSERT ON scenes WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception): self.project()
        self.assertEqual(self.service.db.one('SELECT count(*) AS n FROM projects')['n'], 0)
        self.assertEqual(self.service.db.one('SELECT count(*) AS n FROM scene_revisions')['n'], 0)

    def test_routes_create_and_edit_loaded_prompt(self):
        app = Application.__new__(Application)
        app.service = self.service
        project = app.dispatch('POST', '/api/projects/from-prompts', self.data)
        self.assertEqual(self.service.active_project_id, project['id'])
        scene = project['scenes'][0]
        result = app.dispatch('POST', '/api/scenes/'+scene['id']+'/edit',
            {'revision': 1, 'data': {'manual_prompt': 'Новый общий план корабля.'}})
        self.assertEqual(result['current_revision'], 2)
        self.assertEqual(result['data']['beat'], 'Новый общий план корабля.')
        exported = self.service.project_prompts(project['id'])
        self.assertEqual(exported['scenes'][0]['flow_prompt'], 'Новый общий план корабля.')
        with self.assertRaises(DomainError):
            self.service.edit_scene(scene['id'], {'manual_prompt': 'Старое изменение.'}, 1)

    def test_edit_preserves_import_source_and_does_not_invent_narration(self):
        project = self.project(); scene = project['scenes'][0]
        before = copy.deepcopy(scene['data'])
        result = self.service.edit_scene(scene['id'], {'manual_prompt': 'Корабль крупно.',
            'prompt_import': {'version': 'spoof'}, 'prompt_timing': {}}, 1)
        self.assertEqual(result['data']['prompt_import'], before['prompt_import'])
        self.assertEqual(result['data']['prompt_timing'], before['prompt_timing'])
        self.assertNotIn('source_quote', result['data'])
        with self.assertRaises(DomainError): self.service.edit_scene(scene['id'], {'manual_prompt': ''}, 2)

    def test_final_wire_guard_preserves_text_without_remote_rewrite(self):
        project = self.project(); scene = project['scenes'][0]
        snapshot = self.service.scene_snapshot(scene)
        self.assertEqual(snapshot['prompt'], scene['data']['manual_prompt'])
        with patch('urllib.request.urlopen', side_effect=AssertionError('No remote review')):
            self.assertTrue(check(self.service, snapshot, snapshot['prompt'])['allowed'])
            self.assertTrue(check(self.service, snapshot, snapshot['prompt']+RETRY)['allowed'])
            self.assertFalse(check(self.service, snapshot, 'Other prompt')['allowed'])
        self.assertFalse(snapshot['uninhabited'])
        self.assertEqual(snapshot['source_contract']['story_alignment'], 'not_aligned')

    def test_ordinary_projects_keep_existing_source_guard(self):
        project = self.service.create_project({'title': 'Normal', 'story': self.data['story']})
        detail = self.service.detail(project['id'])
        self.assertFalse(detail['preflight']['ok'])
        self.assertFalse(check(self.service, {'prompt': 'A ship.'}, 'A ship.')['allowed'])

    def test_changed_prompt_source_is_blocked(self):
        project = self.project(); scene = project['scenes'][0]
        for mutation in ('story', 'file'):
            changed = copy.deepcopy(project); changed_scene = copy.deepcopy(scene)
            if mutation == 'story': changed['story'] = 'Different source'
            else: changed_scene['data']['prompt_import']['file_hash'] = 'wrong'
            with self.assertRaises(DomainError): self.service.scene_snapshot(changed_scene, _project=changed)

    def test_batch_uses_imported_prompt_and_stays_queued_without_auto_start(self):
        project = self.project()
        result = self.service.batch(project['id'], 'import-test', [project['scenes'][1]['id']])
        generations = self.service.db.all('SELECT purpose,state,snapshot FROM generations WHERE batch_id=?', (result['id'],))
        self.assertEqual(len(generations), 1)
        self.assertEqual((generations[0]['purpose'], generations[0]['state']), ('scene', 'queued'))
        self.assertEqual(json.loads(generations[0]['snapshot'])['prompt'], parse(self.data['prompts_text'])[1])
        self.assertEqual(len(self.service.project_prompts(project['id'], scene_ids=[project['scenes'][1]['id']])['scenes']), 1)

    def test_montage_uses_requested_still_duration(self):
        project = self.project(seconds_per_scene=7.5)
        image = io.BytesIO(); Image.new('RGB', (1024,576), (25,40,60)).save(image, 'PNG')
        # Upload a local test fixture; no image generation service is invoked.
        self.service.upload_scene_image(project['scenes'][0]['id'], image.getvalue())
        editing = self.service.editing
        result = editing.sync(project['id'], {'expected_revision': 0})
        self.assertEqual(result['document']['clips'][0]['trim_out_ms'], 7500)
        from imagegen.editing import normalize
        self.assertEqual(normalize(result['document'])['duration_ms'], 7500)

    def test_flow_admission_accepts_exact_uploaded_prompt_and_rejects_changes(self):
        from imagegen.flow_admission import FlowAdmission
        project = self.project(); scene = project['scenes'][0]
        gate = FlowAdmission(self.service)
        prompt = scene['data']['manual_prompt']
        accepted = gate.claim(scene['id'], 'test-imported-prompt', 1, prompt)
        self.assertTrue(accepted['allowed'])
        self.assertFalse(gate.claim(scene['id'], 'test-altered-prompt', 1, 'Подменённое описание.')['allowed'])
        with gate.db() as db:
            self.assertEqual(db.execute('SELECT prompt FROM request_documents WHERE request_id=?',
                ('test-imported-prompt',)).fetchone()['prompt'], prompt)

    def test_imported_video_is_trimmed_to_plan_without_changing_ordinary_video(self):
        project = self.project(); editing = self.service.editing
        scene = copy.deepcopy(project['scenes'][0]); scene['video_asset_id'] = 'fixture-video'
        current = {'revision':0,'document':{'schema_version':1,'clips':[],'transitions':[],
            'audio_tracks':[],'source_audio':False,'export_settings':{'width':1920,'height':1080,'fps':24}}}
        with patch.object(editing, 'get', return_value=current), patch.object(editing, 'media', return_value=8000), \
                patch.object(editing, 'validate', side_effect=lambda pid,doc:doc), \
                patch.object(self.service, 'detail', return_value={'scenes':[scene]}):
            result = editing.sync(project['id'], {'expected_revision':0})
            self.assertEqual(result['document']['clips'][0]['trim_out_ms'], 5000)
            del scene['data']['prompt_timing']
            result = editing.sync(project['id'], {'expected_revision':0})
            self.assertEqual(result['document']['clips'][0]['trim_out_ms'], 8000)

    def test_supplied_aral_files_round_trip_all_269_prompts(self):
        folder = Path('C:/Users/Administrator/Downloads/Telegram Desktop')
        if not (folder/'aral_visual_prompts_5sec.txt').exists(): self.skipTest('User sample is local only')
        text = (folder/'aral_visual_prompts_5sec.txt').read_text(encoding='utf-8-sig')
        story = (folder/'aralskoe_more_chernovik.txt').read_text(encoding='utf-8-sig')
        project = self.project(story=story, prompts_text=text)
        self.assertEqual(len(project['scenes']), 269)
        self.assertEqual([s['flow_prompt'] for s in self.service.project_prompts(project['id'])['scenes']], parse(text))
        self.assertEqual(project['analysis_metadata']['prompt_import']['duration_seconds'], 1345)
        self.assertEqual(project['generations'], [])

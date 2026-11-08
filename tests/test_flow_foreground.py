import base64
import io
import json
import av
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageChops

from imagegen.domain import DomainError
from imagegen.export_worker import render
from imagegen.montage_foreground import ForegroundReader
from imagegen.parallax_layers import BadBackground, prepare_registered
from imagegen.server import Application
from imagegen.script_analysis.analyzer import AnalysisResult
from imagegen.script_analysis.schemas import Registry, Scene


def png(image):
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


class FlowForegroundTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Application(temp.name)
        self.service = self.app.service
        project = self.service.create_project({'title':'Жест', 'story':'Рука поднимает кольцо. Ночь над замком.',
                                               'style':'Cinematic', 'mode':'illustration'})
        self.pid = pid = project['id']
        self.path = f'/api/projects/{pid}/edit/flow-layers'
        quotes = ('Рука поднимает кольцо.', 'Ночь над замком.')
        self.service.import_analysis(pid, {'world':'Замок', 'characters':[], 'locations':[], 'props':[],
            'scenes':[{'beat':text, 'source_quote':text, 'visible_people':0, 'characters':[],
                       'props':[], 'wardrobe':{}, 'shot':'Medium shot', 'location':'Замок',
                       'time':'Night', 'emotion':'calm'} for text in quotes]}, project['version'])
        self.scenes = self.service.detail(pid)['scenes']
        size = (1024, 576)
        self.plate = Image.new('RGB', size, (21, 37, 56))
        ImageDraw.Draw(self.plate).rectangle((60, 100, 210, 449), fill=(47, 78, 111))
        self.keyed = Image.new('RGB', size, (0, 255, 0))
        source = self.plate.copy()
        for image in (source, self.keyed):
            draw = ImageDraw.Draw(image)
            draw.polygon([(706,310),(896,351),(1023,427),(1023,480),(816,435),(673,369)],
                         fill=(159, 85, 54))
            draw.ellipse((696, 322, 753, 368), outline=(232, 168, 56), width=7)
        self.source = self.service.upload_scene_image(self.scenes[0]['id'], png(source))['asset_id']
        self.service.upload_scene_image(self.scenes[1]['id'], png(self.plate))
        self.editor = self.service.editing
        self.app.dispatch('POST', '/api/projects/current', {'project_id':pid})

    def get(self):
        return self.app.dispatch('GET', self.path, {})

    def post(self, body):
        return self.app.dispatch('POST', self.path, body)

    def send(self, job, image, **overrides):
        return self.post({**job, 'action':'save', 'image':base64.b64encode(png(image)).decode(), **overrides})

    def first_jobs(self):
        return [job for job in self.get()['jobs'] if job['position'] == 1]

    def prepare(self):
        b, f = self.first_jobs()
        self.send(b, self.plate)
        self.send(f, self.keyed)

    def test_motion_is_applied_to_the_right_scene_only(self):
        b, f = self.first_jobs()
        self.assertEqual([b['role'],f['role']], ['background','foreground'])
        self.assertEqual(b['source'], self.source)
        self.assertEqual(b['scene_id'], self.scenes[0]['id'])
        with self.assertRaisesRegex(DomainError, 'Исходный кадр изменился'):
            self.send(b, self.plate, scene_revision=b['scene_revision']+1)
        original = self.send(b, self.plate)
        self.assertEqual(self.send(b, self.plate), {**original, 'reused':True})
        self.assertEqual([j['role'] for j in self.first_jobs()], ['foreground'])
        wrong = self.keyed.copy()
        ImageDraw.Draw(wrong).rectangle((630,310,1023,485), fill=(240,20,250))
        with self.assertRaisesRegex(DomainError, 'не совмещаются'):
            self.send(f, wrong)
        self.send(f, self.keyed)
        self.assertEqual([job['position'] for job in self.get()['jobs']], [2, 2])
        result = self.post({'action':'finish', 'expected_revision':0})
        self.assertTrue(result['applied'])
        self.assertEqual(result['paired'], 1)
        edit = self.editor.get(self.pid)
        moving, still = edit['timeline']['clips']
        self.assertEqual(moving['scene_id'], self.scenes[0]['id'])
        self.assertEqual(moving['foreground_effect']['mode'], 'parallax')
        self.assertNotIn('motion', moving['foreground_effect'])
        self.assertNotIn('foreground_effect', still)
        resolve = lambda aid: self.service.root/self.service.asset(aid)['path']
        reader = ForegroundReader(resolve(self.source), moving, resolve)
        try:
            start, peak, end = [reader.image(t, (1024,576)) for t in (0,2.5,4.95)]
        finally:
            reader.close()
        self.assertGreater(ImageChops.difference(peak, end).getbbox()[0], 600)
        self.assertGreater(ImageChops.difference(start, end).getbbox()[0], 600)
        self.assertGreater(ImageChops.difference(start, peak).getbbox()[0], 600)
        output = Path(self.service.root) / 'foreground-test.mp4'
        render(self.service, edit['document'], output, lambda *_: None, lambda: False, size=(640, 360))
        with av.open(str(output)) as movie:
            samples = [frame.to_image() for i, frame in enumerate(movie.decode(video=0)) if i in (0, 60, 115)]
        self.assertEqual(len(samples), 3)
        region = (0, 0, 290, 360)
        for frame in samples[1:]:
            self.assertLessEqual(max(high for _, high in ImageChops.difference(
                samples[0].crop(region), frame.crop(region)).getextrema()), 8)
        self.assertGreater(ImageChops.difference(samples[0], samples[-1]).crop((325, 110, 639, 250)).getextrema()[0][1], 40)
        self.assertEqual(self.post({'action':'finish', 'expected_revision':1})['revision'], 1)

    def test_imported_layers_use_the_same_translation(self):
        draft = self.editor.sync(self.pid, {'expected_revision':0})['document']
        self.editor.save(self.pid, {'document':draft, 'expected_revision':0})
        clip = draft['clips'][0]
        rgba = self.keyed.convert('RGBA')
        g = rgba.getchannel('G')
        rgba.putalpha(g.point(lambda green: 0 if green==255 else 255))
        effect = self.app.dispatch('POST', f'/api/projects/{self.pid}/edit/foreground-pair', {
            'clip_id':clip['edit_clip_id'], 'expected_revision':1,
            'background':base64.b64encode(png(self.plate)).decode(),
            'foreground':base64.b64encode(png(rgba)).decode(),
            'shift_x':-.03, 'shift_y':.01,
        })['effect']
        self.assertEqual(effect['mode'],'parallax')
        clip['foreground_effect']=effect
        timeline = self.editor.save(self.pid, {'document':draft, 'expected_revision':1})['timeline']
        resolve = lambda aid: self.service.root/self.service.asset(aid)['path']
        reader = ForegroundReader(resolve(self.source), timeline['clips'][0], resolve)
        try:
            start, middle, end = [reader.image(t,(1024,576)) for t in (0,2.5,4.95)]
            fixed_plate = reader.frame.copy()
        finally:
            reader.close()
        lefts = [ImageChops.difference(fixed_plate,frame).getbbox()[0] for frame in (start,middle,end)]
        self.assertGreater(lefts[0],lefts[1])
        self.assertGreater(lefts[1],lefts[2])
        self.assertAlmostEqual(lefts[0]-lefts[2],31,delta=3)

    def test_bad_background_can_be_replaced_without_publishing_a_partial_pair(self):
        bg, fg = self.first_jobs()
        with Image.open(self.service.root / self.service.asset(self.source)['path']) as still:
            self.send(bg, still)
        with self.assertRaises(BadBackground):
            self.send(fg, self.keyed)
        self.assertEqual([job['role'] for job in self.first_jobs()], ['background', 'foreground'])
        self.assertEqual(self.post({'action':'finish', 'expected_revision':0})['paired'], 0)
        self.send(bg, self.plate)
        self.send(fg, self.keyed)
        self.assertEqual(self.post({'action':'finish', 'expected_revision':1})['paired'], 1)

    def test_registered_pair_pins_visible_background_and_rejects_a_local_shift(self):
        rgba = self.keyed.convert('RGBA')
        rgba.putalpha(rgba.getchannel('G').point(lambda g: 0 if g == 255 else 255))
        with Image.open(self.service.root / self.service.asset(self.source)['path']) as still:
            changed = self.plate.copy()
            changed.paste((240, 200, 180), (100, 120, 160, 200))
            fixed, _ = prepare_registered(still, changed, rgba, .028, .01)
            self.assertEqual(fixed.getpixel((130, 150)), still.getpixel((130, 150)))
            changed.paste((240, 200, 180), (680, 300, 720, 342))
            with self.assertRaises(BadBackground):
                prepare_registered(still, changed, rgba, .028, .01)

    def test_existing_timeline_order_and_trims_stay_intact_and_conflicts_are_safe(self):
        draft = self.editor.sync(self.pid, {'expected_revision':0})['document']
        draft['clips'].reverse()
        draft['clips'][1]['trim_out_ms'] = 4200
        self.editor.save(self.pid, {'document':draft, 'expected_revision':0})
        self.prepare()
        self.assertFalse(self.post({'action':'finish', 'expected_revision':0})['applied'])
        self.assertEqual(self.editor.get(self.pid)['document'], draft)
        self.post({'action':'finish', 'expected_revision':1})
        clips = self.editor.get(self.pid)['document']['clips']
        self.assertEqual([clip['edit_clip_id'] for clip in clips],[clip['edit_clip_id'] for clip in draft['clips']])
        self.assertEqual(clips[1]['trim_out_ms'], 4200)
        self.assertIn('foreground_effect', clips[1])

    def test_command_checks_capability_and_selects_images_only(self):
        beat = {'project_id':self.pid, 'flow_project_id':'flow', 'adapter_instance_id':'adapter',
                'adapter_connected':True, 'images_only':True, 'staged_image_batch':True,
                'quota_admission':True}
        heartbeat = lambda **data:self.app.dispatch('POST', '/api/bridge/v1/heartbeat', {**beat, **data})
        start = lambda **data:self.app.dispatch('POST', '/api/bridge/v1/command',
            {'action':'start', 'project_id':self.pid, 'foreground_pairs':True, **data})
        heartbeat()
        with self.assertRaisesRegex(DomainError, r'0\.1\.92'):
            start()
        heartbeat(foreground_parallax=True)
        with self.assertRaises(DomainError):
            start(batch_stage='portraits', images_only=True)
        start(batch_stage='scenes', images_only=True)
        cmd = heartbeat(foreground_parallax=True)['command']
        self.assertTrue(cmd['foreground_pairs'])
        self.assertTrue(cmd['images_only'])
        self.assertEqual(cmd['batch_stage'], 'scenes')

    def test_project_preset_is_used_before_analysis_and_by_default_flow_start(self):
        story = 'Золотое кольцо лежит на камне. За камнем видны развалины. Туман закрывает холмы.'
        dispatch = self.app.dispatch
        project = dispatch('POST', '/api/projects', {
            'title': 'Кольцо', 'story': story, 'mode': 'illustration', 'flow_composition': 'foreground',
        })
        pid = project['id']
        route = f'/api/projects/{pid}'
        self.assertEqual(dispatch('GET', route, {})['flow_composition'], 'foreground')
        queued = dispatch('POST', route + '/analyse', {'target': 3})
        generation = self.service.db.one('SELECT * FROM generations WHERE id=?', (queued['generation_id'],))
        self.assertEqual(json.loads(generation['snapshot'])['project']['flow_composition'], 'foreground')
        registry = Registry(title='Кольцо', logline='', genre='', characters=[], locations=[], props=[])
        scene = Scene(index=0, heading='EXT. РАЗВАЛИНЫ - ДЕНЬ', location_id='', time_of_day='day',
                      character_ids=[], summary=story, mood='calm', duration_sec=12)
        with patch('imagegen.worker.deepseek_analyze', return_value=AnalysisResult(registry, [scene])):
            self.app.worker.prepare(generation)

        detail = dispatch('GET', route, {})
        self.assertTrue(detail['analysis_metadata']['diagnostics']['storyboard']['layered_composition'])
        self.assertEqual([s['data']['shot'] for s in detail['scenes']], ['wide shot', 'medium shot', 'medium close-up'])
        self.assertTrue(all(story[s['data']['source_start']:s['data']['source_end']] == s['data']['source_quote']
                            for s in detail['scenes']))
        prompts = dispatch('GET', route + '/prompts', {})['scenes']
        self.assertEqual(len(prompts), 3)
        self.assertTrue(all('TWO-PLANE COMPOSITION FOR PARALLAX' in s['flow_prompt'] for s in prompts))
        ordinary = dispatch('GET', f'/api/projects/{self.pid}/prompts', {})['scenes']
        self.assertTrue(all('TWO-PLANE COMPOSITION FOR PARALLAX' not in s['flow_prompt'] for s in ordinary))

        self.service.upload_scene_image(detail['scenes'][0]['id'], png(self.plate))
        self.assertEqual(len(dispatch('GET', route + '/edit/flow-layers', {})['jobs']), 2)
        heartbeat = {'project_id': pid, 'flow_project_id': 'flow', 'adapter_instance_id': 'adapter',
                     'adapter_connected': True, 'images_only': True, 'staged_image_batch': True,
                     'quota_admission': True, 'foreground_parallax': True}
        dispatch('POST', '/api/bridge/v1/heartbeat', heartbeat)
        dispatch('POST', '/api/bridge/v1/command', {'action': 'start', 'project_id': pid})
        command = dispatch('POST', '/api/bridge/v1/heartbeat', heartbeat)['command']
        self.assertTrue(command['foreground_pairs'])
        self.assertTrue(command['images_only'])

    def test_direct_layers_publish_one_composite_and_reuse_the_clean_plate(self):
        dispatch = self.app.dispatch
        story = 'Кольцо лежит на ближнем камне.'
        project = dispatch('POST', '/api/projects', {
            'title': 'Прямые слои', 'story': story, 'mode': 'illustration',
            'flow_composition': 'foreground', 'flow_layer_source': 'direct',
        })
        pid = project['id']
        self.service.import_analysis(pid, {'world':'Замок', 'characters':[], 'locations':[], 'props':[],
            'scenes':[{'beat':story, 'source_quote':story, 'visible_people':0, 'characters':[],
                       'props':[], 'wardrobe':{}, 'shot':'Medium shot', 'location':'Замок',
                       'time':'Night', 'emotion':'calm'}]}, project['version'])
        route = f'/api/projects/{pid}/edit/flow-layers'
        jobs = lambda: dispatch('GET', route, {})['jobs']
        post = lambda data: dispatch('POST', route, data)
        send = lambda job, image: post({**job, 'action':'save', 'image':base64.b64encode(png(image)).decode()})
        b, f = jobs()
        self.assertEqual([b['role'],f['role']], ['background','foreground'])
        self.assertIn('OUTPUT ONLY THE CLEAN FAR BACKGROUND', b['prompt'])
        self.assertIn('OUTPUT ONLY THE NEAR PLANE', f['prompt'])
        self.assertIsNone(f['source'])
        with self.assertRaises(DomainError):
            send(f, self.keyed)
        saved = send(b, self.plate)
        self.assertEqual(send(b, self.plate)['asset_id'], saved['asset_id'])
        self.assertFalse(self.service.scene(b['scene_id'])['active_generation_id'])
        f, = jobs()
        self.assertEqual(f['source'], saved['asset_id'])
        with self.assertRaises(DomainError):
            send(f, self.plate)
        self.assertEqual(jobs()[0]['source'], saved['asset_id'])
        with self.assertRaisesRegex(DomainError, 'Передний план слишком мал'):
            send(f, self.keyed)
        self.assertEqual(jobs()[0]['source'], saved['asset_id'])
        self.assertIn('Передний план слишком мал', jobs()[0]['last_error'])
        self.assertFalse(self.service.scene(b['scene_id'])['active_generation_id'])
        rgba = Image.new('RGBA', self.keyed.size)
        draw = ImageDraw.Draw(rgba)
        draw.polygon([(0,245),(460,205),(1023,245),(1023,575),(0,575)], fill=(159,85,54,255))
        draw.ellipse((696,322,753,368), outline=(232,168,56,255), width=7)
        with patch.object(self.app.worker, 'validate_external_scene_image') as validate:
            composite = send(f, rgba)
            self.assertEqual(validate.call_count, 1)
        self.assertEqual(send(f, rgba)['asset_id'], composite['asset_id'])
        self.assertEqual(jobs(), [])
        self.assertEqual(post({'action':'finish', 'expected_revision':0})['paired'], 1)
        edit = self.editor.get(pid)
        clip, = edit['timeline']['clips']
        effect = clip['foreground_effect']
        self.assertEqual(effect['layout'], 'parallax-v2')
        self.assertEqual(effect['source'], composite['asset_id'])
        resolve = lambda aid: self.service.root/self.service.asset(aid)['path']
        with Image.open(resolve(composite['asset_id'])) as still:
            self.assertEqual(still.getpixel((100, 130)), self.plate.getpixel((100, 130)))
            self.assertEqual(still.getpixel((730, 358)), self.keyed.getpixel((730, 358)))
        reader = ForegroundReader(resolve(composite['asset_id']), clip, resolve)
        try:
            a, b = (reader.image(t, (1024, 576)) for t in (0, 4.9))
        finally:
            reader.close()
        self.assertGreater(ImageChops.difference(a,b).getbbox()[1], 180)
        self.assertEqual(a.getpixel((100,130)), b.getpixel((100,130)))

        heartbeat = {'project_id':pid, 'flow_project_id':'flow', 'adapter_instance_id':'adapter',
                     'adapter_connected':True, 'images_only':True, 'staged_image_batch':True,
                     'quota_admission':True, 'foreground_parallax':True}
        dispatch('POST', '/api/bridge/v1/heartbeat', heartbeat)
        with self.assertRaisesRegex(DomainError, r'0\.1\.93'):
            dispatch('POST', '/api/bridge/v1/command', {'action':'start', 'project_id':pid})
        heartbeat['foreground_direct'] = True
        dispatch('POST', '/api/bridge/v1/heartbeat', heartbeat)
        dispatch('POST', '/api/bridge/v1/command', {'action':'start', 'project_id':pid})
        cmd = dispatch('POST', '/api/bridge/v1/heartbeat', heartbeat)['command']
        self.assertTrue(cmd['direct_layers'] and cmd['foreground_pairs'] and cmd['images_only'])

    def test_direct_prompt_separates_roles_and_duplicate_texture_is_rejected(self):
        from imagegen.flow_direct_layers import _reject_copied_detail, _role_prompts

        project = {'style':'Cinematic fantasy, softly lit'}
        scene = {'location':'долина',
                 'manual_prompt':'Ближний слой — ring on a low slate. Дальний слой — misty peaks.'}
        background, foreground = _role_prompts(project, scene, 'combined: ring on a slate in the mountains')
        self.assertIn('misty peaks', background)
        self.assertIn('ring on a low slate', foreground)
        # Both renders need the same spatial context, with a separate final
        # output instruction preventing near objects from leaking into the plate.
        self.assertEqual(background.split('OUTPUT ONLY')[0], foreground.split('OUTPUT ONLY')[0])
        self.assertIn('ring on a low slate', background)
        self.assertIn('misty peaks', foreground)
        self.assertIn('softly lit', background)
        self.assertIn('Remove every near subject and its support', background)
        plate = Image.effect_noise((256, 192), 30).convert('RGB')
        copy = plate.convert('RGBA')
        mask = Image.new('L', plate.size)
        ImageDraw.Draw(mask).rectangle((0, 100, 255, 191), fill=255)
        copy.putalpha(mask)
        with self.assertRaisesRegex(DomainError, 'повторяются одни и те же детали'):
            _reject_copied_detail(plate, copy)
        # Matching texture hidden inside a broad opaque plane is harmless.
        # Only the top edge can be revealed during this motion.
        hidden_copy = copy.copy()
        ImageDraw.Draw(hidden_copy).rectangle((0,100,255,114), fill=(181,91,40,255))
        _reject_copied_detail(plate, hidden_copy)
        different = Image.new('RGBA', plate.size, '#b57725')
        different.putalpha(mask)
        _reject_copied_detail(plate, different)

    def test_direct_near_plane_requires_broad_connected_ground(self):
        from imagegen.flow_direct_layers import _validate_near_plane
        size = (1024,576)
        supported = Image.new('RGBA', size)
        ImageDraw.Draw(supported).rectangle((0,220,1023,575), fill='white')
        _validate_near_plane(supported)

        # A complete character plus a broad ledge can cover less than half
        # the pixels while spanning most of the frame as one grounded scene.
        character = Image.new('RGBA', size)
        draw = ImageDraw.Draw(character)
        draw.rectangle((0,470,1023,575), fill='white')
        draw.rectangle((600,100,850,575), fill='white')
        _validate_near_plane(character)

        close = Image.new('RGBA', size)
        ImageDraw.Draw(close).rectangle((0,120,1023,575), fill='white')
        _validate_near_plane(close)
        arch = Image.new('RGBA', size)
        ImageDraw.Draw(arch).rectangle((0,80,1023,575), fill='white')
        _validate_near_plane(arch)

        strip = Image.new('RGBA', size)
        ImageDraw.Draw(strip).rectangle((0,420,1023,575), fill='white')
        with self.assertRaisesRegex(DomainError, 'слишком мал'):
            _validate_near_plane(strip)

        floating = Image.new('RGBA', size)
        draw = ImageDraw.Draw(floating)
        draw.rectangle((0,20,1023,340), fill='white')
        draw.rectangle((0,545,1023,575), fill='white')
        with self.assertRaisesRegex(DomainError, 'оторвана'):
            _validate_near_plane(floating)

        # Even a large island cannot become a supported foreground via a border.
        draw.rectangle((0,545,1023,574), fill=(0,0,0,0))
        with self.assertRaisesRegex(DomainError, 'нет широкой опоры'):
            _validate_near_plane(floating)

        obscured = Image.new('RGBA', size)
        ImageDraw.Draw(obscured).rectangle((0,40,1023,575), fill='white')
        with self.assertRaisesRegex(DomainError, 'закрывает дальнюю сцену'):
            _validate_near_plane(obscured)

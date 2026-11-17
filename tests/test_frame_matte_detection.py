import io
import shutil
import tempfile
import unittest
import numpy as np
from PIL import Image, ImageDraw

from imagegen.service import Service
from imagegen.worker import Worker, ContentRejectedError
from imagegen.domain import compile_flow_prompt, compile_prompt


def make_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


class TestFrameMatteDetection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = Service(self.temp_dir)
        self.worker = Worker(service=self.service)
        self.snapshot = {
            'profile': {'width': 1024, 'height': 576},
            'expected_people': 0
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_white_pillarbox_rejected(self):
        # 16:9 canvas (1024x576) with white vertical borders (100px on left & right)
        img = Image.new('RGB', (1024, 576), (255, 255, 255))
        np_scene = np.random.RandomState(42).randint(30, 150, (576, 824, 3), dtype=np.uint8)
        scene_img = Image.fromarray(np_scene)
        img.paste(scene_img, (100, 0))
        
        self.assertTrue(Worker._has_embedded_frame_or_matte(img))
        
        raw_bytes = make_png_bytes(img)
        with self.assertRaises(ContentRejectedError) as ctx:
            self.worker.validate_external_scene_image(self.snapshot, raw_bytes)
        self.assertEqual('Кадр содержит встроенные поля или рамку и не заполняет холст 16:9. Кадр не принят.', str(ctx.exception))

    def test_black_letterbox_rejected(self):
        # 16:9 canvas (1024x576) with black horizontal bars (50px on top & bottom)
        img = Image.new('RGB', (1024, 576), (0, 0, 0))
        np_scene = np.random.RandomState(42).randint(40, 200, (476, 1024, 3), dtype=np.uint8)
        scene_img = Image.fromarray(np_scene)
        img.paste(scene_img, (0, 50))
        
        self.assertTrue(Worker._has_embedded_frame_or_matte(img))
        
        raw_bytes = make_png_bytes(img)
        with self.assertRaises(ContentRejectedError) as ctx:
            self.worker.validate_external_scene_image(self.snapshot, raw_bytes)
        self.assertEqual('Кадр содержит встроенные поля или рамку и не заполняет холст 16:9. Кадр не принят.', str(ctx.exception))

    def test_rounded_white_matte_rejected(self):
        # 16:9 canvas with rounded rectangle scene on white background
        img = Image.new('RGB', (1024, 576), (255, 255, 255))
        np_scene = np.random.RandomState(42).randint(40, 160, (576, 1024, 3), dtype=np.uint8)
        scene_img = Image.fromarray(np_scene)
        mask = Image.new('L', (1024, 576), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle([40, 40, 984, 536], radius=40, fill=255)
        img.paste(scene_img, (0, 0), mask)
        
        self.assertTrue(Worker._has_embedded_frame_or_matte(img))
        
        raw_bytes = make_png_bytes(img)
        with self.assertRaises(ContentRejectedError) as ctx:
            self.worker.validate_external_scene_image(self.snapshot, raw_bytes)
        self.assertEqual('Кадр содержит встроенные поля или рамку и не заполняет холст 16:9. Кадр не принят.', str(ctx.exception))

    def test_all_around_white_frame_rejected(self):
        # 16:9 canvas with 20px white frame around entire image
        img = Image.new('RGB', (1024, 576), (255, 255, 255))
        np_scene = np.random.RandomState(42).randint(50, 180, (536, 984, 3), dtype=np.uint8)
        scene_img = Image.fromarray(np_scene)
        img.paste(scene_img, (20, 20))
        
        self.assertTrue(Worker._has_embedded_frame_or_matte(img))
        
        raw_bytes = make_png_bytes(img)
        with self.assertRaises(ContentRejectedError) as ctx:
            self.worker.validate_external_scene_image(self.snapshot, raw_bytes)
        self.assertEqual('Кадр содержит встроенные поля или рамку и не заполняет холст 16:9. Кадр не принят.', str(ctx.exception))

    def test_full_bleed_forest_accepted(self):
        # Natural forest scene filling full 1024x576 canvas
        np_scene = np.random.RandomState(42).randint(30, 180, (576, 1024, 3), dtype=np.uint8)
        img = Image.fromarray(np_scene)
        
        self.assertFalse(Worker._has_embedded_frame_or_matte(img))
        raw_bytes = make_png_bytes(img)
        # Should not raise ContentRejectedError
        self.worker.validate_external_scene_image(self.snapshot, raw_bytes)

    def test_natural_snow_scene_accepted(self):
        # Snow scene with white bottom half (snow texture) and blue sky top
        np_scene = np.zeros((576, 1024, 3), dtype=np.uint8)
        for y in range(250):
            np_scene[y, :, :] = [int(135 + y * 0.2), int(180 + y * 0.2), int(235 + y * 0.05)]
        noise = np.random.RandomState(42).randint(-15, 15, (326, 1024, 3))
        snow_base = np.clip(240 + noise, 0, 255).astype(np.uint8)
        np_scene[250:, :, :] = snow_base
        img = Image.fromarray(np_scene)
        
        self.assertFalse(Worker._has_embedded_frame_or_matte(img))
        raw_bytes = make_png_bytes(img)
        self.worker.validate_external_scene_image(self.snapshot, raw_bytes)

    def test_bright_interior_with_window_accepted(self):
        # Room with bright window and white wall
        np_scene = np.random.RandomState(42).randint(210, 250, (576, 1024, 3), dtype=np.uint8)
        np_scene[450:, :, :] = np.random.RandomState(43).randint(80, 120, (126, 1024, 3), dtype=np.uint8)
        np_scene[100:350, 200:500, :] = np.random.RandomState(44).randint(150, 255, (250, 300, 3), dtype=np.uint8)
        img = Image.fromarray(np_scene)
        
        self.assertFalse(Worker._has_embedded_frame_or_matte(img))
        raw_bytes = make_png_bytes(img)
        self.worker.validate_external_scene_image(self.snapshot, raw_bytes)

    def test_dummy_flat_image_accepted(self):
        # Flat test image used in test suites
        img = Image.new('RGB', (1024, 576), '#526880')
        self.assertFalse(Worker._has_embedded_frame_or_matte(img))
        raw_bytes = make_png_bytes(img)
        self.worker.validate_external_scene_image(self.snapshot, raw_bytes)

    def test_compile_flow_prompt_contains_full_bleed_instruction(self):
        project = {'style': 'Cinematic photorealism'}
        scene = {
            'beat': 'Daniel walks through the forest path',
            'location': 'Pine forest',
            'time': 'Morning',
            'shot': 'Wide shot',
            'visible_people': 1
        }
        character = {'name': 'Daniel', 'description': 'Forester in green jacket'}
        prompt = compile_flow_prompt(project, scene, characters=[character])
        self.assertIn('Full-bleed 16:9 image', prompt)
        self.assertIn('No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox', prompt)

    def test_compile_scene_prompt_contains_full_bleed_instruction(self):
        project = {'title': 'Test', 'world': 'Real', 'mode': 'characters', 'style': 'Cinematic photorealism'}
        scene = {
            'beat': 'Daniel walks through the forest path',
            'location': 'Pine forest',
            'time': 'Morning',
            'shot': 'Wide shot',
            'visible_people': 1
        }
        character = {'id': 'c1', 'name': 'Daniel', 'description': 'Forester in green jacket', 'locked': True, 'revision': 1, 'references': {'portrait': 'a1'}}
        prompt, _ = compile_prompt(project, scene, characters=[character])
        self.assertIn('Full-bleed 16:9 image', prompt)
        self.assertIn('No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox', prompt)


if __name__ == '__main__':
    unittest.main()

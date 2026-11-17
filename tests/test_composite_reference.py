import unittest
import os
import io
import json
import base64
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image

from imagegen.service import Service, build_composite_reference_sheet
from imagegen.server import Application


class TestCompositeReference(unittest.TestCase):
    def setUp(self):
        self.root = Path('data')
        self.service = Service(self.root)
        self.app = Application(self.root)

    def test_build_composite_sheet_2_images(self):
        im1 = Image.new('RGB', (100, 100), color=(255, 0, 0))
        im2 = Image.new('RGB', (100, 100), color=(0, 255, 0))
        sheet = build_composite_reference_sheet([
            (im1, "Character: @Alpha"),
            (im2, "Character: @Beta")
        ], target_width=800, target_height=450)
        self.assertEqual(sheet.size, (800, 450))

    def test_build_composite_sheet_3_images(self):
        im1 = Image.new('RGB', (200, 100), color=(255, 0, 0))
        im2 = Image.new('RGB', (100, 100), color=(0, 255, 0))
        im3 = Image.new('RGB', (100, 100), color=(0, 0, 255))
        sheet = build_composite_reference_sheet([
            (im1, "Location: Forest Glade"),
            (im2, "Character: @Ворон"),
            (im3, "Character: @Ворона")
        ], target_width=1536, target_height=864)
        self.assertEqual(sheet.size, (1536, 864))

    def test_build_composite_sheet_4_images(self):
        images = [(Image.new('RGB', (100, 100), color=(i * 50, 0, 0)), f"Ref {i}") for i in range(1, 5)]
        sheet = build_composite_reference_sheet(images, target_width=1536, target_height=864)
        self.assertEqual(sheet.size, (1536, 864))

    def test_build_composite_sheet_5_images(self):
        images = [(Image.new('RGB', (100, 100), color=(0, i * 50, 0)), f"Ref {i}") for i in range(1, 6)]
        sheet = build_composite_reference_sheet(images, target_width=1536, target_height=864)
        self.assertEqual(sheet.size, (1536, 864))

    def test_scene_composite_reference_single_ref_scene(self):
        # Scene 1 has only 1 reference (location)
        scene_1_id = '96632f02-f0e6-4288-b7a2-792337a5d8a7'
        res = self.service.scene_composite_reference(scene_1_id)
        self.assertEqual(res['has_composite'], False)
        self.assertEqual(res['count'], 1)

    def test_scene_composite_reference_multi_ref_scene_5(self):
        # Scene 5 has 3 references (1 location + 2 characters)
        scene_5_id = 'd77418db-8568-4e80-a102-8c229dfb7933'
        res = self.service.scene_composite_reference(scene_5_id)
        self.assertEqual(res['has_composite'], True)
        self.assertEqual(res['count'], 3)
        self.assertIn('data_url', res)
        self.assertTrue(res['data_url'].startswith('data:image/png;base64,'))
        self.assertEqual(len(res['names']), 3)
        self.assertIn('Location: Natural environment', res['names'][0])

    def test_server_dispatch_composite_reference_endpoint(self):
        scene_5_id = 'd77418db-8568-4e80-a102-8c229dfb7933'
        res = self.app.dispatch('GET', f'/api/scenes/{scene_5_id}/composite-reference', {})
        self.assertEqual(res['has_composite'], True)
        self.assertEqual(res['count'], 3)
        self.assertTrue(len(res['sha256']) > 10)


if __name__ == '__main__':
    unittest.main()

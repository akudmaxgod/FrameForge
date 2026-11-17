import io
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from imagegen.service import Service
from imagegen.server import Application


def create_dummy_png(color="blue", size=(768, 432)):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestPropsService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = Service(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_portrait_reference_preserves_object_proportions(self):
        from PIL import ImageChops, ImageDraw
        source = Image.new('RGB', (100, 200), 'black')
        ImageDraw.Draw(source).rectangle((40, 50, 59, 149), fill='white')
        buf = io.BytesIO()
        source.save(buf, format='PNG')
        result = Image.open(io.BytesIO(self.service._process_image_bytes(
            buf.getvalue(), (768, 432), preserve_aspect=True)))
        self.assertEqual(result.size, (768, 432))
        # Allow interpolation pixels while rejecting the old horizontal stretching.
        mask = result.convert('L').point(lambda value: 255 if value > 128 else 0)
        x1, y1, x2, y2 = mask.getbbox()
        self.assertAlmostEqual((x2 - x1) / (y2 - y1), 0.2, delta=0.02)
        self.assertGreater(x1, 200)
        self.assertLess(x2, 568)

    def test_props_db_and_service_lifecycle(self):
        # 1. Create project
        project = self.service.create_project({
            "title": "Knight and Shield",
            "story": "A brave knight carries an enchanted golden shield.",
            "style": "cinematic film",
            "idea": "Prop test"
        })
        project_id = project["id"]

        # 2. Import analysis with props
        analysis = {
            "world": "Medieval fantasy",
            "characters": [{"name": "Knight", "description": "Warrior in plate armor", "wardrobe": {}}],
            "locations": [{"name": "Castle", "description": "Stone castle courtyard"}],
            "props": [
                {
                    "name": "Golden Shield",
                    "description": "Round golden shield with lion emblem",
                    "importance": "key",
                    "first_appearance": 1,
                    "owner": "Knight",
                    "default_state": "held"
                }
            ],
            "scenes": [
                {
                    "beat": "Knight stands in courtyard holding golden shield",
                    "source_quote": "A brave knight carries an enchanted golden shield.",
                    "characters": ["Knight"],
                    "wardrobe": {},
                    "location": "Castle",
                    "location_ref": "Castle",
                    "time": "day",
                    "shot": "Medium",
                    "emotion": "heroic",
                    "continuity_before": "",
                    "continuity_after": "",
                    "visible_people": 1,
                    "props": [{"name": "Golden Shield", "state": "held", "holder": "Knight", "visible": True}]
                }
            ]
        }
        detailed = self.service.import_analysis(project_id, analysis, project["version"])
        self.assertEqual(len(detailed["props"]), 1)
        prop_item = detailed["props"][0]
        self.assertEqual(prop_item["name"], "Golden Shield")
        self.assertEqual(prop_item["description"], "Round golden shield with lion emblem")
        self.assertFalse(prop_item["locked"])

        # 3. Edit prop
        prop_id = prop_item["id"]
        updated_prop = self.service.edit_prop(prop_id, "Polished round golden shield with glowing lion emblem", prop_item["revision"])
        self.assertIn("glowing lion emblem", updated_prop["description"])

        # 4. Upload prop image reference
        png_bytes = create_dummy_png("gold")
        uploaded_prop = self.service.upload_prop_image(prop_id, png_bytes, lock=True)
        self.assertTrue(uploaded_prop["locked"])
        prop_ref = json.loads(uploaded_prop["references_json"])
        self.assertIn("prop", prop_ref)

        # 5. Check project_prompts returns prop information
        prompts_data = self.service.project_prompts(project_id)
        self.assertEqual(len(prompts_data["props"]), 1)
        self.assertEqual(len(prompts_data["locked_props"]), 1)
        self.assertEqual(prompts_data["locked_props"][0]["id"], prop_id)
        self.assertEqual(len(prompts_data["scenes"][0]["references"]), 1)
        self.assertEqual(prompts_data["scenes"][0]["references"][0]["type"], "prop")

        # 6. Unlock prop
        unlocked = self.service.unlock_prop(prop_id)
        self.assertFalse(unlocked["locked"])

    def test_props_api_dispatch(self):
        app = Application(self.temp_dir.name)
        project = app.service.create_project({
            "title": "API Prop Project",
            "story": "Hero finds magic ring.",
            "style": "cinematic film"
        })
        project_id = project["id"]
        analysis = {
            "world": "Fantasy",
            "characters": [{"name": "Hero", "description": "Young adventurer", "wardrobe": {}}],
            "locations": [{"name": "Cave", "description": "Dark stone cave"}],
            "props": [{"name": "Magic Ring", "description": "Glowing silver ring"}],
            "scenes": [
                {"beat": "Hero in cave finds magic ring", "source_quote": "Hero finds magic ring.", "characters": ["Hero"], "wardrobe": {}, "location": "Cave", "location_ref": "Cave", "time": "night", "shot": "Close-up", "emotion": "wonder", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": [{"name": "Magic Ring", "state": "held", "holder": "Hero", "visible": True}]}
            ]
        }
        app.dispatch("POST", f"/api/projects/{project_id}/import", {"analysis": analysis, "version": project["version"]})
        detail = app.dispatch("GET", f"/api/projects/{project_id}", None)
        prop_id = detail["props"][0]["id"]

        # Edit via API
        edit_res = app.dispatch("POST", f"/api/props/{prop_id}/edit", {"description": "Gleaming ruby ring", "revision": detail["props"][0]["revision"]})
        self.assertEqual(edit_res["description"], "Gleaming ruby ring")

        # Upload via API
        png_bytes = create_dummy_png("red")
        upload_res = app.dispatch("POST", f"/api/props/{prop_id}/upload", {"image": png_bytes, "lock": True})
        self.assertTrue(upload_res["locked"])

        # Unlock via API
        unlock_res = app.dispatch("POST", f"/api/props/{prop_id}/unlock", {})
        self.assertFalse(unlock_res["locked"])


if __name__ == "__main__":
    unittest.main()

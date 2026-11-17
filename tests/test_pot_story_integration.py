import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from PIL import Image

from imagegen.service import Service


def create_dummy_png(color="blue", size=(768, 432)):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestPotStoryIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = Service(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pot_story_service_end_to_end(self):
        """Local service integration benchmark for 'История с кастрюлей' (Задача 3).

        1. Story with 9 scenes:
           - Scenes 1-5: Forest / House entrance (No pot).
           - Scene 6: Finds pot and puts it on head (worn_on_head).
           - Scene 7: Continues walking with pot on head (worn_on_head).
           - Scene 8: Takes off pot and leaves it on table in House (placed).
           - Scene 9: Steps out into Street (pot stays in house, absent in scene 9).
        This test does not call the analyser or Google Flow. It verifies the
        normalized service contract, reference attachments and Flow prompts.

        2. Verify reference image attachments & flow prompts per scene.
        3. Verify export manifest & zip package contains props and reference assets.
        4. Verify manual scene editing of props state.
        """
        # 1. Create Project
        story_text = (
            "Герой идет по лесу. Ищет еду. Доходит до поляны. Видит дом. Заходит внутрь. "
            "Находит старую кастрюлю и надевает ее на голову. Идет дальше в кастрюле. "
            "Снял кастрюлю и оставил на столе. Вышел на улицу."
        )
        project = self.service.create_project({
            "title": "История с кастрюлей",
            "story": story_text,
            "style": "cinematic fairytale, high detail",
            "idea": "Benchmark continuity story for persistent props"
        })
        project_id = project["id"]

        # 2. Import Analysis with Props & Continuity
        analysis = {
            "world": "Mysterious dense forest and rustic wooden house",
            "characters": [
                {
                    "name": "Герой",
                    "description": "Brave wandering traveler in rustic tunic and cloak",
                    "wardrobe": {"default": "Traveler tunic, leather belt, sturdy boots"}
                }
            ],
            "locations": [
                {"name": "Лес", "description": "Lush green pine forest with winding pathways"},
                {"name": "Дом", "description": "Cozy wooden cabin interior with a sturdy wooden table"},
                {"name": "Улица", "description": "Village dirt road right outside the cabin"}
            ],
            "props": [
                {
                    "name": "Кастрюля",
                    "description": "Weathered vintage iron cooking pot with side handles",
                    "importance": "key",
                    "first_appearance": 6,
                    "owner": "Герой",
                    "default_state": "absent"
                }
            ],
            "scenes": [
                {"beat": "Герой идет по лесу", "source_quote": "Герой идет по лесу.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Ищет еду", "source_quote": "Ищет еду.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Medium", "emotion": "focused", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Доходит до поляны", "source_quote": "Доходит до поляны.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Видит дом", "source_quote": "Видит дом.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Medium", "emotion": "curiosity", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Заходит внутрь", "source_quote": "Заходит внутрь.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "curiosity", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Находит старую кастрюлю и надевает ее на голову", "source_quote": "Находит старую кастрюлю и надевает ее на голову.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Close-up", "emotion": "amusement", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Идет дальше в кастрюле", "source_quote": "Идет дальше в кастрюле.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "determination", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Снял кастрюлю и оставил на столе", "source_quote": "Снял кастрюлю и оставил на столе.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "relief", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Вышел на улицу", "source_quote": "Вышел на улицу.", "characters": ["Герой"], "wardrobe": {}, "location": "Улица", "location_ref": "Улица", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []}
            ]
        }

        detailed = self.service.import_analysis(project_id, analysis, project["version"])
        self.assertEqual(len(detailed["props"]), 1)
        prop_id = detailed["props"][0]["id"]
        char_id = detailed["characters"][0]["id"]
        loc_les_id = detailed["locations"][0]["id"]
        loc_dom_id = detailed["locations"][1]["id"]
        loc_ulica_id = detailed["locations"][2]["id"]
        scenes = detailed["scenes"]
        self.assertEqual(len(scenes), 9)

        # 3. Save references and lock them
        dummy_png = create_dummy_png("green")
        self.service.upload_character_portrait(char_id, dummy_png, lock=True)
        self.service.upload_location_image(loc_les_id, dummy_png, lock=True)
        self.service.upload_location_image(loc_dom_id, dummy_png, lock=True)
        self.service.upload_location_image(loc_ulica_id, dummy_png, lock=True)
        self.service.upload_prop_image(prop_id, dummy_png, lock=True)

        # 4. Check Prompts generation across all scenes
        prompts = self.service.project_prompts(project_id)
        self.assertEqual(len(prompts["props"]), 1)
        self.assertTrue(prompts["props"][0]["locked"])

        prompt_scenes = prompts["scenes"]
        self.assertEqual(len(prompt_scenes), 9)

        # Scenes 1-5: No pot in references, no pot prompt
        for i in range(5):
            scene_refs = prompt_scenes[i]["references"]
            prop_refs = [r for r in scene_refs if r.get("type") == "prop"]
            self.assertEqual(len(prop_refs), 0, f"Scene {i+1} must not have prop reference")
            self.assertNotIn("Кастрюля", prompt_scenes[i]["flow_prompt"])

        # Scene 6: Pot worn on head
        s6_refs = prompt_scenes[5]["references"]
        s6_prop_refs = [r for r in s6_refs if r.get("type") == "prop"]
        self.assertEqual(len(s6_prop_refs), 1)
        self.assertEqual(s6_prop_refs[0]["id"], prop_id)
        self.assertIn("Кастрюля", prompt_scenes[5]["flow_prompt"])
        self.assertIn("worn securely on @Герой's head", prompt_scenes[5]["flow_prompt"])

        # Scene 7: Continuity continues worn on head
        s7_refs = prompt_scenes[6]["references"]
        s7_prop_refs = [r for r in s7_refs if r.get("type") == "prop"]
        self.assertEqual(len(s7_prop_refs), 1)
        self.assertEqual(s7_prop_refs[0]["id"], prop_id)
        self.assertIn("worn securely on @Герой's head", prompt_scenes[6]["flow_prompt"])

        # Scene 8: Placed on table in House
        s8_refs = prompt_scenes[7]["references"]
        s8_prop_refs = [r for r in s8_refs if r.get("type") == "prop"]
        self.assertEqual(len(s8_prop_refs), 1)
        self.assertEqual(s8_prop_refs[0]["id"], prop_id)
        self.assertIn("placed in the setting", prompt_scenes[7]["flow_prompt"])

        # Scene 9: Outside on Street - pot stays in House, absent on street
        s9_refs = prompt_scenes[8]["references"]
        s9_prop_refs = [r for r in s9_refs if r.get("type") == "prop"]
        self.assertEqual(len(s9_prop_refs), 0, "Scene 9 must not have pot reference because pot was left in House")
        self.assertNotIn("Кастрюля", prompt_scenes[8]["flow_prompt"])

        # Upload scene images so export succeeds
        colors = ["red", "blue", "yellow", "cyan", "magenta", "orange", "purple", "brown", "pink"]
        for idx, sc in enumerate(scenes):
            scene_img = create_dummy_png(colors[idx])
            self.service.upload_scene_image(sc["id"], scene_img)

        # 5. Verify Export Archive contains Props manifest and Prop assets
        zip_bytes = self.service.export(project_id)
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            file_names = zf.namelist()
            self.assertIn("manifest.json", file_names)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertIn("props", manifest)
            self.assertEqual(len(manifest["props"]), 1)
            self.assertEqual(manifest["props"][0]["name"], "Кастрюля")

            # Check that prop reference file exists in zip
            prop_obj = self.service.prop(prop_id)
            prop_ref_asset_id = json.loads(prop_obj["references_json"])["prop"]
            self.assertIn(f"references/{prop_ref_asset_id}.png", file_names)

        # 6. Verify Manual Scene Edit: change prop in Scene 7 to 'held'
        s7 = self.service.scene(scenes[6]["id"])
        edited = self.service.edit_scene(s7["id"], {
            "beat": "Идет дальше в кастрюле",
            "shot": "Close-up",
            "manual_prompt": "",
            "characters": ["Герой"],
            "location": "Дом",
            "props": [
                {"name": "Кастрюля", "state": "held", "holder": "Герой", "visible": True}
            ]
        }, s7["current_revision"])
        self.assertEqual(edited["data"]["shot"], "Close-up")
        self.assertEqual(len(edited["data"]["props"]), 1)
        self.assertEqual(edited["data"]["props"][0]["state"], "held")

        # Re-fetch prompts and verify updated state in prompt
        updated_prompts = self.service.project_prompts(project_id)
        self.assertIn("held by @Герой", updated_prompts["scenes"][6]["flow_prompt"])


if __name__ == "__main__":
    unittest.main()

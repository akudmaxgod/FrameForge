import unittest
from imagegen.domain import normalize_prop_continuity, validate_analysis, compile_prompt, compile_flow_prompt
from imagegen.providers import normalize_analysis


class TestPropsContinuity(unittest.TestCase):
    def test_normalizer_recovers_pot_when_model_omits_props(self):
        data = {
            "world": "Forest and house",
            "characters": [{"name": "Герой", "description": "Traveler", "wardrobe": {}}],
            "locations": [{"name": "Дом", "description": "Old house"}],
            "props": [],
            "scenes": [
                {"beat": "Герой идет по лесу", "source_quote": "Герой идет по лесу.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Дом", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "He finds an old metal pot, lifts it and places it on his head.", "source_quote": "Герой находит старую металлическую кастрюлю, берет ее и надевает на голову.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Close-up", "emotion": "surprise", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "With the pot on his head, he feels strong.", "source_quote": "В кастрюле на голове он чувствует силу.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "strong", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
            ],
        }
        result = normalize_analysis(data)
        self.assertEqual(["Кастрюля"], [item["name"] for item in result["props"]])
        self.assertEqual(2, result["props"][0]["first_appearance"])
        self.assertEqual("worn_on_head", result["scenes"][1]["props"][0]["state"])
        self.assertEqual("worn_on_head", result["scenes"][2]["props"][0]["state"])

    def test_pot_story_continuity_benchmark(self):
        """Benchmark: 'История с кастрюлей'.
        Scenes 1-5: Pot absent.
        Scene 6: Finds pot and puts it on head -> state: worn_on_head, holder: Hero, visible: True.
        Scene 7+: Pot stays on head (continuity: continues).
        Scene with 'снял кастрюлю и оставил на столе' -> placed at location, not following hero to next location.
        """
        story = "Герой идет по лесу. Ищет еду. Доходит до поляны. Видит дом. Заходит внутрь. Находит старую кастрюлю и надевает ее на голову. Идет дальше в кастрюле. Снял кастрюлю и оставил на столе. Вышел на улицу."
        data = {
            "world": "Fantasy realm",
            "characters": [{"name": "Герой", "description": "Adult male adventurer in his 20s", "wardrobe": {}}],
            "locations": [
                {"name": "Лес", "description": "Dense forest"},
                {"name": "Дом", "description": "Wooden house interior"},
                {"name": "Улица", "description": "Outside the house"}
            ],
            "props": [
                {
                    "name": "Кастрюля",
                    "description": "Old metal cooking pot",
                    "first_appearance": 6,
                    "owner": "Герой",
                    "default_state": "absent"
                }
            ],
            "scenes": [
                {"beat": "Герой идет по лесу", "source_quote": "Герой идет по лесу.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Ищет еду", "source_quote": "Ищет еду.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Medium", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Доходит до поляны", "source_quote": "Доходит до поляны.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Видит дом", "source_quote": "Видит дом.", "characters": ["Герой"], "wardrobe": {}, "location": "Лес", "location_ref": "Лес", "time": "day", "shot": "Medium", "emotion": "curiosity", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Заходит внутрь", "source_quote": "Заходит внутрь.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "curiosity", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Находит старую кастрюлю и надевает ее на голову", "source_quote": "Находит старую кастрюлю и надевает ее на голову.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Close-up", "emotion": "surprise", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Идет дальше в кастрюле", "source_quote": "Идет дальше в кастрюле.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "determination", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Снял кастрюлю и оставил на столе", "source_quote": "Снял кастрюлю и оставил на столе.", "characters": ["Герой"], "wardrobe": {}, "location": "Дом", "location_ref": "Дом", "time": "day", "shot": "Medium", "emotion": "relief", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
                {"beat": "Вышел на улицу", "source_quote": "Вышел на улицу.", "characters": ["Герой"], "wardrobe": {}, "location": "Улица", "location_ref": "Улица", "time": "day", "shot": "Wide", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1, "props": []},
            ]
        }

        normalized = normalize_analysis(data)
        scenes = normalized["scenes"]

        # Scenes 1-5: No pot props visible
        for i in range(5):
            self.assertEqual(len(scenes[i]["props"]), 0, f"Scene {i+1} should have no active props")

        # Scene 6: Pot worn on head
        s6_props = scenes[5]["props"]
        self.assertEqual(len(s6_props), 1)
        self.assertEqual(s6_props[0]["name"], "Кастрюля")
        self.assertEqual(s6_props[0]["state"], "worn_on_head")
        self.assertEqual(s6_props[0]["holder"], "Герой")
        self.assertTrue(s6_props[0]["visible"])

        # Scene 7: Pot continues on head automatically
        s7_props = scenes[6]["props"]
        self.assertEqual(len(s7_props), 1)
        self.assertEqual(s7_props[0]["state"], "worn_on_head")
        self.assertEqual(s7_props[0]["continuity"], "continues")

        # Scene 8: Pot removed and left on table in house
        s8_props = scenes[7]["props"]
        self.assertEqual(len(s8_props), 1)
        self.assertEqual(s8_props[0]["state"], "placed")

        # Scene 9: Outside on street -> pot was left in house, so it is absent in street
        s9_props = scenes[8]["props"]
        self.assertEqual(len(s9_props), 0, "Pot placed in house should not appear outside on street")

    def test_backward_compatibility_without_props(self):
        """Legacy analyses without 'props' should pass validation and normalize cleanly."""
        data = {
            "world": "Sci-fi world",
            "characters": [{"name": "Pilot", "description": "Space pilot in uniform", "wardrobe": {}}],
            "locations": [{"name": "Cockpit", "description": "Spaceship cockpit"}],
            "scenes": [
                {"beat": "Pilot checks controls", "source_quote": "Pilot checks controls.", "characters": ["Pilot"], "wardrobe": {}, "location": "Cockpit", "location_ref": "Cockpit", "time": "night", "shot": "Medium", "emotion": "calm", "continuity_before": "", "continuity_after": "", "visible_people": 1}
            ]
        }
        normalized = normalize_analysis(data)
        validated = validate_analysis(normalized, "Pilot checks controls.")
        self.assertEqual(validated["props"], [])
        self.assertEqual(validated["scenes"][0]["props"], [])

    def test_prompt_compilation_with_props(self):
        """Test prompt compiler formats props and attaches reference markers."""
        scene = {
            "beat": "Alex puts on the iron helmet",
            "source_quote": "Alex puts on the iron helmet.",
            "characters": ["Alex"],
            "wardrobe": {},
            "location": "Armory",
            "location_ref": "Armory",
            "time": "morning",
            "shot": "Close-up",
            "emotion": "focus",
            "continuity_before": "",
            "continuity_after": "",
            "visible_people": 1,
            "props": [{"name": "Iron Helmet", "state": "worn_on_head", "holder": "Alex", "visible": True}]
        }
        characters = [{"name": "Alex", "description": "Male warrior 30 yo"}]
        locations = [{"name": "Armory", "description": "Stone armory with weapon racks"}]
        props = [{"name": "Iron Helmet", "description": "Sturdy iron helmet with visor"}]

        project = {"mode": "characters", "style": "cinematic film", "world": "medieval"}
        prompt, _ = compile_prompt(project, scene, characters=characters, locations=locations, props=props, allow_unlocked=True)
        self.assertIn("Iron Helmet", prompt)
        self.assertIn("worn securely on @Alex's head", prompt)

        flow_prompt = compile_flow_prompt(project, scene, characters=characters, locations=locations, props=props)
        self.assertIn("Iron Helmet", flow_prompt)


if __name__ == "__main__":
    unittest.main()

"""Single-scenario channel acceptance requested for Stage 10."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from imagegen.analyzer_v3.models import DocumentIR
from imagegen.analyzer_v3.planner_models import VisualEventPlan
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service
from imagegen.domain import validate_project_preflight
from imagegen.service import Service


FIXTURE = Path(__file__).parent / "fixtures" / "forgotten_cities_stage10_single.txt"


class TestForgottenCitiesStage10SingleScenario(unittest.TestCase):
    def test_one_new_long_scenario_meets_channel_acceptance_contract(self):
        story = FIXTURE.read_text(encoding="utf-8")
        self.assertGreater(len(story), 5_000)
        with tempfile.TemporaryDirectory(prefix="ff_fc_stage10_") as root:
            service = Service(root)
            channel = service.create_channel({
                "name": "Забытые города — приёмка",
                "profile_type": "forgotten_cities",
            })
            project = service.create_project({
                "title": "Кара-Тепе — приёмочный выпуск",
                "story": story,
                "style": "documentary",
                "mode": "objects",
                "channel_profile_id": channel["id"],
            })
            result = service.analyze_v3(
                story,
                settings={"forgotten_cities_primary_city": "Кара-Тепе"},
                target_budget=18,
                story_id=project["id"],
            )

            document = result["document_ir"]
            metadata = document["metadata"]["forgotten_cities"]
            sequences = metadata["sequences"]
            contexts = metadata["event_contexts"]
            geography = metadata["geography_mentions"]
            anchors = metadata["anchors"]
            exported = StoryAnalyzerV3Service.export_to_app_analysis(
                DocumentIR.from_dict(document),
                VisualEventPlan.from_dict(result["visual_event_plan"]),
                story,
                mode="objects",
                profile_type="forgotten_cities",
            )
            scenes = exported["scenes"]

            self.assertGreaterEqual(len(sequences), 12)
            self.assertLessEqual(len(sequences), 20)
            self.assertEqual(metadata["preflight"]["status"], "PASS")
            self.assertEqual(metadata["city_bible"]["canonical_name"], "Кара-Тепе")
            self.assertTrue(metadata["city_bible"]["architecture_profile"])
            self.assertTrue(metadata["city_bible"]["material_palette"])

            story_length = len(story)
            for sequence in sequences:
                span = sequence["source_span"]
                self.assertGreaterEqual(span["start"], 0)
                self.assertLessEqual(span["end"], story_length)
                self.assertEqual(story[span["start"]:span["end"]], span["text"])

            forbidden_locations = {"волков", "бадахшан", "ур", "казахстан"}
            exported_locations = {item["name"].casefold() for item in exported["locations"]}
            self.assertTrue(forbidden_locations.isdisjoint(exported_locations))
            self.assertFalse(any(
                item["geography_type"] == "RESEARCHER_NAME" and item["text"].casefold() in exported_locations
                for item in geography
            ))

            layers = {item["time_layer"] for item in contexts}
            self.assertTrue({"modern_ruins", "historical_reconstruction", "excavation", "museum_display", "map"} <= layers)
            hypotheses = [item for item in contexts if item["evidence_level"] == "illustrative_hypothesis"]
            self.assertTrue(hypotheses)
            artifact_anchors = [item for item in anchors if item["anchor_type"] == "artifact"]
            self.assertGreaterEqual(len(artifact_anchors), 3)
            self.assertTrue(all(item["description"] and item["evidence_spans"] for item in artifact_anchors))

            self.assertTrue(scenes)
            self.assertTrue(all(scene["source_quote"] for scene in scenes))
            self.assertTrue(all(scene["time_layer"] for scene in scenes))
            self.assertTrue(all(scene["forgotten_cities_prompt_contract"] for scene in scenes))
            self.assertTrue(all(
                all(label in scene["forgotten_cities_prompt_contract"]["compiled_prompt"] for label in (
                    "STYLE CONTRACT:", "CITY CONTRACT:", "TIME LAYER:", "SHOT CONTRACT:", "NEGATIVE CONTRACT:",
                )) for scene in scenes
            ))
            self.assertEqual(exported["characters"], [])
            project_preflight = validate_project_preflight({
                "story": story,
                "characters": exported["characters"],
                "locations": exported["locations"],
                "props": exported["props"],
                "scenes": [
                    {"id": f"scene_{index}", "position": index, "data": scene}
                    for index, scene in enumerate(scenes, 1)
                ],
            }, story)
            self.assertFalse(any(
                item["code"] == "composition_contradiction"
                for item in project_preflight["errors"]
            ))


if __name__ == "__main__":
    unittest.main()

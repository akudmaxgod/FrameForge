import io
import unittest
from unittest.mock import patch

from PIL import Image
from imagegen.domain import compile_prompt
from imagegen.worker import Worker
from tests import test_veo_animation as fixtures


class TypedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.worker = Worker(self.fixture.service, runtime=object())

    def test_human_face_gate_excludes_animal_and_creature_references(self):
        snapshot = {"references": [
            {"role": "portrait", "asset_id": "hero", "entity_type": "human"},
            {"role": "portrait", "asset_id": "wolf", "entity_type": "animal"},
            {"role": "portrait", "asset_id": "dragon", "entity_type": "creature"},
            {"role": "environment", "asset_id": "place"},
        ]}
        self.assertEqual(self.worker._human_portrait_references(snapshot), ["hero"])

    def test_legacy_reference_uses_registry_type(self):
        character = {"id": "pet", "cast_id": "cast-pet", "name": "Companion",
                     "description": "White coat.", "project_id": "p"}
        with (
            patch.object(self.worker.service, "character", return_value=character),
            patch.object(self.worker.service, "project", return_value={
                 "cast_bible": {"characters": [{"cast_id": "cast-pet", "entity_type": "animal"}]},
            }),
        ):
            self.assertEqual(self.worker._human_portrait_references({
                "references": [{"role": "portrait", "asset_id": "pet-image", "character_id": "pet"}],
            }), [])

    def test_mixed_scene_compares_one_human_and_two_human_scene_compares_both(self):
        image = Image.new("RGB", (1024, 576), "grey")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        snapshot = {
            "profile": {"width": 1024, "height": 576}, "expected_people": 1, "style": "Cinematic",
            "references": [
                {"role": "portrait", "asset_id": "hero", "entity_type": "human"},
                {"role": "portrait", "asset_id": "pet", "entity_type": "animal"},
            ],
        }
        with (
            patch.object(self.worker, "_has_panel_grid", return_value=False),
            patch.object(self.worker, "_validate_matte"),
            patch.object(self.worker, "_has_embedded_text", return_value=False),
            patch.object(self.worker, "_detected_face_count", return_value=1),
            patch.object(self.worker, "_detected_person_count", return_value=1),
            patch.object(self.worker, "_detected_hand_count", return_value=2),
            patch.object(self.worker, "_identity_face_too_small", return_value=False),
            patch.object(self.worker, "_identity_similarity", return_value=.9) as single,
            patch.object(self.worker, "_identity_assignment_similarity", return_value=.9) as multiple,
        ):
            self.worker.validate_external_scene_image(snapshot, stream.getvalue())
            self.worker._validate_generated_composition({"purpose": "scene"}, snapshot, image)
            self.assertEqual(single.call_count, 2)
            multiple.assert_not_called()
            snapshot["references"][1]["entity_type"] = "human"
            self.worker.validate_external_scene_image(snapshot, stream.getvalue())
            self.assertEqual(multiple.call_args.args[1], ["hero", "pet"])

    def test_nonhuman_reference_retains_anatomy_instead_of_human_wardrobe(self):
        creature = {"id": "creature", "name": "Companion", "description": "Winged creature.",
                    "entity_type": "creature", "identity_scope": "individual", "locked": True,
                    "revision": 1, "references": {"portrait": "creature-image"}}
        scene = {"beat": "The creature rests.", "source_quote": "The creature rests.",
                 "characters": ["Companion"], "visible_people": 0}
        prompt, refs = compile_prompt({"mode": "dynamic", "style": "Cinematic"}, scene, [creature])
        self.assertEqual(refs[0]["entity_type"], "creature")
        self.assertIn("Preserve this non-human subject's species", prompt)
        self.assertNotIn("exact same person", prompt)

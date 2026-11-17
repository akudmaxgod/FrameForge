import io
import unittest
from unittest.mock import patch

from PIL import Image
from imagegen.worker import Worker, ContentRejectedError
from tests import test_veo_animation as fixtures


class GroupPeopleReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.pid = self.fixture._create_sample_project(1)
        self.worker = Worker(self.service, runtime=object())
        self.image = Image.new("RGB", (1024, 576), "grey")
        out = io.BytesIO()
        self.image.save(out, format="PNG")
        self.data = out.getvalue()
        self.snapshot = {
            "profile": {"width": 1024, "height": 576},
            "expected_people": 1, "people_count_unresolved": True,
            "style": "Cinematic", "references": [{"role": "portrait", "asset_id": "hero"}],
        }
        for name, result in (
            ("_has_panel_grid", False), ("_has_embedded_text", False), ("_validate_matte", None),
            ("_detected_face_count", 10), ("_detected_person_count", 12), ("_detected_hand_count", 20),
            ("_identity_similarity", .9), ("_identity_face_too_small", False),
        ):
            self.enterContext(patch.object(self.worker, name, return_value=result))

    def test_named_person_and_group_do_not_establish_exact_total(self):
        scene = self.service.detail(self.pid)["scenes"][0]
        scene["data"].update({
            "source_groups": ["Residents"],
            "source_subjects": [{
                "name": "Residents", "entity_type": "human", "identity_scope": "collective",
                "description": "Residents in the village.", "portrait_required": False,
            }],
        })
        snapshot = self.service.scene_snapshot(scene, allow_unlocked=True)
        self.assertTrue(snapshot["people_count_unresolved"])
        self.assertEqual(snapshot["expected_people"], 1)
        self.assertFalse(snapshot["uninhabited"])

    def test_open_group_accepts_background_people_and_retains_identity_check(self):
        self.worker.validate_external_scene_image(self.snapshot, self.data)
        self.worker._identity_similarity.assert_called_once()
        self.worker._validate_generated_composition({"purpose": "scene"}, self.snapshot, self.image)
        self.assertEqual(self.worker._identity_similarity.call_count, 2)
        self.worker._identity_similarity.return_value = .01
        self.worker.validate_external_scene_image(self.snapshot, self.data)
        self.assertEqual(self.snapshot['identity_review']['status'], 'needs_review')

    def test_exact_scene_preserves_unconfirmed_excess_as_review_evidence(self):
        self.snapshot["people_count_unresolved"] = False
        self.worker.validate_external_scene_image(self.snapshot, self.data)
        self.assertEqual(self.snapshot['composition_review']['status'], 'needs_review')
        self.worker._validate_generated_composition({"purpose": "scene"}, self.snapshot, self.image)
        self.assertEqual(self.snapshot['composition_review']['status'], 'needs_review')
        self.assertFalse(self.snapshot['composition_review']['confirmed_mismatch'])

    def test_marker_group_does_not_invent_exact_head_count(self):
        from imagegen.portrait_contract import marker_scene_review
        self.snapshot["style"] = "Minimalist Marker Explainer"
        with patch("imagegen.portrait_contract.marker_head_count", return_value=8):
            self.assertTrue(marker_scene_review(self.snapshot, self.image))
            self.snapshot["people_count_unresolved"] = False
            self.assertTrue(marker_scene_review(self.snapshot, self.image))
            self.assertEqual(self.snapshot['composition_review']['status'], 'needs_review')

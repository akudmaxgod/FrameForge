import unittest

from imagegen.domain import DomainError
from tests.test_portrait_retry import drawing, png
from tests import test_veo_animation as fixtures


class FlowPortraitStageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestVeoAnimationPlan()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.service = self.fixture.service = self.app.service
        self.pid = self.fixture._create_sample_project(2)
        self.cid = self.service.detail(self.pid)["characters"][0]["id"]
        self.app.dispatch("POST", "/api/projects/current", {"project_id": self.pid})
        self.heartbeat()

    def heartbeat(self, **changes):
        return self.app.dispatch("POST", "/api/bridge/v1/heartbeat", {
            "project_id": self.pid, "flow_project_id": "flow", "adapter_instance_id": "adapter",
            "adapter_connected": True, "character_casting": True, "images_only": True,
            "staged_image_batch": True, "quota_admission": True, **changes,
        })

    def start(self, stage):
        return self.app.dispatch("POST", "/api/bridge/v1/command", {
            "action": "start", "project_id": self.pid, "batch_stage": stage, "images_only": True,
        })

    def test_portrait_upload_and_manifest_leave_reference_unlocked_until_review(self):
        command = self.start("portraits")
        self.assertEqual(self.heartbeat()["command"]["batch_stage"], "portraits")
        self.heartbeat(ack_command_id=command["command_id"])
        path = f"/api/projects/{self.pid}/prompts"
        manifest = self.app.dispatch("GET", path, None, {"portraits_only": ["1"]}, client="google-flow")
        self.assertEqual(manifest["scenes"], [])
        self.assertEqual([item["id"] for item in manifest["characters"]], [self.cid])
        references = self.app.dispatch("GET", path, None, {"references_only": ["1"]}, client="google-flow")
        self.assertEqual(manifest, references)
        generation = self.service.cast(self.cid)["generation_ids"][0]
        self.service.claim_character_cast(self.cid, generation, "portrait-request")
        portrait = self.service.upload_character_portrait(
            self.cid, png(drawing()), generation_id=generation, request_id="portrait-request",
        )
        manifest = self.app.dispatch("GET", path, None, {"portraits_only": ["1"]}, client="google-flow")
        self.assertEqual(manifest["characters"][0]["variants_count"], 1)
        self.app.dispatch("GET", path, None, client="google-flow")
        self.assertFalse(self.service.character(self.cid)["locked"])
        with self.assertRaisesRegex(DomainError, "закрепите обязательные портреты"):
            self.start("scenes")
        self.service.lock(self.cid, {"portrait": portrait["asset_id"]}, 1, {"one_person_confirmed": True})
        self.start("scenes")
        self.assertEqual(self.heartbeat()["command"]["batch_stage"], "scenes")

    def test_old_bridge_cannot_receive_a_stage_it_does_not_understand(self):
        self.heartbeat(staged_image_batch=False)
        with self.assertRaisesRegex(DomainError, r"0\.1\.82"):
            self.start("portraits")
        self.assertIsNone(self.app.flow_bridge_v1["command"])

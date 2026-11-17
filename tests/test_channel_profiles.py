import os
import io
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imagegen.db import Database, uid, now
from imagegen.domain import (
    DomainError,
    GenerationReferenceBundle,
    compile_prompt,
    compile_flow_prompt,
    compile_channel_style_contract,
    STICKMAN_DEFAULT_VERSION,
    STICKMAN_DEFAULT_CHARACTER,
    STICKMAN_DEFAULT_CHARACTER_VERSION,
)
from imagegen.service import Service
from imagegen.server import Application


class TestChannelProfiles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ff_test_channel_")
        self.root = Path(self.temp_dir)
        self.service = Service(self.root)
        self.app = Application(self.root)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_schema_and_legacy_project_compatibility(self):
        """Ensure database tables exist and legacy projects without channels operate normally."""
        # Check tables
        tables = [r["name"] for r in self.service.db.all("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("channel_profiles", tables)
        self.assertIn("channel_profile_versions", tables)
        self.assertIn("channel_characters", tables)
        self.assertIn("channel_character_versions", tables)
        self.assertIn("channel_reference_assets", tables)

        # Create a legacy project without channel profile
        proj = self.service.create_project({
            "title": "Legacy Project",
            "story": "A man walks down the street. The sun sets.",
            "style": "cinematic realism",
            "mode": "characters"
        })
        self.assertIsNotNone(proj["id"])
        self.assertIsNone(proj.get("channel_profile_id"))

        # Detailed view works and channel_profile is None
        detail = self.service.detail(proj["id"])
        self.assertIsNone(detail.get("channel_profile"))

        # Import analysis works
        analysis = {
            "characters": [{"name": "Man", "description": "30yo man in coat"}],
            "locations": [{"name": "Street", "description": "City street at dusk"}],
            "props": [],
            "scenes": [
                {
                    "position": 1,
                    "beat": "Man walks down street",
                    "source_quote": "A man walks down the street.",
                    "characters": ["Man"],
                    "locations": ["Street"],
                    "props": [],
                    "camera_plan": "medium shot",
                    "visible_people": 1,
                    "presence_mode": "hero_present",
                    "framing_type": "medium"
                }
            ]
        }
        imported = self.service.import_analysis(proj["id"], analysis, 1)
        self.assertEqual(len(imported["scenes"]), 1)

        # Snapshot compiles normally without channel
        scene = self.service.scene(imported["scenes"][0]["id"])
        snap = self.service.scene_snapshot(scene, allow_unlocked=True)
        self.assertIn("cinematic realism", snap["prompt"])
        self.assertNotIn("STICKMAN STYLE RULES", snap["prompt"])

    def test_channel_creation_and_stickman_defaults(self):
        """Test channel creation with default stickman character."""
        channel = self.service.create_channel({
            "name": "Stickman Animation Show",
            "slug": "stickman-show",
            "profile_type": "stickman",
            "description": "2D minimalist animated series",
            "style_prompt": "Clean 2D stickman animation, expressive posing, white background",
            "line_style": "Thick black vector outlines, 3-4px",
            "background_style": "Minimalist soft pastel gradient backgrounds",
            "init_stickman": True
        })

        self.assertEqual(channel["name"], "Stickman Animation Show")
        self.assertEqual(channel["slug"], "stickman-show")
        self.assertEqual(channel["profile_type"], "stickman")
        self.assertEqual(channel["is_archived"], 0)
        self.assertIsNotNone(channel["active_version"])
        self.assertEqual(channel["active_version"]["version_number"], 1)
        self.assertEqual(channel["active_version"]["line_style"], "Thick black vector outlines, 3-4px")

        # Check default stickman character was created
        self.assertEqual(len(channel["characters"]), 1)
        stickman = channel["characters"][0]
        self.assertEqual(stickman["canonical_name"], "Стикмен")
        self.assertEqual(stickman["role"], "main")
        self.assertEqual(stickman["is_locked"], 1)
        self.assertEqual(stickman["active_version"]["version_number"], 1)
        self.assertIn("stick figure", stickman["active_version"]["appearance_prompt"])

    def test_channel_versioning_and_immutability(self):
        """Verify immutable channel versions and project snapshot isolation."""
        # 1. Create channel v1
        channel = self.service.create_channel({
            "name": "Comic Universe",
            "slug": "comic-uni",
            "profile_type": "comic",
            "style_prompt": "Bold comic book art, halftone shading",
            "line_style": "Ink outlines",
            "background_style": "Graphic cityscapes"
        })
        ch_id = channel["id"]
        v1_id = channel["active_version_id"]

        # 2. Create project using channel v1
        proj = self.service.create_project({
            "title": "Comic Episode 1",
            "story": "Hero arrives at rooftop.",
            "style": "standard comic",
            "mode": "characters",
            "channel_profile_id": ch_id
        })
        self.assertEqual(proj["channel_profile_id"], ch_id)
        self.assertEqual(proj["channel_profile_version_id"], v1_id)

        # 3. Create new channel version v2
        updated_ch = self.service.save_channel_version(ch_id, {
            "style_prompt": "Advanced modern comic art with vibrant cel shading",
            "line_style": "Crisp dynamic ink brush strokes",
            "background_style": "Detailed neon cityscape"
        })
        self.assertEqual(updated_ch["active_version"]["version_number"], 2)
        v2_id = updated_ch["active_version_id"]
        self.assertNotEqual(v1_id, v2_id)

        # 4. Existing project detail should still reflect v1 snapshot but show update available
        proj_detail = self.service.detail(proj["id"])
        ch_summary = proj_detail["channel_profile"]
        self.assertIsNotNone(ch_summary)
        self.assertEqual(ch_summary["active_version_number"], 1)
        self.assertEqual(ch_summary["latest_version_number"], 2)
        self.assertTrue(ch_summary["has_update"])

        # 5. Preview project channel update
        preview = self.service.preview_project_channel_update(proj["id"])
        self.assertEqual(preview["channel_id"], ch_id)
        self.assertEqual(preview["current_version_id"], v1_id)
        self.assertEqual(preview["target_version_id"], v2_id)
        self.assertEqual(preview["style_diff"]["old_version_number"], 1)
        self.assertEqual(preview["style_diff"]["new_version_number"], 2)
        self.assertTrue(preview["can_apply"])

        # 6. Apply update
        applied = self.service.apply_project_channel_update(proj["id"])
        self.assertEqual(applied["channel_profile_version_id"], v2_id)

        # 7. Check detail again
        proj_detail_after = self.service.detail(proj["id"])
        self.assertFalse(proj_detail_after["channel_profile"]["has_update"])
        self.assertEqual(proj_detail_after["channel_profile"]["active_version_number"], 2)

    def test_channel_archiving_and_duplication(self):
        """Test channel archiving/unarchiving and deep duplication."""
        channel = self.service.create_channel({
            "name": "SciFi Channel",
            "slug": "scifi-orig",
            "profile_type": "cinematic",
            "style_prompt": "Hard scifi cinema",
            "init_stickman": False
        })
        ch_id = channel["id"]

        # Add custom character
        self.service.save_channel_character(ch_id, {
            "canonical_name": "Commander Vance",
            "role": "main",
            "appearance_prompt": "Astronaut in sleek silver exosuit"
        })

        # Archive
        archived = self.service.archive_channel(ch_id)
        self.assertEqual(archived["is_archived"], 1)
        
        # list_channels without archived flag excludes it
        active_channels = self.service.list_channels(include_archived=False)
        self.assertNotIn(ch_id, [c["id"] for c in active_channels])

        # list_channels with archived flag includes it
        all_channels = self.service.list_channels(include_archived=True)
        self.assertIn(ch_id, [c["id"] for c in all_channels])

        # Unarchive
        unarchived = self.service.unarchive_channel(ch_id)
        self.assertEqual(unarchived["is_archived"], 0)

        # Duplicate channel
        dup = self.service.duplicate_channel(ch_id, "SciFi Channel Clone")
        self.assertNotEqual(dup["id"], ch_id)
        self.assertEqual(dup["name"], "SciFi Channel Clone")
        self.assertEqual(dup["slug"], "scifi-orig-copy")
        self.assertEqual(len(dup["characters"]), 1)
        self.assertEqual(dup["characters"][0]["canonical_name"], "Commander Vance")

    def test_channel_reference_assets_and_scenario_g(self):
        """Test reference assets upload, retrieval, and Scenario G missing-asset error handling."""
        channel = self.service.create_channel({
            "name": "Anime Channel",
            "profile_type": "anime",
            "init_stickman": False
        })
        ch_id = channel["id"]

        # Fake image bytes
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        
        # Upload reference
        updated_ch = self.service.upload_channel_reference(
            ch_id,
            character_id=None,
            asset_type="style_reference",
            file_bytes=fake_png,
            filename="style_sample.png",
            is_primary=1
        )
        self.assertEqual(len(updated_ch["reference_assets"]), 1)
        asset = updated_ch["reference_assets"][0]
        self.assertEqual(asset["asset_type"], "style_reference")
        self.assertEqual(asset["is_primary"], 1)
        self.assertTrue((self.root / asset["file_path"]).exists())

        # Create project with this channel
        proj = self.service.create_project({
            "title": "Anime Project",
            "story": "Hero walks in Tokyo.",
            "style": "anime",
            "mode": "characters",
            "channel_profile_id": ch_id
        })

        analysis = {
            "characters": [],
            "locations": [],
            "props": [],
            "scenes": [
                {
                    "position": 1,
                    "beat": "Street view",
                    "source_quote": "Hero walks in Tokyo.",
                    "characters": [],
                    "locations": [],
                    "props": [],
                    "camera_plan": "wide shot",
                    "visible_people": 0,
                    "presence_mode": "uninhabited",
                    "framing_type": "wide"
                }
            ]
        }
        imported = self.service.import_analysis(proj["id"], analysis, 1)
        scene = self.service.scene(imported["scenes"][0]["id"])

        # Snapshot succeeds with valid asset
        snap = self.service.scene_snapshot(scene, allow_unlocked=True)
        self.assertIsNotNone(snap["generation_reference_bundle"])

        # Scenario G: Corrupt / remove the reference file from disk
        asset_file = self.root / asset["file_path"]
        asset_file.unlink()

        # Assembly of snapshot must now raise DomainError (Scenario G)
        with self.assertRaises(DomainError) as ctx:
            self.service.scene_snapshot(scene, allow_unlocked=True)
        self.assertIn("style_reference", str(ctx.exception))

    def test_prompt_compilation_and_uninhabited_scenes(self):
        """Verify prompt compilation rules, stickman inclusion, and uninhabited frame isolation."""
        channel = self.service.create_channel({
            "name": "Stickman Studio",
            "profile_type": "stickman",
            "style_prompt": "Minimalist stick figure drawings with black lines",
            "line_style": "Vector outline lines 3px",
            "background_style": "Flat soft grey background",
            "init_stickman": True
        })

        proj = self.service.create_project({
            "title": "Stickman Adventure",
            "story": "Ada sits at table. Station shines outside in empty space.",
            "style": "stickman style",
            "mode": "characters",
            "channel_profile_id": channel["id"]
        })

        analysis = {
            "world": "Studio",
            "characters": [{"name": "Ada", "description": "Protagonist girl", "wardrobe": {}}],
            "locations": [],
            "props": [],
            "scenes": [
                {
                    "position": 1,
                    "beat": "Ada sits at table",
                    "source_quote": "Ada sits at table.",
                    "characters": ["Ada"],
                    "wardrobe": {},
                    "location": "Room",
                    "time": "Day",
                    "shot": "Medium",
                    "continuity_before": "",
                    "continuity_after": "",
                    "visible_people": 1,
                    "presence_mode": "hero_present",
                    "framing_type": "medium"
                },
                {
                    "position": 2,
                    "beat": "Empty station outside",
                    "source_quote": "Station shines outside in empty space.",
                    "characters": [],
                    "wardrobe": {},
                    "location": "Space",
                    "time": "Night",
                    "shot": "Wide",
                    "continuity_before": "",
                    "continuity_after": "",
                    "visible_people": 0,
                    "presence_mode": "uninhabited",
                    "framing_type": "wide"
                }
            ]
        }
        imported = self.service.import_analysis(proj["id"], analysis, 1)

        # Scene 1 (Hero present): Must include stickman rules & styling
        scene1 = self.service.scene(imported["scenes"][0]["id"])
        snap1 = self.service.scene_snapshot(scene1, allow_unlocked=True)
        self.assertIn("STICKMAN STYLE RULES", snap1["prompt"])
        self.assertIn("Vector outline lines 3px", snap1["prompt"])

        # Scene 2 (Uninhabited): Must NOT include Stickman protagonist rules
        scene2 = self.service.scene(imported["scenes"][1]["id"])
        snap2 = self.service.scene_snapshot(scene2, allow_unlocked=True)
        self.assertNotIn("STICKMAN STYLE RULES", snap2["prompt"])
        self.assertNotIn("Protagonist girl", snap2["prompt"])
        # But should preserve general channel background & line rules
        self.assertIn("Flat soft grey background", snap2["prompt"])

    def test_server_rest_api_dispatch(self):
        """Test channel REST API endpoints through Application.dispatch."""
        app = self.app

        # 1. POST /api/channels
        created = app.dispatch("POST", "/api/channels", {
            "name": "Dispatch Channel Test",
            "slug": "dispatch-ch",
            "profile_type": "stickman",
            "style_prompt": "Test dispatch style prompt"
        })
        self.assertIsNotNone(created.get("id"))
        ch_id = created["id"]

        # 2. GET /api/channels
        channels = app.dispatch("GET", "/api/channels", None)
        self.assertTrue(any(c["id"] == ch_id for c in channels))

        # 3. GET /api/channels/<id>
        fetched = app.dispatch("GET", f"/api/channels/{ch_id}", None)
        self.assertEqual(fetched["name"], "Dispatch Channel Test")

        # 4. POST /api/channels/<id>/update
        updated = app.dispatch("POST", f"/api/channels/{ch_id}/update", {
            "name": "Dispatch Channel Renamed"
        })
        self.assertEqual(updated["name"], "Dispatch Channel Renamed")

        # 5. POST /api/channels/<id>/versions
        with_new_ver = app.dispatch("POST", f"/api/channels/{ch_id}/versions", {
            "style_prompt": "New v2 style prompt"
        })
        self.assertEqual(with_new_ver["active_version"]["version_number"], 2)

        # 6. POST /api/channels/<id>/characters
        with_char = app.dispatch("POST", f"/api/channels/{ch_id}/characters", {
            "canonical_name": "Assistant Bot",
            "role": "supporting",
            "appearance_prompt": "A tiny floating sphere robot"
        })
        self.assertTrue(any(c["canonical_name"] == "Assistant Bot" for c in with_char["characters"]))

        # 7. POST /api/channels/<id>/archive
        archived = app.dispatch("POST", f"/api/channels/{ch_id}/archive", {})
        self.assertEqual(archived["is_archived"], 1)

        # 8. POST /api/channels/<id>/unarchive
        unarchived = app.dispatch("POST", f"/api/channels/{ch_id}/unarchive", {})
        self.assertEqual(unarchived["is_archived"], 0)


if __name__ == "__main__":
    unittest.main()

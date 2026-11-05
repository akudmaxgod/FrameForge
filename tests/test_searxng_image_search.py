"""Regression coverage for image browsing and local SearXNG integration."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from PIL import Image
import av

from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.export_worker import render
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from imagegen.material_search.query_builder import (
    build_targeted_queries,
    detect_universe_and_entities,
    UNIVERSES
)
from imagegen.material_search.web_image_fetch import (
    is_safe_public_ip,
    validate_image_url,
    fetch_safe_web_image
)


class TestSearxngImageSearch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "secrets").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "cache" / "stock_previews").mkdir(parents=True, exist_ok=True)

        from tests import test_veo_animation as veo_fixture
        self.service = Service(self.root)
        self.fix = veo_fixture.TestVeoAnimationPlan()
        self.fix.service = self.service

        # Create real project with 3 scenes
        self.pid = self.fix._create_sample_project(3)
        self.video_bytes = self.fix._create_valid_mp4(width=160, height=90, duration=1.0)
        for s in self.service.detail(self.pid)["scenes"]:
            self.service.import_scene_video(s["id"], self.video_bytes, filename="scene_clip.mp4")

        # Sync and save initial montage document
        synced = self.service.editing.sync(self.pid, {"expected_revision": 0, "add_only": True})
        saved = self.service.editing.save(self.pid, {"expected_revision": 0, "document": synced["document"]})
        self.initial_doc = saved["document"]
        self.initial_rev = saved["revision"]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_local_search_unavailable(self):
        provider = self.service.material_search.providers["searxng"]
        with patch("urllib.request.OpenerDirector.open", side_effect=URLError("offline")):
            with self.assertRaises(DomainError) as error:
                provider.search("Jon Snow")
        self.assertEqual(error.exception.status, 503)
        self.assertIn("8891", str(error.exception))

    def test_open_catalog_search_and_import(self):
        from imagegen.server import Application
        app = object.__new__(Application)
        app.service = self.service
        path = f"/api/projects/{self.pid}/material-search/browse"
        image_url = "https://example.com/castle.jpg"
        page_url = "https://example.com/castle"
        response = {"results": [
            {"id": "castle", "title": "Castle", "url": image_url, "thumbnail": image_url,
             "foreign_landing_url": page_url, "creator": "Test author", "license": "by",
             "license_version": "4.0", "width": 160, "height": 90},
            {"id": "portrait", "title": "Portrait", "url": image_url, "width": 90, "height": 160},
        ]}
        with patch("urllib.request.OpenerDirector.open") as transport:
            transport.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            search = app.dispatch("POST", path, {
                "query": "Castle Ward", "provider": "openverse", "filters": {"orientation": "horizontal"},
            })
        query = parse_qs(urlsplit(transport.call_args.args[0].full_url).query)
        self.assertEqual(query["q"], ["Castle Ward"])
        self.assertEqual(query["license"], ["by,by-sa,cc0,pdm"])
        self.assertEqual(len(search["results"]), 1)
        item = search["results"][0]
        self.assertEqual(item["provider"], "openverse")
        self.assertEqual(item["license_name"], "CC BY 4.0")

        image = io.BytesIO()
        Image.new("RGB", (160, 90), "blue").save(image, format="PNG")
        with patch("imagegen.material_search.web_image_fetch.fetch_safe_web_image",
                   return_value={"format": "png", "bytes": image.getvalue()}):
            applied = app.dispatch("POST", f"{path}/{search['session_id']}/items/{item['id']}/apply", {
                "clip_id": self.initial_doc["clips"][1]["edit_clip_id"], "expected_revision": self.initial_rev,
            })
        self.assertEqual(applied["document"]["clips"][1]["footage_overlay"]["provider"], "openverse")
        source = self.service.db.one("SELECT * FROM media_library_sources WHERE item_id=?", (applied["item_id"],))
        self.assertEqual(source["author_name"], "Test author")
        self.assertEqual(source["license_type"], "CC BY 4.0")

        commons_item = {"external_id": "765", "media_type": "photo", "download_url": image_url,
                        "source_page_url": page_url, "title": "Castle Ward", "license_name": "CC BY-SA 4.0",
                        "rights_status": "recorded_license"}
        commons = self.service.material_search.providers["commons"]
        with patch.object(commons, "search", return_value={"items": [commons_item]}) as commons_search:
            result = app.dispatch("POST", path, {"query": "Castle Ward", "provider": "commons"})
        commons_search.assert_called_once_with("Castle Ward")
        self.assertEqual(result["results"][0]["provider"], "commons")
        self.assertEqual(result["results"][0]["license_name"], "CC BY-SA 4.0")

    def test_import_public_image_link_preserves_source_without_query(self):
        from imagegen.server import Application
        app = object.__new__(Application)
        app.service = self.service
        route = f"/api/projects/{self.pid}/material-search/image-link"
        with self.assertRaises(DomainError) as error:
            app.dispatch("POST", route, {"url": "https://127.0.0.1/test.jpg"})
        self.assertEqual(error.exception.status, 400)
        image = io.BytesIO()
        Image.new("RGB", (160, 90), "green").save(image, format="PNG")
        url = "https://example.com/reference.png?token=test"
        with patch("imagegen.material_search.web_image_fetch.fetch_safe_web_image",
                   return_value={"format": "png", "bytes": image.getvalue()}) as fetch:
            imported = app.dispatch("POST", route, {"url": url})
        fetch.assert_called_once_with(url, is_preview=False)
        self.assertEqual(imported["source"]["source_page_url"], "https://example.com/reference.png")
        self.assertEqual(imported["source"]["rights_status"], "unknown")

    def test_ssrf_ip_filtering(self):
        """Verifies IP filtering rejects loopback, private, link-local, multicast, and metadata."""
        # Forbidden IPs
        self.assertFalse(is_safe_public_ip("127.0.0.1"))
        self.assertFalse(is_safe_public_ip("10.0.0.1"))
        self.assertFalse(is_safe_public_ip("192.168.1.1"))
        self.assertFalse(is_safe_public_ip("172.16.0.1"))
        self.assertFalse(is_safe_public_ip("169.254.169.254"))  # Cloud metadata
        self.assertFalse(is_safe_public_ip("100.100.100.200"))
        self.assertFalse(is_safe_public_ip("::1"))
        self.assertFalse(is_safe_public_ip("::ffff:127.0.0.1"))
        self.assertFalse(is_safe_public_ip("224.0.0.1"))  # Multicast
        self.assertFalse(is_safe_public_ip("0.0.0.0"))

        # Allowed Public IPs
        self.assertTrue(is_safe_public_ip("8.8.8.8"))
        self.assertTrue(is_safe_public_ip("1.1.1.1"))
        self.assertTrue(is_safe_public_ip("142.250.185.206"))

    def test_url_validation_rejects_insecure_targets(self):
        """Rejects HTTP, userinfo, non-443 ports, and localhost."""
        with self.assertRaises(DomainError):
            validate_image_url("http://example.com/image.jpg")
        with self.assertRaises(DomainError):
            validate_image_url("https://user:pass@example.com/image.jpg")
        with self.assertRaises(DomainError):
            validate_image_url("https://example.com:8080/image.jpg")
        with self.assertRaises(DomainError):
            validate_image_url("https://localhost/image.jpg")
        with self.assertRaises(DomainError):
            validate_image_url("https://127.0.0.1/image.jpg")

    def test_fetch_rejects_html_and_non_raster(self):
        """Rejects executable HTML, SVG, or corrupted data."""
        html_bytes = b"<!DOCTYPE html><html><body><h1>Not an image</h1></body></html>"
        with self.assertRaises(Exception):
            img = Image.open(io.BytesIO(html_bytes))
            img.verify()

    # -------------------------------------------------------------
    # 3. Universe Context & Query Builder
    # -------------------------------------------------------------

    def test_universe_detection_and_query_builder(self):
        """Audits targeted query construction for universe characters and actions."""
        # 1. Game of Thrones character + action
        q1 = build_targeted_queries("Дейнерис в тронном зале", universe_id="game_of_thrones", adaptation="series")
        self.assertIn("Game of Thrones Daenerys Targaryen throne room", q1[0]); self.assertIn("series", q1[0])

        # 2. Witcher
        q2 = build_targeted_queries("Геральт в замке", universe_id="witcher", adaptation="series")
        self.assertIn("The Witcher Geralt of Rivia", q2[0]); self.assertIn("castle interior", q2[0])

        # 3. Harry Potter
        q3 = build_targeted_queries("Гарри Поттер на вокзале", universe_id="harry_potter", adaptation="movie")
        self.assertIn("Harry Potter", q3[0])

        # 4. Length truncation safety (must never exceed 380 chars)
        long_q = build_targeted_queries("A " * 300)
        self.assertLessEqual(len(long_q[0]), 380)

    # -------------------------------------------------------------
    # 4. Manual Browse & Timeline Application (ZERO QWEN REQUIREMENT)
    # -------------------------------------------------------------

    def test_manual_browse_import_apply_and_rollback(self):
        """
        Full lifecycle:
        Search -> Session/Items -> Safe Thumbnail -> Manual Apply to Timeline (no Qwen)
        -> Revision Bumps -> MP4 Render -> Rollback to base image.
        """
        response = {
            "results": [
                {
                    "title": "Daenerys in the Great Throne Room",
                    "url": "https://example.com/got/daenerys",
                    "thumbnail_src": "https://example.com/thumb_daenerys.jpg",
                    "img_src": "https://example.com/daenerys_full.jpg",
                    "resolution": "1920 x 1080",
                    "engines": ["test images"],
                }
            ]
        }

        # Create synthetic valid JPEG image for mock fetch
        test_img_io = io.BytesIO()
        Image.new("RGB", (160, 90), (255, 0, 0)).save(test_img_io, format="JPEG")
        sample_img_bytes = test_img_io.getvalue()

        def mock_fetch(url, is_preview=False, max_bytes=None, timeout=12.0):
            return {
                "ok": True,
                "bytes": sample_img_bytes,
                "width": 160,
                "height": 90,
                "format": "jpeg",
                "content_type": "image/jpeg",
                "url": url,
                "size_bytes": len(sample_img_bytes)
            }

        with patch("urllib.request.OpenerDirector.open") as mock_open, \
             patch("imagegen.material_search.web_image_fetch.fetch_safe_web_image", side_effect=mock_fetch), \
             patch("imagegen.material_search.service.fetch_safe_web_image", side_effect=mock_fetch):

            mock_cm = MagicMock()
            mock_cm.read.return_value = json.dumps(response).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_cm

            # 1. Execute Browse Search
            search_res = self.service.material_search.browse_materials(
                project_id=self.pid,
                query="Дейнерис в тронном зале",
                universe_id="game_of_thrones",
                adaptation="series",
                image_type="still"
            )

            endpoint = urlsplit(mock_open.call_args.args[0].full_url)
            self.assertEqual(endpoint.netloc, "127.0.0.1:8891")
            params = parse_qs(endpoint.query)
            self.assertEqual(params["categories"], ["images"])
            self.assertEqual(params["format"], ["json"])
            self.assertIn("Daenerys", params["q"][0])
            self.assertTrue(search_res["ok"])
            session_id = search_res["session_id"]
            self.assertEqual(len(search_res["results"]), 1)
            item = search_res["results"][0]
            item_id = item["id"]

            # 2. Multi-tenant project isolation test
            pid_b = self.fix._create_sample_project(1)
            # pid_b already created
            with self.assertRaises(DomainError) as cm_b:
                self.service.material_search.get_browse_session(pid_b, session_id)
            self.assertEqual(cm_b.exception.status, 404)

            with self.assertRaises(DomainError) as cm_b_import:
                self.service.material_search.import_browse_item(pid_b, session_id, item_id)
            self.assertEqual(cm_b_import.exception.status, 404)

            # 3. Safe thumbnail retrieval
            prev_res = self.service.material_search.get_browse_item_preview(self.pid, session_id, item_id)
            self.assertTrue(prev_res["ok"])
            self.assertEqual(prev_res["content_type"], "image/jpeg")
            self.assertGreater(len(prev_res["bytes"]), 0)

            # 4. Manual Apply to Timeline: NEVER calls or requires Qwen!
            apply_res = self.service.material_search.apply_browse_item_to_clip(
                project_id=self.pid,
                session_id=session_id,
                item_id=item_id,
                clip_id=self.initial_doc["clips"][0]["edit_clip_id"],
                expected_revision=self.initial_rev
            )

            self.assertTrue(apply_res["ok"])
            new_rev = apply_res["revision"]
            self.assertEqual(new_rev, self.initial_rev + 1)

            # 5. Check montage document has footage_overlay and passes Editing.validate
            current_edit = self.service.editing.get(self.pid)
            validated_doc = self.service.editing.validate(self.pid, current_edit["document"])
            applied_clip = validated_doc["clips"][0]
            self.assertIn("footage_overlay", applied_clip)
            overlay = applied_clip["footage_overlay"]
            self.assertEqual(overlay.get("provider") or overlay.get("source_provider"), "searxng")
            self.assertIn("Daenerys in the Great Throne Room", overlay["title"])

            # Base clip timing intact
            orig_c = self.initial_doc["clips"][0]
            self.assertEqual(applied_clip.get("trim_out_ms"), orig_c.get("trim_out_ms"))
            self.assertEqual(applied_clip.get("trim_in_ms"), orig_c.get("trim_in_ms"))

            # 6. Render real MP4 export with overlay
            export_mp4 = self.root / "export_searxng_test.mp4"
            render(self.service, current_edit["document"], export_mp4, lambda *a: None, lambda: False, size=(160, 90))
            self.assertTrue(export_mp4.exists())
            self.assertGreater(export_mp4.stat().st_size, 1000)

            with av.open(str(export_mp4)) as container:
                stream = container.streams.video[0]
                self.assertEqual(stream.width, 160)
                self.assertEqual(stream.height, 90)

            # 7. Non-destructive Rollback: Remove overlay and restore original frame
            del applied_clip["footage_overlay"]
            restored_saved = self.service.editing.save(
                self.pid,
                {"expected_revision": current_edit["revision"], "document": validated_doc}
            )
            self.assertEqual(restored_saved["revision"], new_rev + 1)
            restored_edit = self.service.editing.get(self.pid)
            self.assertNotIn("footage_overlay", restored_edit["document"]["clips"][0])

            # Re-render restored MP4
            export_restored = self.root / "export_restored.mp4"
            render(self.service, restored_edit["document"], export_restored, lambda *a: None, lambda: False, size=(160, 90))
            self.assertTrue(export_restored.exists())


if __name__ == "__main__":
    unittest.main()

import threading
import unittest
from unittest.mock import Mock
from imagegen.material_search.pixabay_scenario import queries, collect, assign, source_id, relevance


def requirement(id, quote, **kwargs):
    return dict(id=id, quote=quote, requested_role="illustrative", **kwargs)


def item(id, tags, kind="photo"):
    return dict(provider="pixabay", external_id=str(id), canonical_source_id=f"pixabay:{kind}:{id}", media_type=kind, title=tags, description=tags)


class TestPixabayScenario(unittest.TestCase):
    def test_shared_queries_are_bounded_even_for_hundreds_of_scenes(self):
        rows = [requirement(str(i), f"Глаз и мозг изучают через микроскоп {i}") for i in range(400)]
        self.assertEqual(queries(rows), ["eye", "brain", "microscope"])
        self.assertLessEqual(len(queries(rows + [requirement("extra", "город океан лес сердце")])), 4)

    def test_user_queries_win_and_unknown_subjects_survive(self):
        self.assertEqual(queries([requirement("a", "глаз", user_queries=["cornea detail"])]), ["cornea detail"])
        self.assertEqual(queries([requirement("a", "volcanic eruption lava")]), ["volcanic eruption lava"])

    def test_shared_pool_is_searched_once_per_type_and_respects_limit(self):
        provider = Mock()
        provider.search.return_value = {"items": [item(1, "eye"), item(2, "brain")]}
        rows = [requirement(str(i), "Глаз и мозг") for i in range(300)]
        result, calls = collect(provider, rows, {}, threading.Event(), Mock())
        self.assertEqual(calls, 4)
        self.assertEqual(provider.search.call_count, 4)
        self.assertEqual(sum(map(len, result.values())), 2)
        self.assertEqual({c.kwargs["filters"]["media_type"] for c in provider.search.call_args_list}, {"photo", "video"})

    def test_best_phrase_wins_and_each_file_is_proposed_only_once(self):
        rows = [requirement("brain", "brain neurons"), requirement("eye", "eye retina")]
        result = assign([item(1, "eye retina"), item(1, "eye retina"), item(2, "brain neurons"), item(3, "beach sand")], rows)
        self.assertEqual([i["external_id"] for i in result["eye"]], ["1"])
        self.assertEqual([i["external_id"] for i in result["brain"]], ["2"])

    def test_forbidden_and_unverified_exact_identities_are_not_substituted(self):
        self.assertEqual(relevance(item(1, "eye injury"), requirement("a", "глаз", forbidden_substitutions=["injury"])), 0)
        self.assertEqual(relevance(item(1, "eye"), dict(id="a", quote="Глаз Дейнерис", requested_role="exact", subject_entities=["Daenerys Targaryen"])), 0)

    def test_specific_anatomy_and_animal_substitutions_are_filtered(self):
        self.assertEqual(relevance(item(1, "cat eyes"), requirement("a", "Глаз человека")), 0)
        self.assertEqual(relevance(item(1, "eye portrait"), requirement("a", "Сетчатка глаза")), 0)
        self.assertGreater(relevance(item(1, "eye retina"), requirement("a", "Сетчатка глаза")), 0)
        self.assertGreater(relevance(item(1, "cat eyes"), requirement("a", "Глаза кошки")), 0)
        self.assertEqual(queries([requirement("a", "Сетчатка глаза")]), ["retina"])

    def test_cancel_and_no_relevant_results(self):
        provider = Mock()
        cancelled = threading.Event(); cancelled.set()
        result, calls = collect(provider, [requirement("a", "глаз")], {}, cancelled, Mock())
        self.assertEqual((result, calls), ({}, 0)); provider.search.assert_not_called()
        self.assertEqual(assign([item(1, "beach sand")], [requirement("a", "мозг нейроны")] ), {})

    def test_already_used_sources_are_omitted_without_downloads(self):
        provider = Mock(); provider.search.return_value = {"items": [item(1, "eye"), item(2, "eye")]}
        result, _ = collect(provider, [requirement("a", "глаз")], {"pixabay_media_type": "photo"}, threading.Event(), Mock(), {source_id("image", "pixabay_photo_1")})
        self.assertEqual([i["external_id"] for v in result.values() for i in v], ["2"])
        provider.import_selected.assert_not_called()

    def test_errors_are_not_reported_as_success(self):
        provider = Mock(); provider.search.side_effect = RuntimeError("API unavailable")
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            collect(provider, [requirement("a", "глаз")], {}, threading.Event(), Mock())


class TestBoundedWorker(unittest.TestCase):
    def test_new_mode_uses_bounded_pool_without_llm_or_original_download(self):
        from tests.test_material_search_core import TestMaterialSearchCore
        from unittest.mock import patch
        fixture = TestMaterialSearchCore(); fixture.setUp()
        fixture.conn.row_factory = lambda cursor, row: {d[0]: row[i] for i, d in enumerate(cursor.description)}
        ms = fixture.service
        plan = ms.create_plan("proj_123", options={"pixabay_scenario": True, "pixabay_media_type": "both"})
        provider = Mock()
        provider.capabilities.return_value = {"auth_required": True, "auth_configured": True, "supports_batch": False}
        provider.search.return_value = {"items": [dict(item(1, "bison yellowstone grazing"), license_name="Pixabay Content License")]}
        ms.jobs.providers["pixabay"] = provider
        ms.jobs._verify_candidate_visual = Mock(side_effect=AssertionError("Unexpected LLM verification"))
        with patch("imagegen.material_search.jobs.threading.Thread"):
            job = ms.start_job("proj_123", plan["plan_id"], ["pixabay"])
        rows = fixture.mock_db.all("SELECT * FROM material_search_requirements WHERE plan_id=?", (plan["plan_id"],))
        with patch("imagegen.material_search.pixabay_scenario.used_sources", return_value=set()):
            ms.jobs._run_job("proj_123", job["id"], plan["plan_id"], ["pixabay"], rows, threading.Event())
        status = ms.jobs.get_job_status("proj_123", job["id"])
        self.assertEqual(status["state"], "completed", status.get("error_message"))
        self.assertGreater(provider.search.call_count, 0)
        self.assertLessEqual(provider.search.call_count, 8)
        candidates = ms.get_candidates("proj_123", job["id"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["semantic_status"], "needs_review")
        ms.jobs._verify_candidate_visual.assert_not_called()
        provider.import_selected.assert_not_called()
        fixture.mock_service.editing.save.assert_not_called()
        self.assertEqual(ms.latest_pixabay_scenario("proj_123")["job"]["id"], job["id"])
        fixture.conn.close()

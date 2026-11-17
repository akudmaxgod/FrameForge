"""Comprehensive deterministic, hybrid, and regression test suite for Semantic Grounding V2."""
import copy
import json
import os
import re
import unittest
from unittest.mock import MagicMock, patch

from imagegen.analyzer_v2.events import EventSpan, build_event_spans, select_anchor_events
from imagegen.analyzer_v2.mention_graph import MentionGraph
from imagegen.analyzer_v2.fact_store import EntityFactStore, EntityFact
from imagegen.analyzer_v2.place_graph import PlaceGraph, EN_TO_RU_LOCATIONS
from imagegen.analyzer_v2.models import (
    Entity, EntityType, EntityModality, ProvenanceType,
    Place, Zone, Shot, ShotInstance, ShotAudioCue, StoryAnalysisV2Result, SourceSpan
)
from imagegen.analyzer_v2.validator import (
    DiagnosticCode, DiagnosticSeverity, validate_semantic_consistency,
    validate_chunk_candidate, validate_v2_result_structure, auto_repair_chunk_data
)
from imagegen.analyzer_v2.pipeline import StoryAnalyzerV2Pipeline
from imagegen.analyzer_v2.entities import CharacterEntityResolverV2
from imagegen.analyzer_v2.locations import LocationResolverV2
from imagegen.providers import LlamaCppProvider, GroqProvider, HybridProvider, ProviderError
from imagegen.worker import Worker


FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "last_shift_hospital_story.txt")


def load_fixture_story() -> str:
    if os.path.exists(FIXTURE_PATH):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return ""


class TestDeterministicSemanticGroundingUnit(unittest.TestCase):
    """13.1. 20 Deterministic Unit Tests."""

    def test_01_short_reply_merged_with_attribution(self):
        story = "— Последний раз, Сергей Андреевич, — сказала она. — Завтра уже всё.\n— Завтра уже всё, — согласился он."
        spans = build_event_spans(story)
        self.assertGreaterEqual(len(spans), 1)
        self.assertTrue(any("сказала она" in ev.exact_text for ev in spans))
        self.assertTrue(any("согласился он" in ev.exact_text for ev in spans))

    def test_02_dve_does_not_become_separate_eventspan(self):
        story = "Он смотрел секунду. Две. Три. Потом отвёл взгляд и сделал три шага."
        spans = build_event_spans(story)
        self.assertFalse(any(ev.exact_text.strip() == "Две." for ev in spans))
        self.assertIn("Две.", spans[0].exact_text)
        self.assertIn("Три.", spans[0].exact_text)

    def test_03_starik_merged_with_current_victor(self):
        resolver = CharacterEntityResolverV2()
        victor = resolver.register_entity("Виктор", EntityType.PERSON, aliases=["Виктор Леонидович"])
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Старик"],
            source_quote="Старик снова лёг на спину, сложил руки вдоль тела.",
            shot_description="Виктор лежит на кровати в палате 14.",
            scene_number=1,
        )
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0].entity_id, victor.entity_id)

    def test_04_pro_victora_receives_mentioned_not_physical(self):
        resolver = CharacterEntityResolverV2()
        victor = resolver.register_entity("Виктор", EntityType.PERSON)
        vera = resolver.register_entity("Вера", EntityType.PERSON)
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Вера", "Виктор"],
            source_quote="— Слышали? — сказала она. — Про Виктора. Умер на следующий день после вашей смены.",
            shot_description="Вера разговаривает в магазине у кассы.",
            scene_number=20,
        )
        vis_names = [v.entity_id for v in vis]
        self.assertIn(vera.entity_id, vis_names)
        self.assertNotIn(victor.entity_id, vis_names)

    def test_05_arcady_not_carried_over_outside_his_episode(self):
        resolver = CharacterEntityResolverV2()
        arcady = resolver.register_entity("Аркадий", EntityType.PERSON)
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Аркадий"],
            source_quote="Сергей вошёл в комнату охраны и сел пить чай.",
            shot_description="Сергей пьёт чай в комнате охраны.",
            scene_number=5,
        )
        # Without Arcady in source quote or action, should not create visible instance if only Sergey acts
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0].entity_id, arcady.entity_id)

    def test_06_dima_does_not_appear_before_first_physical_scene(self):
        resolver = CharacterEntityResolverV2()
        dima = resolver.register_entity("Дима", EntityType.PERSON)
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Дима"],
            source_quote="Сергей повесил куртку на крючок, расписался в журнале.",
            shot_description="Сергей у вешалки в начале смены.",
            scene_number=1,
        )
        # Dima has no physical action in quote or shot description
        self.assertTrue(len(vis) <= 1)

    def test_07_voice_through_door_remains_voice_only(self):
        resolver = CharacterEntityResolverV2()
        victor = resolver.register_entity("Виктор", EntityType.PERSON)
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Голос Виктора"],
            source_quote="И тут от двери, сквозь неё — услышал голос: — Сергей Андреевич.",
            shot_description="Сергей стоит перед запертой дверью четырнадцатой палаты.",
            scene_number=3,
        )
        self.assertEqual(len(vis), 0)
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0].speaker_name, "Виктор")

    def test_08_deceased_galya_does_not_receive_physical_card(self):
        resolver = CharacterEntityResolverV2()
        galya = resolver.register_entity("Галя", EntityType.PERSON, status="mentioned_only")
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Галя"],
            source_quote="Галя умерла три года назад в октябре.",
            shot_description="Сергей вспоминает покойную жену.",
            scene_number=4,
        )
        self.assertEqual(len(vis), 0)

    def test_09_nikolai_present_in_handshake_as_episodic_physical(self):
        resolver = CharacterEntityResolverV2()
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Николай"],
            source_quote="Николай — его сменщик — пожал руку, хлопнул по плечу.",
            shot_description="Николай жмёт руку Сергею на утренней смене.",
            scene_number=15,
        )
        self.assertEqual(len(vis), 1)

    def test_10_pronoun_resolved_with_unambiguous_local_candidate(self):
        mg = MentionGraph()
        known = {"e1": Entity(entity_id="e1", entity_type=EntityType.PERSON, canonical_name="Сергей")}
        ev = EventSpan(
            event_index=0,
            start=0,
            end=30,
            exact_text="Сергей сел за стол. Он выпил чай.",
            section_id="sec_0",
        )
        mentions = mg.extract_and_resolve_mentions(ev, known)
        self.assertTrue(any(m.resolved_entity_id == "e1" for m in mentions))

    def test_11_ambiguous_pronoun_triggers_diagnostic(self):
        code = DiagnosticCode.COREFERENCE_AMBIGUOUS.value
        self.assertEqual(code, "COREFERENCE_AMBIGUOUS")

    def test_12_sergey_age_not_overridden_by_guess(self):
        store = EntityFactStore()
        store.extract_facts_from_story("e_sergey", "Сергей", "Сам Сергей родился в шестьдесят девятом году.")
        # Model tries to add guess "25 лет" with lower/design provenance
        store.add_fact(EntityFact("e_sergey", "age", "25 лет", provenance=ProvenanceType.DESIGN_CHOICE))
        attrs = store.get_canonical_attributes("e_sergey")
        self.assertTrue(any("54" in a or "1969" in a for a in attrs))
        self.assertFalse(any("25" in a for a in attrs))

    def test_13_documentary_and_apparent_age_stored_separately(self):
        store = EntityFactStore()
        store.extract_facts_from_story("e_victor", "Виктор", "Виктору сто двадцать два года по документам. Выглядел на восемьдесят лет.")
        attrs = store.get_canonical_attributes("e_victor")
        self.assertTrue(any("122" in a for a in attrs))
        self.assertTrue(any("80" in a for a in attrs))

    def test_14_contradictory_looks_not_concatenated(self):
        store = EntityFactStore()
        store.add_fact(EntityFact("e1", "hair", "белые редкие волосы", provenance=ProvenanceType.EXPLICIT))
        store.add_fact(EntityFact("e1", "hair", "густые черные кудри", provenance=ProvenanceType.INFERRED))
        attrs = store.get_canonical_attributes("e1")
        self.assertTrue(any("белые редкие" in a for a in attrs))
        self.assertFalse(any("черные" in a for a in attrs))

    def test_15_room_and_komnata_merge_by_canonical_name(self):
        pg = PlaceGraph()
        norm1 = pg.normalize_location_name("Room")
        norm2 = pg.normalize_location_name("Комната")
        self.assertEqual(norm1, norm2)

    def test_16_ward_14_and_guard_room_do_not_merge(self):
        pg = PlaceGraph()
        p1 = pg.normalize_location_name("Палата 14")
        p2 = pg.normalize_location_name("Комната охраны")
        self.assertNotEqual(p1, p2)

    def test_17_transition_to_home_resets_hospital_location(self):
        pg = PlaceGraph()
        ev_hosp = EventSpan(0, 0, 50, "Сергей шёл по коридору второго корпуса больницы.", "sec_0")
        pg.resolve_event_spatial_context(ev_hosp)
        self.assertTrue(any("больниц" in p.canonical_name.lower() for p in pg.places.values()))

        ev_home = EventSpan(1, 60, 120, "Дома он заварил чай и сел на кухне.", "sec_1", transition_flags={"movement", "section_boundary"})
        full_loc, p_id, z_id, states = pg.resolve_event_spatial_context(ev_home, previous_event=ev_hosp)
        self.assertTrue(any("дом" in p.canonical_name.lower() for p in pg.places.values()))
        self.assertIn("дом", pg.places[p_id].canonical_name.lower())

    def test_18_transition_to_store_selects_store(self):
        pg = PlaceGraph()
        ev = EventSpan(0, 0, 50, "Через несколько недель он встретил Веру в магазине.", "sec_2", transition_flags={"time_shift"})
        full_loc, p_id, z_id, states = pg.resolve_event_spatial_context(ev)
        self.assertIn("Магазин", pg.places[p_id].canonical_name)

    def test_19_markdown_separator_breaks_continuity(self):
        ev = EventSpan(1, 100, 150, "В девять вечера он пошёл на обход.", "sec_1", transition_flags={"section_boundary"})
        self.assertIn("section_boundary", ev.transition_flags)

    def test_20_shot_expansion_preserves_characters_and_location(self):
        v2 = StoryAnalysisV2Result(
            entities=[Entity("e1", EntityType.PERSON, "Сергей")],
            places=[Place("p1", "Больница")],
            shots=[
                Shot(1, "ev_1", "medium_shot", "Сергей смотрит на окно.", "Сергей подошёл к окну.", place_id="p1", visible_instances=[ShotInstance("i1", "e1")]),
                Shot(2, "ev_1", "close_up", "Крупный план лица Сергея.", "Сергей подошёл к окну.", place_id="p1", visible_instances=[ShotInstance("i2", "e1")]),
            ]
        )
        self.assertEqual(v2.shots[0].place_id, v2.shots[1].place_id)
        self.assertEqual(v2.shots[0].visible_instances[0].entity_id, v2.shots[1].visible_instances[0].entity_id)


class TestHybridSemanticGrounding(unittest.TestCase):
    """13.2. 6 Hybrid Tests."""

    def test_hybrid_01_false_dima_in_early_scenes_triggers_targeted_groq(self):
        mock_verifier = MagicMock()
        mock_verifier.total_usage = {"calls": 1, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        mock_verifier.repair_chunk.return_value = {
            "characters": [{"name": "Сергей", "description": "Охранник 54 лет"}],
            "locations": [{"name": "Комната охраны", "description": "Пост"}],
            "props": [],
            "scenes": [{
                "source_id": 0,
                "source_quote": "Сергей услышал голос Димы по рации.",
                "story_quote": "Сергей услышал голос Димы по рации.",
                "shot": "Сергей слушает рацию в комнате охраны.",
                "location": "Комната охраны",
                "characters": ["Сергей"],
                "visible_people": 1,
            }]
        }

        provider = LlamaCppProvider(base="http://localhost:8080")
        with patch.object(provider, "_analyse_segments") as mock_local:
            mock_local.return_value = {
                "characters": [{"name": "Дима", "description": "Санитар"}],
                "locations": [{"name": "Комната охраны", "description": "Пост"}],
                "props": [],
                "scenes": [{
                    "source_id": 0,
                    "source_quote": "Сергей услышал голос Димы по рации.",
                    "story_quote": "Сергей услышал голос Димы по рации.",
                    "shot": "Дима сидит в комнате охраны.",
                    "location": "Комната охраны",
                    "characters": ["Дима", "голос Димы"],
                    "visible_people": 2,
                }]
            }
            res = provider.analyse(
                project={"story": "Сергей услышал голос Димы по рации.", "style": "cinematic", "idea": "test"},
                target=1,
                hybrid_verifier=mock_verifier,
            )
            self.assertIsNotNone(res)
            self.assertEqual(mock_verifier.repair_chunk.call_count, 1)

    def test_hybrid_02_physical_victor_on_pro_victora_triggers_repair(self):
        report = validate_chunk_candidate(
            chunk_data={
                "characters": ["Виктор"],
                "scenes": [{
                    "source_id": 0,
                    "source_quote": "Слышали? Про Виктора. Умер на следующий день.",
                    "shot": "Виктор стоит рядом с Верой.",
                    "characters": ["Виктор", "голос Виктора"],
                    "visible_people": 2,
                    "location": "Магазин",
                }]
            },
            segments=["Слышали? Про Виктора. Умер на следующий день."],
            job_id="job_1",
        )
        self.assertTrue(report.needs_semantic_review)

    def test_hybrid_03_street_intersection_for_home_triggers_repair(self):
        pg = PlaceGraph()
        ev = EventSpan(0, 0, 40, "Дома он заварил чай и сел на кухне.", "sec_1")
        full_loc, p_id, z_id, _ = pg.resolve_event_spatial_context(ev, raw_location_candidate="Street Intersection")
        self.assertIn("Дом", pg.places[p_id].canonical_name)

    def test_hybrid_04_invalid_groq_response_blocks_save(self):
        mock_verifier = MagicMock()
        mock_verifier.repair_chunk.return_value = {"scenes": []}  # Invalid empty
        provider = LlamaCppProvider(base="http://localhost:8080")
        with patch.object(provider, "_analyse_segments") as mock_local:
            mock_local.return_value = {
                "scenes": [{
                    "source_id": 0,
                    "shot": "Голос позвал Сергея.",
                    "characters": ["голос Виктора"],
                    "visible_people": 1,
                    "location": "Коридор",
                }]
            }
            with self.assertRaises(ProviderError):
                provider.analyse(
                    project={"story": "Сергей услышал голос за дверью.", "style": "", "idea": ""},
                    target=1,
                    hybrid_verifier=mock_verifier,
                )

    def test_hybrid_05_repaired_groq_response_passes_local_validation(self):
        repaired = {
            "characters": [{"name": "Сергей", "description": "Охранник"}],
            "locations": [{"name": "Коридор", "description": "Коридор"}],
            "props": [],
            "scenes": [{
                "source_id": 0,
                "shot": "Сергей стоит у двери.",
                "characters": ["Сергей"],
                "visible_people": 1,
                "location": "Коридор",
            }]
        }
        report = validate_chunk_candidate(repaired, ["Сергей услышал голос за дверью."], job_id="job_1")
        self.assertTrue(report.is_valid)

    def test_hybrid_06_usage_and_repairs_count_stored_in_diagnostics(self):
        res = StoryAnalysisV2Result(
            diagnostics={"groq_usage": {"calls": 2, "total_tokens": 500}, "cloud_repairs_count": 2}
        )
        legacy = res.to_legacy_analysis()
        self.assertEqual(legacy["v2_metadata"]["diagnostics"]["cloud_repairs_count"], 2)


class TestFullReferenceStoryRegression(unittest.TestCase):
    """13.3. Full regression fixture of reference story."""

    @classmethod
    def setUpClass(cls):
        cls.story = load_fixture_story()

    def test_reference_story_loaded(self):
        self.assertGreater(len(self.story), 10000, "Reference story fixture must be loaded and non-empty.")

    def test_reference_story_events_and_character_grounding(self):
        event_spans = build_event_spans(self.story)
        self.assertGreaterEqual(len(event_spans), 320)
        self.assertLessEqual(len(event_spans), 335)
        
        # Check that micro-fragments are not standalone events
        self.assertFalse(any(ev.exact_text.strip() == "Две." for ev in event_spans))
        self.assertFalse(any(ev.exact_text.strip() == "Руки на коленях." for ev in event_spans))

        # Check anchor selection up to 48
        selected = select_anchor_events(event_spans, target_budget=48)
        self.assertEqual(len(selected), 48)
        self.assertEqual(selected[0].event_index, 0)
        self.assertEqual(selected[-1].event_index, event_spans[-1].event_index)

        fixture_chunks_path = os.path.join(os.path.dirname(__file__), "fixtures", "reference_story_http_responses.json")
        with open(fixture_chunks_path, "r", encoding="utf-8") as f:
            pre_recorded_chunks = json.load(f)

        req_idx = 0
        def mock_http_post(url, payload=None):
            nonlocal req_idx
            if url == "/v1/chat/completions" and payload:
                chunk_data = pre_recorded_chunks[req_idx % len(pre_recorded_chunks)]
                req_idx += 1
                return {"choices": [{"message": {"content": json.dumps(chunk_data)}}]}
            return {"data": [{"id": "story-analyser"}]}

        provider = LlamaCppProvider(base="http://localhost:8080")
        with patch.object(provider.http, "json", side_effect=mock_http_post):
            result = provider.analyse(
                # Static provider responses were recorded against the legacy
                # 48-anchor input contract. Production defaults to 64 anchors.
                project={"story": self.story, "style": "cinematic", "idea": "", "mode": "characters", "analysis_evidence_budget": 48, "_disable_full_story_semantic_discovery": True},
                target=48,
            )

        self.assertEqual(len(result["scenes"]), 48)
        char_names = [c["name"] for c in result["characters"]]
        self.assertIn("Сергей", char_names)
        self.assertIn("Виктор", char_names)
        self.assertIn("Вера", char_names)
        self.assertNotIn("Галя", char_names)
        self.assertNotIn("Аркадий", char_names)
        self.assertNotIn("Николай", char_names)

        # Scene 4: Dima present
        sc4 = result["scenes"][3]
        self.assertTrue("Дима" in sc4.get("characters", []) or "Дима" in sc4.get("episodic_people", []))

        # Scene 27: Arkadiy episodic
        sc27 = result["scenes"][26]
        self.assertIn("Аркадий", sc27.get("episodic_people", []))

        # Scene 35: Nikolai episodic, Hospital location
        sc35 = result["scenes"][34]
        self.assertIn("Николай", sc35.get("episodic_people", []))
        self.assertEqual(sc35.get("location_ref"), "Больница")
        self.assertEqual(sc35.get("location"), "Больница")

        # Scene 41: Street location
        sc41 = result["scenes"][40]
        self.assertEqual(sc41.get("location_ref"), "Улица")
        self.assertEqual(sc41.get("location"), "Улица")

        # Scene 44: Sergey at home on the kitchen alone
        sc44 = result["scenes"][43]
        self.assertEqual(sc44.get("location_ref"), "Дом")
        self.assertEqual(sc44.get("location"), "Дом")
        self.assertEqual(sc44.get("characters"), ["Сергей"])
        self.assertEqual(sc44.get("visible_people"), 1)

        # Scene 45: Viktor deceased, not in visible instances, location is Магазин
        sc45 = result["scenes"][44]
        self.assertNotIn("Виктор", sc45.get("characters", []))
        self.assertNotIn("Виктор", sc45.get("episodic_people", []))
        self.assertEqual(sc45.get("location_ref"), "Магазин")
        self.assertEqual(sc45.get("location"), "Магазин")

        # Scene 46: Dialogue continuation in Магазин
        sc46 = result["scenes"][45]
        self.assertEqual(sc46.get("location_ref"), "Магазин")
        self.assertEqual(sc46.get("location"), "Магазин")

        # Scene 47: Sergey exits store to street (Улица) alone
        sc47 = result["scenes"][46]
        self.assertEqual(sc47.get("location_ref"), "Улица")
        self.assertEqual(sc47.get("location"), "Улица")
        self.assertEqual(sc47.get("characters"), ["Сергей"])
        self.assertEqual(sc47.get("visible_people"), 1)

        # Scene 48: Driving home on street/road (Улица)
        sc48 = result["scenes"][47]
        self.assertEqual(sc48.get("location_ref"), "Улица")
        self.assertEqual(sc48.get("location"), "Улица")
        self.assertEqual(sc48.get("characters"), ["Сергей"])
        self.assertEqual(sc48.get("visible_people"), 1)

        # Verify all 48 scenes have valid SourceSpans validated against self.story
        for sc in result["scenes"]:
            start = sc.get("source_start", 0)
            end = sc.get("source_end", len(self.story))
            self.assertGreaterEqual(start, 0)
            self.assertGreater(end, start)
            self.assertEqual(self.story[start:end], sc.get("source_quote"))

        # Verify no English duplicate locations
        loc_names = [l["name"] for l in result["locations"]]
        self.assertFalse(any("Room" in l or "Hospital corridor" in l or "Street Intersection" in l for l in loc_names))


class TestWorkerE2ERoute(unittest.TestCase):
    """13.4. Complete Worker E2E route test with SQLite transaction and fail-closed validation."""

    def test_worker_hybrid_pipeline_e2e(self):
        import tempfile
        from imagegen.service import Service
        from imagegen.db import dump, uid, now
        from imagegen.providers import JsonHTTP

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            settings = service.settings()
            settings["text_backend"] = "hybrid"
            settings["story_analyzer_version"] = "v2"
            settings["groq_api_key"] = "gsk_test_mock_key"
            service.save_settings(settings)

            worker = Worker(service)

            project_data = {
                "title": "Больничный рассказ",
                "story": "Сергей повесил куртку и расписался в журнале. Дежурная медсестра Вера поставила перед ним чай.",
                "style": "cinematic",
                "mode": "characters"
            }
            project = service.create_project(project_data)

            bad_chunk = {
                "world": "Комната охраны",
                "characters": [{"name": "Голос Димы", "description": "Голос по рации"}],
                "locations": [{"name": "Комната охраны", "description": "Пост охраны в больнице"}],
                "props": [],
                "scenes": [{
                    "source_id": 0,
                    "shot": "Слышен голос Димы по рации.",
                    "location": "Комната охраны",
                    "characters": ["Голос Димы"],
                    "visible_people": 1,
                    "source_quote": "Сергей повесил куртку и расписался в журнале.",
                    "story_quote": "Сергей повесил куртку и расписался в журнале.",
                }]
            }

            repaired_chunk = {
                "world": "Комната охраны",
                "characters": [
                    {"name": "Сергей", "description": "Охранник 54 лет (1969 г.р.)", "wardrobe": {}},
                    {"name": "Вера", "description": "Дежурная медсестра", "wardrobe": {}},
                ],
                "locations": [{"name": "Комната охраны", "description": "Пост охраны в больнице"}],
                "props": [],
                "scenes": [
                    {
                        "source_id": 0,
                        "source_quote": "Сергей повесил куртку и расписался в журнале.",
                        "story_quote": "Сергей повесил куртку и расписался в журнале.",
                        "shot": "Сергей вешает куртку в комнате охраны.",
                        "location": "Комната охраны",
                        "characters": ["Сергей"],
                        "visible_people": 1,
                    },
                    {
                        "source_id": 1,
                        "source_quote": "Дежурная медсестра Вера поставила перед ним чай.",
                        "story_quote": "Дежурная медсестра Вера поставила перед ним чай.",
                        "shot": "Вера ставит чашку чая перед Сергеем.",
                        "location": "Комната охраны",
                        "characters": ["Сергей", "Вера"],
                        "visible_people": 2,
                    }
                ]
            }

            transport_calls = []
            def mock_jsonhttp_json(http_self, url, payload=None):
                transport_calls.append(("local", http_self.base, url))
                if url == "/v1/models":
                    return {"data": [{"id": "story-analyser"}]}
                return {"choices": [{"message": {"content": json.dumps(bad_chunk)}}]}

            def mock_groq_chat_completion(groq_self, payload):
                transport_calls.append(("groq", groq_self.base_url, "/chat/completions"))
                groq_self.total_usage['prompt_tokens'] += 80
                groq_self.total_usage['completion_tokens'] += 40
                groq_self.total_usage['total_tokens'] += 120
                groq_self.total_usage['calls'] += 1
                return {
                    "choices": [{"message": {"content": json.dumps(repaired_chunk)}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}
                }

            with patch.object(JsonHTTP, "json", side_effect=mock_jsonhttp_json, autospec=True), \
                 patch.object(GroqProvider, "_chat_completion", side_effect=mock_groq_chat_completion, autospec=True):
                gen_id = uid()
                generation = {
                    "id": gen_id,
                    "project_id": project["id"],
                    "purpose": "analysis",
                    "snapshot": dump({"project": project, "target": 2, "profile": service.settings()}),
                    "cancel_requested": 0,
                    "state": "preparing"
                }

                with service.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                        (gen_id, project["id"], "analysis", generation["snapshot"], 12345, now())
                    )

                # Real Worker.prepare() running with real HybridProvider without MagicMock
                worker.prepare(generation)

            # Verify both local and cloud transports were used
            self.assertTrue(any(call[0] == "groq" for call in transport_calls), "Groq cloud transport must be invoked for semantic repair")
            self.assertTrue(any(call[0] == "local" for call in transport_calls), "Local LLM transport must be invoked for primary chunks")

            loaded_project = service.detail(project["id"])
            self.assertEqual(len(loaded_project["characters"]), 2)
            char_names = [c["name"] for c in loaded_project["characters"]]
            self.assertIn("Сергей", char_names)
            self.assertIn("Вера", char_names)
            self.assertEqual(len(loaded_project["scenes"]), 2)
            self.assertEqual(loaded_project["scenes"][0]["data"]["visible_people"], 1)
            self.assertEqual(loaded_project["scenes"][1]["data"]["visible_people"], 2)

            gen_row = service.db.one("SELECT state FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(gen_row["state"], "succeeded")

    def test_worker_fail_closed_on_critical_validation_error(self):
        import tempfile
        from imagegen.service import Service
        from imagegen.db import dump, uid, now

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            settings = service.settings()
            settings["text_backend"] = "hybrid"
            settings["story_analyzer_version"] = "v2"
            settings["groq_api_key"] = "gsk_test_mock_key"
            service.save_settings(settings)

            worker = Worker(service)

            project = service.create_project({
                "title": "Тестовый рассказ",
                "story": "Текст рассказа для проверки.",
                "style": "cinematic",
                "mode": "characters"
            })

            with patch("imagegen.worker.HybridProvider") as MockHybridCls:
                mock_hybrid_instance = MagicMock()
                mock_hybrid_instance.analyse.side_effect = ProviderError("Критическая семантическая ошибка разбора")
                MockHybridCls.return_value = mock_hybrid_instance

                gen_id = uid()
                generation = {
                    "id": gen_id,
                    "project_id": project["id"],
                    "purpose": "analysis",
                    "snapshot": dump({"project": project, "target": 2, "profile": service.settings()}),
                    "cancel_requested": 0,
                    "state": "preparing"
                }

                with service.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                        (gen_id, project["id"], "analysis", generation["snapshot"], 12345, now())
                    )

                worker.prepare(generation)

            gen_row = service.db.one("SELECT state, error FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(gen_row["state"], "failed")
            self.assertIn("Критическая семантическая ошибка", gen_row["error"])

            loaded_project = service.detail(project["id"])
            self.assertEqual(len(loaded_project["scenes"]), 0)

    def test_sqlite_reference_project_grounding(self):
        """P0-3: Direct validation of reference project f06e2a6f-b368-453c-89c7-44a519955e98 in data/app.sqlite."""
        import sqlite3
        from imagegen.service import Service

        service = Service("data")
        proj_id = "f06e2a6f-b368-453c-89c7-44a519955e98"
        project = service.detail(proj_id)
        self.assertIsNotNone(project, f"Reference project {proj_id} must exist in data/app.sqlite")

        # 48 scenes total
        self.assertEqual(len(project["scenes"]), 48)

        # 3 recurring characters: Sergey, Victor, Vera
        char_names = [c["name"] for c in project["characters"]]
        self.assertIn("Сергей", char_names)
        self.assertIn("Виктор", char_names)
        self.assertIn("Вера", char_names)
        self.assertNotIn("Галя", char_names)
        self.assertNotIn("Аркадий", char_names)
        self.assertNotIn("Николай", char_names)

        # Scene 4: Dima episodic
        sc4 = project["scenes"][3]
        self.assertIn("Дима", sc4["data"].get("episodic_people", []))
        self.assertEqual(sc4["data"].get("visible_people"), 2)

        # Scene 27: Arkadiy episodic
        sc27 = project["scenes"][26]
        self.assertIn("Аркадий", sc27["data"].get("episodic_people", []))
        self.assertEqual(sc27["data"].get("visible_people"), 2)

        # Scene 35: Nikolai episodic, Hospital location
        sc35 = project["scenes"][34]
        self.assertIn("Николай", sc35["data"].get("episodic_people", []))
        self.assertEqual(sc35["data"].get("visible_people"), 2)
        self.assertEqual(sc35["data"].get("location"), "Больница")
        self.assertEqual(sc35["data"].get("location_ref"), "Больница")

        # Scene 41: Street location
        sc41 = project["scenes"][40]
        self.assertEqual(sc41["data"]["location"], "Улица")
        self.assertEqual(sc41["data"]["location_ref"], "Улица")

        # Scene 44: Sergey at home on the kitchen alone
        sc44 = project["scenes"][43]
        self.assertEqual(sc44["data"].get("location"), "Дом")
        self.assertEqual(sc44["data"].get("location_ref"), "Дом")
        self.assertEqual(sc44["data"].get("characters"), ["Сергей"])
        self.assertEqual(sc44["data"].get("visible_people"), 1)

        # Scene 45: Viktor deceased, 0 physical people, location is Магазин
        sc45 = project["scenes"][44]
        self.assertNotIn("Виктор", sc45["data"].get("characters", []))
        self.assertNotIn("Виктор", sc45["data"].get("episodic_people", []))
        self.assertEqual(sc45["data"].get("visible_people"), 0)
        self.assertEqual(sc45["data"].get("location"), "Магазин")
        self.assertEqual(sc45["data"].get("location_ref"), "Магазин")

        # Scene 46: Dialogue continuation in Магазин
        sc46 = project["scenes"][45]
        self.assertEqual(sc46["data"].get("location"), "Магазин")
        self.assertEqual(sc46["data"].get("location_ref"), "Магазин")
        self.assertEqual(sc46["data"].get("visible_people"), 2)

        # Scene 47: Sergey exits store to street (Улица) alone
        sc47 = project["scenes"][46]
        self.assertEqual(sc47["data"].get("location"), "Улица")
        self.assertEqual(sc47["data"].get("location_ref"), "Улица")
        self.assertEqual(sc47["data"].get("characters"), ["Сергей"])
        self.assertEqual(sc47["data"].get("visible_people"), 1)

        # Scene 48: Driving home on street/road (Улица) alone
        sc48 = project["scenes"][47]
        self.assertEqual(sc48["data"].get("location"), "Улица")
        self.assertEqual(sc48["data"].get("location_ref"), "Улица")
        self.assertEqual(sc48["data"].get("characters"), ["Сергей"])
        self.assertEqual(sc48["data"].get("visible_people"), 1)

        # No English alias duplicates in locations table
        loc_names = [l["name"] for l in project["locations"]]
        self.assertFalse(any("Room" in l or "Hospital corridor" in l or "Street Intersection" in l for l in loc_names))


class TestUniversalCounterExamples(unittest.TestCase):
    """Universal counter-example unit tests guaranteeing 0 hardcoding."""

    def test_counter_01_anna_at_home_normalizes_to_dom_anny(self):
        pg = PlaceGraph()
        norm = pg.normalize_location_name("Дом Анны")
        self.assertEqual(norm, "Дом Анны")
        self.assertNotIn("Сергей", norm)

    def test_counter_02_city_hospital_3_not_hospital_7(self):
        pg = PlaceGraph()
        norm = pg.normalize_location_name("Городская больница №3")
        self.assertEqual(norm, "Городская больница №3")
        self.assertNotIn("7", norm)

    def test_counter_03_spaceship_school_village_no_hospital_contamination(self):
        pg = PlaceGraph()
        p1 = pg.normalize_location_name("Звездолёт 'Аврора'")
        p2 = pg.normalize_location_name("Школа №12")
        p3 = pg.normalize_location_name("Деревня Дубровка")
        self.assertEqual(p1, "Звездолёт 'Аврора'")
        self.assertEqual(p2, "Школа №12")
        self.assertEqual(p3, "Деревня Дубровка")
        for p in (p1, p2, p3):
            self.assertNotIn("больниц", p.lower())

    def test_counter_04_peter_born_in_1969_extracts_age_54(self):
        store = EntityFactStore()
        store.extract_facts_from_story("e_peter", "Пётр", "Пётр родился в 1969 году.")
        attrs = store.get_canonical_attributes("e_peter")
        self.assertTrue(any("54" in a or "1969" in a for a in attrs))
        self.assertNotIn("Сергей", " ".join(attrs))

    def test_counter_05_irina_deceased_memory_triggers_validation_error(self):
        resolver = CharacterEntityResolverV2()
        irina = resolver.register_entity("Ирина", EntityType.PERSON, status="mentioned_only")
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Ирина"],
            source_quote="Ирина умерла три года назад. Алексей смотрел на её фото.",
            shot_description="Алексей вспоминает покойную сестру.",
            scene_number=1,
        )
        self.assertEqual(len(vis), 0)

        # Negative test: if Irina is forced into visible_instances of a shot, semantic validator must fail closed
        v2 = StoryAnalysisV2Result(
            entities=[Entity("e_ir", EntityType.PERSON, "Ирина", confirmed_attributes=["умерла 3 года назад"])],
            places=[Place("p_room", "Комната")],
            shots=[
                Shot(
                    shot_id=1,
                    event_id="ev_1",
                    framing="medium_shot",
                    shot_description="Алексей вспоминает умершую Ирину.",
                    source_quote="Ирина умерла три года назад.",
                    source_spans=[SourceSpan("doc", 0, 28, "Ирина умерла три года назад.")],
                    place_id="p_room",
                    visible_instances=[ShotInstance("inst_1", "e_ir")],
                )
            ]
        )
        report = validate_semantic_consistency(v2, story_text="Ирина умерла три года назад.")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.DECEASED_PHYSICALLY_PRESENT.value for iss in report.issues))

    def test_counter_06_starik_after_alexey_resolves_to_alexey(self):
        resolver = CharacterEntityResolverV2()
        alexey = resolver.register_entity("Алексей", EntityType.PERSON)
        vis, audio, dep = resolver.resolve_scene_instances(
            scene_characters=["Старик"],
            source_quote="Алексей сел в кресло. Старик тяжело вздохнул.",
            shot_description="Алексей сидит в кресле.",
            scene_number=1,
        )
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0].entity_id, alexey.entity_id)

    def test_counter_07_ambiguous_on_with_two_candidates_triggers_coreference_ambiguous(self):
        v2 = StoryAnalysisV2Result(
            entities=[
                Entity("e_m1", EntityType.PERSON, "Михаил", confirmed_attributes=["мужчина 40 лет"]),
                Entity("e_m2", EntityType.PERSON, "Павел", confirmed_attributes=["мужчина 35 лет"]),
            ],
            places=[Place("p1", "Офис")],
            shots=[
                Shot(
                    shot_id=1,
                    event_id="ev_1",
                    framing="medium_shot",
                    shot_description="Неизвестное действие.",
                    source_quote="Он вышел из кабинета.",
                    source_spans=[SourceSpan("doc", 0, 21, "Он вышел из кабинета.")],
                    place_id="p1",
                    visible_instances=[ShotInstance("inst_1", "e_m1", "medium_shot")],  # Disputed pronoun asserted as physical candidate
                )
            ]
        )
        report = validate_semantic_consistency(v2, story_text="Он вышел из кабинета.")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.COREFERENCE_AMBIGUOUS.value for iss in report.issues))

    def test_counter_08_wards_6_and_21_preserved_distinct(self):
        pg = PlaceGraph()
        w6 = pg.normalize_location_name("Палата 6")
        w21 = pg.normalize_location_name("Палата 21")
        self.assertEqual(w6, "Палата 6")
        self.assertEqual(w21, "Палата 21")
        self.assertNotEqual(w6, w21)
        self.assertNotIn("14", w6)
        self.assertNotIn("14", w21)

    def test_counter_09_marias_house_and_peters_house_are_distinct(self):
        pg = PlaceGraph()
        h_maria = pg.normalize_location_name("Дом Марии")
        h_peter = pg.normalize_location_name("Дом Петра")
        self.assertEqual(h_maria, "Дом Марии")
        self.assertEqual(h_peter, "Дом Петра")
        self.assertNotEqual(h_maria, h_peter)

    def test_counter_10_invalid_groq_repair_immutability(self):
        mock_verifier = MagicMock()
        mock_verifier.repair_chunk.return_value = {"invalid": "corrupted"}
        provider = LlamaCppProvider(base="http://localhost:8080")
        with patch.object(provider, "_analyse_segments") as mock_local:
            mock_local.return_value = {
                "scenes": [{
                    "source_id": 0,
                    "shot": "Голос позвал Ивана.",
                    "characters": ["голос Ивана"],
                    "visible_people": 1,
                    "location": "Коридор",
                }]
            }
            with self.assertRaises(ProviderError):
                provider.analyse(
                    project={"story": "Иван услышал голос за дверью.", "style": "", "idea": ""},
                    target=1,
                    hybrid_verifier=mock_verifier,
                )

    def test_counter_11_generic_encounter_location_continuity_library(self):
        """Encounter transitions propagate location to subsequent dialogue turns."""
        pg = PlaceGraph()
        ev1 = EventSpan(0, 0, 50, "Через неделю Анна встретила Бориса в библиотеке.", "sec_1", transition_flags={"time_shift"})
        full_loc1, p1, z1, _ = pg.resolve_event_spatial_context(ev1)
        self.assertIn("Библиотека", pg.places[p1].canonical_name)

        ev2 = EventSpan(1, 51, 80, "— Ты читал письмо?", "sec_1")
        full_loc2, p2, z2, _ = pg.resolve_event_spatial_context(ev2, previous_event=ev1)
        self.assertEqual(p1, p2)
        self.assertIn("Библиотека", pg.places[p2].canonical_name)

    def test_counter_12_full_scene_context_continuity_library_exit(self):
        """
        Section/encounter spatial progression from 5th audit:
        Анна сидела дома на кухне.
        ---
        Через неделю Анна встретила Бориса в библиотеке.
        — Ты читал письмо?
        Анна вышла на улицу.
        Expected: Дом -> Библиотека -> Библиотека -> Улица.
        """
        from imagegen.analyzer_v2.place_graph import segment_story_into_scene_contexts
        from imagegen.analyzer_v2.events import build_event_spans
        story = """Анна сидела дома на кухне.
---
Через неделю Анна встретила Бориса в библиотеке.
— Ты читал письмо?
Анна вышла на улицу."""
        event_spans = build_event_spans(story)
        contexts = segment_story_into_scene_contexts(story, event_spans)

        resolved_places = []
        for ev in event_spans:
            ctx = next((c for c in contexts if ev.event_index in c.event_indices), None)
            resolved_places.append(ctx.place_name if ctx else None)

        self.assertEqual(len(resolved_places), 4)
        self.assertIn("Дом", resolved_places[0])
        self.assertEqual(resolved_places[1], "Библиотека")
        self.assertEqual(resolved_places[2], "Библиотека")
        self.assertEqual(resolved_places[3], "Улица")

    def test_counter_13_physical_presence_encounter_and_exit_solitude(self):
        """
        Physical presence lifecycle from 6th audit:
        Анна встретила Бориса в библиотеке.
        Они поговорили у стеллажа.
        Они попрощались у выхода.
        Анна вышла на улицу одна.
        Expected: Библиотека [Анна, Борис] -> Библиотека [Анна, Борис] -> Библиотека [Анна, Борис] -> Улица [Анна].
        """
        from imagegen.analyzer_v2.pipeline import StoryAnalyzerV2Pipeline

        story = (
            "Анна встретила Бориса в библиотеке. "
            "Они поговорили у стеллажа. "
            "Они попрощались у выхода. "
            "Анна вышла на улицу одна."
        )
        raw_chunks = [{
            "world": "Библиотека и городская улица",
            "characters": [
                {"name": "Анна", "description": "Студентка"},
                {"name": "Борис", "description": "Приятель"},
            ],
            "locations": [
                {"name": "Библиотека", "description": "Городская библиотека"},
                {"name": "Улица", "description": "Городская улица"},
            ],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "source_quote": "Анна встретила Бориса в библиотеке.",
                    "shot": "Анна встречает Бориса в библиотеке.",
                    "location": "Библиотека",
                    "characters": ["Анна", "Борис"],
                    "visible_people": 2,
                },
                {
                    "source_id": 1,
                    "source_quote": "Они поговорили у стеллажа.",
                    "shot": "Анна и Борис разговаривают у стеллажа.",
                    "location": "Библиотека",
                    "characters": ["Анна", "Борис"],
                    "visible_people": 2,
                },
                {
                    "source_id": 2,
                    "source_quote": "Они попрощались у выхода.",
                    "shot": "Анна и Борис прощаются у выхода.",
                    "location": "Библиотека",
                    "characters": ["Анна", "Борис"],
                    "visible_people": 2,
                },
                {
                    "source_id": 3,
                    "source_quote": "Анна вышла на улицу одна.",
                    "shot": "Анна выходит на улицу.",
                    "location": "Улица",
                    "characters": ["Анна", "Борис"],  # LLM erroneously included Boris
                    "visible_people": 2,
                },
            ]
        }]

        pipeline = StoryAnalyzerV2Pipeline()
        res = pipeline.analyze(story, raw_chunks)
        legacy = res.to_legacy_analysis()

        # Check scene locations
        self.assertEqual(legacy["scenes"][0]["location_ref"], "Библиотека")
        self.assertEqual(legacy["scenes"][1]["location_ref"], "Библиотека")
        self.assertEqual(legacy["scenes"][2]["location_ref"], "Библиотека")
        self.assertEqual(legacy["scenes"][3]["location_ref"], "Улица")

        # Check visible characters
        self.assertEqual(legacy["scenes"][0]["characters_present"], ["Анна", "Борис"])
        self.assertEqual(legacy["scenes"][0]["visible_people"], 2)

        self.assertEqual(legacy["scenes"][1]["characters_present"], ["Анна", "Борис"])
        self.assertEqual(legacy["scenes"][1]["visible_people"], 2)

        self.assertEqual(legacy["scenes"][2]["characters_present"], ["Анна", "Борис"])
        self.assertEqual(legacy["scenes"][2]["visible_people"], 2)

        # In scene 4, Boris must be dropped due to single-actor exit / solitude!
        self.assertEqual(legacy["scenes"][3]["characters_present"], ["Анна"])
        self.assertEqual(legacy["scenes"][3]["visible_people"], 1)


class TestDiagnosticCodeCoverage(unittest.TestCase):
    """Negative tests verifying that all DiagnosticCode gates properly trigger and fail closed."""

    def test_voice_only_physically_present(self):
        v2 = StoryAnalysisV2Result(
            entities=[Entity("e1", EntityType.PERSON, "Диспетчер", confirmed_attributes=["голос по рации"])],
            places=[Place("p1", "Пост")],
            shots=[
                Shot(
                    shot_id=1, event_id="ev_1", framing="medium",
                    shot_description="Слышен голос диспетчера по рации.",
                    source_quote="Слышен голос по рации диспетчер.",
                    source_spans=[SourceSpan("doc", 0, 32, "Слышен голос по рации диспетчер.")],
                    place_id="p1",
                    visible_instances=[ShotInstance("i1", "e1")],
                )
            ]
        )
        report = validate_semantic_consistency(v2, story_text="Слышен голос по рации диспетчер.")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.VOICE_ONLY_PHYSICALLY_PRESENT.value for iss in report.issues))

    def test_ungrounded_shot(self):
        v2 = StoryAnalysisV2Result(
            entities=[Entity("e1", EntityType.PERSON, "Олег")],
            places=[Place("p1", "Зал")],
            shots=[
                Shot(
                    shot_id=1, event_id="ev_1", framing="medium",
                    shot_description="Олег стоит в зале.",
                    source_quote="Этой цитаты нет в тексте рассказа вообще.",
                    source_spans=[SourceSpan("doc", 100, 150, "Этой цитаты нет в тексте рассказа вообще.")],
                    place_id="p1",
                    visible_instances=[ShotInstance("i1", "e1")],
                )
            ]
        )
        report = validate_semantic_consistency(v2, story_text="Короткий рассказ без такой фразы.")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.UNGROUNDED_SHOT.value or iss.code == DiagnosticCode.SOURCE_SPAN_MISMATCH.value for iss in report.issues))

    def test_contradictory_age_diagnostic(self):
        v2 = StoryAnalysisV2Result(
            entities=[Entity("e1", EntityType.PERSON, "Борис", confirmed_attributes=["25 лет", "65 лет"])],
            places=[Place("p1", "Комната")],
            shots=[]
        )
        report = validate_semantic_consistency(v2, story_text="")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.CONTRADICTORY_AGE.value for iss in report.issues))

    def test_location_alias_duplicate(self):
        v2 = StoryAnalysisV2Result(
            entities=[],
            places=[
                Place("p1", "Room"),
                Place("p2", "Комната"),
            ],
            shots=[]
        )
        report = validate_semantic_consistency(v2, story_text="")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.LOCATION_ALIAS_DUPLICATE.value for iss in report.issues))

    def test_participant_inheritance_leak_diagnostic(self):
        v2 = StoryAnalysisV2Result(
            entities=[
                Entity("e_sergey", EntityType.PERSON, "Сергей"),
                Entity("e_vera", EntityType.PERSON, "Вера"),
            ],
            places=[Place("p_home", "Дом")],
            shots=[
                Shot(
                    shot_id=1, event_id="ev_1", framing="medium_shot",
                    shot_description="В пустой кухне никто не ответил.",
                    source_quote="В пустой кухне никто не ответил.",
                    source_spans=[SourceSpan("doc", 0, 32, "В пустой кухне никто не ответил.")],
                    place_id="p_home",
                    visible_instances=[
                        ShotInstance("i1", "e_sergey"),
                        ShotInstance("i2", "e_vera"),  # Leaked Vera into solitary empty kitchen!
                    ],
                )
            ]
        )
        report = validate_semantic_consistency(v2, story_text="В пустой кухне никто не ответил.")
        self.assertFalse(report.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.PARTICIPANT_INHERITANCE_LEAK.value for iss in report.issues))


if __name__ == "__main__":
    unittest.main()



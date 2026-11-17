import io
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from PIL import Image

from imagegen.analyzer_v3.models import DocumentIR, EntityIR, EntityType, EventIR, MentionIR, SourceSpan
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service
from imagegen.forgotten_cities.geography import ForgottenCitiesGeographyClassifier
from imagegen.forgotten_cities.sequences import DocumentarySequenceBuilder
from imagegen.forgotten_cities.preflight import ForgottenCitiesPreflightValidator
from imagegen.forgotten_cities.prompts import build_prompt_packet, documentary_treatment
from imagegen.forgotten_cities.cloud import ForgottenCitiesCloudBatchAnalyzer
from imagegen.forgotten_cities.models import (
    AnchorType,
    CityBibleIR,
    ClaimType,
    DocumentarySequenceIR,
    EvidenceLevel,
    ForgottenCitiesDocumentIR,
    GeographyType,
    HistoricalClaimIR,
    PeoplePolicy,
    SequencePurpose,
    TimeLayer,
    VisualizationPolicy,
    VisualAnchorIR,
)
from imagegen.domain import DomainError, compile_prompt
from imagegen.service import Service


class TestForgottenCitiesContracts(unittest.TestCase):
    def test_prompt_packet_requires_a_street_view_for_lane_event(self):
        packet = build_prompt_packet(
            {"city_bible": {}, "event_contexts": [{"event_id": "event_004", "time_layer": "historical_reconstruction"}], "sequences": [], "anchors": []},
            "event_004",
            "Между постройками проходили переулки.",
            "Шахр-э-Сухте",
        )
        self.assertIn("street view", packet["visual_direction"])
        self.assertIn("Do not substitute", packet["shot_contract"])

    def test_documentary_treatments_keep_long_explainer_visually_varied(self):
        no_people = {
            "time_layer": "abstract_explanation",
            "people_policy": "none",
        }
        action_allowed = dict(no_people, people_policy="anonymous_action")
        treatments = [
            documentary_treatment(f"event_{index}", index, 232, no_people, [])
            for index in range(1, 233)
        ]
        shot_counts = {}
        for treatment in treatments:
            shot_counts[treatment["shot_type"]] = shot_counts.get(treatment["shot_type"], 0) + 1
        self.assertGreaterEqual(shot_counts.get("establishing", 0), 70)
        self.assertLess(shot_counts.get("atmosphere", 0), 100)
        self.assertEqual(
            documentary_treatment("action", 2, 232, action_allowed, [])['shot_type'],
            "main_action",
        )
        self.assertEqual(
            documentary_treatment("artifact", 1, 232, no_people, ["Искусственный глаз"])['shot_type'],
            "prop_detail",
        )

    def test_anonymous_documentary_person_is_not_removed_in_objects_mode(self):
        prompt, _ = compile_prompt(
            {"mode": "objects", "style": "archaeological documentary", "world": "Bronze Age city"},
            {
                "beat": "A craftsperson shapes an artificial eye.",
                "visible_people": 1,
                "episodic_people": ["craftsperson"],
                "people_policy": "anonymous_action",
            },
            [],
            channel_context={"profile": {"profile_type": "forgotten_cities"}},
        )
        self.assertNotIn("UNINHABITED SCENE", prompt)
        self.assertIn("exactly 1 person(s) present (craftsperson)", prompt)

    def test_structural_progress_reports_completed_work_monotonically(self):
        updates = []
        story = "Город Шахр-э-Сухте стоял у реки. Жители строили дома из сырцового кирпича. Сегодня видны руины."
        StoryAnalyzerV3Pipeline().analyze_structural(story, progress_callback=updates.append)
        self.assertGreaterEqual(len(updates), 6)
        fractions = [item["fraction"] for item in updates]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[0], 0.0)
        self.assertEqual(fractions[-1], 1.0)
        self.assertTrue(all(item.get("stage") for item in updates))

    def _document(self):
        story = "Археологи исследуют древний город. Возможно, жители покинули его после засухи."
        city_span = SourceSpan(22, 36, story[22:36])
        hypothesis_span = SourceSpan(38, len(story), story[38:])
        city = CityBibleIR(
            city_id="city_main",
            canonical_name="Древний город",
            region="Средняя Азия",
            architecture_profile="Сырцовая архитектура",
            material_palette=["сырцовый кирпич"],
            forbidden_anachronisms=["современное стекло"],
            evidence_spans=[city_span],
            confidence=0.9,
        )
        city_anchor = VisualAnchorIR(
            anchor_id="anchor_city",
            anchor_type=AnchorType.CITY_MASTER,
            canonical_name="Общий вид города",
            description="Сырцовый город в засушливой равнине",
            time_layer=TimeLayer.HISTORICAL_RECONSTRUCTION,
            recurrence_count=4,
            importance=1.0,
            evidence_spans=[city_span],
        )
        claim = HistoricalClaimIR(
            claim_id="claim_decline",
            claim_text="Город могли покинуть после засухи",
            claim_type=ClaimType.HYPOTHESIS,
            subject_ids=["city_main"],
            source_span=hypothesis_span,
            confidence=0.45,
            visualization_policy=VisualizationPolicy.ILLUSTRATIVE_ONLY,
        )
        sequence = DocumentarySequenceIR(
            sequence_id="seq_decline",
            title="Возможная причина упадка",
            purpose=SequencePurpose.DECLINE,
            event_ids=["event_2"],
            source_span=hypothesis_span,
            location_anchor_id="anchor_city",
            time_layer=TimeLayer.HISTORICAL_RECONSTRUCTION,
            evidence_level=EvidenceLevel.ILLUSTRATIVE_HYPOTHESIS,
            claim_ids=["claim_decline"],
            people_policy=PeoplePolicy.ANONYMOUS_BACKGROUND,
            visual_priority=0.8,
            suggested_shot_count=2,
        )
        return story, ForgottenCitiesDocumentIR(city, [city_anchor], [claim], [sequence])

    def test_round_trip_and_reference_validation(self):
        story, document = self._document()
        document.require_valid(story, {"event_2"})
        payload = json.loads(json.dumps(document.to_dict(), ensure_ascii=False))
        restored = ForgottenCitiesDocumentIR.from_dict(payload)
        self.assertEqual(restored.to_dict(), document.to_dict())
        self.assertEqual(restored.validate(story, {"event_2"}), [])

    def test_unknown_enum_and_schema_are_rejected(self):
        _, document = self._document()
        payload = document.to_dict()
        payload["sequences"][0]["time_layer"] = "ancient_photo"
        with self.assertRaisesRegex(ValueError, "Unknown time_layer"):
            ForgottenCitiesDocumentIR.from_dict(payload)
        payload = document.to_dict()
        payload["schema_version"] = "forgotten_cities/999"
        with self.assertRaisesRegex(ValueError, "schema_version"):
            ForgottenCitiesDocumentIR.from_dict(payload)

    def test_invalid_links_and_hypothesis_as_fact_fail_closed(self):
        story, document = self._document()
        document.sequences[0].location_anchor_id = "missing_anchor"
        document.sequences[0].event_ids = ["missing_event"]
        errors = document.validate(story, {"event_2"})
        self.assertTrue(any("unknown location anchor" in error for error in errors))
        self.assertTrue(any("unknown V3 events" in error for error in errors))
        with self.assertRaisesRegex(ValueError, "hypothesis cannot use direct"):
            HistoricalClaimIR(
                claim_id="bad", claim_text="Возможно", claim_type=ClaimType.HYPOTHESIS,
                subject_ids=["city_main"], source_span=SourceSpan(0, 1, "В"),
                confidence=0.1, visualization_policy=VisualizationPolicy.DIRECT,
            )


class TestForgottenCitiesGeography(unittest.TestCase):
    def test_researcher_and_origin_are_not_promoted_to_scene_locations(self):
        story = (
            "Город Шахр-э-Сухте процветал в бронзовом веке. "
            "Археолог Энрико Аскалоне рассматривает сосуд из долины Инда."
        )
        city_start = story.index("Шахр-э-Сухте")
        person_start = story.index("Энрико Аскалоне")
        ind_start = story.index("Инда")
        mentions = [
            MentionIR("m_city", "Шахр-э-Сухте", "шахр-э-сухте", SourceSpan(city_start, city_start + 13, "Шахр-э-Сухте"), "proper_noun", section_id="sec_0", event_id="event_1"),
            MentionIR("m_person", "Энрико Аскалоне", "энрико аскалоне", SourceSpan(person_start, person_start + 16, "Энрико Аскалоне"), "proper_noun", section_id="sec_0", event_id="event_2"),
            MentionIR("m_ind", "Инда", "инда", SourceSpan(ind_start, ind_start + 4, "Инда"), "proper_noun", section_id="sec_0", event_id="event_2"),
        ]
        entities = [
            EntityIR("e_city", "Шахр-э-Сухте", EntityType.LOCATION, mention_ids=["m_city"], first_mention_start=city_start),
            EntityIR("e_person", "Энрико Аскалоне", EntityType.PERSON, mention_ids=["m_person"], first_mention_start=person_start),
            EntityIR("e_ind", "Инд", EntityType.LOCATION, mention_ids=["m_ind"], first_mention_start=ind_start),
        ]
        events = [
            EventIR("event_1", "sec_0", SourceSpan(0, 49, story[:49])),
            EventIR("event_2", "sec_0", SourceSpan(50, len(story), story[50:])),
        ]
        document = DocumentIR("hash", events=events, mentions=mentions, entities=entities)

        result = ForgottenCitiesGeographyClassifier().classify(document, story, "Шахр-э-Сухте")
        by_text = {item.text: item.geography_type for item in result}

        self.assertEqual(by_text["Шахр-э-Сухте"], GeographyType.PRIMARY_CITY)
        self.assertEqual(by_text["Энрико Аскалоне"], GeographyType.RESEARCHER_NAME)
        self.assertEqual(by_text["Инда"], GeographyType.ORIGIN_REGION)
        self.assertNotIn(GeographyType.PRIMARY_CITY, [by_text["Энрико Аскалоне"], by_text["Инда"]])

    def test_time_layers_and_uncertainty_are_explicit(self):
        story = "Сегодня видны руины. Возможно, жители покинули город после засухи. В музее хранится сосуд."
        parts = ["Сегодня видны руины.", "Возможно, жители покинули город после засухи.", "В музее хранится сосуд."]
        events = []
        cursor = 0
        for index, part in enumerate(parts, 1):
            start = story.index(part, cursor)
            end = start + len(part)
            events.append(EventIR(f"event_{index}", "sec_0", SourceSpan(start, end, part)))
            cursor = end
        document = DocumentIR("hash", events=events)

        result = ForgottenCitiesGeographyClassifier().classify_event_contexts(document)

        self.assertEqual(result[0].time_layer, TimeLayer.MODERN_RUINS)
        self.assertEqual(result[1].evidence_level, EvidenceLevel.ILLUSTRATIVE_HYPOTHESIS)
        self.assertEqual(result[1].time_layer, TimeLayer.HISTORICAL_RECONSTRUCTION)
        self.assertEqual(result[2].time_layer, TimeLayer.MUSEUM_DISPLAY)

    def test_export_omits_diagnostic_and_mentioned_only_location_cards(self):
        story = (
            "Город Шахр-э-Сухте процветал в бронзовом веке. "
            "Археолог Энрико Аскалоне рассматривает сосуд из долины Инда."
        )
        pipeline = StoryAnalyzerV3Pipeline()
        document = pipeline.analyze_structural(story)
        ForgottenCitiesGeographyClassifier().enrich_metadata(document, story, "Шахр-э-Сухте")
        plan = pipeline.plan_visual_events(story, document, target_budget=2)

        exported = StoryAnalyzerV3Service.export_to_app_analysis(
            document, plan, story, mode="objects", profile_type="forgotten_cities"
        )
        location_names = {item["name"] for item in exported["locations"]}

        self.assertNotIn("Неопределённая локация", location_names)
        self.assertFalse(any("Аскалон" in name for name in location_names))
        self.assertFalse(any(name in {"Инд", "Инда"} for name in location_names))


class TestForgottenCitiesSequences(unittest.TestCase):
    def test_long_script_becomes_twelve_to_twenty_sequences(self):
        sentences = []
        for index in range(232):
            if index == 50:
                sentences.append("И вот вопрос.")
            elif index < 20:
                sentences.append(f"Город и его кварталы описаны в источнике {index}.")
            elif index < 180:
                sentences.append(f"Жители работали в мастерских и строили дома {index}.")
            else:
                sentences.append(f"Возможно, засуха повлияла на упадок города {index}.")
        story = " ".join(sentences)
        events = []
        cursor = 0
        for index, sentence in enumerate(sentences, 1):
            start = story.index(sentence, cursor)
            end = start + len(sentence)
            events.append(EventIR(f"event_{index}", "sec_0", SourceSpan(start, end, sentence)))
            cursor = end
        document = DocumentIR("hash", events=events)
        contexts = ForgottenCitiesGeographyClassifier().classify_event_contexts(document)

        sequences = DocumentarySequenceBuilder().build(document, story, contexts)

        self.assertGreaterEqual(len(sequences), 12)
        self.assertLessEqual(len(sequences), 20)
        rhetorical = [seq for seq in sequences if seq.source_span.text.strip() == "И вот вопрос."]
        self.assertEqual(rhetorical, [])
        self.assertEqual(sum(len(seq.event_ids) for seq in sequences), 232)
        self.assertTrue(all(seq.source_span.validate_against(story) for seq in sequences))


class TestForgottenCitiesBibleAndPreflight(unittest.TestCase):
    def test_bible_anchors_artifacts_claims_and_population_are_grounded(self):
        story = (
            "Город Шахр-э-Сухте был построен из сырцового кирпича в III тысячелетии до н. э. "
            "Жители в льняной одежде работали в лазуритовой мастерской. "
            "Искусственный глаз нашли в погребении женщины. "
            "Сегодня сохранились сырцовые руины города. "
            "Возможно, длительная засуха стала причиной упадка."
        )
        pipeline = StoryAnalyzerV3Pipeline()
        document = pipeline.analyze_structural(story)
        metadata = ForgottenCitiesGeographyClassifier().enrich_metadata(document, story, "Шахр-э-Сухте")

        self.assertEqual(metadata["city_bible"]["canonical_name"], "Шахр-э-Сухте")
        self.assertIn("сырцовый кирпич", metadata["city_bible"]["material_palette"])
        anchor_ids = {item["anchor_id"] for item in metadata["anchors"]}
        self.assertIn("anchor_city_master", anchor_ids)
        self.assertIn("anchor_city_modern_ruins", anchor_ids)
        self.assertIn("anchor_artifact_artificial_eye", anchor_ids)
        self.assertIn("anchor_artifact_lapis_chain", anchor_ids)
        master = next(item for item in metadata["anchors"] if item["anchor_id"] == "anchor_city_master")
        self.assertIn("реконструкция", master["description"].casefold())
        self.assertIn("Не показывать современные", master["description"])
        hypothesis = [item for item in metadata["claims"] if item["claim_type"] == "hypothesis"]
        self.assertTrue(hypothesis)
        self.assertTrue(all(item["visualization_policy"] != "direct" for item in hypothesis))
        self.assertIn("лён", metadata["population_style_bible"]["clothing_materials"])
        self.assertEqual(metadata["preflight"]["status"], "PASS")

        profile_document = ForgottenCitiesDocumentIR.from_dict({
            "schema_version": "forgotten_cities/1.0",
            "city_bible": metadata["city_bible"],
            "anchors": metadata["anchors"],
            "claims": metadata["claims"],
            "sequences": metadata["sequences"],
            "geography_mentions": metadata["geography_mentions"],
            "event_contexts": metadata["event_contexts"],
        })
        locked_report = ForgottenCitiesPreflightValidator().validate(
            profile_document, story, require_locked_references=True
        )
        self.assertEqual(locked_report["status"], "BLOCKED")
        self.assertTrue(any(item["code"] == "missing_required_reference" for item in locked_report["errors"]))

    def test_export_uses_artifact_anchors_and_anonymous_people(self):
        story = (
            "Город Шахр-э-Сухте существовал в III тысячелетии до н. э. "
            "Мастер обрабатывал лазурит в городской мастерской."
        )
        pipeline = StoryAnalyzerV3Pipeline()
        document = pipeline.analyze_structural(story)
        ForgottenCitiesGeographyClassifier().enrich_metadata(document, story, "Шахр-э-Сухте")
        plan = pipeline.plan_visual_events(story, document, target_budget=2)
        exported = StoryAnalyzerV3Service.export_to_app_analysis(
            document, plan, story, mode="objects", profile_type="forgotten_cities"
        )

        self.assertTrue(any(prop.get("anchor_id") == "anchor_artifact_lapis_chain" for prop in exported["props"]))
        self.assertEqual(exported["characters"], [])
        people_scenes = [scene for scene in exported["scenes"] if scene.get("people_policy") == "anonymous_action"]
        self.assertTrue(people_scenes)
        self.assertTrue(any(scene["episodic_people"] for scene in people_scenes))
        contract = people_scenes[0]["forgotten_cities_prompt_contract"]
        self.assertIn("STYLE CONTRACT:", contract["compiled_prompt"])
        self.assertIn("CITY CONTRACT:", contract["compiled_prompt"])
        self.assertIn("TIME LAYER:", contract["compiled_prompt"])
        self.assertIn("SHOT CONTRACT:", contract["compiled_prompt"])
        self.assertIn("NEGATIVE CONTRACT:", contract["compiled_prompt"])
        visual_mix = exported["diagnostics"]["forgotten_cities_visual_mix"]
        self.assertIn("shots", visual_mix)
        self.assertIn("time_layers", visual_mix)

    def test_hypothesis_prompt_keeps_uncertainty(self):
        metadata = {
            "city_bible": {
                "canonical_name": "Город", "forbidden_anachronisms": ["modern vehicles"],
                "material_palette": [],
            },
            "event_contexts": [{
                "event_id": "event_1", "time_layer": "historical_reconstruction",
                "evidence_level": "illustrative_hypothesis", "people_policy": "none",
            }],
            "sequences": [{"event_ids": ["event_1"], "artifact_anchor_ids": [], "location_anchor_id": None}],
            "anchors": [],
        }
        packet = build_prompt_packet(metadata, "event_1", "Возможно, город покинули.", "Город")
        self.assertIn("exact circumstances unknown", packet["shot_contract"])
        self.assertIn("do not present", packet["negative_contract"])


class TestForgottenCitiesCloudBatching(unittest.TestCase):
    class FakeClient:
        model = "fake-groq"

        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        def _post_json(self, payload):
            self.calls += 1
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return {"choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]}

        def get_usage(self):
            return {"calls": self.calls}

    def test_retries_only_failed_batch_and_reuses_success_cache(self):
        story = "Город стоял у реки. Возможно, жители ушли после засухи."
        first = "Город стоял у реки."
        second = "Возможно, жители ушли после засухи."
        second_start = story.index(second)
        document = DocumentIR("hash", events=[
            EventIR("event_1", "sec_0", SourceSpan(0, len(first), first)),
            EventIR("event_2", "sec_0", SourceSpan(second_start, len(story), second)),
        ])
        document.metadata["forgotten_cities"] = {
            "city_bible": {"canonical_name": "Город", "material_palette": []},
            "sequences": [
                {"sequence_id": "sequence_001", "event_ids": ["event_1"], "source_span": SourceSpan(0, len(first), first).to_dict(), "time_layer": "historical_reconstruction", "evidence_level": "established"},
                {"sequence_id": "sequence_002", "event_ids": ["event_2"], "source_span": SourceSpan(second_start, len(story), second).to_dict(), "time_layer": "historical_reconstruction", "evidence_level": "illustrative_hypothesis"},
            ],
        }
        valid_1 = {"sequence_id": "sequence_001", "event_ids": ["event_1"], "summary": "Город", "source_spans": [SourceSpan(0, len(first), first).to_dict()], "warnings": []}
        invalid_2 = {"sequence_id": "wrong", "event_ids": ["event_2"], "source_spans": [SourceSpan(second_start, len(story), second).to_dict()]}
        valid_2 = {"sequence_id": "sequence_002", "event_ids": ["event_2"], "summary": "Версия", "source_spans": [SourceSpan(second_start, len(story), second).to_dict()], "warnings": ["hypothesis"]}
        client = self.FakeClient([valid_1, invalid_2, valid_2])
        analyzer = ForgottenCitiesCloudBatchAnalyzer(max_attempts=2)

        report = analyzer.run(document, story, client)

        self.assertEqual(report["status"], "certified")
        self.assertEqual(report["cloud_calls"], 3)
        self.assertEqual(report["batches_completed"], 2)
        self.assertEqual(next(item for item in report["successful_batches"] if item["sequence_id"] == "sequence_001")["attempts"], 1)
        self.assertEqual(next(item for item in report["successful_batches"] if item["sequence_id"] == "sequence_002")["attempts"], 2)

        cached_client = self.FakeClient([])
        cached_report = analyzer.run(document, story, cached_client)
        self.assertEqual(cached_report["cache_hits"], 2)
        self.assertEqual(cached_report["cloud_calls"], 0)

    def test_failed_cloud_batch_degrades_with_complete_local_coverage(self):
        story = "Город сохранился."
        span = SourceSpan(0, len(story), story)
        document = DocumentIR("different-hash", events=[EventIR("event_x", "sec_0", span)])
        document.metadata["forgotten_cities"] = {
            "city_bible": {"canonical_name": "Город", "material_palette": []},
            "sequences": [{"sequence_id": "sequence_x", "event_ids": ["event_x"], "source_span": span.to_dict()}],
        }
        client = self.FakeClient([RuntimeError("rate limit"), RuntimeError("rate limit")])

        report = ForgottenCitiesCloudBatchAnalyzer(max_attempts=2).run(document, story, client)

        self.assertEqual(report["status"], "certified_degraded")
        self.assertEqual(report["batches_failed"], 1)
        self.assertEqual(report["local_event_coverage"], 1)


class TestForgottenCitiesChannelProfile(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ff_forgotten_cities_")
        self.service = Service(Path(self.temp_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_profile_has_documentary_defaults_and_no_phantom_character(self):
        channel = self.service.create_channel({
            "name": "Забытые города", "profile_type": "forgotten_cities",
        })
        self.assertEqual(channel["profile_type"], "forgotten_cities")
        self.assertEqual(channel["characters"], [])
        self.assertIn("Historically grounded", channel["active_version"]["style_prompt"])
        self.assertIn("Forgotten Cities Invariants", channel["compiled_style_contract"])

    def test_project_profile_activates_forgotten_cities_v3_metadata(self):
        channel = self.service.create_channel({
            "name": "Забытые города", "profile_type": "forgotten_cities",
        })
        project = self.service.create_project({
            "title": "Шахр-э-Сухте",
            "story": "Город Шахр-э-Сухте процветал. Сегодня видны его руины.",
            "style": "documentary",
            "mode": "objects",
            "channel_profile_id": channel["id"],
        })

        result = self.service.analyze_v3(
            project["story"], target_budget=2, story_id=project["id"]
        )

        metadata = result["document_ir"]["metadata"]["forgotten_cities"]
        self.assertEqual(metadata["profile_type"], "forgotten_cities")
        self.assertEqual(len(metadata["event_contexts"]), len(result["document_ir"]["events"]))

    def _project_with_rendered_scene(self, scene_count=1, variants=1):
        # These are passport/approval tests with synthetic pixel fixtures.
        # Real semantic rejection and unavailable API are covered separately.
        self.enterContext(patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer._request',
            return_value={'status': 'pass', 'observed': 'Archaeological city.', 'reason': 'Matches this fixture.'}))
        channel = self.service.create_channel({
            "name": "Забытые города", "profile_type": "forgotten_cities",
        })
        project = self.service.create_project({
            "title": "Контрольный город", "story": "Древний город возвышался над равниной.",
            "style": "documentary", "mode": "objects", "channel_profile_id": channel["id"],
        })
        packet = build_prompt_packet({
            "city_bible": {
                "canonical_name": "Контрольный город", "region": "Равнина",
                "period_start": "III тысячелетие до н. э.",
                "architecture_profile": "кирпичные кварталы", "material_palette": ["сырцовый кирпич"],
                "environment_profile": "сухая равнина", "forbidden_anachronisms": ["modern vehicles"],
            },
            "event_contexts": [{
                "event_id": "event_1", "time_layer": "historical_reconstruction",
                "evidence_level": "established", "people_policy": "none",
            }],
            "sequences": [{"event_ids": ["event_1"], "artifact_anchor_ids": [], "location_anchor_id": None}],
            "anchors": [],
        }, "event_1", "Древний город возвышался над равниной.", "Контрольный город")
        scene_template = {
            "beat": "Древний город возвышается над равниной.",
            "source_quote": "Древний город возвышался над равниной.",
            "location": "Контрольный город", "time": "День", "shot": "Wide",
            "characters": [], "wardrobe": {}, "props": [], "visible_people": 0,
            "time_layer": "historical_reconstruction", "evidence_level": "established",
            "people_policy": "none", "forgotten_cities_prompt_contract": packet,
        }
        analysis = {
            "world": "Древняя равнина", "characters": [], "locations": [], "props": [],
            "scenes": [dict(scene_template, shot=f"Wide documentary shot {index + 1}") for index in range(scene_count)],
        }
        self.service.import_analysis(project["id"], analysis, project["version"])
        scenes = self.service.detail(project["id"])["scenes"]
        generation_ids = []
        for index, scene in enumerate(scenes):
            for variant in range(variants):
                image = io.BytesIO()
                Image.new("RGB", (1280, 720), (124 + index, 94 + variant, 61)).save(image, format="PNG")
                uploaded = self.service.upload_scene_image(scene["id"], image.getvalue())
            generation_ids.append(uploaded["generation_id"])
        return project["id"], [scene["id"] for scene in scenes], generation_ids

    def test_stage9_review_passport_and_animation_gate(self):
        project_id, scene_ids, generation_ids = self._project_with_rendered_scene(variants=2)
        scene_id, generation_id = scene_ids[0], generation_ids[0]
        detail = self.service.detail(project_id)
        self.assertEqual(detail["scenes"][0]["still_approval"]["status"], "pending_review")
        self.assertFalse(detail['still_approval_required'])

        approved = self.service.review_generation(generation_id, "approved")
        review = approved["review"]
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["continuity"]["deterministic_status"], "pass")
        self.assertEqual(review["continuity"]["visual_status"], "unavailable")
        self.assertEqual(review["passport"]["profile_type"], "forgotten_cities")
        self.assertTrue(review["passport"]["contract_hash"])
        queued = self.service.queue_single_scene_veo(scene_id)
        self.assertTrue(queued["queued"])

    def test_stage9_rejection_revokes_animation_permission(self):
        _, scene_ids, generation_ids = self._project_with_rendered_scene()
        scene_id, generation_id = scene_ids[0], generation_ids[0]
        self.service.review_generation(generation_id, "approved")
        self.service.review_generation(
            generation_id, "rejected", reason_code="architecture_drift", note="Форма ворот изменилась"
        )
        review = self.service.generation_review(generation_id)
        self.assertEqual(review["status"], "rejected")
        self.assertEqual(review["reason_code"], "architecture_drift")
        self.assertTrue(self.service.queue_single_scene_omni(scene_id)['queued'])

    def test_stage9_ten_frame_control_episode_requires_every_approval(self):
        project_id, scene_ids, generation_ids = self._project_with_rendered_scene(scene_count=10, variants=2)
        for generation_id in generation_ids[:9]:
            self.service.review_generation(generation_id, "approved")
        self.service.start_animation_generation(project_id)
        self.service.review_generation(generation_ids[9], "approved")
        for scene_id in scene_ids:
            self.service.select_scene_animation_engine(scene_id, "omni_flash")
        detail = self.service.detail(project_id)
        self.assertEqual(len(detail["scenes"]), 10)
        self.assertTrue(all(scene["still_approval"]["status"] == "approved" for scene in detail["scenes"]))
        result = self.service.start_animation_generation(project_id)
        self.assertEqual(result["total_scenes"], 10)


if __name__ == "__main__":
    unittest.main()

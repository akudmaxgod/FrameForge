import unittest
from pathlib import Path

from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service


class GroundingContracts(unittest.TestCase):

    def test_named_craftsperson_absorbs_grounded_role_aliases(self):
        text = (
            "Кузнец Мартин чинил плуг. Ученик Лука помогал ему. "
            "Лука спросил, почему мастер задержался. Позже кузнец разжёг горн."
        )
        doc = self.pipeline.analyze_structural(text)
        people = [e for e in doc.entities if e.entity_type.value == "person"]
        martin = next(e for e in people if e.canonical_name == "Мартин")
        names = [e.canonical_name for e in people]

        self.assertNotIn("Кузнец", names)
        self.assertNotIn("Мастер", names)
        self.assertIn("кузнец", [a.casefold() for a in martin.aliases])
        self.assertIn("мастер", [a.casefold() for a in martin.aliases])

    def test_open_vocabulary_actor_role_is_extracted_without_body_part_false_positive(self):
        text = "Королевский гонец прискакал во двор. Мартин всматривался в лица людей."
        doc = self.pipeline.analyze_structural(text)
        names = [e.canonical_name for e in doc.entities if e.entity_type.value == "person"]

        self.assertIn("Гонец", names)
        self.assertNotIn("Лицо", names)
    @classmethod
    def setUpClass(cls):
        cls.pipeline = StoryAnalyzerV3Pipeline()

    def analyze(self, text):
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(doc.validate_invariants(text), [])
        return doc

    def test_function_words_never_create_people(self):
        doc = self.analyze("Из кухни выходит Марина. Она держит чашку.")
        names = [e.canonical_name for e in doc.entities]
        self.assertIn("Марина", names)
        self.assertNotIn("Иза", names)
        self.assertFalse(any("unresolved" in c.status.value for c in doc.coreferences))

    def test_address_variant_links_to_existing_name(self):
        doc = self.analyze("Алексей вошел в комнату. Оригинально, Леша. Алексей сел.")
        names = [e.canonical_name for e in doc.entities]
        self.assertNotIn("Лёша", names)
        alexey = next(e for e in doc.entities if e.canonical_name == "Алексей")
        self.assertIn("Леша", alexey.aliases)

    def test_separately_introduced_variant_stays_separate(self):
        doc = self.analyze("Алексей вошел в комнату. Леша вошел следом.")
        self.assertEqual(len([e for e in doc.entities if e.entity_type.value == "person"]), 2)

    def test_unlisted_style_hypocoristics_link_only_as_grounded_vocatives(self):
        cases = (
            ("Владимир вошёл. — Вова, закрой дверь, — сказала Анна.", "Владимир", "Вова"),
            ("Николай поднялся. — Коля, подойди сюда, — позвала Мария.", "Николай", "Коля"),
        )
        for text, canonical, alias in cases:
            with self.subTest(alias=alias):
                doc = self.analyze(text)
                people = [e for e in doc.entities if e.entity_type.value == "person"]
                target = next(e for e in people if e.canonical_name == canonical)
                self.assertIn(alias, target.aliases)
                self.assertNotIn(alias, [e.canonical_name for e in people])

    def test_distinct_similar_name_is_not_joined_by_vocative_shape_alone(self):
        doc = self.analyze("Томас стоял у окна. Капитан крикнул: — Тим, запускай двигатель!")
        names = {e.canonical_name for e in doc.entities if e.entity_type.value == "person"}
        self.assertIn("Томас", names)
        self.assertIn("Тим", names)

    def test_first_person_inside_straight_quotes_does_not_create_narrator(self):
        doc = self.analyze('Томас сказал: "Я проверил маяк". Затем Томас закрыл дверь.')
        names = {e.canonical_name for e in doc.entities if e.entity_type.value == "person"}
        self.assertEqual(names, {"Томас"})

    def test_open_ended_religious_exclamation_is_not_a_character(self):
        doc = self.analyze("Матерь Божья! — вскрикнул Павел. Павел выбежал наружу.")
        names = {e.canonical_name for e in doc.entities if e.entity_type.value == "person"}
        self.assertEqual(names, {"Павел"})

    def test_remote_voice_that_later_appears_is_globally_physical(self):
        text = (
            "Из радиоприёмника раздался голос капитана. "
            "Утром капитан вошёл в маяк и заметил надпись на стене."
        )
        doc = self.analyze(text)
        captain = next(e for e in doc.entities if e.canonical_name == "Капитан")
        self.assertEqual(captain.modality.value, "physical")
        presences = [p for ev in doc.events for p in ev.presences if p.entity_id == captain.entity_id]
        self.assertTrue(any(p.modality.value == "voice_only" for p in presences))
        self.assertTrue(any(p.physical_presence for p in presences))

    def test_message_sender_is_not_physical_cast(self):
        text = "Марина открыла окно мессенджера. От некой «Кати»."
        doc = self.analyze(text)
        plan = self.pipeline.plan_visual_events(text, doc, 2)
        result = StoryAnalyzerV3Service.export_to_app_analysis(doc, plan, text)
        self.assertNotIn("Катя", [c["name"] for c in result["characters"]])

    def test_location_origin_does_not_move_current_scene(self):
        doc = self.analyze("Гостиная погружена в полумрак. Из кухни выходит Марина.")
        spatial = doc.metadata["spatial_grounding"]
        self.assertEqual(spatial["event_locations"][doc.events[-1].event_id], "Гостиная")
        self.assertIn("origin", [r["kind"] for r in spatial["evidence"] if r["name"] == "Кухня"])

    def test_screenplay_reported_location_does_not_teleport_actors(self):
        text = "Гостиная погружена в полумрак.\n\nМАРИНА\n\nИ фото. Ты на пляже. Без рубашки.\n\nАлексей замирает."
        doc = self.analyze(text)
        spatial = doc.metadata["spatial_grounding"]
        self.assertEqual(spatial["event_locations"][doc.events[-1].event_id], "Гостиная")
        beach = next(r for r in spatial["evidence"] if r["name"] == "Пляж")
        self.assertEqual(beach["kind"], "reported")

    def test_export_uses_event_location(self):
        text = "Анна стоит в кухне. Анна входит в спальню."
        doc = self.analyze(text)
        result = StoryAnalyzerV3Service.export_to_app_analysis(doc, self.pipeline.plan_visual_events(text, doc, 2), text)
        self.assertEqual([s["location"] for s in result["scenes"]], ["Кухня", "Спальня"])

    def test_unspecified_time_is_not_invented_daylight(self):
        text = "Анна вошла в комнату."
        doc = self.analyze(text)
        result = StoryAnalyzerV3Service.export_to_app_analysis(doc, self.pipeline.plan_visual_events(text, doc, 1), text)
        self.assertEqual(result["scenes"][0]["time"], "unspecified")

    def test_memory_does_not_move_physical_location(self):
        doc = self.analyze("Анна стоит в кухне. Она вспомнила пляж. Она закрыла окно.")
        spatial = doc.metadata["spatial_grounding"]
        self.assertEqual(spatial["event_locations"][doc.events[-1].event_id], "Кухня")

    def test_depicted_setting_is_not_current_location(self):
        doc = self.analyze("Анна стоит в кухне. На фотографии пляж. Анна улыбнулась.")
        spatial = doc.metadata["spatial_grounding"]
        self.assertEqual(spatial["event_locations"][doc.events[-1].event_id], "Кухня")

    def test_complete_user_screenplay_identity_and_spatial_contract(self):
        text = Path(__file__).with_name("grounding_screenplay.txt").read_text(encoding="utf-8")
        doc = self.analyze(text)
        plan = self.pipeline.plan_visual_events(text, doc, 16)
        result = StoryAnalyzerV3Service.export_to_app_analysis(doc, plan, text)
        self.assertEqual({c["name"] for c in result["characters"]}, {"Алексей", "Марина"})
        self.assertFalse(any("unresolved" in c.status.value for c in doc.coreferences))
        self.assertNotIn("Пляж", {s["location"] for s in result["scenes"]})
        self.assertNotIn("Основное место действия", {s["location"] for s in result["scenes"]})
        for scene in result["scenes"]:
            self.assertTrue(set(scene["characters"]) <= {"Алексей", "Марина"})
            self.assertIn(scene["source_quote"], text)

    def test_first_person_family_story_identity_and_location_timeline(self):
        text = Path(__file__).with_name("grounding_first_person_story.txt").read_text(encoding="utf-8")
        doc = self.analyze(text)
        names = [e.canonical_name for e in doc.entities]
        self.assertEqual(names.count("Муж"), 1)
        self.assertEqual(names.count("Женщина"), 1)
        self.assertIn("Рассказчица", names)

        by_mention = {
            next(m.text for m in doc.mentions if m.mention_id == cd.mention_id): cd
            for cd in doc.coreferences
        }
        entity_names = {e.entity_id: e.canonical_name for e in doc.entities}
        self.assertEqual({entity_names[eid] for eid in by_mention["Они"].resolved_entity_ids}, {"Дочь", "Андрей"})
        self.assertEqual([entity_names[eid] for eid in by_mention["он"].resolved_entity_ids], ["Нотариус"])

        locations = doc.metadata["spatial_grounding"]["event_locations"]
        self.assertEqual(locations["ev_0"], "Больница")
        self.assertEqual(locations["ev_1"], "Дом")
        self.assertEqual(locations["ev_2"], "Больница")
        self.assertEqual(locations["ev_6"], "Дом")
        self.assertEqual(locations["ev_9"], "Банковская ячейка")
        self.assertIsNone(locations["ev_12"])

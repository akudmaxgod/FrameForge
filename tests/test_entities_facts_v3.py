"""Comprehensive Unit, Invariant, and Counterexample Tests for Stage 3 (Entities, Mentions, Facts, Presences)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imagegen.analyzer_v3.models import (
    DocumentIR,
    DocumentMode,
    EntityIR,
    EntityModalityV3,
    EventEntityPresenceIR,
    EventIR,
    FactIR,
    MentionIR,
    SourceSpan,
)
from imagegen.analyzer_v3.nlp import RussianNLPAdapter
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline


class TestEntitiesFactsStage3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nlp = RussianNLPAdapter.get_instance()
        cls.pipeline = StoryAnalyzerV3Pipeline(cls.nlp)

        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "v3_reference_corpus.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            cls.corpus = data if isinstance(data, list) else data.get("documents", [])

    def test_entity_modality_classification_basic(self):
        """Tests basic modality tagging across single-entity contexts."""
        test_cases = [
            ("По телефону раздался голос Андрея, который требовал срочного ответа.", "Андрей", EntityModalityV3.VOICE_ONLY),
            ("Ему почудилось, что мама зовет его из темноты коридора.", "мама", EntityModalityV3.PERCEIVED_VOICE),
            ("На старой фотографии был изображен молодой капитан в парадном кителе.", "капитан", EntityModalityV3.DEPICTED),
            ("Он с грустью вспомнил об умершем дедушке, глядя на пустую мастерскую.", "дедушка", EntityModalityV3.DECEASED_MENTION),
            ("Михаил решительно вошел в комнату и сел за письменный стол.", "Михаил", EntityModalityV3.PHYSICAL),
        ]

        for text, expected_name, expected_modality in test_cases:
            doc_ir = self.pipeline.analyze_structural(text)
            matched_ent = None
            for ent in doc_ir.entities:
                if expected_name.lower() in ent.canonical_name.lower() or any(expected_name.lower() in a.lower() for a in ent.aliases):
                    matched_ent = ent
                    break

            self.assertIsNotNone(matched_ent, f"Entity '{expected_name}' not extracted from text: '{text}'")
            self.assertEqual(
                matched_ent.modality,
                expected_modality,
                f"Modality mismatch for '{expected_name}'. Expected {expected_modality}, got {matched_ent.modality}"
            )

    # -------------------------------------------------------------------------
    # 8.1 Counterexample: Single Character across Multiple Event Modalities
    # -------------------------------------------------------------------------
    def test_8_1_single_character_multiple_modalities(self):
        """Counterexample 8.1: A character appearing in 4 distinct events with different modalities."""
        text = (
            "Андрей вошёл в комнату.\n"
            "Позже голос Андрея прозвучал по телефону.\n"
            "На стене висела фотография Андрея.\n"
            "Мария вспомнила Андрея."
        )
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        # Locate Andrey and Maria
        andrey = next((e for e in doc_ir.entities if "андрей" in e.canonical_name.lower()), None)
        maria = next((e for e in doc_ir.entities if "мария" in e.canonical_name.lower()), None)
        self.assertIsNotNone(andrey, "Entity Andrey not found")
        self.assertIsNotNone(maria, "Entity Maria not found")

        self.assertEqual(len(doc_ir.events), 4)

        # Event 1: Physical entry
        ev1 = doc_ir.events[0]
        self.assertIn(andrey.entity_id, ev1.actor_ids)
        pres1 = next(p for p in ev1.presences if p.entity_id == andrey.entity_id)
        self.assertEqual(pres1.modality, EntityModalityV3.PHYSICAL)
        self.assertTrue(pres1.physical_presence)

        # Event 2: Phone voice
        ev2 = doc_ir.events[1]
        self.assertNotIn(andrey.entity_id, ev2.actor_ids)
        self.assertIn(andrey.entity_id, ev2.mentioned_entity_ids)
        pres2 = next(p for p in ev2.presences if p.entity_id == andrey.entity_id)
        self.assertEqual(pres2.modality, EntityModalityV3.VOICE_ONLY)
        self.assertFalse(pres2.physical_presence)

        # Event 3: Photo depicted
        ev3 = doc_ir.events[2]
        self.assertNotIn(andrey.entity_id, ev3.actor_ids)
        self.assertIn(andrey.entity_id, ev3.mentioned_entity_ids)
        pres3 = next(p for p in ev3.presences if p.entity_id == andrey.entity_id)
        self.assertEqual(pres3.modality, EntityModalityV3.DEPICTED)
        self.assertFalse(pres3.physical_presence)

        # Event 4: Memory mention
        ev4 = doc_ir.events[3]
        self.assertIn(maria.entity_id, ev4.actor_ids)
        self.assertNotIn(andrey.entity_id, ev4.actor_ids)
        self.assertIn(andrey.entity_id, ev4.mentioned_entity_ids)
        pres4 = next(p for p in ev4.presences if p.entity_id == andrey.entity_id)
        self.assertEqual(pres4.modality, EntityModalityV3.MENTIONED)
        self.assertFalse(pres4.physical_presence)

    # -------------------------------------------------------------------------
    # 8.2 Counterexample: Memory mention without physical presence
    # -------------------------------------------------------------------------
    def test_8_2_memory_mention_not_actor(self):
        """Counterexample 8.2: Sergey remembered by Anna is NOT a physical actor in event 2."""
        text = "Сергей вошёл в дом. Анна вспомнила Сергея."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        sergey = next(e for e in doc_ir.entities if "сергей" in e.canonical_name.lower())
        anna = next(e for e in doc_ir.entities if "анна" in e.canonical_name.lower())

        self.assertEqual(len(doc_ir.events), 2)
        ev1 = doc_ir.events[0]
        ev2 = doc_ir.events[1]

        self.assertIn(sergey.entity_id, ev1.actor_ids)
        self.assertIn(anna.entity_id, ev2.actor_ids)
        self.assertNotIn(sergey.entity_id, ev2.actor_ids)
        self.assertIn(sergey.entity_id, ev2.mentioned_entity_ids)

    # -------------------------------------------------------------------------
    # 8.3 Counterexample: Two people of same profession (no fake merged entity)
    # -------------------------------------------------------------------------
    def test_8_3_two_people_same_profession_distinct(self):
        """Counterexample 8.3: 'Доктор Иван' and 'Доктор Пётр' are distinct entities without a 3rd 'Доктор' entity."""
        text = "Доктор Иван вошёл в палату. Доктор Пётр вышел в коридор."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        ivan = next((e for e in doc_ir.entities if "иван" in e.canonical_name.lower()), None)
        petr = next((e for e in doc_ir.entities if "пётр" in e.canonical_name.lower() or "петр" in e.canonical_name.lower()), None)

        self.assertIsNotNone(ivan, "Ivan entity missing")
        self.assertIsNotNone(petr, "Petr entity missing")
        self.assertNotEqual(ivan.entity_id, petr.entity_id)

        # Check there is NO standalone entity whose canonical name is just "Доктор"
        generic_doctor = [e for e in doc_ir.entities if e.canonical_name.lower() == "доктор"]
        self.assertEqual(len(generic_doctor), 0, f"Found unwanted generic Doctor entity: {generic_doctor}")

    # -------------------------------------------------------------------------
    # 8.4 Counterexample: Two objects with different characteristics
    # -------------------------------------------------------------------------
    def test_8_4_two_objects_separate_characteristics(self):
        """Counterexample 8.4: 'BMW мощностью 250 л.с. обогнал Audi мощностью 300 л.с.' binds specs correctly."""
        text = "BMW мощностью 250 л.с. обогнал Audi мощностью 300 л.с."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        bmw = next(e for e in doc_ir.entities if "bmw" in e.canonical_name.lower())
        audi = next(e for e in doc_ir.entities if "audi" in e.canonical_name.lower())

        spec_facts = [f for f in doc_ir.facts if any(u in f.object_value for u in ("250 л.с.", "300 л.с."))]
        self.assertEqual(len(spec_facts), 2)

        bmw_fact = next(f for f in spec_facts if "250 л.с." in f.object_value)
        audi_fact = next(f for f in spec_facts if "300 л.с." in f.object_value)

        self.assertEqual(bmw_fact.subject_id, bmw.entity_id)
        self.assertEqual(bmw_fact.predicate, "мощность")
        self.assertEqual(bmw_fact.object_value, "250 л.с.")

        self.assertEqual(audi_fact.subject_id, audi.entity_id)
        self.assertEqual(audi_fact.predicate, "мощность")
        self.assertEqual(audi_fact.object_value, "300 л.с.")

    # -------------------------------------------------------------------------
    # 8.5 Counterexample: Repeated unnamed roles not merged
    # -------------------------------------------------------------------------
    def test_8_5_unnamed_roles_not_merged_automatically(self):
        """Counterexample 8.5: 'Один врач вошёл в кабинет. Другой врач остался в коридоре.' creates 2 distinct entities."""
        text = "Один врач вошёл в кабинет. Другой врач остался в коридоре."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        doctor_entities = [e for e in doc_ir.entities if "врач" in e.canonical_name.lower()]
        self.assertGreaterEqual(len(doctor_entities), 2, "Expected at least 2 distinct doctor entities")
        self.assertNotEqual(doctor_entities[0].entity_id, doctor_entities[1].entity_id)

    # -------------------------------------------------------------------------
    # 8.6 Counterexample: Unique and Deterministic Fact IDs across events
    # -------------------------------------------------------------------------
    def test_8_6_unique_and_deterministic_fact_ids(self):
        """Counterexample 8.6: Fact IDs are globally unique within DocumentIR and deterministic across runs."""
        text = (
            "1. Внедорожник Toyota развивает мощность 249 л.с.\n"
            "2. Седан BMW имеет мощность 250 л.с.\n"
            "3. Хэтчбек Audi развивает мощность 300 л.с."
        )
        doc1 = self.pipeline.analyze_structural(text)
        doc2 = self.pipeline.analyze_structural(text)

        self.assertEqual(len(doc1.validate_invariants(text)), 0)
        self.assertEqual(len(doc2.validate_invariants(text)), 0)

        fact_ids_1 = [f.fact_id for f in doc1.facts]
        fact_ids_2 = [f.fact_id for f in doc2.facts]

        # Global uniqueness
        self.assertEqual(len(fact_ids_1), len(set(fact_ids_1)), f"Duplicate fact IDs: {fact_ids_1}")
        # Determinism
        self.assertEqual(fact_ids_1, fact_ids_2)

    # -------------------------------------------------------------------------
    # 8.7 Counterexample: Invariant Validation Fails-Closed on Broken References
    # -------------------------------------------------------------------------
    def test_8_7_corrupted_references_fail_closed(self):
        """Counterexample 8.7: validate_invariants detects non-existent entity references and duplicate IDs."""
        text = "Анна вошла в комнату."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        # Corrupt Fact subject_id
        bad_fact = FactIR(
            fact_id="fact_corrupted_1",
            subject_id="ent_non_existent_999",
            predicate="действие",
            object_value="комната",
            source_span=SourceSpan(start=0, end=4, text="Анна"),
            section_id="sec_0",
            event_id=doc_ir.events[0].event_id,
        )
        doc_ir.events[0].facts.append(bad_fact)
        errors = doc_ir.validate_invariants(text)
        self.assertTrue(any("ent_non_existent_999" in e for e in errors), f"Expected broken entity error in: {errors}")

        # Corrupt Event actor_ids
        doc_ir.events[0].facts.pop()
        doc_ir.events[0].actor_ids.append("ent_ghost_123")
        errors2 = doc_ir.validate_invariants(text)
        self.assertTrue(any("ent_ghost_123" in e for e in errors2), f"Expected broken actor error in: {errors2}")

    # -------------------------------------------------------------------------
    # 8.8 Counterexample: Unknown Subject in Inanimate Events
    # -------------------------------------------------------------------------
    def test_8_8_inanimate_unknown_subject(self):
        """Counterexample 8.8: 'Раздался резкий стук. Затем погас свет.' leaves subject_id as None or inanimate."""
        text = "Раздался резкий стук. Затем погас свет."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        for ev in doc_ir.events:
            self.assertEqual(len(ev.actor_ids), 0, f"Inanimate event must have 0 actors: {ev.actor_ids}")

        # Ensure no random character subject was attached
        for f in doc_ir.facts:
            if f.subject_id is not None:
                ent = next((e for e in doc_ir.entities if e.entity_id == f.subject_id), None)
                if ent:
                    self.assertNotIn(ent.entity_type, ("person", "animal"))

    # -------------------------------------------------------------------------
    # 8.9 Counterexample: 5 Cars with 3 specs each and comparisons
    # -------------------------------------------------------------------------
    def test_8_9_five_cars_review_no_crosstalk(self):
        """Counterexample 8.9: Multi-car review with 5 vehicles, 3 specs each, and 0 crosstalk."""
        text = (
            "# Обзор пяти автомобилей\n"
            "## Toyota Land Cruiser\n"
            "Мощность двигателя составляет 249 л.с., дорожный просвет 225 мм, а крутящий момент 700 нм.\n"
            "## BMW X5\n"
            "Оснащен мотором 340 л.с., клиренс 214 мм и момент 450 нм.\n"
            "## Audi Q7\n"
            "Предлагает 250 л.с., дорожный просвет 210 мм и момент 600 нм.\n"
            "## Porsche Cayenne\n"
            "Развивает 440 л.с., клиренс 215 мм и момент 550 нм.\n"
            "## Volvo XC90\n"
            "Имеет 235 л.с., дорожный просвет 238 мм и момент 480 нм.\n"
            "## Сравнение\n"
            "Porsche Cayenne мощнее, чем Volvo XC90."
        )
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        vehicles = [e for e in doc_ir.entities if e.entity_type == "vehicle"]
        self.assertGreaterEqual(len(vehicles), 5, f"Expected 5 vehicles, found {len(vehicles)}")

        # Check Porsche Cayenne 440 л.с. and Volvo 235 л.с.
        # Check all 15 characteristics across 5 vehicles (subject, predicate, and value)
        expected_specs = [
            ("Toyota", "мощность", "249 л.с."),
            ("Toyota", "клиренс", "225 мм"),
            ("Toyota", "крутящий момент", "700 нм"),
            ("Bmw", "мощность", "340 л.с."),
            ("Bmw", "клиренс", "214 мм"),
            ("Bmw", "крутящий момент", "450 нм"),
            ("Audi", "мощность", "250 л.с."),
            ("Audi", "клиренс", "210 мм"),
            ("Audi", "крутящий момент", "600 нм"),
            ("Porsche", "мощность", "440 л.с."),
            ("Porsche", "клиренс", "215 мм"),
            ("Porsche", "крутящий момент", "550 нм"),
            ("Volvo", "мощность", "235 л.с."),
            ("Volvo", "клиренс", "238 мм"),
            ("Volvo", "крутящий момент", "480 нм"),
        ]
        ent_by_name = {e.canonical_name.casefold(): e for e in doc_ir.entities}
        for car_name, exp_pred, exp_val in expected_specs:
            car = ent_by_name.get(car_name.casefold())
            self.assertIsNotNone(car, f"Vehicle entity {car_name} not found")
            matching_fact = next(
                (f for f in doc_ir.facts if f.object_value == exp_val),
                None
            )
            self.assertIsNotNone(matching_fact, f"Fact with value {exp_val} not found")
            self.assertEqual(
                matching_fact.subject_id,
                car.entity_id,
                f"Fact {exp_val} has subject {matching_fact.subject_id}, expected {car_name} ({car.entity_id})"
            )
            self.assertEqual(
                matching_fact.predicate,
                exp_pred,
                f"Fact {exp_val} has predicate {matching_fact.predicate}, expected {exp_pred}"
            )

    # -------------------------------------------------------------------------
    # Specific Strict Syntactic Modality & Fact Strings Tests
    # -------------------------------------------------------------------------
    def test_strict_andrey_photo_viktor(self):
        """«Андрей посмотрел на фотографию Виктора» -> Андрей PHYSICAL, Виктор DEPICTED."""
        text = "Андрей посмотрел на фотографию Виктора."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        andrey = next(e for e in doc_ir.entities if "андрей" in e.canonical_name.lower())
        viktor = next(e for e in doc_ir.entities if "виктор" in e.canonical_name.lower())
        ev = doc_ir.events[0]

        andrey_p = next(p for p in ev.presences if p.entity_id == andrey.entity_id)
        viktor_p = next(p for p in ev.presences if p.entity_id == viktor.entity_id)

        self.assertTrue(andrey_p.physical_presence)
        self.assertEqual(andrey_p.modality, EntityModalityV3.PHYSICAL)
        self.assertIn(andrey.entity_id, ev.actor_ids)

        self.assertFalse(viktor_p.physical_presence)
        self.assertEqual(viktor_p.modality, EntityModalityV3.DEPICTED)
        self.assertNotIn(viktor.entity_id, ev.actor_ids)

    def test_strict_maria_phone_andrey(self):
        """«Мария услышала по телефону голос Андрея» -> Мария PHYSICAL, Андрей VOICE_ONLY, телефон OBJECT."""
        text = "Мария услышала по телефону голос Андрея."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        maria = next(e for e in doc_ir.entities if "мария" in e.canonical_name.lower())
        andrey = next(e for e in doc_ir.entities if "андрей" in e.canonical_name.lower())
        phone = next(e for e in doc_ir.entities if "телефон" in e.canonical_name.lower())
        ev = doc_ir.events[0]

        maria_p = next(p for p in ev.presences if p.entity_id == maria.entity_id)
        andrey_p = next(p for p in ev.presences if p.entity_id == andrey.entity_id)
        phone_p = next(p for p in ev.presences if p.entity_id == phone.entity_id)

        self.assertTrue(maria_p.physical_presence)
        self.assertEqual(maria_p.modality, EntityModalityV3.PHYSICAL)
        self.assertIn(maria.entity_id, ev.actor_ids)

        self.assertFalse(andrey_p.physical_presence)
        self.assertEqual(andrey_p.modality, EntityModalityV3.VOICE_ONLY)
        self.assertNotIn(andrey.entity_id, ev.actor_ids)

        self.assertEqual(phone_p.modality, EntityModalityV3.OBJECT)
        self.assertNotEqual(phone_p.role, "speaker")

    def test_strict_two_unmarked_doctors(self):
        """«Врач вошёл в кабинет. Врач остался в коридоре.» -> 2 distinct doctor entities, not merged."""
        text = "Врач вошёл в кабинет. Врач остался в коридоре."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        doctors = [e for e in doc_ir.entities if "врач" in e.canonical_name.lower()]
        self.assertEqual(len(doctors), 2)
        self.assertNotEqual(doctors[0].entity_id, doctors[1].entity_id)
        self.assertNotEqual(doc_ir.events[0].actor_ids, doc_ir.events[1].actor_ids)

    def test_strict_toyota_three_specs(self):
        """«Toyota имеет мощность 249 л.с., клиренс 225 мм и крутящий момент 700 Нм.» -> all 3 specs owned by Toyota."""
        text = "Toyota имеет мощность 249 л.с., клиренс 225 мм и крутящий момент 700 Нм."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        toyota = next(e for e in doc_ir.entities if "toyota" in e.canonical_name.lower())
        props = {
            f.object_value: (f.subject_id, f.predicate)
            for f in doc_ir.facts
            if f.object_value in {"249 л.с.", "225 мм", "700 Нм"}
        }
        self.assertEqual(
            props,
            {
                "249 л.с.": (toyota.entity_id, "мощность"),
                "225 мм": (toyota.entity_id, "клиренс"),
                "700 Нм": (toyota.entity_id, "крутящий момент"),
            }
        )

    def test_strict_radio_voice_only(self):
        """«По рации прозвучал голос Виктора.» -> Виктор is VOICE_ONLY, physical False."""
        text = "По рации прозвучал голос Виктора."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        viktor = next(e for e in doc_ir.entities if "виктор" in e.canonical_name.lower())
        ev = doc_ir.events[0]
        pres = next(p for p in ev.presences if p.entity_id == viktor.entity_id)

        self.assertEqual(pres.modality, EntityModalityV3.VOICE_ONLY)
        self.assertFalse(pres.physical_presence)

    # -------------------------------------------------------------------------
    # 8.10 Counterexample: Repeating Phrases across Events
    # -------------------------------------------------------------------------
    def test_8_10_repeating_phrases_have_unique_spans_and_facts(self):
        """Counterexample 8.10: Repeated sentences have unique SourceSpans, unique IDs, and correct event mapping."""
        text = "Тьма накрыла город. Тьма накрыла город. Тьма накрыла город."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        self.assertEqual(len(doc_ir.events), 3)
        ev_ids = [e.event_id for e in doc_ir.events]
        self.assertEqual(len(ev_ids), len(set(ev_ids)))

        fact_ids = [f.fact_id for f in doc_ir.facts]
        self.assertEqual(len(fact_ids), len(set(fact_ids)))

        # Verify exact, non-overlapping spans
        for i in range(len(doc_ir.events) - 1):
            self.assertLessEqual(doc_ir.events[i].span.end, doc_ir.events[i + 1].span.start)

    # -------------------------------------------------------------------------
    # Syntactic Role Population Check
    # -------------------------------------------------------------------------
    def test_syntactic_role_population(self):
        """Tests that MentionIR.syntactic_role is populated with subject, object, attribute, location, etc."""
        text = "Иван увидел Ольгу в саду. Он передал ей книгу."
        doc_ir = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc_ir.validate_invariants(text)), 0)

        roles = [m.syntactic_role for m in doc_ir.mentions if m.syntactic_role is not None]
        self.assertGreater(len(roles), 0)
        self.assertTrue(any(r in ("subject", "object", "location", "oblique") for r in roles))

    # -------------------------------------------------------------------------
    # Complete Reference Regression Corpus Test
    # -------------------------------------------------------------------------
    def test_all_reference_corpus_structural_analysis(self):
        """Runs the complete StoryAnalyzerV3Pipeline over all 20 regression corpus documents with strict invariant checks."""
        for doc in self.corpus:
            doc_id = doc["id"]
            text = doc["text"]

            doc_ir = self.pipeline.analyze_structural(text)

            # Invariant validation (must have 0 errors)
            errors = doc_ir.validate_invariants(text)
            self.assertEqual(len(errors), 0, f"Invariant errors in {doc_id}: {errors}")

            # Coverage audit
            audit = doc_ir.audit_text_coverage(text)
            self.assertTrue(audit["is_fully_covered"], f"Text coverage failed for {doc_id}: {audit['gaps']}")

            # Verify non-empty structure
            self.assertGreater(len(doc_ir.sections), 0)
            self.assertGreater(len(doc_ir.events), 0)

    # -------------------------------------------------------------------------
    # Verbal Action Facts Object Value Non-Subject Invariant Tests
    # -------------------------------------------------------------------------
    def test_verbal_action_facts_do_not_contain_subject_as_object(self):
        """Action facts must never assign the acting subject as its own object_value."""
        # 1. Andrey looked at Victor's photo -> object must be photo, not Andrey
        text1 = "Андрей посмотрел на фотографию Виктора."
        doc1 = self.pipeline.analyze_structural(text1)
        self.assertEqual(len(doc1.validate_invariants(text1)), 0)
        andrey = next(e for e in doc1.entities if "андрей" in e.canonical_name.lower())
        act1 = next(f for f in doc1.facts if f.predicate == "посмотреть")
        self.assertEqual(act1.subject_id, andrey.entity_id)
        self.assertNotEqual(str(act1.object_value).lower(), "андрей")
        self.assertEqual(str(act1.object_value).lower(), "фотография")

        # 2. Maria heard Victor's voice -> object must be voice, not Maria
        text2 = "Мария услышала по телефону голос Андрея."
        doc2 = self.pipeline.analyze_structural(text2)
        self.assertEqual(len(doc2.validate_invariants(text2)), 0)
        maria = next(e for e in doc2.entities if "мария" in e.canonical_name.lower())
        act2 = next(f for f in doc2.facts if f.predicate == "услышать")
        self.assertEqual(act2.subject_id, maria.entity_id)
        self.assertNotEqual(str(act2.object_value).lower(), "мария")
        self.assertEqual(str(act2.object_value).lower(), "голос")

        # 3. BMW passed Audi -> object must be Audi, not BMW
        text3 = "BMW мощностью 250 л.с. обогнал Audi мощностью 300 л.с."
        doc3 = self.pipeline.analyze_structural(text3)
        self.assertEqual(len(doc3.validate_invariants(text3)), 0)
        bmw = next(e for e in doc3.entities if "bmw" in e.canonical_name.lower())
        act3 = next(f for f in doc3.facts if f.predicate == "обогнать")
        self.assertEqual(act3.subject_id, bmw.entity_id)
        self.assertNotEqual(str(act3.object_value).lower(), "bmw")
        self.assertEqual(str(act3.object_value).lower(), "audi")

        # 4. Doctor entered room -> object must be cabinet, not Doctor
        text4 = "Врач вошёл в кабинет."
        doc4 = self.pipeline.analyze_structural(text4)
        self.assertEqual(len(doc4.validate_invariants(text4)), 0)
        doc_ent = next(e for e in doc4.entities if "врач" in e.canonical_name.lower())
        act4 = next(f for f in doc4.facts if f.predicate == "войти")
        self.assertEqual(act4.subject_id, doc_ent.entity_id)
        self.assertNotEqual(str(act4.object_value).lower(), "врач")
        self.assertEqual(str(act4.object_value).lower(), "кабинет")

    # -------------------------------------------------------------------------
    # Presence Bidirectional Consistency Invariant Tests
    # -------------------------------------------------------------------------
    def test_presence_consistency_invariant_enforcement(self):
        """EventIR presences are the single source of truth; invariants strictly validate presence integrity."""
        import copy
        text = "Андрей посмотрел на фотографию Виктора."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        # 1. Dropping presence removes it from both event and dynamic aggregate doc.event_entity_presences
        doc_ev_missing = copy.deepcopy(doc)
        removed_presence = doc_ev_missing.events[0].presences.pop(0)
        self.assertNotIn(removed_presence, doc_ev_missing.event_entity_presences)
        errs1 = doc_ev_missing.validate_invariants(text)
        # Actor without presence fails closed
        self.assertTrue(any("without EventEntityPresence record" in err for err in errs1))

        # 2. Duplicate presence ID fails closed
        doc_dup = copy.deepcopy(doc)
        p_dup = copy.deepcopy(doc_dup.events[0].presences[0])
        doc_dup.events[0].presences.append(p_dup)
        errs2 = doc_dup.validate_invariants(text)
        self.assertTrue(any("duplicate presence ID" in err for err in errs2))

        # 3. Attribute modification: physical_presence
        doc_phys_mismatch = copy.deepcopy(doc)
        p0 = doc_phys_mismatch.events[0].presences[0]
        p0.physical_presence = False
        errs3 = doc_phys_mismatch.validate_invariants(text)
        self.assertTrue(any("without physical presence" in err for err in errs3))

        # 4. Presence referencing non-existent entity fails closed
        doc_ghost_ent = copy.deepcopy(doc)
        p_ghost = copy.deepcopy(doc_ghost_ent.events[0].presences[0])
        object.__setattr__(p_ghost, "presence_id", "pres_ghost_999")
        p_ghost.entity_id = "ent_ghost_non_existent"
        doc_ghost_ent.events[0].presences.append(p_ghost)
        errs4 = doc_ghost_ent.validate_invariants(text)
        self.assertTrue(any("references non-existent entity 'ent_ghost_non_existent'" in err for err in errs4))

    # -------------------------------------------------------------------------
    # Fact Consistency Invariant Tests
    # -------------------------------------------------------------------------
    def test_fact_consistency_invariant_enforcement(self):
        """EventIR facts are the single source of truth; invariants strictly validate fact integrity."""
        import copy
        text = "Андрей посмотрел на фотографию Виктора."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        # 1. Dropping fact removes it from both event and dynamic aggregate doc.facts
        doc_ev_missing = copy.deepcopy(doc)
        removed_fact = doc_ev_missing.events[0].facts.pop(0)
        self.assertNotIn(removed_fact, doc_ev_missing.facts)

        # 2. Duplicate fact ID across events or within event fails closed
        doc_dup = copy.deepcopy(doc)
        f_dup = copy.deepcopy(doc_dup.events[0].facts[0])
        doc_dup.events[0].facts.append(f_dup)
        errs2 = doc_dup.validate_invariants(text)
        self.assertTrue(any("duplicate fact ID" in err for err in errs2))

        # 3. Fact referencing non-existent subject entity fails closed
        doc_ghost_sub = copy.deepcopy(doc)
        f0 = doc_ghost_sub.events[0].facts[0]
        f0.subject_id = "ent_ghost_unknown"
        errs3 = doc_ghost_sub.validate_invariants(text)
        self.assertTrue(any("references non-existent subject entity 'ent_ghost_unknown'" in err for err in errs3))

        # 4. Fact referencing non-existent event or mismatched event_id fails closed
        doc_ev_mismatch = copy.deepcopy(doc)
        f1 = doc_ev_mismatch.events[0].facts[0]
        f1.event_id = "ev_non_existent_404"
        errs4 = doc_ev_mismatch.validate_invariants(text)
        self.assertTrue(any("mismatched event_id" in err or "references non-existent event" in err for err in errs4))


if __name__ == "__main__":
    unittest.main()


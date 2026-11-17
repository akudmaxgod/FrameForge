"""Critical semantic contracts for Story Analyzer V3.

These tests intentionally use short, independent texts rather than the
development regression corpus.  They assert the semantic result consumed by
later stages: event presence, entity identity, fact ownership, and fail-closed
invariant validation.

Do not weaken an assertion because the current implementation fails it.  A
failing test represents a user-visible analysis defect that must be fixed in
production code.
"""
from __future__ import annotations

import unittest

from imagegen.analyzer_v3.models import EntityModalityV3
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline


class StoryAnalyzerV3CriticalContracts(unittest.TestCase):
    """Small, deterministic tests for contracts that must never regress."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = StoryAnalyzerV3Pipeline()

    def analyse(self, text: str):
        document = self.pipeline.analyze_structural(text)
        errors = document.validate_invariants(text)
        self.assertEqual(errors, [], f"Baseline document must be valid: {errors}")
        return document

    @staticmethod
    def entity(document, name: str):
        normalized = name.casefold()
        matches = [
            entity
            for entity in document.entities
            if normalized in entity.canonical_name.casefold()
            or any(normalized in alias.casefold() for alias in entity.aliases)
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"Expected exactly one entity named {name!r}; got "
                f"{[(item.entity_id, item.canonical_name) for item in matches]}"
            )
        return matches[0]

    @staticmethod
    def presence(event, entity_id: str):
        matches = [item for item in event.presences if item.entity_id == entity_id]
        if len(matches) != 1:
            raise AssertionError(
                f"Expected exactly one presence for {entity_id}; got "
                f"{[(item.modality.value, item.role) for item in matches]}"
            )
        return matches[0]

    def test_photograph_does_not_remove_observer_from_scene(self):
        """The observer is physical; the person in the photograph is depicted."""
        text = "Андрей посмотрел на фотографию Виктора."
        document = self.analyse(text)
        event = document.events[0]
        andrey = self.entity(document, "Андрей")
        viktor = self.entity(document, "Виктор")

        andrey_presence = self.presence(event, andrey.entity_id)
        viktor_presence = self.presence(event, viktor.entity_id)

        self.assertTrue(andrey_presence.physical_presence)
        self.assertEqual(andrey_presence.modality, EntityModalityV3.PHYSICAL)
        self.assertIn(andrey.entity_id, event.actor_ids)

        self.assertFalse(viktor_presence.physical_presence)
        self.assertEqual(viktor_presence.modality, EntityModalityV3.DEPICTED)
        self.assertNotIn(viktor.entity_id, event.actor_ids)

    def test_phone_marker_applies_to_speaker_not_listener_or_device(self):
        """A listener remains physical, while the distant speaker is voice-only."""
        text = "Мария услышала по телефону голос Андрея."
        document = self.analyse(text)
        event = document.events[0]
        maria = self.entity(document, "Мария")
        andrey = self.entity(document, "Андрей")
        phone = self.entity(document, "телефон")

        maria_presence = self.presence(event, maria.entity_id)
        andrey_presence = self.presence(event, andrey.entity_id)
        phone_presence = self.presence(event, phone.entity_id)

        self.assertTrue(maria_presence.physical_presence)
        self.assertEqual(maria_presence.modality, EntityModalityV3.PHYSICAL)
        self.assertIn(maria.entity_id, event.actor_ids)

        self.assertFalse(andrey_presence.physical_presence)
        self.assertEqual(andrey_presence.modality, EntityModalityV3.VOICE_ONLY)
        self.assertNotIn(andrey.entity_id, event.actor_ids)

        self.assertEqual(phone_presence.modality, EntityModalityV3.OBJECT)
        self.assertNotEqual(phone_presence.role, "speaker")

    def test_subject_binding_survives_relative_attribute_without_comma(self):
        """Specs remain attached to the nearest named vehicle without delimiter tricks."""
        text = "BMW мощностью 250 л.с. обогнал Audi мощностью 300 л.с."
        document = self.analyse(text)
        bmw = self.entity(document, "BMW")
        audi = self.entity(document, "Audi")

        specs = {fact.object_value: fact for fact in document.facts if "л.с." in str(fact.object_value)}
        self.assertEqual(set(specs), {"250 л.с.", "300 л.с."})
        self.assertEqual(specs["250 л.с."].subject_id, bmw.entity_id)
        self.assertEqual(specs["300 л.с."].subject_id, audi.entity_id)

    def test_each_spec_keeps_its_own_property_name(self):
        """Power, clearance, and torque must not become three 'power' facts."""
        text = "Toyota имеет мощность 249 л.с., клиренс 225 мм и крутящий момент 700 Нм."
        document = self.analyse(text)
        properties = {
            fact.object_value: fact.predicate
            for fact in document.facts
            if fact.object_value in {"249 л.с.", "225 мм", "700 Нм"}
        }
        self.assertEqual(
            properties,
            {
                "249 л.с.": "мощность",
                "225 мм": "клиренс",
                "700 Нм": "крутящий момент",
            },
        )

    def test_unmarked_repeated_roles_are_not_assumed_to_be_one_person(self):
        """Two repeated unnamed roles may not silently become one character."""
        text = "Врач вошёл в кабинет. Врач остался в коридоре."
        document = self.analyse(text)
        doctors = [entity for entity in document.entities if "врач" in entity.canonical_name.casefold()]
        self.assertEqual(
            len(doctors),
            2,
            "Without proof of identity, two role mentions must remain separate entities.",
        )
        self.assertNotEqual(document.events[0].actor_ids, document.events[1].actor_ids)

    def test_actor_without_presence_fails_closed(self):
        """An actor reference without an event presence is a corrupt DocumentIR."""
        text = "Анна вошла в комнату."
        document = self.analyse(text)
        document.event_entity_presences = []
        document.events[0].presences = []

        errors = document.validate_invariants(text)
        self.assertTrue(
            any("without EventEntityPresence" in error for error in errors),
            f"Missing presence must be rejected; got {errors}",
        )

    def test_voice_from_real_speaker_is_not_perceived_voice(self):
        """A real device is voice-only; perceived voice is reserved for imagination."""
        text = "По рации прозвучал голос Виктора."
        document = self.analyse(text)
        event = document.events[0]
        viktor = self.entity(document, "Виктор")
        presence = self.presence(event, viktor.entity_id)

        self.assertEqual(presence.modality, EntityModalityV3.VOICE_ONLY)
        self.assertFalse(presence.physical_presence)


if __name__ == "__main__":
    unittest.main()

import unittest

from imagegen.providers import select_story_segments
from imagegen.analyzer_v2.coreference import ContextualCoreferenceResolver
from imagegen.analyzer_v2.events import build_event_spans
from imagegen.analyzer_v2.models import Entity, EntityType
from imagegen.analyzer_v2.place_graph import PlaceGraph


class TestNarrativeAnchorRegression(unittest.TestCase):
    def test_downsampling_keeps_named_action_inside_its_time_bucket(self):
        segments = [f"Служебный отсек {index} мерцает индикаторами." for index in range(40)]
        key_event = "Инженер Максим и пилот Денис вышли в открытый космос и включили сварочный аппарат."
        segments[17] = key_event

        selected = select_story_segments(segments, 8)

        self.assertEqual(len(selected), 8)
        self.assertIn(key_event, selected)

    def test_technical_location_aliases_keep_the_same_number(self):
        graph = PlaceGraph()

        self.assertEqual(
            graph.normalize_location_name("Spacecraft Interior - Service Section 068"),
            "Служебный отсек 068",
        )
        self.assertEqual(graph.normalize_location_name("Control room"), "Комната управления")

    def test_empty_alias_does_not_make_every_man_an_active_subject(self):
        story = (
            "Виктор закончил смену и ушёл. "
            "Около одиннадцати Алексей Морозов сидел на посту и смотрел камеры. "
            "На мониторе появилась рябь. "
            "В один момент ему показалось, что возле двери кто-то стоит. "
            "Он переключил изображение и вернул его обратно."
        )
        entities = {
            "alex": Entity("alex", EntityType.PERSON, "Алексей Морозов", aliases=["Алексей", ""]),
            "victor": Entity("victor", EntityType.PERSON, "Виктор", aliases=[""]),
            "tech": Entity("tech", EntityType.PERSON, "Техник", aliases=[""]),
            "duty": Entity("duty", EntityType.PERSON, "Дежурный", aliases=[""]),
        }
        events = build_event_spans(story)
        target_index = next(i for i, event in enumerate(events) if event.exact_text.startswith("Он переключил"))
        resolver = ContextualCoreferenceResolver(entities)

        resolutions = resolver.resolve_event_pronouns(
            events[target_index],
            events[:target_index],
            story_text=story,
        )

        self.assertTrue(resolutions)
        self.assertTrue(all(item.resolved_entity_id == "alex" for item in resolutions))
        self.assertTrue(all(item.confidence >= 0.6 for item in resolutions))


if __name__ == "__main__":
    unittest.main()

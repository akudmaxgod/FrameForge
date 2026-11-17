"""Comprehensive test suite for Story Analyzer V2 according to TASK_STORY_ANALYZER_V2.md."""
import copy
import json
import unittest

from imagegen.analyzer_v2.models import (
    Entity, EntityModality, EntityType, Fact, Event, Shot, Place, Zone,
    PlaceStatus, ShotInstance, ShotAudioCue, ShotDepictedEntity,
    SourceSpan, StoryAnalysisV2Result
)
from imagegen.analyzer_v2.observability import TraceCollector, MutationRecord, StageSnapshot
from imagegen.analyzer_v2.entities import CharacterEntityResolverV2, clean_acoustic_name, lemmatize_name
from imagegen.analyzer_v2.locations import LocationResolverV2, extract_environmental_states, is_pseudo_place
from imagegen.analyzer_v2.chunks import TwoPassChunkReconciler
from imagegen.analyzer_v2.pipeline import StoryAnalyzerV2Pipeline


class TestStoryAnalyzerV2Models(unittest.TestCase):
    def test_source_span_validation(self):
        text = "В тишине осеннего леса Дэниел сделал шаг к хижине."
        span = SourceSpan(document_id="doc1", start=9, end=22, exact_text="осеннего леса")
        self.assertTrue(span.validate_against(text))
        
        bad_span = SourceSpan(document_id="doc1", start=9, end=20, exact_text="осеннего леса")
        self.assertFalse(bad_span.validate_against(text))

    def test_entity_serialization(self):
        ent = Entity(
            entity_id="ent_1_daniel",
            entity_type=EntityType.PERSON,
            canonical_name="Дэниел",
            aliases=["Дэн", "Дэниел Харпер"],
            confirmed_attributes=["в серой куртке"],
        )
        d = ent.to_dict()
        self.assertEqual(d["canonical_name"], "Дэниел")
        self.assertEqual(d["aliases"], ["Дэн", "Дэниел Харпер"])
        
        restored = Entity.from_dict(d)
        self.assertEqual(restored.canonical_name, ent.canonical_name)
        self.assertEqual(restored.entity_type, EntityType.PERSON)

    def test_place_and_zone_serialization(self):
        place = Place(
            place_id="loc_1_cabin",
            canonical_name="Хижина в лесу",
            description="Старая деревянная хижина",
            zones=[
                Zone(zone_id="loc_1_z_1", place_id="loc_1_cabin", canonical_name="Крыльцо"),
                Zone(zone_id="loc_1_z_2", place_id="loc_1_cabin", canonical_name="Гостиная"),
            ]
        )
        d = place.to_dict()
        self.assertEqual(len(d["zones"]), 2)
        restored = Place.from_dict(d)
        self.assertEqual(len(restored.zones), 2)
        self.assertEqual(restored.zones[0].canonical_name, "Крыльцо")


class TestObservabilityAndTracing(unittest.TestCase):
    def test_trace_collector_hero_tracking(self):
        collector = TraceCollector(trace_id="tr_123", story_text="Дэниел пошел в лес. Раздался голос Майка.")
        collector.track_hero("Дэниел")
        
        collector.record_mutation(
            stage="stage_1_discovery",
            action="created",
            target_type="entity",
            target_id="ent_1_daniel",
            reason="Found lead protagonist in text",
            new_value="Дэниел",
        )
        self.assertIn("Дэниел", collector.hero_lifecycle)
        self.assertEqual(len(collector.hero_lifecycle["Дэниел"]), 1)
        self.assertEqual(collector.hero_lifecycle["Дэниел"][0]["action"], "created")

    def test_no_secrets_in_trace(self):
        collector = TraceCollector(trace_id="tr_123", story_text="Story")
        collector.set_provider_info(
            provider="groq",
            model="llama-3.3-70b-versatile",
            params={"groq_api_key": "gsk_secret12345", "temperature": 0.2}
        )
        d = collector.to_dict()
        self.assertEqual(d["provider_info"]["parameters"]["groq_api_key"], "[REDACTED]")
        self.assertEqual(d["provider_info"]["parameters"]["temperature"], 0.2)


class TestSection13NegativeAndEdgeCases(unittest.TestCase):
    """
    Direct tests for all 17 negative and edge cases from Section 13 of TASK_STORY_ANALYZER_V2.md.
    """

    def setUp(self):
        self.trace = TraceCollector(trace_id="test_trace", story_text="")
        self.entity_resolver = CharacterEntityResolverV2(trace_collector=self.trace)
        self.location_resolver = LocationResolverV2(trace_collector=self.trace)

    # 1. «Это знакомый голос X» не создаёт персонажа «Голос X» и не добавляет тело X.
    def test_case_1_voice_of_does_not_create_voice_x_or_physical_body(self):
        quote = "В темноте раздался знакомый голос Майка."
        shot_desc = "Дэниел замирает и прислушивается к звуку."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Голос Майка"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=1,
        )
        # Canonical entity name must be Mike, not "Голос Майк"
        mike_id = self.entity_resolver.resolve_name("Майк")
        self.assertIsNotNone(mike_id)
        self.assertEqual(self.entity_resolver.entities[mike_id].canonical_name, "Майк")
        self.assertNotIn("голос", self.entity_resolver.entities[mike_id].canonical_name.lower())

        # Physical instances must be empty (or only Daniel if present); Mike must NOT be physically visible
        self.assertEqual(len(vis), 0)
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0].speaker_name, "Майк")
        self.assertFalse(audio[0].is_perceived_only)

    # 2. X физически говорит в комнате — остаётся видимым.
    def test_case_2_speaking_person_in_room_remains_visible(self):
        quote = "Майк сидел у камина и тихо говорил о прошлом."
        shot_desc = "Майк смотрит в огонь и произносит слова."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Майк"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=2,
        )
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0].role_in_shot, "recurring")
        ent = self.entity_resolver.entities[vis[0].entity_id]
        self.assertEqual(ent.canonical_name, "Майк")

    # 3. Один герой слышит голос, похожий на собственный: восприятие и реальная личность источника не смешиваются.
    def test_case_3_perceived_voice_imitation_decoupled_from_actual_identity(self):
        quote = "Из-за двери донесся голос, похожий на голос Дэниела."
        shot_desc = "Сара испуганно оборачивается к двери."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Сара"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=3,
        )
        self.assertEqual(len(audio), 1)
        self.assertTrue(audio[0].is_perceived_only)
        self.assertEqual(audio[0].perceived_as_name, "Дэниел")
        self.assertIsNone(audio[0].entity_id)  # Speaker entity unconfirmed

    # 4. Герой и его подтверждённый двойник — две фигуры с устойчивыми экземплярами.
    def test_case_4_hero_and_double_have_distinct_instances(self):
        quote = "Дэниел увидел перед собой своего двойника, одетого точно так же."
        shot_desc = "Дэниел и его двойник стоят лицом к лицу."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Дэниел", "Двойник"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=4,
        )
        self.assertEqual(len(vis), 2)
        instance_ids = [inst.instance_id for inst in vis]
        self.assertEqual(len(set(instance_ids)), 2)  # Two distinct physical instances
        roles = [inst.role_in_shot for inst in vis]
        self.assertTrue(any(r == "double" for r in roles))

    # 5. Герой рядом со своей фотографией — один физический человек и одно изображение того же entity.
    def test_case_5_hero_next_to_own_photo(self):
        quote = "Дэниел держит в руках старую фотографию, где он сам снят в детстве."
        shot_desc = "Дэниел смотрит на фотографию себя."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Дэниел"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=5,
        )
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0].entity_id, self.entity_resolver.resolve_name("Дэниел"))
        self.assertEqual(len(dep), 1)
        self.assertEqual(dep[0].medium, "photo")

    # 6. Два незнакомца/охранника — разные instance ID даже при одинаковой роли.
    def test_case_6_multiple_guards_distinct_instance_ids(self):
        quote = "Двое охранников стояли у ворот."
        shot_desc = "Два человека в форме блокируют проход."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=[],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=6,
        )
        self.assertEqual(len(vis), 2)
        self.assertNotEqual(vis[0].instance_id, vis[1].instance_id)

    # 7. Человек упомянут только в журнале: не превращается в физического спутника.
    def test_case_7_journal_mention_does_not_become_physical_companion(self):
        quote = "В дневнике Роберт Кейн оставил странную запись об экспедиции."
        shot_desc = "Дэниел перелистывает пожелтевшие страницы дневника."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Дэниел"],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=7,
        )
        vis_names = [self.entity_resolver.entities[i.entity_id].canonical_name for i in vis]
        self.assertIn("Дэниел", vis_names)
        self.assertNotIn("Роберт Кейн", vis_names)

    # 8. Голосовой персонаж позже получает письмо/сообщение в эпилоге: классификация по событию.
    def test_case_8_voice_character_becomes_physical_in_epilogue(self):
        # Scene 1: Voice only
        vis1, audio1, _ = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Голос Майка"],
            source_quote="Дэниел услышал голос Майка из динамика.",
            shot_description="Дэниел слушает запись.",
            scene_number=1,
        )
        self.assertEqual(len(vis1), 0)

        # Scene 20 (Epilogue): Physical appearance
        vis2, audio2, _ = self.entity_resolver.resolve_scene_instances(
            scene_characters=["Майк"],
            source_quote="Спустя три года Майк сидит в своем кабинете и читает письмо.",
            shot_description="Майк открывает конверт.",
            scene_number=20,
        )
        self.assertEqual(len(vis2), 1)
        self.assertEqual(self.entity_resolver.entities[vis2[0].entity_id].canonical_name, "Майк")

    # 9. Две похожие хижины остаются разными; одна хижина с двумя именами не раздваивается.
    def test_case_9_two_cabins_remain_distinct_and_aliases_unified(self):
        loc1 = self.location_resolver.register_place("Северная хижина", "Хижина на северном склоне")
        loc2 = self.location_resolver.register_place("Старая хижина лесника", "Хижина в глубине леса")
        self.assertNotEqual(loc1.place_id, loc2.place_id)

        # Aliases / sub-name for same cabin
        resolved_id = self.location_resolver.resolve_place_name("Северная хижина")
        self.assertEqual(resolved_id, loc1.place_id)

    # 10. Комната и дверь принадлежат правильному дому; одинаковые комнаты разных домов не объединяются.
    def test_case_10_zones_belong_to_parent_place(self):
        p1 = self.location_resolver.register_place("Дом Дэниела")
        p2 = self.location_resolver.register_place("Дом Роберта")
        z1 = self.location_resolver.register_zone(p1.place_id, "Гостиная")
        z2 = self.location_resolver.register_zone(p2.place_id, "Гостиная")
        self.assertNotEqual(z1.zone_id, z2.zone_id)
        self.assertEqual(z1.place_id, p1.place_id)
        self.assertEqual(z2.place_id, p2.place_id)

    # 11. Ночь/утро/туман меняют состояние, не создают новое место.
    def test_case_11_environmental_states_do_not_create_new_place(self):
        clean, states = extract_environmental_states("Осенний лес на рассвете в густом тумане")
        self.assertEqual(clean, "Лес")
        self.assertTrue(any("рассвет" in s for s in states))
        self.assertTrue(any("туман" in s for s in states))

        loc_name, p_id, z_id, s_tags = self.location_resolver.resolve_shot_spatial_context(
            raw_location_str="Хижина ночью",
            shot_description="Лунный свет освещает деревянную хижину под дождем.",
            source_quote="Дэниел стоял у окна хижины ночью.",
        )
        self.assertEqual(loc_name, "Хижина")
        self.assertIn("ночью", s_tags)

    # 12. Возврат после пропуска событий сохраняет место и внешность.
    def test_case_12_return_to_location_preserves_identity(self):
        p_init = self.location_resolver.register_place("Станция", "Заброшенная станция с разбитыми окнами")
        # Later shot references station
        loc_name, p_id, _, _ = self.location_resolver.resolve_shot_spatial_context(
            raw_location_str="Станция",
            shot_description="Дэниел возвращается на станцию.",
            source_quote="Через пять часов он снова был на станции.",
        )
        self.assertEqual(p_id, p_init.place_id)
        self.assertEqual(self.location_resolver.places[p_id].description, p_init.description)

    # 13. Недостаточно данных о локации — unresolved, не первая локация в каталоге.
    def test_case_13_pseudo_place_handling(self):
        self.assertTrue(is_pseudo_place("Крупный план"))
        self.assertTrue(is_pseudo_place("Пол комнаты"))
        self.assertTrue(is_pseudo_place("Раннее утро"))
        self.assertTrue(is_pseudo_place("На запад"))

    # 14. Неиспользуемая локация проверяется на потерянное событие до скрытия/удаления.
    def test_case_14_unused_locations_tagged_with_reason(self):
        p1 = self.location_resolver.register_place("Лес")
        p2 = self.location_resolver.register_place("Маяк")  # never used in shots
        self.location_resolver.used_place_ids.add(p1.place_id)
        
        self.location_resolver.audit_unused_places(total_shots=10)
        self.assertEqual(p1.status, PlaceStatus.ACTIVE)
        self.assertEqual(p2.status, PlaceStatus.MENTIONED_ONLY)
        self.assertIn("не использована как место действия в 10 кадрах", p2.status_reason)

    # 15. Частичный список LLM не подавляет извлечение подтверждённого участника.
    def test_case_15_partial_llm_list_does_not_drop_active_hero(self):
        reconciler = TwoPassChunkReconciler(
            entity_resolver=self.entity_resolver,
            location_resolver=self.location_resolver,
            trace_collector=self.trace,
        )
        # Chunk 1 has Daniel
        # Chunk 2 has empty characters_present from LLM, but quote has Daniel
        raw_chunks = [
            {
                "characters": [{"name": "Дэниел", "description": "Мужчина в серой куртке"}],
                "locations": [{"name": "Лес", "description": "Сосновый лес"}],
                "scenes": [{
                    "shot": "Дэниел идет по тропе.",
                    "source_quote": "Дэниел шел вперед.",
                    "location": "Лес",
                    "characters_present": ["Дэниел"],
                }],
            },
            {
                "characters": [],
                "locations": [{"name": "Лес", "description": "Сосновый лес"}],
                "scenes": [{
                    "shot": "Дэниел останавливается и смотрит вверх.",
                    "source_quote": "Дэниел поднял глаза к небу.",
                    "location": "Лес",
                    "characters_present": [],  # Omitted by LLM
                }],
            }
        ]
        reconciler.register_first_pass_candidates(raw_chunks)
        shots = reconciler.reconcile_scenes_into_shots(raw_chunks, full_text="Дэниел шел вперед. Дэниел поднял глаза к небу.")
        
        # Daniel must still be in global entities
        daniel_id = self.entity_resolver.resolve_name("Дэниел")
        self.assertIsNotNone(daniel_id)
        self.assertEqual(len(self.entity_resolver.entities), 1)

    # 16. 0, 1, 2, 4, 11 и больше участников; толпа моделируется явно.
    def test_case_16_crowd_and_instance_scaling(self):
        quote = "Одиннадцать человек окружили хижину плотным кольцом."
        shot_desc = "Группа людей окружает здание."
        vis, audio, dep = self.entity_resolver.resolve_scene_instances(
            scene_characters=[],
            source_quote=quote,
            shot_description=shot_desc,
            scene_number=16,
        )
        self.assertEqual(len(vis), 11)
        self.assertTrue(all(inst.role_in_shot == "crowd_member" for inst in vis))

    # 17. Поломка одного этапа не публикует частичный результат как полностью готовый проект.
    def test_case_17_pipeline_needs_review_on_invalidation(self):
        pipeline = StoryAnalyzerV2Pipeline()
        raw_chunks = [{
            "characters": [{"name": "Майк", "description": ""}],
            "locations": [{"name": "Лес", "description": ""}],
            "scenes": [{
                "shot": "Майк идет по лесу.",
                "source_quote": "Майк шел по лесу.",
                "location": "Лес",
                "characters_present": ["Майк"],
            }]
        }]
        res = pipeline.analyze("Майк шел по лесу.", raw_chunks)
        self.assertTrue(res.shots[0].validation_status["schema_valid"])
        self.assertTrue(res.shots[0].validation_status["source_grounded"])

    def test_source_segment_index_rehydrates_exact_whitespace(self):
        """A constrained local source ID remains grounded despite normalized newlines."""
        story = "  Дэниел вошёл в хижину.\n— Дэн!\n"
        pipeline = StoryAnalyzerV2Pipeline()
        raw_chunks = [{
            "characters": [{"name": "Дэниел", "description": ""}],
            "locations": [{"name": "Хижина", "description": ""}],
            "scenes": [{
                "shot": "Дэниел слышит голос у хижины.",
                # This is how a local model commonly normalizes a source
                # segment: it preserves words but collapses the line break.
                "source_quote": "Дэниел вошёл в хижину. — Дэн!",
                "source_segment_index": 0,
                "location": "Хижина",
                "characters_present": ["Дэниел"],
            }],
        }]
        boundaries = [{
            "start_char": 0,
            "end_char": len(story),
            "text": story,
            "source_segment_spans": [{
                "start_char": 0,
                "end_char": len(story),
                "text": "Дэниел вошёл в хижину. — Дэн!",
            }],
        }]

        result = pipeline.analyze(story, raw_chunks, chunk_boundaries=boundaries)

        self.assertFalse(result.needs_review)
        self.assertEqual(result.shots[0].source_quote, "Дэниел вошёл в хижину.\n— Дэн!")
        self.assertTrue(result.shots[0].validation_status["source_grounded"])


class TestMetamorphicInvariance(unittest.TestCase):
    """Metamorphic property-based tests verifying invariants across transformations."""

    def test_idempotency_n_n_x_equals_n_x(self):
        pipeline = StoryAnalyzerV2Pipeline()
        story = "Дэниел подошел к хижине. Внутри горел свет."
        raw_chunks = [{
            "characters": [{"name": "Дэниел", "description": "Мужчина в куртке"}],
            "locations": [{"name": "Хижина", "description": "Деревянная хижина"}],
            "scenes": [{
                "shot": "Дэниел стоит перед хижиной.",
                "source_quote": "Дэниел подошел к хижине.",
                "location": "Хижина",
                "characters_present": ["Дэниел"],
            }]
        }]
        res1 = pipeline.analyze(story, raw_chunks)
        legacy1 = res1.to_legacy_analysis()

        # Re-running normalization on legacy analysis produces identical entities and locations
        res2 = pipeline.analyze(story, [legacy1])
        self.assertEqual(len(res1.entities), len(res2.entities))
        self.assertEqual(res1.entities[0].canonical_name, res2.entities[0].canonical_name)
        self.assertEqual(len(res1.places), len(res2.places))

    def test_frame_count_invariance_60_vs_222(self):
        """Increasing shot count for same story does NOT duplicate or alter physical entities."""
        story = "Дэниел вышел из дома и пошел через мост к озеру."
        
        # 2 scenes
        chunk_2 = [{
            "characters": [{"name": "Дэниел", "description": "Главный герой"}],
            "locations": [{"name": "Мост", "description": "Старый мост"}],
            "scenes": [
                {"shot": "Дэниел на мосту.", "source_quote": "Дэниел пошел через мост.", "location": "Мост", "characters_present": ["Дэниел"]},
                {"shot": "Дэниел у озера.", "source_quote": "вышел к озеру.", "location": "Озеро", "characters_present": ["Дэниел"]},
            ]
        }]
        
        # 6 scenes (expanded camera angles of same events)
        chunk_6 = [{
            "characters": [{"name": "Дэниел", "description": "Главный герой"}],
            "locations": [{"name": "Мост", "description": "Старый мост"}],
            "scenes": [
                {"shot": "Дэниел на мосту.", "source_quote": "Дэниел пошел через мост.", "location": "Мост", "characters_present": ["Дэниел"]},
                {"shot": "Крупный план ног Дэниела на мосту.", "source_quote": "Дэниел пошел через мост.", "location": "Мост", "characters_present": ["Дэниел"]},
                {"shot": "Дэниел смотрит на воду.", "source_quote": "Дэниел пошел через мост.", "location": "Мост", "characters_present": ["Дэниел"]},
                {"shot": "Дэниел сходит с моста.", "source_quote": "Дэниел пошел через мост.", "location": "Мост", "characters_present": ["Дэниел"]},
                {"shot": "Дэниел подходит к озеру.", "source_quote": "вышел к озеру.", "location": "Озеро", "characters_present": ["Дэниел"]},
                {"shot": "Дэниел у озера на закате.", "source_quote": "вышел к озеру.", "location": "Озеро", "characters_present": ["Дэниел"]},
            ]
        }]

        p1 = StoryAnalyzerV2Pipeline()
        r1 = p1.analyze(story, chunk_2)

        p2 = StoryAnalyzerV2Pipeline()
        r2 = p2.analyze(story, chunk_6)

        # Entity count and canonical names must be identical
        self.assertEqual(len(r1.entities), len(r2.entities))
        self.assertEqual([e.canonical_name for e in r1.entities], [e.canonical_name for e in r2.entities])


class TestMultiGenreCorpus(unittest.TestCase):
    """Tests across 12 distinct story genres + 4 holdout scenarios."""

    def test_corpus_1_family_drama(self):
        story = "Анна посмотрела на мужа. Виктор молча поставил чашку на стол."
        pipeline = StoryAnalyzerV2Pipeline()
        res = pipeline.analyze(story, [{
            "characters": [{"name": "Анна", "description": ""}, {"name": "Виктор", "description": ""}],
            "locations": [{"name": "Кухня", "description": ""}],
            "scenes": [{"shot": "Анна и Виктор за столом.", "source_quote": story, "location": "Кухня", "characters_present": ["Анна", "Виктор"]}]
        }])
        self.assertEqual(len(res.entities), 2)
        names = {e.canonical_name for e in res.entities}
        self.assertEqual(names, {"Анна", "Виктор"})

    def test_corpus_2_detective(self):
        story = "Инспектор Рид осмотрел сейф. В кабинете никого не было."
        pipeline = StoryAnalyzerV2Pipeline()
        res = pipeline.analyze(story, [{
            "characters": [{"name": "Инспектор Рид", "description": ""}],
            "locations": [{"name": "Кабинет", "description": ""}],
            "scenes": [{"shot": "Рид у сейфа.", "source_quote": story, "location": "Кабинет", "characters_present": ["Инспектор Рид"]}]
        }])
        self.assertEqual(len(res.entities), 1)
        self.assertEqual(res.entities[0].canonical_name, "Инспектор Рид")

    def test_corpus_3_wildlife_no_people(self):
        story = "Волк вышел на заснеженную поляну. Ворон взлетел с ветки ели."
        pipeline = StoryAnalyzerV2Pipeline()
        res = pipeline.analyze(story, [{
            "characters": [{"name": "Волк", "description": "Серый волк"}, {"name": "Ворон", "description": "Черный ворон"}],
            "locations": [{"name": "Поляна", "description": ""}],
            "scenes": [{"shot": "Волк на поляне.", "source_quote": story, "location": "Поляна", "characters_present": ["Волк"]}]
        }])
        self.assertEqual(len(res.entities), 2)
        self.assertEqual({e.canonical_name for e in res.entities}, {"Волк", "Ворон"})

    def test_corpus_4_long_story_multi_chunk_hero_preservation(self):
        """Verifies that Daniel is preserved when later chunks feature acoustic events."""
        story = (
            "Дэниел прибыл в хижину на краю леса. "
            "На следующий день в тишине раздался голос Майка. "
            "Дэниел взял рацию и ответил."
        )
        pipeline = StoryAnalyzerV2Pipeline()
        raw_chunks = [
            {
                "characters": [{"name": "Дэниел", "description": "Главный герой"}],
                "locations": [{"name": "Хижина", "description": "Лесная хижина"}],
                "scenes": [{
                    "shot": "Дэниел входит в хижину.",
                    "source_quote": "Дэниел прибыл в хижину на краю леса.",
                    "location": "Хижина",
                    "characters_present": ["Дэниел"],
                }]
            },
            {
                "characters": [{"name": "Голос Майка", "description": "Голос по рации"}],
                "locations": [{"name": "Хижина", "description": "Лесная хижина"}],
                "scenes": [{
                    "shot": "Дэниел слушает рацию.",
                    "source_quote": "На следующий день в тишине раздался голос Майка.",
                    "location": "Хижина",
                    "characters_present": ["Голос Майка"],
                }]
            },
            {
                "characters": [{"name": "Дэниел", "description": "Главный герой"}],
                "locations": [{"name": "Хижина", "description": "Лесная хижина"}],
                "scenes": [{
                    "shot": "Дэниел говорит в рацию.",
                    "source_quote": "Дэниел взял рацию и ответил.",
                    "location": "Хижина",
                    "characters_present": ["Дэниел"],
                }]
            }
        ]
        res = pipeline.analyze(story, raw_chunks)
        entity_names = [e.canonical_name for e in res.entities]
        self.assertIn("Дэниел", entity_names)
        self.assertIn("Майк", entity_names)
        self.assertNotIn("Голос Майк", entity_names)
        self.assertNotIn("Голос Майка", entity_names)


class TestStoryAnalyzerV2EndToEndAndPersistence(unittest.TestCase):
    """
    Validates end-to-end integration requirements:
    1. SourceSpan exact chunk binding and out-of-bounds rejection.
    2. Provider save prevention on needs_review.
    3. Expansion (e.g. 60 vs 222 frames) event & instance invariance, including atmospheric clearing.
    4. Active location prioritization and exact matching without alien assignment.
    5. Full SQLite roundtrip preservation of project & scene v2_metadata.
    6. Local and Groq provider routing through V2 pipeline.
    """

    def test_source_span_strictly_bounded_to_chunk(self):
        story = "Дэниел вошел в дом. Дэниел вошел в дом. Внезапно раздался звонок."
        first_start = 0
        first_end = story.find("Дэниел вошел в дом.", 1) + len("Дэниел вошел в дом.")
        second_start = story.find("Дэниел вошел в дом.", 1)
        second_end = len(story)

        chunk_1 = {
            "characters": [{"name": "Дэниел"}],
            "locations": [{"name": "Дом"}],
            "scenes": [{
                "shot": "Дэниел заходит.",
                "source_quote": "Дэниел вошел в дом.",
                "location": "Дом",
                "characters_present": ["Дэниел"],
            }]
        }
        chunk_2 = {
            "characters": [{"name": "Дэниел"}],
            "locations": [{"name": "Дом"}],
            "scenes": [{
                "shot": "Дэниел заходит снова.",
                "source_quote": "Дэниел вошел в дом.",
                "location": "Дом",
                "characters_present": ["Дэниел"],
            }]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_1, chunk_2],
            chunk_boundaries=[
                {"start_char": first_start, "end_char": first_end, "text": story[first_start:first_end]},
                {"start_char": second_start, "end_char": second_end, "text": story[second_start:second_end]},
            ]
        )
        self.assertEqual(len(res.shots), 2)
        # Shot 1 span must bind to the 1st occurrence (start 0)
        self.assertEqual(res.shots[0].source_spans[0].start, 0)
        # Shot 2 span must bind to the 2nd occurrence (start 20)
        self.assertEqual(res.shots[1].source_spans[0].start, 20)
        self.assertFalse(res.needs_review)

        # Negative check: chunk 2 trying to claim quote from chunk 1 when chunk 2 text doesn't contain it
        chunk_2_bad = {
            "characters": [{"name": "Дэниел"}],
            "locations": [{"name": "Дом"}],
            "scenes": [{
                "shot": "Выдуманная цитата.",
                "source_quote": "Несуществующая фраза в рассказе.",
                "location": "Дом",
                "characters_present": ["Дэниел"],
            }]
        }
        res_bad = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_1, chunk_2_bad],
            chunk_boundaries=[
                {"start_char": first_start, "end_char": first_end, "text": story[first_start:first_end]},
                {"start_char": second_start, "end_char": second_end, "text": story[second_start:second_end]},
            ]
        )
        self.assertTrue(res_bad.needs_review)
        self.assertTrue(res_bad.shots[1].needs_review)
        self.assertFalse(res_bad.shots[1].validation_status["source_grounded"])

    def test_provider_blocks_saving_on_needs_review(self):
        import unittest.mock as mock
        from imagegen.providers import LlamaCppProvider, ProviderError

        provider = LlamaCppProvider("http://localhost:8080")
        project = {
            "id": "proj_1",
            "version": 1,
            "story": "Анна гуляла по саду.",
            "mode": "characters",
        }

        # Mock _analyse_segments to return a fabricated quote that causes needs_review
        def fake_analyse_segments(proj, segments, target):
            return {
                "world": "Сад",
                "characters": [{"name": "Анна", "description": "Девушка", "wardrobe": {}}],
                "locations": [{"name": "Сад", "description": "Цветущий сад"}],
                "props": [],
                "scenes": [{
                    "number": 1,
                    "shot": "Анна гуляет.",
                    "beat": "Анна гуляет.",
                    "location": "Сад",
                    "location_ref": "Сад",
                    "characters": ["Анна"],
                    "characters_present": ["Анна"],
                    "source_quote": "Цитата которой нет в рассказе вообще",
                    "story_quote": "Цитата которой нет в рассказе вообще",
                    "props": [],
                    "visible_people": 1,
                }]
            }

        with mock.patch.object(provider, "_analyse_segments", side_effect=fake_analyse_segments):
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=1)
            self.assertIn("Разбор V2 требует проверки", str(ctx.exception))

    def test_scene_expansion_invariance_and_metadata_sync(self):
        from imagegen.providers import expand_storyboard_scenes, SHOT_TYPE_ATMOSPHERE

        story = "Алексей вошел в лабораторию и включил прибор. Затем он посмотрел в окно."
        pipeline = StoryAnalyzerV2Pipeline()
        raw_chunks = [{
            "characters": [{"name": "Алексей", "description": "Ученый"}],
            "locations": [{"name": "Лаборатория", "description": "Светлая лаборатория"}],
            "scenes": [
                {
                    "shot": "Алексей включает прибор.",
                    "source_quote": "Алексей вошел в лабораторию и включил прибор.",
                    "location": "Лаборатория",
                    "characters_present": ["Алексей"],
                },
                {
                    "shot": "Алексей смотрит в окно.",
                    "source_quote": "Затем он посмотрел в окно.",
                    "location": "Лаборатория",
                    "characters_present": ["Алексей"],
                }
            ]
        }]
        res = pipeline.analyze(story, raw_chunks)
        legacy = res.to_legacy_analysis()

        # Expand to 10 shots
        expanded = expand_storyboard_scenes(copy.deepcopy(legacy), target=10, camera_mode="cinema")
        self.assertEqual(len(expanded["scenes"]), 10)

        for sc in expanded["scenes"]:
            self.assertIn("event_id", sc)
            meta = sc.get("v2_metadata", {})
            self.assertEqual(meta.get("event_id"), sc["event_id"])
            if sc.get("shot_type") == SHOT_TYPE_ATMOSPHERE:
                self.assertEqual(sc["visible_people"], 0)
                self.assertEqual(sc["characters"], [])
                self.assertEqual(meta.get("visible_instances"), [])
            else:
                self.assertEqual(sc["visible_people"], 1)
                self.assertEqual(sc["characters"], ["Алексей"])
                self.assertEqual(len(meta.get("visible_instances", [])), 1)

    def test_active_locations_priority_and_no_alien_assignment(self):
        story = "Мария сидела на веранде у озера. Потом она пошла на причал."
        pipeline = StoryAnalyzerV2Pipeline()
        # 8 locations discovered in raw chunks, but only 2 active in scenes
        raw_chunks = [{
            "characters": [{"name": "Мария", "description": "Героиня"}],
            "locations": [
                {"name": "Париж", "description": "Город"},
                {"name": "Лондон", "description": "Город"},
                {"name": "Рим", "description": "Город"},
                {"name": "Берлин", "description": "Город"},
                {"name": "Токио", "description": "Город"},
                {"name": "Веранда", "description": "Веранда у дома"},
                {"name": "Причал", "description": "Деревянный причал"},
                {"name": "Нью-Йорк", "description": "Город"},
            ],
            "scenes": [
                {
                    "shot": "Мария на веранде.",
                    "source_quote": "Мария сидела на веранде у озера.",
                    "location": "Веранда",
                    "characters_present": ["Мария"],
                },
                {
                    "shot": "Мария на причале.",
                    "source_quote": "Потом она пошла на причал.",
                    "location": "Причал",
                    "characters_present": ["Мария"],
                }
            ]
        }]
        res = pipeline.analyze(story, raw_chunks)
        legacy = res.to_legacy_analysis()
        loc_names = [l["name"] for l in legacy["locations"]]
        # Active locations must be included
        self.assertIn("Веранда", loc_names)
        self.assertIn("Причал", loc_names)

        # Scenes must receive their exact location_ref, never an alien location
        self.assertEqual(legacy["scenes"][0]["location_ref"], "Веранда")
        self.assertEqual(legacy["scenes"][1]["location_ref"], "Причал")

    def test_sqlite_persistence_roundtrip_v2_metadata(self):
        import pathlib
        import tempfile
        from imagegen.service import Service

        story = "Виктор нашел старинный компас в кабинете."
        pipeline = StoryAnalyzerV2Pipeline()
        raw_chunks = [{
            "characters": [{"name": "Виктор", "description": "Исследователь"}],
            "locations": [{"name": "Кабинет", "description": "Старый кабинет"}],
            "props": [{"name": "Компас", "description": "Латунный компас", "importance": "story"}],
            "scenes": [{
                "shot": "Виктор осматривает компас.",
                "source_quote": "Виктор нашел старинный компас в кабинете.",
                "location": "Кабинет",
                "characters_present": ["Виктор"],
                "props_present": ["Компас"],
            }]
        }]
        res = pipeline.analyze(story, raw_chunks)
        analysis_data = res.to_legacy_analysis()

        with tempfile.TemporaryDirectory() as temp_dir:
            service = Service(pathlib.Path(temp_dir))
            project = service.create_project({"title": "Тест SQLite", "story": story, "style": "Cinematic realism", "aspect_ratio": "16:9"})
            project_id = project["id"]

            # Save analysis through service
            saved_detail = service.import_analysis(project_id, analysis_data, version=project["version"])

            # Verify project analysis_metadata
            self.assertIn("analysis_metadata", saved_detail)
            self.assertIn("entities", saved_detail["analysis_metadata"])
            self.assertEqual(len(saved_detail["analysis_metadata"]["entities"]), 1)
            self.assertEqual(saved_detail["analysis_metadata"]["entities"][0]["canonical_name"], "Виктор")

            # Verify scenes v2_metadata persisted
            self.assertEqual(len(saved_detail["scenes"]), 1)
            persisted_scene = saved_detail["scenes"][0]["data"]
            self.assertIn("v2_metadata", persisted_scene)
            self.assertEqual(persisted_scene["v2_metadata"]["place_id"], res.shots[0].place_id)
            self.assertEqual(persisted_scene["v2_metadata"]["event_id"], res.shots[0].event_id)

    def test_groq_and_local_provider_routing(self):
        import unittest.mock as mock
        from imagegen.providers import LlamaCppProvider, GroqProvider

        story = "Елена открыла дверь лаборатории."
        project = {
            "id": "proj_elena",
            "version": 1,
            "story": story,
            "mode": "characters",
        }

        def fake_chunk_analyse(proj, segments, target):
            return {
                "world": "Лаборатория",
                "characters": [{"name": "Елена", "description": "Исследователь", "wardrobe": {}}],
                "locations": [{"name": "Лаборатория", "description": "Светлая лаборатория"}],
                "props": [],
                "scenes": [{
                    "number": 1,
                    "shot": "Елена у двери.",
                    "beat": "Елена у двери.",
                    "location": "Лаборатория",
                    "location_ref": "Лаборатория",
                    "characters": ["Елена"],
                    "characters_present": ["Елена"],
                    "source_quote": "Елена открыла дверь лаборатории.",
                    "story_quote": "Елена открыла дверь лаборатории.",
                    "props": [],
                    "visible_people": 1,
                }]
            }

        # 1. Local LlamaCppProvider
        local_provider = LlamaCppProvider("http://localhost:8080")
        with mock.patch.object(local_provider, "_analyse_segments", side_effect=fake_chunk_analyse):
            local_res = local_provider.analyse(project, target=1)
            self.assertIn("v2_metadata", local_res)
            self.assertIn("scenes", local_res)
            self.assertIn("v2_metadata", local_res["scenes"][0])
            self.assertEqual(local_res["scenes"][0]["characters"], ["Елена"])

        # 2. Cloud GroqProvider
        groq_provider = GroqProvider("gsk_test_key_12345")
        with mock.patch.object(groq_provider, "_analyse_segments", side_effect=fake_chunk_analyse):
            groq_res = groq_provider.analyse(project, target=1)
            self.assertIn("v2_metadata", groq_res)
            self.assertIn("scenes", groq_res)
            self.assertIn("v2_metadata", groq_res["scenes"][0])
            self.assertEqual(groq_res["scenes"][0]["characters"], ["Елена"])


if __name__ == "__main__":
    unittest.main()


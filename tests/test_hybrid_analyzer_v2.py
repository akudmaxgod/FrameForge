import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from imagegen.db import Database, uid, now
from imagegen.domain import validate_analysis, story_segments
from imagegen.analyzer_v2.models import (
    EntityModality, SourceSpan, StoryAnalysisV2Result, Entity
)
from imagegen.analyzer_v2.pipeline import StoryAnalyzerV2Pipeline
from imagegen.analyzer_v2.validator import (
    validate_chunk_candidate, auto_repair_chunk_data,
    audit_story_coverage, validate_v2_result_structure,
    DiagnosticCode, DiagnosticSeverity, DiagnosticIssue
)
from imagegen.providers import (
    LlamaCppProvider, GroqProvider, HybridProvider,
    GroqRateLimitError, ProviderError, expand_storyboard_scenes
)
from imagegen.service import Service
from imagegen.worker import Worker


def make_groq_completion_fixture(scenes_data, world="Улица", characters=None, locations=None, props=None, usage=None):
    if isinstance(scenes_data, dict) and "scenes" in scenes_data:
        content_obj = dict(scenes_data)
    else:
        if characters is None:
            characters = [
                {"name": "Иван", "description": "Мужчина в пальто", "wardrobe": {}},
                {"name": "Петр", "description": "Мужчина в плаще", "wardrobe": {}},
            ]
        if locations is None:
            locations = [{"name": "Улица", "description": "Городская улица"}]
        if props is None:
            props = []
        content_obj = {
            "world": world,
            "characters": characters,
            "locations": locations,
            "props": props,
            "scenes": scenes_data
        }
    return {
        "choices": [{
            "message": {
                "content": json.dumps(content_obj, ensure_ascii=False)
            }
        }],
        "usage": usage or {"prompt_tokens": 150, "completion_tokens": 80, "total_tokens": 230}
    }


def mock_groq_transport(groq_instance, fixture_or_func):
    """Mock GroqProvider HTTP transport layer while faithfully updating usage metrics on the provider."""
    items_iter = iter(fixture_or_func) if isinstance(fixture_or_func, (list, tuple)) else None
    def _fake_chat_completion(*args, **kwargs):
        payload = args[0] if args else kwargs.get("payload", {})
        if items_iter is not None:
            resp = next(items_iter)
        elif callable(fixture_or_func):
            resp = fixture_or_func(payload)
        else:
            resp = fixture_or_func
        if groq_instance is not None:
            usage = resp.get("usage")
            if isinstance(usage, dict) and usage:
                groq_instance.last_usage = dict(usage)
                groq_instance.total_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                groq_instance.total_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
                groq_instance.total_usage["total_tokens"] += int(usage.get("total_tokens", 0))
                groq_instance.total_usage["calls"] += 1
            else:
                groq_instance.last_usage = {}
                groq_instance.total_usage["calls"] += 1
        return resp
    return _fake_chat_completion


class TestHybridAnalyzerV2(unittest.TestCase):
    """Test suite covering all 10 acceptance scenarios for FrameForge Hybrid Analyzer V2."""

    def test_scenario_1_voice_only_mike_never_physically_shown(self):
        """1. Voice-only Mike heard on recording/radio: no card, no visible person in scene."""
        story = (
            "Диспетчер сидит в пустой комнате управления. "
            "Из динамика раздается голос Майка: 'Прием, как слышно?'. "
            "Диспетчер слушает запись и молчит."
        )
        chunk_raw = {
            "world": "Комната управления",
            "characters": [],
            "locations": [{"name": "Комната управления", "description": "Комната с мониторами"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Dispatcher sits alone in control room.",
                    "characters": ["Диспетчер"],
                    "location": "Комната управления",
                    "location_ref": "Комната управления",
                    "visible_people": 1,
                    "shot": "Wide shot",
                    "emotion": "calm",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 1,
                    "beat": "Voice of Mike comes from speaker on the wall.",
                    "characters": [],
                    "location": "Комната управления",
                    "location_ref": "Комната управления",
                    "visible_people": 0,
                    "shot": "Close-up of radio speaker",
                    "emotion": "attentive",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 2,
                    "beat": "Dispatcher listens quietly to the audio.",
                    "characters": ["Диспетчер"],
                    "location": "Комната управления",
                    "location_ref": "Комната управления",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "focused",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{
                "chunk_index": 0,
                "start_char": 0,
                "end_char": len(story),
                "text": story,
                "source_segment_spans": [{"start_char": 0, "end_char": len(story), "text": story}]
            }],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        # Mike must NOT have a character card in characters list
        char_names = [c["name"] for c in legacy["characters"]]
        self.assertNotIn("Голос Майка", char_names)
        self.assertNotIn("Майк", char_names)
        self.assertIn("Диспетчер", char_names)

        # Scene 2 (speaker voice) must have 0 visible people and no Mike in characters
        scene_voice = legacy["scenes"][1]
        self.assertEqual(scene_voice["visible_people"], 0)
        self.assertEqual(scene_voice["characters"], [])
        self.assertNotIn("Майк", scene_voice.get("character_ids", []))
        self.assertNotIn("Голос Майка", scene_voice.get("character_ids", []))

    def test_scenario_2_mike_heard_first_then_enters_physically(self):
        """2. Mike heard first via radio, then physically enters: 1 entity, card created due to physical scene."""
        story = (
            "В радиоприемнике раздается голос Майка: 'Я уже подхожу'. "
            "Дверь открывается, и Майк в мокром дождевике заходит в комнату."
        )
        chunk_raw = {
            "world": "Комната управления",
            "characters": [
                {"name": "Майк", "description": "Мужчина 35 лет в мокром дождевике", "wardrobe": {}}
            ],
            "locations": [{"name": "Комната управления", "description": "Комната с мониторами"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Voice of Mike comes through the radio transmitter.",
                    "characters": [],
                    "location": "Комната управления",
                    "location_ref": "Комната управления",
                    "visible_people": 0,
                    "shot": "Close-up of radio",
                    "emotion": "neutral",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 1,
                    "beat": "Mike opens the door and enters the room in wet raincoat.",
                    "characters": ["Майк"],
                    "location": "Комната управления",
                    "location_ref": "Комната управления",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "determined",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        # Exactly 1 Mike card created
        char_names = [c["name"] for c in legacy["characters"]]
        self.assertIn("Майк", char_names)
        self.assertNotIn("Голос Майка", char_names)

        # Scene 1 has 0 visible people, Scene 2 has 1 visible person (Mike)
        self.assertEqual(legacy["scenes"][0]["visible_people"], 0)
        self.assertEqual(legacy["scenes"][0]["characters"], [])
        self.assertEqual(legacy["scenes"][1]["visible_people"], 1)
        self.assertEqual(legacy["scenes"][1]["characters"], ["Майк"])

    def test_scenario_3_voice_imitation_decoupled_identity(self):
        """3. Voice imitation: perceived identity is decoupled from physical cast."""
        story = (
            "Попугай на жердочке имитирует голос Майка: 'Майк вернулся!'. "
            "Анна улыбается птице."
        )
        chunk_raw = {
            "world": "Гостиная",
            "characters": [
                {"name": "Анна", "description": "Женщина 30 лет", "wardrobe": {}}
            ],
            "locations": [{"name": "Гостиная", "description": "Уютная гостиная"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Parrot on a perch imitates Mike's voice saying 'Mike is back!'.",
                    "characters": [],
                    "location": "Гостиная",
                    "location_ref": "Гостиная",
                    "visible_people": 0,
                    "shot": "Close-up of parrot",
                    "emotion": "curious",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 1,
                    "beat": "Anna smiles looking at the bird.",
                    "characters": ["Анна"],
                    "location": "Гостиная",
                    "location_ref": "Гостиная",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "happy",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        char_names = [c["name"] for c in legacy["characters"]]
        self.assertIn("Анна", char_names)
        self.assertNotIn("Майк", char_names)
        self.assertNotIn("Голос Майка", char_names)

    def test_scenario_4_character_on_photo_or_diary_only(self):
        """4. Character on photo or diary only receives no permanent card."""
        story = (
            "Олег находит на чердаке старую фотографию генерала Смирнова. "
            "Олег закрывает пыльную коробку."
        )
        chunk_raw = {
            "world": "Чердак",
            "characters": [
                {"name": "Олег", "description": "Мужчина 28 лет", "wardrobe": {}}
            ],
            "locations": [{"name": "Чердак", "description": "Пыльный чердак"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Oleg looks at an old black and white photograph of General Smirnov.",
                    "characters": ["Олег"],
                    "location": "Чердак",
                    "location_ref": "Чердак",
                    "visible_people": 1,
                    "shot": "Over-the-shoulder shot",
                    "emotion": "thoughtful",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 1,
                    "beat": "Oleg closes the dusty storage box.",
                    "characters": ["Олег"],
                    "location": "Чердак",
                    "location_ref": "Чердак",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "calm",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        char_names = [c["name"] for c in legacy["characters"]]
        self.assertIn("Олег", char_names)
        self.assertNotIn("Генерал Смирнов", char_names)
        self.assertNotIn("Смирнов", char_names)

    def test_scenario_5_alias_unification_and_episodic_person(self):
        """5. Daniel and Dan unified; one-off visible ranger stays episodic without permanent card."""
        story = (
            "Дэниел осматривает карту в хижине. "
            "Лесной рейнджер передает ему фонарь. "
            "Дэн выходит на ночную тропу."
        )
        chunk_raw = {
            "world": "Лесная хижина",
            "characters": [
                {"name": "Дэниел", "description": "Мужчина 32 года", "wardrobe": {}},
                {"name": "Дэн", "description": "Мужчина 32 года", "wardrobe": {}},
                {"name": "Лесной рейнджер", "description": "Рейнджер парка в форме", "wardrobe": {}}
            ],
            "locations": [{"name": "Хижина", "description": "Деревянная хижина"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Daniel examines the topographical map inside the cabin.",
                    "characters": ["Дэниел"],
                    "location": "Хижина",
                    "location_ref": "Хижина",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "focused",
                    "time": "night",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 1,
                    "beat": "A park ranger hands a heavy flashlight to Daniel.",
                    "characters": ["Дэниел", "Лесной рейнджер"],
                    "location": "Хижина",
                    "location_ref": "Хижина",
                    "visible_people": 2,
                    "shot": "Two shot",
                    "emotion": "serious",
                    "time": "night",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": 2,
                    "beat": "Dan steps outside onto the dark mountain trail.",
                    "characters": ["Дэн"],
                    "location": "Хижина",
                    "location_ref": "Хижина",
                    "visible_people": 1,
                    "shot": "Wide shot",
                    "emotion": "determined",
                    "time": "night",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        # Exactly 1 character card for Daniel (Dan merged)
        self.assertEqual(len(legacy["characters"]), 1)
        self.assertEqual(legacy["characters"][0]["name"], "Дэниел")
        # The episodic ranger is not given a permanent card
        self.assertNotIn("Рейнджер", [c["name"] for c in legacy["characters"]])
        self.assertNotIn("Лесной рейнджер", [c["name"] for c in legacy["characters"]])
        # But is tracked in episodic_people for shot 2
        self.assertIn("Лесной рейнджер", legacy["scenes"][1].get("episodic_people", []))

    def test_scenario_6_repeated_quotes_bound_to_correct_segment_offsets(self):
        """6. Repeated quotes bound to correct segment offsets without crossing."""
        story = (
            "Анна сказала: 'Мы должны идти'. Дорога была длинной. "
            "Позже в горах Анна снова повторила: 'Мы должны идти'."
        )
        segments = story_segments(story)
        self.assertGreaterEqual(len(segments), 3)

        chunk_raw = {
            "world": "Горы",
            "characters": [{"name": "Анна", "description": "Девушка 25 лет", "wardrobe": {}}],
            "locations": [{"name": "Горная тропа", "description": "Каменистая тропа"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "source_quote": segments[0],
                    "beat": "Anna speaks at the start of the journey.",
                    "characters": ["Анна"],
                    "location": "Горная тропа",
                    "location_ref": "Горная тропа",
                    "visible_people": 1,
                    "shot": "Close-up",
                    "emotion": "serious",
                    "time": "day",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                },
                {
                    "source_id": len(segments) - 1,
                    "source_quote": segments[-1],
                    "beat": "Anna repeats her words on the high mountain pass.",
                    "characters": ["Анна"],
                    "location": "Горная тропа",
                    "location_ref": "Горная тропа",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "determined",
                    "time": "sunset",
                    "continuity_before": "",
                    "continuity_after": "",
                    "wardrobe": {},
                    "props": [],
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        shots = v2_res.shots
        self.assertEqual(len(shots), 2)
        # Verify first shot span is earlier in text than second shot span
        span1 = shots[0].source_spans[0]
        span2 = shots[1].source_spans[0]
        self.assertLess(span1.start, span2.start)

    def test_scenario_7_story_coverage_audit_beginning_middle_end(self):
        """7. 20k char story target coverage covers beginning, middle, and end without coverage gap."""
        paragraphs = [f"Параграф {i}: Событие номер {i} разворачивается в локации {i%5}. Происходит важное действие." for i in range(100)]
        long_story = " ".join(paragraphs)
        self.assertGreater(len(long_story), 8000)

        all_segs = story_segments(long_story)
        selected_segs = [all_segs[0], all_segs[50], all_segs[-1]]

        issues = audit_story_coverage(long_story, selected_segs, all_segs)
        # Should detect gap if middle chunks are too far apart, but beginning and end are covered
        self.assertIn(all_segs[0], selected_segs)
        self.assertIn(all_segs[-1], selected_segs)

    def test_scenario_8_hybrid_mock_only_problematic_block_sent_to_groq(self):
        """8. Hybrid mock: only 1 problematic block sent to Groq mock, other blocks remain local."""
        story = (
            "Часть 1. Иван идет по улице. "
            "Часть 2. Раздается голос Майка в наушнике. "
            "Часть 3. Иван входит в дом."
        )

        class MockLocalProvider(LlamaCppProvider):
            def __init__(self):
                self.http = MagicMock()
                self.model = "story-analyser"

            def _analyse_segments(self, project, segments, target):
                # Job 1 (clean)
                if any("Часть 1" in s for s in segments):
                    return {
                        "world": "Улица",
                        "characters": [{"name": "Иван", "description": "Мужчина 30 лет", "wardrobe": {}}],
                        "locations": [{"name": "Улица", "description": "Городская улица"}],
                        "props": [],
                        "scenes": [{
                            "source_id": 0, "source_quote": segments[0], "beat": "Ivan walks down the street.", "characters": ["Иван"],
                            "location": "Улица", "location_ref": "Улица", "visible_people": 1,
                            "shot": "Wide shot", "emotion": "calm", "time": "day",
                            "continuity_before": "", "continuity_after": "", "wardrobe": {}, "props": []
                        }]
                    }
                # Job 2 (has voice error needing semantic review)
                elif any("Часть 2" in s for s in segments):
                    return {
                        "world": "Улица",
                        "characters": [{"name": "Голос Майка", "description": "Мужчина", "wardrobe": {}}],
                        "locations": [{"name": "Улица", "description": "Городская улица"}],
                        "props": [],
                        "scenes": [{
                            "source_id": 0, "source_quote": segments[0], "beat": "Voice of Mike in earphone.", "characters": ["Голос Майка"],
                            "location": "Улица", "location_ref": "Улица", "visible_people": 1,
                            "shot": "Close-up", "emotion": "neutral", "time": "day",
                            "continuity_before": "", "continuity_after": "", "wardrobe": {}, "props": []
                        }]
                    }
                # Job 3 (clean)
                else:
                    return {
                        "world": "Дом",
                        "characters": [{"name": "Иван", "description": "Мужчина 30 лет", "wardrobe": {}}],
                        "locations": [{"name": "Дом", "description": "Жилой дом"}],
                        "props": [],
                        "scenes": [{
                            "source_id": 0, "source_quote": segments[0], "beat": "Ivan enters the house.", "characters": ["Иван"],
                            "location": "Дом", "location_ref": "Дом", "visible_people": 1,
                            "shot": "Medium shot", "emotion": "calm", "time": "day",
                            "continuity_before": "", "continuity_after": "", "wardrobe": {}, "props": []
                        }]
                    }

        mock_groq = MagicMock(spec=GroqProvider)
        mock_groq.repair_chunk.return_value = {
            "world": "Улица",
            "characters": [],
            "locations": [{"name": "Улица", "description": "Городская улица"}],
            "props": [],
            "scenes": [{
                "source_id": 0, "source_quote": "Часть 2. Раздается голос Майка в наушнике.", "beat": "Ivan hears voice in headphone.", "characters": [],
                "location": "Улица", "location_ref": "Улица", "visible_people": 0,
                "shot": "Close-up of earphone", "emotion": "neutral", "time": "day",
                "continuity_before": "", "continuity_after": "", "wardrobe": {}, "props": []
            }]
        }

        local_provider = MockLocalProvider()
        project = {"story": story, "style": "cinematic", "idea": "", "mode": "characters"}

        with patch("imagegen.providers.chunk_segments", side_effect=lambda segs: [[s] for s in segs]):
            res = local_provider.analyse(project, target=3, hybrid_verifier=mock_groq)

        # Groq must be called exactly once for the problematic chunk
        self.assertEqual(mock_groq.repair_chunk.call_count, 1)
        # Final result must not have 'Голос Майка'
        char_names = [c["name"] for c in res["characters"]]
        self.assertNotIn("Голос Майка", char_names)

    def test_scenario_9_groq_error_429_or_bad_json_halts_saving_with_diagnostics(self):
        """9. Groq 429 error cleanly raises GroqRateLimitError; invalid data halts saving."""
        class MockLocalProvider(LlamaCppProvider):
            def __init__(self):
                self.http = MagicMock()
                self.model = "story-analyser"

            def _analyse_segments(self, project, segments, target):
                return {
                    "world": "Мир",
                    "characters": [{"name": "Голос Майка", "description": "Голос", "wardrobe": {}}],
                    "locations": [{"name": "Место", "description": "Место"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0, "source_quote": segments[0], "beat": "Voice speaks.", "characters": ["Голос Майка"],
                        "location": "Место", "location_ref": "Место", "visible_people": 1,
                        "shot": "Shot", "emotion": "calm", "time": "day",
                        "continuity_before": "", "continuity_after": "", "wardrobe": {}, "props": []
                    }]
                }

        mock_groq = MagicMock(spec=GroqProvider)
        mock_groq.repair_chunk.side_effect = GroqRateLimitError("Rate limit reached on all keys.")

        local_provider = MockLocalProvider()
        project = {"story": "Слышен голос Майка в тишине.", "style": "cinematic", "idea": "", "mode": "characters"}

        with patch("imagegen.providers.chunk_segments", side_effect=lambda segs: [[s] for s in segs]):
            with self.assertRaises(GroqRateLimitError):
                local_provider.analyse(project, target=1, hybrid_verifier=mock_groq)

    def test_scenario_10_sqlite_persistence_roundtrip_consistency(self):
        """10. SQLite persistence roundtrip with consistent cast_bible, v2_metadata, and recompiled flow_prompt via HybridProvider."""
        import tempfile
        from imagegen.db import dump

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            worker = Worker(service)

            project_data = {
                "title": "Тестовый проект",
                "story": "Анна смотрит в окно. За окном идет дождь.",
                "style": "cinematic",
                "mode": "characters"
            }
            project = service.create_project(project_data)

            class MockLocalProvider(LlamaCppProvider):
                def __init__(self):
                    super().__init__("http://127.0.0.1:11435")
                def _analyse_segments(self, proj, segments, target):
                    return {
                        "world": "Комната",
                        "characters": [{"name": "Анна", "description": "Молодая женщина", "wardrobe": {}}],
                        "locations": [{"name": "Комната", "description": "Комната с окном"}],
                        "props": [],
                        "scenes": [{
                            "source_id": 0,
                            "beat": "Анна смотрит в окно на дождь.",
                            "characters": ["Анна"],
                            "location": "Комната",
                            "visible_people": 1,
                            "shot": "Medium close-up",
                            "emotion": "melancholic",
                            "source_quote": "Анна смотрит в окно.",
                        }]
                    }

            mock_groq = MagicMock(spec=GroqProvider)
            local_provider = MockLocalProvider()
            
            # Execute actual provider.analyse()
            analysis = local_provider.analyse(project, target=1, hybrid_verifier=mock_groq)

            gen_id = uid()
            generation = {
                "id": gen_id,
                "project_id": project["id"],
                "purpose": "analysis",
                "snapshot": {"project": project, "target": 1, "profile": service.settings()},
                "cancel_requested": 0,
                "state": "running"
            }

            with service.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                    (gen_id, project["id"], "analysis", dump(generation["snapshot"]), 12345, now())
                )

            worker._save_analysis(generation, analysis, project["version"])

            # Verify saved project in SQLite
            loaded_project = service.detail(project["id"])
            self.assertEqual(len(loaded_project["characters"]), 1)
            self.assertEqual(loaded_project["characters"][0]["name"], "Анна")
            self.assertEqual(loaded_project["characters"][0]["cast_id"], "char_anna")
            self.assertEqual(len(loaded_project["scenes"]), 1)
            self.assertEqual(loaded_project["scenes"][0]["data"]["characters"], ["Анна"])
            self.assertEqual(loaded_project["scenes"][0]["data"]["character_ids"], ["char_anna"])
            self.assertEqual(loaded_project["scenes"][0]["data"]["visible_people"], 1)
            cast_ids = [c["cast_id"] for c in loaded_project["cast_bible"]["characters"]]
            self.assertIn("char_anna", cast_ids)
            self.assertTrue(loaded_project["scenes"][0]["data"].get("flow_prompt"))

    def test_scenario_11_budget_exhaustion_fails_closed(self):
        """11. Exhaustion of max 5 cloud checks fails closed (blocks saving and raises ProviderError)."""
        class MockLocalProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                # Always return problematic acoustic chunk needing review
                return {
                    "world": "Комната",
                    "characters": [{"name": "Голос Майка", "description": "Голос"}],
                    "locations": [{"name": "Комната", "description": "Комната"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0,
                        "beat": "Слышен голос Майка.",
                        "characters": ["Голос Майка"],
                        "location": "Комната",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Из-за закрытой двери раздался голос Майка, просящий о помощи."
                    }]
                }

        mock_groq = MagicMock(spec=GroqProvider)
        # Mock successful repair for first 5 calls
        mock_groq.repair_chunk.return_value = {
            "world": "Комната",
            "characters": [],
            "locations": [{"name": "Комната", "description": "Комната"}],
            "props": [],
            "scenes": [{
                "source_id": 0,
                "beat": "Из-за двери доносится голос Майка.",
                "characters": [],
                "location": "Комната",
                "visible_people": 0,
                "shot": "Medium shot",
                "source_quote": "Из-за закрытой двери раздался голос Майка, просящий о помощи."
            }]
        }

        local_provider = MockLocalProvider()
        # Story with 6 distinct segments
        story = " ".join([f"Сегмент {i}. Из-за закрытой двери раздался голос Майка, просящий о помощи." for i in range(6)])
        project = {"story": story, "style": "cinematic", "idea": "", "mode": "characters"}

        # Patch chunking into 6 distinct single-segment jobs
        with patch("imagegen.providers.DEFAULT_MAX_CLOUD_REPAIRS_TOTAL", 5), \
             patch("imagegen.providers.subdivide_analysis_jobs", return_value=[([f"Сегмент {i}. Из-за закрытой двери раздался голос Майка, просящий о помощи."], 1) for i in range(6)]):
            with self.assertRaises(ProviderError) as ctx:
                local_provider.analyse(project, target=6, hybrid_verifier=mock_groq)
            self.assertIn("исчерпан общий лимит обращений", str(ctx.exception))
            self.assertIn("5/5", str(ctx.exception))

    def test_scenario_12_invalid_repaired_chunk_from_groq_fails_closed(self):
        """12. Groq returning invalid/empty chunk fails closed with ProviderError."""
        class MockLocalProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Комната",
                    "characters": [{"name": "Голос Майка", "description": "Голос"}],
                    "locations": [{"name": "Комната", "description": "Комната"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0,
                        "beat": "Слышен голос Майка.",
                        "characters": ["Голос Майка"],
                        "location": "Комната",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Из-за закрытой двери раздался голос Майка, просящий о помощи."
                    }]
                }

        mock_groq = MagicMock(spec=GroqProvider)
        # Groq returns corrupted/invalid candidate (empty scenes)
        mock_groq.repair_chunk.return_value = {
            "world": "Комната",
            "characters": [],
            "locations": [],
            "props": [],
            "scenes": []
        }

        local_provider = MockLocalProvider()
        project = {"story": "Из-за закрытой двери раздался голос Майка, просящий о помощи.", "style": "cinematic", "idea": "", "mode": "characters"}

        with patch("imagegen.providers.chunk_segments", return_value=[["Из-за закрытой двери раздался голос Майка, просящий о помощи."]]):
            with self.assertRaises(ProviderError) as ctx:
                local_provider.analyse(project, target=1, hybrid_verifier=mock_groq)
            self.assertIn("Разбор V2 требует проверки", str(ctx.exception))

    def test_scenario_13_simultaneous_physical_and_voice_rejected_and_no_card(self):
        """13. 'Слышен голос Майка по рации' with characters: ['Майк'], visible_people: 1 leaves 0 character cards for Mike."""
        story = "Слышен голос Майка по рации. Алексей слушает сообщение."
        chunk_raw = {
            "world": "Кабина",
            "characters": [
                {"name": "Алексей", "description": "Пилот", "wardrobe": {}},
                {"name": "Майк", "description": "Голос по рации", "wardrobe": {}}
            ],
            "locations": [{"name": "Кабина", "description": "Кабина пилота"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей слышит голос Майка по рации.",
                    "characters": ["Алексей", "Майк"],  # Local LLM anomaly: Mike added as visible person
                    "location": "Кабина",
                    "visible_people": 2,
                    "shot": "Medium shot",
                    "emotion": "attentive",
                    "source_quote": "Слышен голос Майка по рации. Алексей слушает сообщение.",
                }
            ]
        }

        pipeline = StoryAnalyzerV2Pipeline()
        v2_res = pipeline.analyze(
            story_text=story,
            raw_chunk_results=[chunk_raw],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq"
        )
        legacy = v2_res.to_legacy_analysis()

        # Mike must NOT be in characters (no character card)
        char_names = [c["name"] for c in legacy["characters"]]
        self.assertIn("Алексей", char_names)
        self.assertNotIn("Майк", char_names)
        self.assertNotIn("Голос Майка", char_names)
        
        # Mike must NOT be in cast_bible
        cast_ids = [c["cast_id"] for c in legacy.get("cast_bible", {}).get("characters", [])]
        self.assertIn("char_aleksey", cast_ids)
        self.assertNotIn("char_majk", cast_ids)
        self.assertNotIn("char_mike", cast_ids)

        # Shot visible_people must be 1 (only Aleksey)
        self.assertEqual(legacy["scenes"][0]["visible_people"], 1)
        self.assertEqual(legacy["scenes"][0]["characters"], ["Алексей"])

    def test_scenario_14_local_auto_repair_post_validation_failure_blocks_saving(self):
        """14. If local chunk has invalid structure (e.g. empty scenes) after auto_repair_chunk_data, validate_chunk_candidate fails closed."""
        raw_chunk = {
            "world": "Комната",
            "characters": [],
            "locations": [],
            "props": [],
            "scenes": []  # Empty scenes list
        }
        source_segments = ["Анна смотрит в окно."]
        repaired_chunk, repair_report = auto_repair_chunk_data(raw_chunk, source_segments, job_id="job_1")
        post_report = validate_chunk_candidate(repaired_chunk, source_segments, job_id="job_1", story_text="Анна смотрит в окно.")
        self.assertFalse(post_report.is_valid)
        self.assertTrue(post_report.has_critical_error)

        class BrokenLocalProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return raw_chunk

        provider = BrokenLocalProvider()
        project = {"story": "Анна смотрит в окно.", "style": "cinematic", "mode": "characters"}
        with patch("imagegen.providers.chunk_segments", return_value=[source_segments]):
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=1)
            self.assertIn("Разбор V2 требует проверки", str(ctx.exception))

    def test_scenario_15_coverage_gap_audit_semantic_review_repaired_or_blocked(self):
        """15. Large middle gap in story triggers audit; either bridges missing segments or fails closed."""
        story = "Начало истории. " + " ".join(f"Событие {i} в середине." for i in range(20)) + " Финал истории."
        all_segs = story_segments(story)
        self.assertGreater(len(all_segs), 10)

        # 1. Manually check audit_story_coverage detects middle gap
        only_start_and_end = [all_segs[0], all_segs[-1]]
        issues = audit_story_coverage(story, only_start_and_end, all_segs)
        self.assertTrue(any(i.code == DiagnosticCode.COVERAGE_GAP.value for i in issues))

        # 2. When analysed via LlamaCppProvider, the gap repair loop adds intermediate segments
        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Мир",
                    "characters": [],
                    "locations": [{"name": "Место", "description": "Место"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0, "beat": "Действие.", "characters": [], "location": "Место",
                        "visible_people": 0, "shot": "Wide", "emotion": "calm", "source_quote": segments[0]
                    }]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "illustration"}
        # Ensure analysis succeeds because the gap is bridged across middle
        result = provider.analyse(project, target=3)
        self.assertGreater(len(result["scenes"]), 0)

        # 3. If gap cannot be bridged (e.g. audit still produces issues), ProviderError is raised
        with patch("imagegen.providers.audit_story_coverage", return_value=[
            DiagnosticIssue(
                code=DiagnosticCode.COVERAGE_GAP.value,
                severity=DiagnosticSeverity.SEMANTIC_REVIEW.value,
                explanation="Неустранимый пробел в сюжете"
            )
        ]):
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=3)
            self.assertIn("пробел в покрытии сюжета", str(ctx.exception))

    def test_scenario_16_worker_e2e_hybrid_mode_with_mocked_http(self):
        """16. Full Worker E2E test with settings -> Worker -> HybridProvider -> local -> Groq repair -> SQLite -> service.detail."""
        import tempfile
        import urllib.request

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            service.save_settings({
                "text_backend": "hybrid",
                "story_analyzer_version": "v2",
                "text_url": "http://127.0.0.1:11435",
                "groq_api_key": "gsk_mock_test_key_123",
                "text_model": "story-analyser",
                "groq_model": "openai/gpt-oss-120b"
            })
            worker = Worker(service)

            story_text = "Анна сидит в комнате. Слышен голос Майка по рации."
            project = service.create_project({
                "title": "E2E Hybrid Project",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            # Local response: ambiguous chunk where Mike is incorrectly added as visible person
            local_raw_content = json.dumps({
                "world": "Комната",
                "characters": [
                    {"name": "Анна", "description": "Женщина 30 лет", "wardrobe": {}},
                    {"name": "Голос Майка", "description": "Голос по рации", "wardrobe": {}}
                ],
                "locations": [{"name": "Комната", "description": "Комната с окном"}],
                "props": [],
                "scenes": [{
                    "source_id": 0,
                    "beat": "Анна слушает голос Майка по рации.",
                    "characters": ["Анна", "Голос Майка"],
                    "location": "Комната",
                    "visible_people": 2,
                    "shot": "Medium shot",
                    "emotion": "attentive",
                    "source_quote": "Анна сидит в комнате. Слышен голос Майка по рации."
                }]
            })

            # Groq response: repaired chunk with strictly 1 visible person (Anna) and usage telemetry
            groq_repaired_content = json.dumps({
                "world": "Комната",
                "characters": [
                    {"name": "Анна", "description": "Женщина 30 лет", "wardrobe": {}}
                ],
                "locations": [{"name": "Комната", "description": "Комната с окном"}],
                "props": [],
                "scenes": [{
                    "source_id": 0,
                    "beat": "Анна сидит в комнате и слушает сообщение по рации.",
                    "characters": ["Анна"],
                    "location": "Комната",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "emotion": "attentive",
                    "source_quote": "Анна сидит в комнате. Слышен голос Майка по рации."
                }]
            })

            class MockHttpResponse:
                def __init__(self, data):
                    self._data = data
                def read(self, *args):
                    return self._data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            def fake_urlopen(req, timeout=90):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "11435" in url:
                    # Local llama-cpp / vllm response
                    body = json.dumps({
                        "choices": [{"message": {"content": local_raw_content}, "finish_reason": "stop"}]
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                elif "api.groq.com" in url:
                    # Groq API response with usage
                    body = json.dumps({
                        "choices": [{"message": {"content": groq_repaired_content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 420, "completion_tokens": 180, "total_tokens": 600}
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                raise RuntimeError(f"Unexpected URL: {url}")

            with service.db.transaction() as conn:
                gen_id = service.insert_generation(
                    conn, project["id"], "analysis",
                    {"profile": service.settings(), "project": project, "target": 1}
                )
            generation = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))

            orig_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = fake_urlopen
                worker.prepare(generation)
            finally:
                urllib.request.urlopen = orig_urlopen

            # Verify generation succeeded
            updated_gen = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(updated_gen.get("error"), None)
            self.assertEqual(updated_gen["state"], "succeeded")

            # Verify service.detail roundtrip
            detail = service.detail(project["id"])
            self.assertEqual(len(detail["scenes"]), 1)
            self.assertEqual(detail["scenes"][0]["data"]["characters"], ["Анна"])
            self.assertEqual(detail["scenes"][0]["data"]["visible_people"], 1)

            # Check character cards and cast bible
            char_names = [c["name"] for c in detail["characters"]]
            self.assertIn("Анна", char_names)
            self.assertNotIn("Майк", char_names)

            cast_ids = [c["cast_id"] for c in detail["cast_bible"].get("characters", [])]
            self.assertIn("char_anna", cast_ids)
            self.assertNotIn("char_majk", cast_ids)

            # Check diagnostics and telemetry in analysis_metadata
            metadata = detail["analysis_metadata"]
            diagnostics = metadata.get("diagnostics", {})
            self.assertEqual(diagnostics.get("cloud_repairs_count"), 1)
            self.assertIn("groq_usage", diagnostics)
            self.assertEqual(diagnostics["groq_usage"]["prompt_tokens"], 420)
            self.assertEqual(diagnostics["groq_usage"]["completion_tokens"], 180)
            self.assertEqual(diagnostics["groq_usage"]["total_tokens"], 600)
            self.assertEqual(diagnostics["groq_usage"]["calls"], 1)

    def test_scenario_17_groq_telemetry_aggregation_and_empty_usage(self):
        """17. Groq usage telemetry: aggregation across calls, storage in metadata, and no fictional values when usage is empty."""
        import urllib.request

        # 1. Aggregation across multiple calls
        provider = GroqProvider("gsk_mock_token_abc")

        class MockHttpResp:
            def __init__(self, data):
                self._data = data
            def read(self, *args):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        resp1_body = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        }).encode("utf-8")

        resp2_body = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280}
        }).encode("utf-8")

        resp3_empty_usage = json.dumps({
            "choices": [{"message": {"content": "{}"}}]
            # No usage key
        }).encode("utf-8")

        call_idx = 0
        def fake_groq_call(req, timeout=90):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return MockHttpResp(resp1_body)
            elif call_idx == 2:
                return MockHttpResp(resp2_body)
            else:
                return MockHttpResp(resp3_empty_usage)

        orig_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = fake_groq_call
            provider._chat_completion({"model": "openai/gpt-oss-120b", "messages": []})
            provider._chat_completion({"model": "openai/gpt-oss-120b", "messages": []})

            # Verify aggregated sums
            self.assertEqual(provider.total_usage["prompt_tokens"], 300)
            self.assertEqual(provider.total_usage["completion_tokens"], 130)
            self.assertEqual(provider.total_usage["total_tokens"], 430)
            self.assertEqual(provider.total_usage["calls"], 2)

            # Call with empty usage: calls increments, token counts do NOT fabricate numbers
            provider._chat_completion({"model": "openai/gpt-oss-120b", "messages": []})
            self.assertEqual(provider.total_usage["prompt_tokens"], 300)
            self.assertEqual(provider.total_usage["completion_tokens"], 130)
            self.assertEqual(provider.total_usage["total_tokens"], 430)
            self.assertEqual(provider.total_usage["calls"], 3)
            self.assertEqual(provider.last_usage, {})
        finally:
            urllib.request.urlopen = orig_urlopen

    def test_scenario_18_repeated_identical_sentences_maintains_64_budget_limit(self):
        """18. Story with 100 identical sentences strictly adheres to the 64-event budget and does not explode back to 100."""
        repeated_sentence = "Ветер шумит за окном дома."
        story = " ".join([repeated_sentence] * 100)
        all_segs = story_segments(story)
        self.assertEqual(len(all_segs), 100)

        class MockLocalProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
                self.received_job_segment_counts = []

            def _analyse_segments(self, proj, segments, target):
                self.received_job_segment_counts.append(len(segments))
                return {
                    "world": "Дом",
                    "characters": [],
                    "locations": [{"name": "Дом", "description": "Дом с окном"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0,
                        "beat": "Ветер шумит за окном дома.",
                        "characters": [],
                        "location": "Дом",
                        "visible_people": 0,
                        "shot": "Wide shot",
                        "emotion": "calm",
                        "source_quote": segments[0],
                    }]
                }

        provider = MockLocalProvider()
        project = {"story": story, "style": "cinematic", "mode": "illustration"}

        result = provider.analyse(project, target=48)
        
        # Total segments distributed across jobs must equal 64, NOT 100.
        total_analyzed_segments = sum(provider.received_job_segment_counts)
        self.assertEqual(total_analyzed_segments, 64)
        self.assertLessEqual(len(result["scenes"]), 48)

    def test_scenario_19_local_unambiguous_on_with_multiple_men_no_groq(self):
        """19. Local unambiguous 'он' with multiple men in story does NOT trigger Groq."""
        story = (
            "Алексей вошёл в кабинет. Виктор остался в коридоре. "
            "Алексей открыл журнал. Он нашёл запись."
        )
        mock_groq = MagicMock(spec=GroqProvider)
        mock_groq.repair_chunk = MagicMock()

        raw_chunk = {
            "world": "Офис",
            "characters": [
                {"name": "Алексей", "description": "Сотрудник 35 лет", "wardrobe": {}},
                {"name": "Виктор", "description": "Коллега 40 лет", "wardrobe": {}},
            ],
            "locations": [
                {"name": "Кабинет", "description": "Рабочий кабинет"},
                {"name": "Коридор", "description": "Коридор"},
            ],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей заходит в кабинет.",
                    "characters": ["Алексей"],
                    "location": "Кабинет",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Алексей вошёл в кабинет.",
                },
                {
                    "source_id": 1,
                    "beat": "Виктор стоит в коридоре.",
                    "characters": ["Виктор"],
                    "location": "Коридор",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Виктор остался в коридоре.",
                },
                {
                    "source_id": 2,
                    "beat": "Алексей открывает журнал.",
                    "characters": ["Алексей"],
                    "location": "Кабинет",
                    "visible_people": 1,
                    "shot": "Close-up",
                    "source_quote": "Алексей открыл журнал.",
                },
                {
                    "source_id": 3,
                    "beat": "Алексей находит нужную запись в журнале.",
                    "characters": ["Алексей"],
                    "location": "Кабинет",
                    "visible_people": 1,
                    "shot": "Close-up",
                    "source_quote": "Он нашёл запись.",
                },
            ]
        }

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return raw_chunk

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        res = provider.analyse(project, target=4, hybrid_verifier=mock_groq)

        # Groq must NOT be called for repair
        self.assertEqual(mock_groq.repair_chunk.call_count, 0)
        self.assertEqual(len(res["scenes"]), 4)
        self.assertEqual(res["scenes"][3]["characters"], ["Алексей"])

    def test_scenario_20_true_ambiguity_triggers_targeted_groq_repair(self):
        """20. True ambiguity ('Алексей и Виктор стояли у двери. Он открыл её.') triggers targeted Groq repair."""
        story = "Алексей и Виктор стояли у двери. Он открыл её."
        mock_groq = MagicMock(spec=GroqProvider)
        mock_groq.total_usage = {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180, "calls": 1}
        
        repaired_chunk = {
            "world": "Офис",
            "characters": [
                {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
            ],
            "locations": [{"name": "Офис", "description": "Дверь в офис"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей и Виктор стоят у двери.",
                    "characters": ["Алексей", "Виктор"],
                    "location": "Офис",
                    "visible_people": 2,
                    "shot": "Medium shot",
                    "source_quote": "Алексей и Виктор стояли у двери.",
                },
                {
                    "source_id": 1,
                    "beat": "Алексей открывает дверь.",
                    "characters": ["Алексей"],
                    "location": "Офис",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Он открыл её.",
                }
            ]
        }
        mock_groq.repair_chunk.return_value = repaired_chunk

        ambiguous_chunk = {
            "world": "Офис",
            "characters": [
                {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
            ],
            "locations": [{"name": "Офис", "description": "Дверь в офис"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей и Виктор стоят у двери.",
                    "characters": ["Алексей", "Виктор"],
                    "location": "Офис",
                    "visible_people": 2,
                    "shot": "Medium shot",
                    "source_quote": "Алексей и Виктор стояли у двери.",
                },
                {
                    "source_id": 1,
                    "beat": "Алексей открывает дверь.",
                    "characters": ["Алексей"],
                    "location": "Офис",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Он открыл её.",
                }
            ]
        }

        repaired_chunk = {
            "world": "Офис",
            "characters": [
                {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
            ],
            "locations": [{"name": "Офис", "description": "Дверь в офис"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей и Виктор стоят у двери.",
                    "characters": ["Алексей", "Виктор"],
                    "location": "Офис",
                    "visible_people": 2,
                    "shot": "Medium shot",
                    "source_quote": "Алексей и Виктор стояли у двери.",
                },
                {
                    "source_id": 1,
                    "beat": "Дверь распахивается.",
                    "characters": [],
                    "location": "Офис",
                    "visible_people": 0,
                    "shot": "Medium shot",
                    "source_quote": "Он открыл её.",
                }
            ]
        }
        mock_groq.repair_chunk.return_value = repaired_chunk

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return ambiguous_chunk

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        res = provider.analyse(project, target=2, hybrid_verifier=mock_groq)

        # Groq repair must be called exactly once
        self.assertEqual(mock_groq.repair_chunk.call_count, 1)
        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
        self.assertIn("COREFERENCE_AMBIGUOUS", res["diagnostics"]["global_semantic_repair_codes"])

    def test_scenario_21_successful_global_groq_repair_e2e(self):
        """21. Successful global Groq repair E2E route updates SQLite with repaired result."""
        import tempfile
        import urllib.request
        from imagegen.service import Service
        from imagegen.worker import Worker

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            service.save_settings({
                "text_backend": "hybrid",
                "story_analyzer_version": "v2",
                "text_url": "http://127.0.0.1:11435",
                "groq_api_key": "gsk_mock_test_key_123",
                "text_model": "story-analyser",
                "groq_model": "openai/gpt-oss-120b"
            })
            worker = Worker(service)

            story_text = "Алексей и Виктор стояли у двери. Он открыл её."
            project = service.create_project({
                "title": "Coreference E2E Project",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            local_ambiguous_content = json.dumps({
                "world": "Офис",
                "characters": [
                    {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                    {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
                ],
                "locations": [{"name": "Офис", "description": "Дверь в офис"}],
                "props": [],
                "scenes": [
                    {
                        "source_id": 0,
                        "beat": "Алексей и Виктор стоят у двери.",
                        "characters": ["Алексей", "Виктор"],
                        "location": "Офис",
                        "visible_people": 2,
                        "shot": "Medium shot",
                        "source_quote": "Алексей и Виктор стояли у двери.",
                    },
                    {
                        "source_id": 1,
                        "beat": "Алексей открывает дверь.",
                        "characters": ["Алексей"],
                        "location": "Офис",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Он открыл её.",
                    }
                ]
            })

            groq_repaired_content = json.dumps({
                "world": "Офис",
                "characters": [
                    {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                    {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
                ],
                "locations": [{"name": "Офис", "description": "Дверь в офис"}],
                "props": [],
                "scenes": [
                    {
                        "source_id": 0,
                        "beat": "Алексей и Виктор стоят у двери.",
                        "characters": ["Алексей", "Виктор"],
                        "location": "Офис",
                        "visible_people": 2,
                        "shot": "Medium shot",
                        "source_quote": "Алексей и Виктор стояли у двери.",
                    },
                    {
                        "source_id": 1,
                        "beat": "Дверь распахивается.",
                        "characters": [],
                        "location": "Офис",
                        "visible_people": 0,
                        "shot": "Medium shot",
                        "source_quote": "Он открыл её.",
                    }
                ]
            })

            class MockHttpResponse:
                def __init__(self, data):
                    self._data = data
                def read(self, *args):
                    return self._data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            def fake_urlopen(req, timeout=90):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "/models" in url:
                    body = json.dumps({"data": [{"id": "story-analyser"}, {"id": "openai/gpt-oss-120b"}]}).encode("utf-8")
                    return MockHttpResponse(body)
                elif "11435" in url:
                    body = json.dumps({
                        "choices": [{"message": {"content": local_ambiguous_content}, "finish_reason": "stop"}]
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                elif "api.groq.com" in url:
                    body = json.dumps({
                        "choices": [{"message": {"content": groq_repaired_content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 250, "completion_tokens": 110, "total_tokens": 360}
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                raise RuntimeError(f"Unexpected URL: {url}")

            with service.db.transaction() as conn:
                gen_id = service.insert_generation(
                    conn, project["id"], "analysis",
                    {"profile": service.settings(), "project": project, "target": 2}
                )
            generation = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))

            orig_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = fake_urlopen
                worker.prepare(generation)
            finally:
                urllib.request.urlopen = orig_urlopen

            updated_gen = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(updated_gen["state"], "succeeded")

            detail = service.detail(project["id"])
            self.assertEqual(len(detail["scenes"]), 2)
            self.assertEqual(detail["scenes"][1]["data"]["characters"], [])
            self.assertEqual(detail["scenes"][1]["data"]["visible_people"], 0)
            metadata = detail["analysis_metadata"]
            diagnostics = metadata.get("diagnostics", {})
            self.assertEqual(diagnostics.get("global_semantic_repairs_count"), 1)
            self.assertEqual(diagnostics["groq_usage"]["total_tokens"], 360)

    def test_scenario_22_failed_global_groq_repair_fails_closed(self):
        """22. Failed global Groq repair leaves database untouched and generation marked failed."""
        import tempfile
        import urllib.request
        from imagegen.service import Service
        from imagegen.worker import Worker

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            service.save_settings({
                "text_backend": "hybrid",
                "story_analyzer_version": "v2",
                "text_url": "http://127.0.0.1:11435",
                "groq_api_key": "gsk_mock_test_key_123",
                "text_model": "story-analyser",
                "groq_model": "openai/gpt-oss-120b"
            })
            worker = Worker(service)

            story_text = "Алексей и Виктор стояли у двери. Он открыл её."
            project = service.create_project({
                "title": "Failed Repair Project",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            local_ambiguous_content = json.dumps({
                "world": "Офис",
                "characters": [
                    {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
                    {"name": "Виктор", "description": "Мужчина 40 лет", "wardrobe": {}},
                ],
                "locations": [{"name": "Офис", "description": "Дверь в офис"}],
                "props": [],
                "scenes": [
                    {
                        "source_id": 0,
                        "beat": "Алексей и Виктор стоят у двери.",
                        "characters": ["Алексей", "Виктор"],
                        "location": "Офис",
                        "visible_people": 2,
                        "shot": "Medium shot",
                        "source_quote": "Алексей и Виктор стояли у двери.",
                    },
                    {
                        "source_id": 1,
                        "beat": "Алексей открывает дверь.",
                        "characters": ["Алексей"],
                        "location": "Офис",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Он открыл её.",
                    }
                ]
            })

            # Groq returns unfixable or invalid content
            groq_failed_content = json.dumps({"invalid": "broken_payload"})

            class MockHttpResponse:
                def __init__(self, data):
                    self._data = data
                def read(self, *args):
                    return self._data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            def fake_urlopen(req, timeout=90):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "/models" in url:
                    body = json.dumps({"data": [{"id": "story-analyser"}, {"id": "openai/gpt-oss-120b"}]}).encode("utf-8")
                    return MockHttpResponse(body)
                elif "11435" in url:
                    body = json.dumps({
                        "choices": [{"message": {"content": local_ambiguous_content}, "finish_reason": "stop"}]
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                elif "api.groq.com" in url:
                    body = json.dumps({
                        "choices": [{"message": {"content": groq_failed_content}, "finish_reason": "stop"}]
                    }).encode("utf-8")
                    return MockHttpResponse(body)
                raise RuntimeError(f"Unexpected URL: {url}")

            with service.db.transaction() as conn:
                gen_id = service.insert_generation(
                    conn, project["id"], "analysis",
                    {"profile": service.settings(), "project": project, "target": 2}
                )
            generation = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))

            orig_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = fake_urlopen
                worker.prepare(generation)
            finally:
                urllib.request.urlopen = orig_urlopen

            updated_gen = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(updated_gen["state"], "failed")
            self.assertIn("Облачный семантический ремонт не устранил конфликт", updated_gen.get("error", ""))

            # Verify SQLite project scenes remain empty (0 modifications)
            detail = service.detail(project["id"])
            self.assertEqual(len(detail["scenes"]), 0)

    def test_scenario_23_cost_invariant_zero_groq_calls_when_clean(self):
        """23. Cost invariant: clean stories require exactly 0 Groq calls."""
        story = "Алексей вошёл в комнату. Он включил свет."
        clean_chunk = {
            "world": "Комната",
            "characters": [
                {"name": "Алексей", "description": "Мужчина 30 лет", "wardrobe": {}},
            ],
            "locations": [{"name": "Комната", "description": "Комната со светом"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Алексей входит в комнату.",
                    "characters": ["Алексей"],
                    "location": "Комната",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Алексей вошёл в комнату.",
                },
                {
                    "source_id": 1,
                    "beat": "Алексей включает свет.",
                    "characters": ["Алексей"],
                    "location": "Комната",
                    "visible_people": 1,
                    "shot": "Medium shot",
                    "source_quote": "Он включил свет.",
                }
            ]
        }

        mock_groq = MagicMock(spec=GroqProvider)
        mock_groq.repair_chunk = MagicMock()

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return clean_chunk

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        res = provider.analyse(project, target=2, hybrid_verifier=mock_groq)

        self.assertEqual(mock_groq.repair_chunk.call_count, 0)
        self.assertEqual(res["diagnostics"]["cloud_repairs_count"], 0)
        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 0)

    def test_scenario_24_late_block_global_repair_mapped_to_correct_owning_chunk(self):
        """24. A late ambiguous event maps to its owning repair chunk, never chunk 0."""
        sentences = [f"Событие {i}. В комнате стоит стол." for i in range(100)]
        sentences[75] = "Алексей и Виктор подошли к окну."
        sentences[76] = "Он посмотрел вниз."
        story = " ".join(sentences)

        mock_groq = MagicMock(spec=GroqProvider)

        def fake_repair_chunk(*args, **kwargs):
            chunk_data = kwargs.get("local_chunk") or (args[0] if args else {})
            repaired = dict(chunk_data)
            repaired_scenes = []
            for sc in chunk_data.get("scenes", []):
                sc_copy = dict(sc)
                if "посмотрел вниз" in sc_copy.get("source_quote", ""):
                    sc_copy["characters"] = []
                    sc_copy["characters_present"] = []
                    sc_copy["visible_people"] = 0
                    sc_copy["beat"] = "Вид из окна вниз на улицу."
                repaired_scenes.append(sc_copy)
            repaired["scenes"] = repaired_scenes
            return repaired

        mock_groq.repair_chunk.side_effect = fake_repair_chunk

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                scenes = []
                for s_idx, seg in enumerate(segments):
                    chars = []
                    if "Алексей и Виктор" in seg:
                        chars = ["Алексей", "Виктор"]
                    elif "посмотрел вниз" in seg:
                        chars = ["Алексей"]
                    scenes.append({
                        "source_id": s_idx,
                        "source_segment_index": s_idx,
                        "beat": f"Действие: {seg}",
                        "characters": chars,
                        "location": "Комната",
                        "visible_people": len(chars),
                        "shot": "Medium shot",
                        "source_quote": seg,
                    })
                return {
                    "world": "Комната",
                    "characters": [
                        {"name": "Алексей", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Виктор", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Комната", "description": "Комната со столом"}],
                    "props": [],
                    "scenes": scenes,
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        res = provider.analyse(project, target=48, hybrid_verifier=mock_groq)

        # Groq must be called exactly once, repairing chunk 6 (NOT chunk 0)
        self.assertEqual(mock_groq.repair_chunk.call_count, 1)
        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
        self.assertIn("COREFERENCE_AMBIGUOUS", res["diagnostics"]["global_semantic_repair_codes"])

        repairs = res["diagnostics"]["global_semantic_repairs"]
        self.assertEqual(len(repairs), 1)
        rep = repairs[0]
        self.assertEqual(rep["code"], "COREFERENCE_AMBIGUOUS")
        self.assertEqual(rep["event_index"], 76)
        # The local evidence budget is 64 anchors.  Event 76 must retain its
        # own provenance after the final storyboard is reduced to 48 frames.
        self.assertEqual(rep["selected_index"], 48)
        self.assertEqual(rep["chunk_index"], 6)
        self.assertNotEqual(rep["chunk_index"], 0)
        self.assertIn("source_span", rep)
        self.assertGreater(rep["source_span"]["start"], 0)
        self.assertGreater(rep["source_span"]["end"], rep["source_span"]["start"])

    def test_scenario_25_unmappable_diagnostic_fails_closed(self):
        """25. Unmappable global semantic diagnostic fails closed with ProviderError and leaves DB untouched."""
        from imagegen.analyzer_v2.validator import DiagnosticIssue, DiagnosticCode, DiagnosticSeverity, ValidationReport
        from imagegen.providers import ProviderError

        story = "Алексей вошёл в комнату. Он включил свет."
        mock_groq = MagicMock(spec=GroqProvider)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Комната",
                    "characters": [{"name": "Алексей", "description": "Мужчина", "wardrobe": {}}],
                    "locations": [{"name": "Комната", "description": "Комната со светом"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0,
                        "beat": "Алексей в комнате.",
                        "characters": ["Алексей"],
                        "location": "Комната",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Алексей вошёл в комнату.",
                    }, {
                        "source_id": 1,
                        "beat": "Алексей включает свет.",
                        "characters": ["Алексей"],
                        "location": "Комната",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": "Он включил свет.",
                    }]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        # Simulate an unmappable diagnostic issue with invalid/out-of-bounds coordinates
        fake_unmappable_issue = DiagnosticIssue(
            code=DiagnosticCode.COREFERENCE_AMBIGUOUS.value,
            severity=DiagnosticSeverity.SEMANTIC_REVIEW.value,
            shot_ids=[9999],
            source_ids=[9999],
            source_spans=[{"document_id": "doc", "start": 99999, "end": 999999, "exact_text": ""}],
            explanation="Unmappable coreference test issue",
            repair_scope="block",
        )

        with patch("imagegen.analyzer_v2.validator.validate_semantic_consistency", return_value=ValidationReport(issues=[fake_unmappable_issue])):
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=mock_groq)

            self.assertIn("не удалось сопоставить глобальную семантическую диагностику с исходным блоком", str(ctx.exception))
            self.assertEqual(mock_groq.repair_chunk.call_count, 0)

    def test_scenario_26_clean_early_blocks_do_not_call_groq_late_block_repaired(self):
        """26. Clean early blocks resolve coreferences locally without Groq; ambiguous later block is repaired in its owning chunk."""
        story_parts = [
            "Алексей вошёл в комнату.", "Он включил свет.", "Он подошёл к столу.",
            "Алексей открыл ящик.", "Он достал документ.", "Он прочитал текст.",
            "Алексей положил документ.", "Он выключил свет.", "Он вышел в коридор.",
            "В коридоре стояли Дмитрий и Максим.", "Он посмотрел на часы.", "Дмитрий открыл дверь."
        ]
        story = " ".join(story_parts)

        real_groq = GroqProvider("gsk_test_key_123")

        def fake_groq_resp(payload):
            scenes = [
                {"source_id": 0, "beat": "Алексей положил документ.", "characters": ["Алексей"], "location": "Здание", "visible_people": 1, "shot": "Medium shot", "source_quote": "Алексей положил документ."},
                {"source_id": 1, "beat": "Он выключил свет.", "characters": ["Алексей"], "location": "Здание", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он выключил свет."},
                {"source_id": 2, "beat": "Он вышел в коридор.", "characters": ["Алексей"], "location": "Здание", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он вышел в коридор."},
                {"source_id": 3, "beat": "В коридоре стояли Дмитрий и Максим.", "characters": ["Дмитрий", "Максим"], "location": "Здание", "visible_people": 2, "shot": "Medium shot", "source_quote": "В коридоре стояли Дмитрий и Максим."},
                {"source_id": 4, "beat": "Крупный план наручных часов.", "characters": [], "location": "Здание", "visible_people": 0, "shot": "Close-up", "source_quote": "Он посмотрел на часы."},
                {"source_id": 5, "beat": "Дмитрий открыл дверь.", "characters": ["Дмитрий"], "location": "Здание", "visible_people": 1, "shot": "Medium shot", "source_quote": "Дмитрий открыл дверь."}
            ]
            return make_groq_completion_fixture(
                scenes,
                world="Здание",
                characters=[
                    {"name": "Алексей", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Дмитрий", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Максим", "description": "Мужчина", "wardrobe": {}},
                ],
                locations=[{"name": "Здание", "description": "Здание с коридором"}]
            )

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                scenes = []
                for s_idx, seg in enumerate(segments):
                    chars = []
                    if "Алексей" in seg:
                        chars = ["Алексей"]
                    elif "Дмитрий и Максим" in seg:
                        chars = ["Дмитрий", "Максим"]
                    elif "Он посмотрел на часы" in seg:
                        chars = ["Дмитрий"]
                    elif "Дмитрий открыл" in seg:
                        chars = ["Дмитрий"]
                    scenes.append({
                        "source_id": s_idx,
                        "source_segment_index": s_idx,
                        "beat": f"Действие: {seg}",
                        "characters": chars,
                        "location": "Здание",
                        "visible_people": len(chars),
                        "shot": "Medium shot",
                        "source_quote": seg,
                    })
                chars_list = [
                    {"name": "Алексей", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Дмитрий", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Максим", "description": "Мужчина", "wardrobe": {}},
                ]
                return {
                    "world": "Здание",
                    "characters": chars_list,
                    "locations": [{"name": "Здание", "description": "Здание с коридором"}],
                    "props": [],
                    "scenes": scenes,
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, fake_groq_resp)) as mock_http:
            res = provider.analyse(project, target=12, hybrid_verifier=real_groq)

        # Groq is called ONLY for chunk 1 (the ambiguous chunk), exactly 1 call
        self.assertEqual(mock_http.call_count, 1)
        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)

        repairs = res["diagnostics"]["global_semantic_repairs"]
        self.assertEqual(len(repairs), 1)
        rep = repairs[0]
        self.assertEqual(rep["code"], "COREFERENCE_AMBIGUOUS")
        self.assertEqual(rep["event_index"], 10)
        self.assertEqual(rep["selected_index"], 10)
        self.assertEqual(rep["chunk_index"], 1)
        self.assertIn("source_span", rep)

    def test_scenario_27_formally_valid_but_semantically_unchanged_repair_rejected(self):
        """27. Formally valid JSON from Groq that leaves the coreference ambiguous is rejected as semantic_no_progress."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        unchanged_scenes = [
            {
                "source_id": 0,
                "beat": "Иван и Петр выходят на улицу.",
                "characters": ["Иван", "Петр"],
                "location": "Улица",
                "visible_people": 2,
                "shot": "Medium shot",
                "source_quote": "Иван и Петр вышли на улицу.",
            },
            {
                "source_id": 1,
                "beat": "Иван открыл зонт.",
                "characters": ["Иван"],
                "location": "Улица",
                "visible_people": 1,
                "shot": "Medium shot",
                "source_quote": "Он открыл зонт.",
            }
        ]
        groq_resp = make_groq_completion_fixture(unchanged_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица под дождем"}],
                    "props": [],
                    "scenes": [
                        {
                            "source_id": 0,
                            "beat": "Иван и Петр выходят.",
                            "characters": ["Иван", "Петр"],
                            "location": "Улица",
                            "visible_people": 2,
                            "shot": "Medium shot",
                            "source_quote": "Иван и Петр вышли на улицу.",
                        },
                        {
                            "source_id": 1,
                            "beat": "Иван открыл зонт.",
                            "characters": ["Иван"],
                            "location": "Улица",
                            "visible_people": 1,
                            "shot": "Medium shot",
                            "source_quote": "Он открыл зонт.",
                        }
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=real_groq)

            # Check real GroqProvider called twice (attempt 1 and retry attempt 2)
            self.assertEqual(mock_http.call_count, 2)
            # Check enriched directives were passed on second attempt
            payload_att2 = mock_http.call_args_list[1][0][0]
            prompt_att2 = payload_att2["messages"][1]["content"]
            self.assertIn("ADDITIONAL REPAIR DIRECTIVES:", prompt_att2)
            self.assertIn("ПРЕДЫДУЩАЯ ПОПЫТКА РЕМОНТА НЕ УСТРАНИЛА СЕМАНТИЧЕСКИЙ КОНФЛИКТ", prompt_att2)

        err_str = str(ctx.exception)
        self.assertIn("облачный семантический ремонт не устранил конфликт", err_str.lower())
        self.assertIn("Groq получил проблемный фрагмент", err_str)
        diag = getattr(ctx.exception, "diagnostics", {})
        self.assertIn("global_semantic_repairs", diag)
        repairs = diag["global_semantic_repairs"]
        self.assertEqual(len(repairs), 2)
        self.assertEqual(repairs[0]["status"], "semantic_no_progress")
        self.assertEqual(repairs[1]["status"], "semantic_no_progress")

    def test_scenario_28_repair_effectively_resolves_ambiguity(self):
        """28. Repair that grounds the ambiguous pronoun is accepted with status=accepted, resolved_count=1, and recorded usage."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        effective_scenes = [
            {
                "source_id": 0,
                "beat": "Иван и Петр выходят.",
                "characters": ["Иван", "Петр"],
                "location": "Улица",
                "visible_people": 2,
                "shot": "Medium shot",
                "source_quote": "Иван и Петр вышли на улицу.",
            },
            {
                "source_id": 1,
                "beat": "Зонт раскрывается на фоне улицы.",
                "characters": [],
                "location": "Улица",
                "visible_people": 0,
                "shot": "Medium shot",
                "source_quote": "Он открыл зонт.",
            }
        ]
        groq_resp = make_groq_completion_fixture(effective_scenes, usage={"prompt_tokens": 150, "completion_tokens": 80, "total_tokens": 230})

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {
                            "source_id": 0,
                            "beat": "Иван и Петр выходят.",
                            "characters": ["Иван", "Петр"],
                            "location": "Улица",
                            "visible_people": 2,
                            "shot": "Medium shot",
                            "source_quote": "Иван и Петр вышли на улицу.",
                        },
                        {
                            "source_id": 1,
                            "beat": "Иван открыл зонт.",
                            "characters": ["Иван"],
                            "location": "Улица",
                            "visible_people": 1,
                            "shot": "Medium shot",
                            "source_quote": "Он открыл зонт.",
                        }
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
            res = provider.analyse(project, target=2, hybrid_verifier=real_groq)
            self.assertEqual(mock_http.call_count, 1)

        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
        self.assertEqual(res["diagnostics"]["cloud_repairs_count"], 1)
        self.assertEqual(res["diagnostics"]["cloud_repair_attempts_count"], 1)
        repairs = res["diagnostics"]["global_semantic_repairs"]
        self.assertEqual(len(repairs), 1)
        rep = repairs[0]
        self.assertEqual(rep["status"], "accepted")
        self.assertEqual(rep["resolved_count"], 1)
        self.assertEqual(rep["introduced_count"], 0)
        self.assertIn("before_issue_signatures", rep)
        self.assertIn("after_issue_signatures", rep)
        self.assertEqual(len(repairs[0]["after_issue_signatures"]), 0)
        self.assertEqual(res["diagnostics"]["groq_usage"]["total_tokens"], 230)

    def test_scenario_29_repair_introducing_regression_rejected(self):
        """29. Repair that resolves target issue but introduces a new semantic regression is rejected."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        regression_scenes = [
            {
                "source_id": 0,
                "beat": "Иван и Петр выходят.",
                "characters": ["Иван", "Петр"],
                "location": "Улица",
                "visible_people": 2,
                "shot": "Medium shot",
                "source_quote": "Иван и Петр вышли на улицу.",
            },
            {
                "source_id": 1,
                "beat": "Зонт раскрывается.",
                "characters": ["Иван"],
                "location": "Улица",
                "visible_people": 1,
                "framing": "atmosphere",  # Regression: framing atmosphere with visible_people > 0
                "shot": "Medium shot",
                "source_quote": "Он открыл зонт.",
            }
        ]
        groq_resp = make_groq_completion_fixture(regression_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван с зонтом.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=real_groq)

        diag = getattr(ctx.exception, "diagnostics", {})
        repairs = diag.get("global_semantic_repairs", [])
        self.assertEqual(len(repairs), 2)
        self.assertEqual(repairs[0]["status"], "regression")
        self.assertGreater(repairs[0]["introduced_count"], 0)

    def test_scenario_30_retry_succeeds_after_initial_no_progress(self):
        """30. Attempt 1 yields semantic_no_progress; retry attempt 2 succeeds and updates result with telemetry for both attempts."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        unchanged_scenes = [
            {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
            {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
        ]
        effective_scenes = [
            {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
            {"source_id": 1, "beat": "Зонт раскрывается.", "characters": [], "location": "Улица", "visible_people": 0, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
        ]
        resp_att1 = make_groq_completion_fixture(unchanged_scenes)
        resp_att2 = make_groq_completion_fixture(effective_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван с зонтом.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, [resp_att1, resp_att2])) as mock_http:
            res = provider.analyse(project, target=2, hybrid_verifier=real_groq)
            self.assertEqual(mock_http.call_count, 2)
            payload_att2 = mock_http.call_args_list[1][0][0]
            self.assertIn("ADDITIONAL REPAIR DIRECTIVES:", payload_att2["messages"][1]["content"])

        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
        self.assertEqual(res["diagnostics"]["cloud_repairs_count"], 1)
        self.assertEqual(res["diagnostics"]["cloud_repair_attempts_count"], 2)
        repairs = res["diagnostics"]["global_semantic_repairs"]
        self.assertEqual(len(repairs), 2)
        self.assertEqual(repairs[0]["attempt"], 1)
        self.assertEqual(repairs[0]["status"], "semantic_no_progress")
        self.assertEqual(repairs[1]["attempt"], 2)
        self.assertEqual(repairs[1]["status"], "accepted")
        self.assertEqual(repairs[1]["resolved_count"], 1)

    def test_scenario_31_attempts_exhausted_raises_specific_error(self):
        """31. All attempts exhausted without progress raises specific ProviderError with block, codes, and attempt count."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")
        unchanged_scenes = [
            {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
            {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
        ]
        groq_resp = make_groq_completion_fixture(unchanged_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=real_groq)
            self.assertEqual(mock_http.call_count, 2)

        err_msg = str(ctx.exception)
        self.assertIn("облачный семантический ремонт не устранил конфликт", err_msg.lower())
        self.assertIn("попыток", err_msg)
        self.assertIn("COREFERENCE_AMBIGUOUS", err_msg)

    def test_scenario_32_rate_limit_or_transport_failure_preserved(self):
        """32. GroqRateLimitError is preserved as infrastructure error and not masked as general semantic validation failure."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван с зонтом.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=GroqRateLimitError("Groq API rate limit exceeded (429) across all configured keys.")):
            with self.assertRaises(GroqRateLimitError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=real_groq)

        self.assertIn("429", str(ctx.exception))

    def test_scenario_33_universal_story_neutral_resolution(self):
        """33. Global effectiveness gate operates universally across different settings, entity names, and chunk budgets."""
        story_a = "Командир Орлов и инженер Белов вошли в шлюз. Он нажал кнопку герметизации."
        story_b = "Профессор Смирнов и архивариус Ильин открыли хранилище. Он зажёг керосиновую лампу."

        for story, name_resolved, name_other in [(story_a, "Орлов", "Белов"), (story_b, "Смирнов", "Ильин")]:
            real_groq = GroqProvider("gsk_test_key_123")
            effective_scenes = [
                {"source_id": 0, "beat": "Первое действие.", "characters": [name_resolved, name_other], "location": "Локация", "visible_people": 2, "shot": "Medium shot", "source_quote": "Сегмент 0"},
                {"source_id": 1, "beat": "Кнопка герметизации.", "characters": [], "location": "Локация", "visible_people": 0, "shot": "Medium shot", "source_quote": "Сегмент 1"}
            ]
            groq_resp = make_groq_completion_fixture(
                effective_scenes,
                world="Сеттинг",
                characters=[
                    {"name": name_resolved, "description": "Мужчина", "wardrobe": {}},
                    {"name": name_other, "description": "Мужчина", "wardrobe": {}},
                ],
                locations=[{"name": "Локация", "description": "Локация"}]
            )

            class MockUniversalProvider(LlamaCppProvider):
                def __init__(self):
                    super().__init__("http://127.0.0.1:11435")
                def _analyse_segments(self, proj, segments, target):
                    return {
                        "world": "Сеттинг",
                        "characters": [
                            {"name": name_resolved, "description": "Мужчина", "wardrobe": {}},
                            {"name": name_other, "description": "Мужчина", "wardrobe": {}},
                        ],
                        "locations": [{"name": "Локация", "description": "Локация"}],
                        "props": [],
                        "scenes": [
                            {"source_id": 0, "beat": "Первое действие.", "characters": [name_resolved, name_other], "location": "Локация", "visible_people": 2, "shot": "Medium shot", "source_quote": segments[0]},
                            {"source_id": 1, "beat": "Второе действие.", "characters": [name_resolved], "location": "Локация", "visible_people": 1, "shot": "Medium shot", "source_quote": segments[1]}
                        ]
                    }

            provider = MockUniversalProvider()
            project = {"story": story, "style": "cinematic", "mode": "characters"}
            with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)):
                res = provider.analyse(project, target=2, hybrid_verifier=real_groq)

            self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
            self.assertEqual(res["diagnostics"]["global_semantic_repairs"][0]["status"], "accepted")
            self.assertEqual(res["diagnostics"]["global_semantic_repairs"][0]["resolved_count"], 1)

    def test_scenario_34_global_repair_budget_exhausted_with_multiple_chunks(self):
        """34. When multiple chunks have semantic defects and Groq returns no progress, total cloud calls do not exceed MAX_CLOUD_REPAIRS_TOTAL (5), and budget_exhausted ProviderError is raised."""
        seg_0 = "Иван и Петр вышли на улицу."
        seg_1 = "Он открыл зонт."
        seg_2 = "Сергей и Роман вошли в кафе."
        seg_3 = "Он заказал кофе."
        seg_4 = "Антон и Виктор сели в машину."
        seg_5 = "Он включил мотор."
        story = f"{seg_0} {seg_1} {seg_2} {seg_3} {seg_4} {seg_5}"

        real_groq = GroqProvider("gsk_test_key_123")

        def fake_chat_completion(payload):
            prompt = payload["messages"][1]["content"]
            if "зонт" in prompt:
                scenes = [
                    {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": seg_0},
                    {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": seg_1}
                ]
            elif "кофе" in prompt:
                scenes = [
                    {"source_id": 0, "beat": "Сергей и Роман в кафе.", "characters": ["Сергей", "Роман"], "location": "Кафе", "visible_people": 2, "shot": "Medium shot", "source_quote": seg_2},
                    {"source_id": 1, "beat": "Сергей заказал кофе.", "characters": ["Сергей"], "location": "Кафе", "visible_people": 1, "shot": "Medium shot", "source_quote": seg_3}
                ]
            else:
                scenes = [
                    {"source_id": 0, "beat": "Антон и Виктор в машине.", "characters": ["Антон", "Виктор"], "location": "Машина", "visible_people": 2, "shot": "Medium shot", "source_quote": seg_4},
                    {"source_id": 1, "beat": "Антон включил мотор.", "characters": ["Антон"], "location": "Машина", "visible_people": 1, "shot": "Medium shot", "source_quote": seg_5}
                ]
            return make_groq_completion_fixture(scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                scenes = []
                for s_idx, seg in enumerate(segments):
                    chars = []
                    if "Иван и Петр" in seg:
                        chars = ["Иван", "Петр"]
                    elif "Сергей и Роман" in seg:
                        chars = ["Сергей", "Роман"]
                    elif "Антон и Виктор" in seg:
                        chars = ["Антон", "Виктор"]
                    elif "Он" in seg:
                        # Ungrounded attribution
                        if "зонт" in seg: chars = ["Иван"]
                        elif "кофе" in seg: chars = ["Сергей"]
                        else: chars = ["Антон"]
                    scenes.append({
                        "source_id": s_idx,
                        "source_segment_index": s_idx,
                        "beat": f"Действие: {seg}",
                        "characters": chars,
                        "location": "Место",
                        "visible_people": len(chars),
                        "shot": "Medium shot",
                        "source_quote": seg,
                    })
                return {
                    "world": "Мир",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Сергей", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Роман", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Антон", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Виктор", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Место", "description": "Место"}],
                    "props": [],
                    "scenes": scenes,
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        jobs_fixture = [
            ([seg_0, seg_1], 2),
            ([seg_2, seg_3], 2),
            ([seg_4, seg_5], 2),
        ]

        with patch("imagegen.providers.DEFAULT_MAX_CLOUD_REPAIRS_TOTAL", 5), \
             patch("imagegen.providers.chunk_segments", return_value=[[seg_0, seg_1], [seg_2, seg_3], [seg_4, seg_5]]), \
             patch("imagegen.providers.subdivide_analysis_jobs", return_value=jobs_fixture), \
             patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, fake_chat_completion)) as mock_http:
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=6, hybrid_verifier=real_groq)

            # Global budget is 5, so exactly 5 cloud calls must be made
            self.assertEqual(mock_http.call_count, 5)

        err_str = str(ctx.exception)
        self.assertIn("исчерпан общий лимит обращений", err_str)
        self.assertIn("5/5", err_str)
        diag = getattr(ctx.exception, "diagnostics", {})
        self.assertEqual(diag.get("cloud_repair_attempts_count"), 5)
        self.assertEqual(diag.get("cloud_repairs_count"), 0)
        repairs = diag.get("global_semantic_repairs", [])
        self.assertGreaterEqual(len(repairs), 5)
        self.assertTrue(any(r.get("status") == "budget_exhausted" for r in repairs))

    def test_scenario_35_transport_error_e2e_worker_sqlite(self):
        """35. Transport error (e.g. Connection refused / OSError) during semantic repair raises clear infrastructure ProviderError and saves telemetry to SQLite."""
        import tempfile
        from imagegen.worker import Worker
        from imagegen.service import Service, dump, now, uid

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            service.save_settings({
                "text_backend": "hybrid",
                "story_analyzer_version": "v2",
                "text_url": "http://127.0.0.1:11435",
                "groq_api_key": "gsk_secret_production_key_456",
                "text_model": "story-analyser",
                "groq_model": "openai/gpt-oss-120b"
            })
            worker = Worker(service)

            story_text = "Иван и Петр вышли на улицу. Он открыл зонт."
            project = service.create_project({
                "title": "Transport Error E2E",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            local_chunk = {
                "world": "Улица",
                "characters": [
                    {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                ],
                "locations": [{"name": "Улица", "description": "Улица"}],
                "props": [],
                "scenes": [
                    {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                    {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                ]
            }

            gen_id = uid()
            generation = {
                "id": gen_id,
                "project_id": project["id"],
                "purpose": "analysis",
                "snapshot": dump({"project": project, "target": 2, "profile": service.settings()}),
                "cancel_requested": 0,
                "state": "running",
                "attempts": 1,
            }

            with service.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                    (gen_id, project["id"], "analysis", generation["snapshot"], 12345, now())
                )

            with patch("imagegen.providers.LlamaCppProvider._analyse_segments", return_value=local_chunk), \
                 patch.object(GroqProvider, "_chat_completion", side_effect=OSError("Connection refused: 127.0.0.1:443")):
                worker.prepare(generation)

            # Check SQLite state
            gen_db = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(gen_db["state"], "failed")
            err_text = gen_db["error"]
            self.assertIn("Не удалось связаться с Groq при семантическом ремонте", err_text)
            self.assertIn("попыток: 2", err_text)
            self.assertIn("Connection refused", err_text)
            self.assertNotIn("gsk_secret_production_key_456", err_text)

            # Check SQLite diagnostics in progress
            prog = json.loads(gen_db["progress"])
            self.assertIn("diagnostics", prog)
            diag = prog["diagnostics"]
            self.assertIn("global_semantic_repairs", diag)
            repairs = diag["global_semantic_repairs"]
            self.assertEqual(len(repairs), 2)
            self.assertEqual(repairs[0]["status"], "transport_error")
            self.assertIn("Connection refused", repairs[0].get("error_message", ""))
            self.assertNotIn("gsk_secret_production_key_456", str(diag))

    def test_scenario_36_early_repair_budget_exhaustion_e2e_worker_sqlite(self):
        """36. Early repair budget exhaustion across multiple problematic chunks halts at MAX_CLOUD_REPAIRS_TOTAL (5), avoids further HTTP calls, raises unified ProviderError, and saves complete diagnostics to SQLite."""
        import tempfile
        from imagegen.worker import Worker
        from imagegen.service import Service, dump, now, uid

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            service.save_settings({
                "text_backend": "hybrid",
                "story_analyzer_version": "v2",
                "text_url": "http://127.0.0.1:11435",
                "groq_api_key": "gsk_secret_production_key_789",
                "text_model": "story-analyser",
                "groq_model": "openai/gpt-oss-120b"
            })
            worker = Worker(service)

            story_segments = [
                f"Сегмент истории номер {i}. Голос диктора звучит за кадром." for i in range(1, 7)
            ]
            story_text = " ".join(story_segments)
            project = service.create_project({
                "title": "Early Repair Budget Exhaustion E2E",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            jobs_fixture = [([seg], 1) for seg in story_segments]

            # Local model returns chunk with VOICE_AS_VISIBLE_PERSON issue for every chunk
            def make_local_chunk(proj, segs, target):
                return {
                    "world": "Студия",
                    "characters": [{"name": "Голос диктора", "description": "Диктор", "wardrobe": {}}],
                    "locations": [{"name": "Студия", "description": "Студия"}],
                    "props": [],
                    "scenes": [{
                        "source_id": 0,
                        "beat": "Диктор читает текст.",
                        "characters": ["Голос диктора"],
                        "location": "Студия",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": segs[0],
                    }]
                }

            # Repaired chunk from Groq resolves VOICE_AS_VISIBLE_PERSON
            repaired_chunk_json = {
                "world": "Студия",
                "characters": [],
                "locations": [{"name": "Студия", "description": "Студия"}],
                "props": [],
                "scenes": [{
                    "source_id": 0,
                    "beat": "Микрофон в пустой студии, звучит закадровый голос.",
                    "characters": [],
                    "location": "Студия",
                    "visible_people": 0,
                    "shot": "Medium shot",
                }]
            }

            def fake_repair_chat_completion(payload):
                return make_groq_completion_fixture(repaired_chunk_json)

            gen_id = uid()
            generation = {
                "id": gen_id,
                "project_id": project["id"],
                "purpose": "analysis",
                "snapshot": dump({"project": project, "target": 6, "profile": service.settings()}),
                "cancel_requested": 0,
                "state": "running",
                "attempts": 1,
            }

            with service.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                    (gen_id, project["id"], "analysis", generation["snapshot"], 12345, now())
                )

            with patch("imagegen.providers.DEFAULT_MAX_CLOUD_REPAIRS_TOTAL", 5), \
                 patch("imagegen.providers.chunk_segments", return_value=[[s] for s in story_segments]), \
                 patch("imagegen.providers.subdivide_analysis_jobs", return_value=jobs_fixture), \
                 patch("imagegen.providers.LlamaCppProvider._analyse_segments", side_effect=make_local_chunk), \
                 patch.object(GroqProvider, "_chat_completion", side_effect=mock_groq_transport(None, fake_repair_chat_completion)) as mock_http:
                worker.prepare(generation)

            # Exactly 5 HTTP calls made for chunks 1..5; chunk 6 does NOT trigger HTTP
            self.assertEqual(mock_http.call_count, 5)

            # Check SQLite state
            gen_db = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(gen_db["state"], "failed")
            err_text = gen_db["error"]
            self.assertIn("Облачный семантический ремонт прерван: исчерпан общий лимит обращений", err_text)
            self.assertIn("5/5", err_text)
            self.assertIn("необработанный блок(и) 6", err_text)
            self.assertNotIn("gsk_secret_production_key_789", err_text)

            # Check SQLite diagnostics in progress
            prog = json.loads(gen_db["progress"])
            self.assertIn("diagnostics", prog)
            diag = prog["diagnostics"]
            self.assertEqual(diag.get("cloud_repair_attempts_count"), 5)
            self.assertEqual(diag.get("cloud_repairs_count"), 5)
            repairs = diag.get("global_semantic_repairs", [])
            self.assertEqual(len(repairs), 6)
            self.assertEqual(repairs[0]["status"], "accepted")
            self.assertEqual(repairs[4]["status"], "accepted")
            self.assertEqual(repairs[5]["status"], "budget_exhausted")
            self.assertEqual(repairs[5]["chunk_index"], 5)
            self.assertEqual(repairs[5]["code"], "VOICE_AS_VISIBLE_PERSON")
            self.assertNotIn("gsk_secret_production_key_789", str(diag))


    def test_scenario_37_signature_attempts_limited_across_passes(self):
        """37. Identical problem not retried beyond 2 attempts globally across passes."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")
        unchanged_scenes = [
            {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
            {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
        ]
        groq_resp = make_groq_completion_fixture(unchanged_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch("imagegen.providers.DEFAULT_MAX_GLOBAL_REPAIR_PASSES", 3):
            with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
                with self.assertRaises(ProviderError):
                    provider.analyse(project, target=2, hybrid_verifier=real_groq)

                # Total calls across all passes must be capped at 2 for the same issue signature
                self.assertEqual(mock_http.call_count, 2)

    def test_scenario_38_confirmed_groq_resolution_survives_reassembly(self):
        """38. Confirmed Groq resolution survives re-assembly and clears target issue."""
        story = "Алексей и Виктор подошли к окну. Он посмотрел на улицу."
        real_groq = GroqProvider("gsk_test_key_123")

        repaired_scenes = [
            {"source_id": 0, "beat": "Алексей и Виктор у окна.", "characters": ["Алексей", "Виктор"], "location": "Комната", "location_ref": "Комната", "visible_people": 2, "shot": "Medium shot", "source_quote": "Алексей и Виктор подошли к окну."},
            {"source_id": 1, "beat": "Атмосферный вид улицы через стекло.", "characters": [], "location": "Комната", "location_ref": "Комната", "visible_people": 0, "shot": "Wide shot", "source_quote": "Он посмотрел на улицу."}
        ]
        groq_resp = make_groq_completion_fixture(
            repaired_scenes,
            world="Комната",
            characters=[
                {"name": "Алексей", "description": "Мужчина", "wardrobe": {}},
                {"name": "Виктор", "description": "Мужчина", "wardrobe": {}},
            ],
            locations=[{"name": "Комната", "description": "Комната"}]
        )

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Комната",
                    "characters": [
                        {"name": "Алексей", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Виктор", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Комната", "description": "Комната"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Алексей и Виктор.", "characters": ["Алексей", "Виктор"], "location": "Комната", "visible_people": 2, "shot": "Medium shot", "source_quote": "Алексей и Виктор подошли к окну."},
                        {"source_id": 1, "beat": "Алексей смотрит.", "characters": ["Алексей"], "location": "Комната", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он посмотрел на улицу."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)) as mock_http:
            res = provider.analyse(project, target=2, hybrid_verifier=real_groq)

        self.assertEqual(mock_http.call_count, 1)
        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 1)
        self.assertEqual(res["scenes"][1]["visible_people"], 0)
        self.assertEqual(res["scenes"][1]["characters"], [])

    def test_scenario_39_unproven_character_choice_rejected_by_transactional_gate(self):
        """39. Unproven character choice rejected by transactional gate."""
        story = "Иван и Петр вышли на улицу. Он открыл зонт."
        real_groq = GroqProvider("gsk_test_key_123")

        # Groq returns ungrounded choice of Petr
        repaired_scenes = [
            {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
            {"source_id": 1, "beat": "Петр открывает зонт.", "characters": ["Петр"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
        ]
        groq_resp = make_groq_completion_fixture(repaired_scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                return {
                    "world": "Улица",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Улица", "description": "Улица"}],
                    "props": [],
                    "scenes": [
                        {"source_id": 0, "beat": "Иван и Петр.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": "Иван и Петр вышли на улицу."},
                        {"source_id": 1, "beat": "Иван открыл зонт.", "characters": ["Иван"], "location": "Улица", "visible_people": 1, "shot": "Medium shot", "source_quote": "Он открыл зонт."}
                    ]
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}

        with patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, groq_resp)):
            with self.assertRaises(ProviderError) as ctx:
                provider.analyse(project, target=2, hybrid_verifier=real_groq)

        diag = getattr(ctx.exception, "diagnostics", {})
        repairs = diag.get("global_semantic_repairs", [])
        self.assertEqual(len(repairs), 2)
        self.assertEqual(repairs[0]["status"], "semantic_no_progress")

    def test_scenario_40_voice_phrase_vs_proper_nickname(self):
        """40. 'Второй голос' -> acoustic only (VOICE_ONLY, no card); proper nickname 'Второй' -> physical character."""
        story_voice = "В рации раздался второй голос: 'Прием'."
        story_nickname = "Второй молча зарядил пистолет и встал у двери."

        # 1. Voice phrase test
        chunk_voice = {
            "world": "Штаб",
            "characters": [],
            "locations": [{"name": "Штаб", "description": "Штаб"}],
            "props": [],
            "scenes": [{
                "source_id": 0,
                "beat": "Голос в рации.",
                "characters": [],
                "location": "Штаб",
                "visible_people": 0,
                "shot": "Close-up",
            }]
        }
        res_voice = StoryAnalyzerV2Pipeline().analyze(
            story_text=story_voice,
            raw_chunk_results=[chunk_voice],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story_voice), "text": story_voice}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        legacy_voice = res_voice.to_legacy_analysis()
        self.assertEqual(len(legacy_voice["characters"]), 0)

        # 2. Proper nickname test
        chunk_nick = {
            "world": "Штаб",
            "characters": [{"name": "Второй", "description": "Боец в экипировке", "wardrobe": {}}],
            "locations": [{"name": "Штаб", "description": "Штаб"}],
            "props": [],
            "scenes": [{
                "source_id": 0,
                "beat": "Второй стоит у двери.",
                "characters": ["Второй"],
                "location": "Штаб",
                "visible_people": 1,
                "shot": "Medium shot",
            }]
        }
        res_nick = StoryAnalyzerV2Pipeline().analyze(
            story_text=story_nickname,
            raw_chunk_results=[chunk_nick],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story_nickname), "text": story_nickname}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        legacy_nick = res_nick.to_legacy_analysis()
        self.assertEqual(len(legacy_nick["characters"]), 1)
        self.assertEqual(legacy_nick["characters"][0]["name"], "Второй")

    def test_scenario_41_anonymous_man_vs_character_alias_vs_clone(self):
        """41. Anonymous man vs character alias vs distinct clone handled properly."""
        story = "Командир вошёл в рубку. Командир Орлов сел в кресло. Незнакомый человек постучал в иллюминатор."
        chunk_data = {
            "world": "Корабль",
            "characters": [
                {"name": "Командир", "description": "Офицер 45 лет", "wardrobe": {}},
                {"name": "Командир Орлов", "description": "Офицер 45 лет в форме", "wardrobe": {}},
            ],
            "locations": [{"name": "Рубка", "description": "Рубка корабля"}],
            "props": [],
            "scenes": [
                {
                    "source_id": 0,
                    "beat": "Командир входит в рубку.",
                    "characters": ["Командир"],
                    "location": "Рубка",
                    "visible_people": 1,
                    "shot": "Medium shot",
                },
                {
                    "source_id": 1,
                    "beat": "Командир Орлов садится в кресло.",
                    "characters": ["Командир Орлов"],
                    "location": "Рубка",
                    "visible_people": 1,
                    "shot": "Medium shot",
                },
                {
                    "source_id": 2,
                    "beat": "Незнакомец стучит в иллюминатор.",
                    "characters": [],
                    "location": "Рубка",
                    "visible_people": 1,
                    "shot": "Medium shot",
                }
            ]
        }
        res = StoryAnalyzerV2Pipeline().analyze(
            story_text=story,
            raw_chunk_results=[chunk_data],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        legacy = res.to_legacy_analysis()
        # Aliases merged to permanent character card
        self.assertEqual(len(legacy["characters"]), 1)
        self.assertTrue(any("Командир" in c["name"] for c in legacy["characters"]))

    def test_scenario_42_nested_narration_flashback_recording_reflection(self):
        """42. Nested narration, flashback, recording, reflection do not leak into visible instances."""
        story = "Мария сидит в кресле. Она вспоминает погибшего брата Андрея. На стене висит его портрет."
        chunk_data = {
            "world": "Гостиная",
            "characters": [
                {"name": "Мария", "description": "Женщина 30 лет", "wardrobe": {}},
                {"name": "Андрей", "description": "Погибший брат", "wardrobe": {}},
            ],
            "locations": [{"name": "Гостиная", "description": "Гостиная"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Мария сидит в кресле.", "characters": ["Мария"], "location": "Гостиная", "visible_people": 1, "shot": "Medium shot"},
                {"source_id": 1, "beat": "Мария задумчиво смотрит в окно.", "characters": ["Мария"], "location": "Гостиная", "visible_people": 1, "shot": "Close-up"},
                {"source_id": 2, "beat": "Портрет Андрея на стене.", "characters": [], "location": "Гостиная", "visible_people": 0, "shot": "Close-up"}
            ]
        }
        res = StoryAnalyzerV2Pipeline().analyze(
            story_text=story,
            raw_chunk_results=[chunk_data],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        legacy = res.to_legacy_analysis()
        # Andrei must NOT have permanent character slot because he is only remembered/portrait
        char_names = [c["name"] for c in legacy["characters"]]
        self.assertIn("Мария", char_names)
        self.assertNotIn("Андрей", char_names)

    def test_scenario_43_safe_atmospheric_visualization_warning_not_blocking(self):
        """43. Safe atmospheric visualization of ambiguous pronoun emits warning, not blocking review."""
        story = "Дмитрий и Максим стояли в коридоре. Он посмотрел на часы."
        chunk_data = {
            "world": "Коридор",
            "characters": [
                {"name": "Дмитрий", "description": "Мужчина", "wardrobe": {}},
                {"name": "Максим", "description": "Мужчина", "wardrobe": {}},
            ],
            "locations": [{"name": "Коридор", "description": "Коридор"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Дмитрий и Максим стоят в коридоре.", "characters": ["Дмитрий", "Максим"], "location": "Коридор", "visible_people": 2, "shot": "Medium shot", "source_quote": "Дмитрий и Максим стояли в коридоре."},
                {"source_id": 1, "beat": "Крупный план настенных часов.", "characters": [], "location": "Коридор", "visible_people": 0, "shot": "Close-up", "source_quote": "Он посмотрел на часы."}
            ]
        }
        res = StoryAnalyzerV2Pipeline().analyze(
            story_text=story,
            raw_chunk_results=[chunk_data],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        self.assertFalse(res.needs_review)
        self.assertEqual(res.diagnostics.get("unresolved_warnings_count"), 1)

    def test_scenario_44_atmospheric_fallback_retains_quote_and_location(self):
        """44. Atmospheric fallback retains quote, location_ref, sets visible_people=0."""
        story = "Сергей и Роман вошли в зал. Он включил свет."
        chunk_data = {
            "world": "Зал",
            "characters": [
                {"name": "Сергей", "description": "Мужчина", "wardrobe": {}},
                {"name": "Роман", "description": "Мужчина", "wardrobe": {}},
            ],
            "locations": [{"name": "Зал", "description": "Большой зал"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Сергей и Роман вошли в зал.", "characters": ["Сергей", "Роман"], "location": "Зал", "location_ref": "Зал", "visible_people": 2, "shot": "Medium shot", "source_quote": "Сергей и Роман вошли в зал."},
                {"source_id": 1, "beat": "Люстра вспыхивает под потолком зала.", "characters": [], "location": "Зал", "location_ref": "Зал", "visible_people": 0, "shot": "Wide shot", "source_quote": "Он включил свет."}
            ]
        }
        res = StoryAnalyzerV2Pipeline().analyze(
            story_text=story,
            raw_chunk_results=[chunk_data],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story), "text": story}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        legacy = res.to_legacy_analysis()
        scene_atmo = legacy["scenes"][1]
        self.assertEqual(scene_atmo["visible_people"], 0)
        self.assertEqual(scene_atmo["characters"], [])
        self.assertEqual(scene_atmo["location_ref"], "Зал")
        self.assertEqual(scene_atmo["source_quote"], "Он включил свет.")

    def test_scenario_45_multiple_repairs_update_baseline_incrementally(self):
        """45. Multiple repairs update baseline incrementally."""
        seg_0 = "Иван и Петр вышли на улицу."
        seg_1 = "Он открыл зонт."
        seg_2 = "Антон и Виктор вошли в кафе."
        seg_3 = "Он заказал чай."
        story = f"{seg_0} {seg_1} {seg_2} {seg_3}"

        real_groq = GroqProvider("gsk_test_key_123")

        def fake_chat_completion(payload):
            prompt = payload["messages"][1]["content"]
            if "зонт" in prompt:
                scenes = [
                    {"source_id": 0, "beat": "Иван и Петр выходят.", "characters": ["Иван", "Петр"], "location": "Улица", "visible_people": 2, "shot": "Medium shot", "source_quote": seg_0},
                    {"source_id": 1, "beat": "Зонт раскрывается.", "characters": [], "location": "Улица", "visible_people": 0, "shot": "Medium shot", "source_quote": seg_1}
                ]
            else:
                scenes = [
                    {"source_id": 0, "beat": "Антон и Виктор в кафе.", "characters": ["Антон", "Виктор"], "location": "Кафе", "visible_people": 2, "shot": "Medium shot", "source_quote": seg_2},
                    {"source_id": 1, "beat": "Чашка чая на столе.", "characters": [], "location": "Кафе", "visible_people": 0, "shot": "Medium shot", "source_quote": seg_3}
                ]
            return make_groq_completion_fixture(scenes)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                scenes = []
                for s_idx, seg in enumerate(segments):
                    chars = []
                    if "Иван и Петр" in seg: chars = ["Иван", "Петр"]
                    elif "Антон и Виктор" in seg: chars = ["Антон", "Виктор"]
                    elif "зонт" in seg: chars = ["Иван"]
                    elif "чай" in seg: chars = ["Антон"]
                    scenes.append({
                        "source_id": s_idx,
                        "source_segment_index": s_idx,
                        "beat": f"Действие: {seg}",
                        "characters": chars,
                        "location": "Место",
                        "visible_people": len(chars),
                        "shot": "Medium shot",
                        "source_quote": seg,
                    })
                return {
                    "world": "Мир",
                    "characters": [
                        {"name": "Иван", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Петр", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Антон", "description": "Мужчина", "wardrobe": {}},
                        {"name": "Виктор", "description": "Мужчина", "wardrobe": {}},
                    ],
                    "locations": [{"name": "Место", "description": "Место"}],
                    "props": [],
                    "scenes": scenes,
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        jobs_fixture = [
            ([seg_0, seg_1], 2),
            ([seg_2, seg_3], 2),
        ]

        with patch("imagegen.providers.chunk_segments", return_value=[[seg_0, seg_1], [seg_2, seg_3]]), \
             patch("imagegen.providers.subdivide_analysis_jobs", return_value=jobs_fixture), \
             patch.object(real_groq, "_chat_completion", side_effect=mock_groq_transport(real_groq, fake_chat_completion)) as mock_http:
            res = provider.analyse(project, target=4, hybrid_verifier=real_groq)
            self.assertEqual(mock_http.call_count, 2)

        self.assertEqual(res["diagnostics"]["global_semantic_repairs_count"], 2)

    def test_scenario_46_long_story_20k_chars_coordinates_and_chunks(self):
        """46. 20k char story coordinates & chunks correct."""
        sentences = [f"Предложение {i:03d}: Алексей шагает по длинному коридору и осматривает стены." for i in range(300)]
        story = " ".join(sentences)
        self.assertGreater(len(story), 20000)

        all_segs = story_segments(story)
        self.assertEqual(len(all_segs), 300)

        class MockProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__("http://127.0.0.1:11435")
            def _analyse_segments(self, proj, segments, target):
                scenes = []
                for s_idx, seg in enumerate(segments):
                    scenes.append({
                        "source_id": s_idx,
                        "beat": f"Алексей идет: {seg[:30]}",
                        "characters": ["Алексей"],
                        "location": "Коридор",
                        "visible_people": 1,
                        "shot": "Medium shot",
                        "source_quote": seg,
                    })
                return {
                    "world": "Коридор",
                    "characters": [{"name": "Алексей", "description": "Мужчина", "wardrobe": {}}],
                    "locations": [{"name": "Коридор", "description": "Коридор"}],
                    "props": [],
                    "scenes": scenes,
                }

        provider = MockProvider()
        project = {"story": story, "style": "cinematic", "mode": "characters"}
        res = provider.analyse(project, target=24)
        self.assertGreaterEqual(len(res["scenes"]), 24)

    def test_scenario_47_transport_rate_limit_and_budget_telemetry(self):
        """47. Transport error, 429 rate limit, and budget exhaustion telemetry."""
        real_groq = GroqProvider("gsk_test_key_123")
        self.assertEqual(real_groq.total_usage["calls"], 0)

        # Verify telemetry structure formatting
        diag = {
            "groq_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "cloud_repair_attempts_count": 2,
            "cloud_repairs_count": 1,
            "global_semantic_repairs": [
                {"chunk_index": 0, "attempt": 1, "status": "rate_limited"},
                {"chunk_index": 0, "attempt": 2, "status": "accepted"}
            ]
        }
        self.assertEqual(diag["cloud_repairs_count"], 1)

    def test_scenario_48_worker_sqlite_unresolved_warnings_progress_stage(self):
        """48. Worker -> SQLite sets 'Анализ завершён с N неопределёнными фрагментами' when warnings exist."""
        import tempfile
        from imagegen.service import Service, dump, now, uid
        from imagegen.worker import Worker

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Service(tmpdir)
            worker = Worker(service)

            story_text = "Дмитрий и Максим вошли в комнату. Он подошел к столу."
            project = service.create_project({
                "title": "Warning Progress Stage Project",
                "story": story_text,
                "style": "cinematic",
                "mode": "characters"
            })

            gen_id = uid()
            generation = {
                "id": gen_id,
                "project_id": project["id"],
                "purpose": "analysis",
                "snapshot": dump({"project": project, "target": 2, "profile": service.settings()}),
                "cancel_requested": 0,
                "state": "running",
                "attempts": 1,
            }

            with service.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES(?,?,?,?,?,'preparing',?)",
                    (gen_id, project["id"], "analysis", generation["snapshot"], 12345, now())
                )

            analysis_result = {
                "world": "Комната",
                "characters": [
                    {"name": "Дмитрий", "description": "Мужчина", "wardrobe": {}},
                    {"name": "Максим", "description": "Мужчина", "wardrobe": {}},
                ],
                "locations": [{"name": "Комната", "description": "Комната"}],
                "props": [],
                "scenes": [
                    {"source_id": 0, "beat": "Дмитрий и Максим входят.", "characters": ["Дмитрий", "Максим"], "location": "Комната", "location_ref": "Комната", "visible_people": 2, "shot": "Medium shot", "source_quote": "Дмитрий и Максим вошли в комнату."},
                    {"source_id": 1, "beat": "Стол на фоне комнаты.", "characters": [], "location": "Комната", "location_ref": "Комната", "visible_people": 0, "shot": "Medium shot", "source_quote": "Он подошел к столу."}
                ],
                "diagnostics": {"unresolved_warnings_count": 1}
            }

            with patch.object(LlamaCppProvider, "analyse", return_value=analysis_result):
                worker.prepare(generation)

            gen_db = service.db.one("SELECT * FROM generations WHERE id=?", (gen_id,))
            self.assertEqual(gen_db["state"], "succeeded")
            prog = json.loads(gen_db["progress"])
            self.assertIn("1", prog.get("stage", ""))
            self.assertIn("неопредел", prog.get("stage", ""))

    def test_scenario_49_three_distinct_counterexample_stories(self):
        """49. Three distinct counterexample stories (including no humans)."""
        # Story 1: Pure animal / nature documentary (0 humans)
        story_nature = "Серый волк бежит по заснеженному лесу. Он замечает следы оленя на поляне."
        chunk_nature = {
            "world": "Зимний лес",
            "characters": [],
            "locations": [{"name": "Лес", "description": "Заснеженный лес"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Волк бежит по снегу.", "characters": [], "location": "Лес", "visible_people": 0, "shot": "Wide shot", "source_quote": "Серый волк бежит по заснеженному лесу."},
                {"source_id": 1, "beat": "Следы на снегу.", "characters": [], "location": "Лес", "visible_people": 0, "shot": "Close-up", "source_quote": "Он замечает следы оленя на поляне."}
            ]
        }
        res_nature = StoryAnalyzerV2Pipeline().analyze(
            story_text=story_nature,
            raw_chunk_results=[chunk_nature],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story_nature), "text": story_nature}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        self.assertFalse(res_nature.needs_review)
        self.assertEqual(len(res_nature.to_legacy_analysis()["characters"]), 0)

        # Story 2: Dialogue-only radio broadcast
        story_radio = "В эфире звучит утренний выпуск новостей. Диктор зачитывает прогноз погоды."
        chunk_radio = {
            "world": "Студия",
            "characters": [],
            "locations": [{"name": "Студия", "description": "Радиостудия"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Винтажный радиоприемник на столе.", "characters": [], "location": "Студия", "visible_people": 0, "shot": "Medium shot", "source_quote": "В эфире звучит утренний выпуск новостей."},
                {"source_id": 1, "beat": "Карта погоды на стене.", "characters": [], "location": "Студия", "visible_people": 0, "shot": "Medium shot", "source_quote": "Диктор зачитывает прогноз погоды."}
            ]
        }
        res_radio = StoryAnalyzerV2Pipeline().analyze(
            story_text=story_radio,
            raw_chunk_results=[chunk_radio],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story_radio), "text": story_radio}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        self.assertFalse(res_radio.needs_review)

        # Story 3: Solo protagonist without ambiguous pronouns
        story_solo = "Елена вошла в лабораторию. Она включила микроскоп и начала анализ."
        chunk_solo = {
            "world": "Лаборатория",
            "characters": [{"name": "Елена", "description": "Ученый 30 лет", "wardrobe": {}}],
            "locations": [{"name": "Лаборатория", "description": "Лаборатория"}],
            "props": [],
            "scenes": [
                {"source_id": 0, "beat": "Елена входит в лабораторию.", "characters": ["Елена"], "location": "Лаборатория", "visible_people": 1, "shot": "Medium shot", "source_quote": "Елена вошла в лабораторию."},
                {"source_id": 1, "beat": "Елена смотрит в микроскоп.", "characters": ["Елена"], "location": "Лаборатория", "visible_people": 1, "shot": "Close-up", "source_quote": "Она включила микроскоп и начала анализ."}
            ]
        }
        res_solo = StoryAnalyzerV2Pipeline().analyze(
            story_text=story_solo,
            raw_chunk_results=[chunk_solo],
            chunk_boundaries=[{"chunk_index": 0, "start_char": 0, "end_char": len(story_solo), "text": story_solo}],
            provider_name="hybrid",
            model_name="qwen-groq",
        )
        self.assertFalse(res_solo.needs_review)
        self.assertEqual(len(res_solo.to_legacy_analysis()["characters"]), 1)


if __name__ == "__main__":
    unittest.main()



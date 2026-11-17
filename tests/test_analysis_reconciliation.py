"""
Regression and reconciliation test suite for FrameForge script analysis and scene preparation.
Verifies contract rules:
- Anna, Clara, Eliza preserved in scenes.
- Patient identified by role/initials not dropped and not duplicated.
- Unnamed patients not merged across episodes.
- Ivan at Peter's statue: Ivan = physical, Peter = depicted.
- Claudius/Seneca historical/author mentions do not create living participants.
- Scene without location reference passes preflight and compiles Flow prompt without error.
- Text boundaries and cast survive database serialization and re-reading.
"""

import sys
import unittest
import tempfile
import shutil
from pathlib import Path

_repo_dir = str(Path(__file__).resolve().parent.parent)
if _repo_dir not in sys.path:
    sys.path.insert(0, _repo_dir)

from imagegen.script_analysis.schemas import (
    Character,
    Location,
    Prop,
    Registry,
    Scene,
)
from imagegen.script_analysis.analyzer import AnalysisResult
from imagegen.script_analysis.stats import Stats
from imagegen.script_analysis.adapter import export_to_app_analysis
from imagegen.domain import (
    resolve_scene_presence_contract,
    compile_prompt,
    validate_project_preflight,
)
from imagegen.service import Service


class TestAnalysisReconciliation(unittest.TestCase):

    def test_anna_clara_eliza_preserved(self):
        """Verify that characters (Anna, Clara, Eliza) are preserved in scene character lists."""
        registry = Registry(
            title="Тест",
            logline="Тестовый логлайн",
            genre="драма",
            characters=[
                Character(id="ch_1", name="Анна", aliases=["Анна"], role="supporting", description="Женщина в платье", arc=""),
                Character(id="ch_2", name="Клара", aliases=["Клара"], role="supporting", description="Девушка в корсете", arc=""),
                Character(id="ch_3", name="Элиза", aliases=["Элиза"], role="supporting", description="Девушка у столика", arc=""),
            ],
            locations=[
                Location(id="loc_1", name="Туалетный столик Анны", int_ext="INT", description="Столик с косметикой"),
                Location(id="loc_2", name="Примерочная Клары", int_ext="INT", description="Примерочная комната"),
            ],
            props=[]
        )
        story = (
            "Первая сцена про Анну перед зеркалом. "
            "Вторая сцена: примерка платья, Клара затягивает шнуровку. "
            "Третья сцена: Элиза выбирает гребень для волос."
        )
        scenes = [
            Scene(
                index=1,
                heading="INT. ТУАЛЕТНЫЙ СТОЛИК АННЫ - ВЕЧЕР",
                location_id="loc_1",
                time_of_day="ВЕЧЕР",
                character_ids=["ch_1"],
                summary="Нанесение белил перед зеркалом",
                mood="спокойное",
                duration_sec=5
            ),
            Scene(
                index=2,
                heading="INT. ПРИМЕРОЧНАЯ КЛАРЫ - ДЕНЬ",
                location_id="loc_2",
                time_of_day="ДЕНЬ",
                character_ids=["ch_2"],
                summary="Примерка платья и шнуровка",
                mood="напряженное",
                duration_sec=5
            ),
            Scene(
                index=3,
                heading="INT. ТУАЛЕТНЫЙ СТОЛИК ЭЛИЗЫ - ДЕНЬ",
                location_id="loc_1", # intentionally points to loc_1 to test cross-character disambiguation
                time_of_day="ДЕНЬ",
                character_ids=["ch_3"],
                summary="Выбор гребня для волос",
                mood="задумчивое",
                duration_sec=5
            ),
        ]
        analysis = AnalysisResult(
            registry=registry,
            scenes=scenes,
            warnings=[],
            stats=Stats(tokens_in=100, tokens_out=100, cost_usd=0.001)
        )

        app_data = export_to_app_analysis(analysis, story_text=story)
        app_scenes = app_data["scenes"]

        self.assertEqual(len(app_scenes), 3)
        # Check characters preserved
        self.assertIn("Анна", app_scenes[0]["characters"])
        self.assertIn("Клара", app_scenes[1]["characters"])
        self.assertIn("Элиза", app_scenes[2]["characters"])

        # Check that Eliza's dressing table was decoupled from Anna's dressing table
        self.assertNotEqual(app_scenes[2]["location"], "Туалетный столик Анны")

    def test_patient_by_role_or_initials_not_dropped_and_not_duplicated(self):
        """Verify that patient identified by initials ('И. С.') or role is not dropped and not duplicated."""
        characters = [
            {"id": "c1", "name": "Зигмунд Гринбаум", "role": "supporting", "aliases": ["доктор Гринбаум"]},
            {"id": "c2", "name": "И. С.", "role": "supporting", "aliases": ["пациентка", "И. С."]},
        ]
        scene_data = {
            "summary": "Женщина приходит к врачу с жалобой на выпадение волос; в ходе расспросов выясняется недавняя покупка.",
            "characters": ["Зигмунд Гринбаум", "И. С."],
            "character_ids": ["c1", "c2"],
            "beat": "Консультация у врача",
            "analysis_authority": "deepseek-v2",
        }
        contract = resolve_scene_presence_contract(scene_data, characters)
        present_names = contract["visible_characters"]

        # Both must be present
        self.assertIn("Зигмунд Гринбаум", present_names)
        self.assertIn("И. С.", present_names)
        # No duplicate entries
        self.assertEqual(len(present_names), 2)
        self.assertEqual(len(set(present_names)), 2)

    def test_unnamed_patients_and_medical_offices_not_merged_across_episodes(self):
        """Verify that generic medical offices across distinct episodes are disambiguated."""
        registry = Registry(
            title="Тест",
            logline="Логлайн",
            genre="история",
            characters=[
                Character(id="ch_1", name="Зигмунд Гринбаум", aliases=["Гринбаум"], role="supporting", description="", arc=""),
                Character(id="ch_2", name="И. С.", aliases=["И. С."], role="supporting", description="", arc=""),
                Character(id="ch_3", name="Анна Рупперт", aliases=["Рупперт"], role="antagonist", description="", arc=""),
                Character(id="ch_4", name="Клиентка Рупперт", aliases=["клиентка"], role="supporting", description="", arc=""),
                Character(id="ch_5", name="Медсестра", aliases=["медсестра"], role="supporting", description="", arc=""),
                Character(id="ch_6", name="Стивен Лапидус", aliases=["Лапидус"], role="minor", description="", arc=""),
                Character(id="ch_7", name="Пострадавшая от «Лэш-Люр»", aliases=["женщина"], role="supporting", description="", arc=""),
            ],
            locations=[
                Location(id="loc_generic", name="Врачебный кабинет", int_ext="INT", description="Медицинский кабинет"),
            ],
            props=[]
        )
        scenes = [
            Scene(
                index=1,
                heading="INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                location_id="loc_generic",
                time_of_day="ДЕНЬ",
                character_ids=["ch_1", "ch_2"],
                summary="Пациентка И. С. приходит к доктору Гринбауму.",
                mood="",
                duration_sec=5
            ),
            Scene(
                index=2,
                heading="INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                location_id="loc_generic",
                time_of_day="ДЕНЬ",
                character_ids=["ch_3", "ch_4"],
                summary="Клиентка приходит к Анне Рупперт для консультации по мазям.",
                mood="",
                duration_sec=5
            ),
            Scene(
                index=3,
                heading="INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                location_id="loc_generic",
                time_of_day="ДЕНЬ",
                character_ids=["ch_5"],
                summary="Медсестре проводят рентгеновскую эпиляцию лица.",
                mood="",
                duration_sec=5
            ),
            Scene(
                index=4,
                heading="INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                location_id="loc_generic",
                time_of_day="ДЕНЬ",
                character_ids=["ch_6"],
                summary="Хирург Стивен Лапидус описывает клинические случаи.",
                mood="",
                duration_sec=5
            ),
            Scene(
                index=5,
                heading="INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                location_id="loc_generic",
                time_of_day="ДЕНЬ",
                character_ids=["ch_7"],
                summary="Врачи из Дейтона сообщают о повреждении роговицы после окрашивания бровей.",
                mood="",
                duration_sec=5
            ),
        ]
        story = (
            "Пациентка И. С. приходит к доктору Гринбауму на осмотр. "
            "Клиентка приходит к Анне Рупперт за косметикой. "
            "Медсестре делают рентгеновскую процедуру лица. "
            "Хирург Стивен Лапидус подводит итоги операций. "
            "Врачи из Дейтона публикуют отчёт об окрашивании бровей."
        )
        analysis = AnalysisResult(
            registry=registry,
            scenes=scenes,
            warnings=[],
            stats=Stats(tokens_in=100, tokens_out=100, cost_usd=0.001)
        )
        app_data = export_to_app_analysis(analysis, story_text=story)
        app_scenes = app_data["scenes"]

        loc_names = [s["location"] for s in app_scenes]
        # Each episode must have a distinct specific location rather than a single shared 'Врачебный кабинет'
        self.assertEqual(len(set(loc_names)), 5)
        # No source-specific surnames are built into production location rules.
        self.assertTrue(all("сцена" in name for name in loc_names))


    def test_ivan_at_peters_statue(self):
        """Verify syntactic binding for depicted vs physical participants: Ivan = physical, Peter = depicted."""
        registry = Registry(
            title="Тест",
            logline="",
            genre="",
            characters=[
                Character(id="ch_1", name="Иван", aliases=["Иван"], role="protagonist", description="", arc=""),
                Character(id="ch_2", name="Пётр", aliases=["Пётр", "Петра"], role="supporting", description="", arc=""),
            ],
            locations=[
                Location(id="loc_1", name="Дворцовая площадь", int_ext="EXT", description="Площадь"),
            ],
            props=[]
        )
        scenes = [
            Scene(
                index=1,
                heading="EXT. ДВОРЦОВАЯ ПЛОЩАДЬ - ДЕНЬ",
                location_id="loc_1",
                time_of_day="ДЕНЬ",
                character_ids=["ch_1", "ch_2"],
                summary="Иван внимательно рассматривает мраморную статую Петра.",
                mood="",
                duration_sec=5
            )
        ]
        story = "Иван внимательно рассматривает мраморную статую Петра во дворе."
        analysis = AnalysisResult(
            registry=registry,
            scenes=scenes,
            warnings=[],
            stats=Stats(tokens_in=10, tokens_out=10, cost_usd=0.001)
        )
        app_data = export_to_app_analysis(analysis, story_text=story)
        scene = app_data["scenes"][0]

        self.assertIn("Иван", scene["characters"])
        self.assertNotIn("Пётр", scene["characters"]) # Peter is depicted, not a physical character
        self.assertTrue(any("Пётр" in d for d in scene["depicted_people"]))

    def test_historical_author_citation_not_living_participant(self):
        """Verify that mentions like 'как писал Сенека' or 'при Клавдии' do not become living characters,
        while conjunctions like 'при этом' are not misinterpreted."""
        registry = Registry(
            title="Тест",
            logline="",
            genre="",
            characters=[
                Character(id="ch_1", name="Сенека", aliases=["Сенека"], role="minor", description="", arc=""),
                Character(id="ch_2", name="Клавдий", aliases=["Клавдий", "Клавдии"], role="minor", description="", arc=""),
                Character(id="ch_3", name="Купальщик", aliases=["купальщик"], role="supporting", description="", arc=""),
            ],
            locations=[
                Location(id="loc_1", name="Термы Байи", int_ext="EXT", description="Античные термы"),
            ],
            props=[]
        )
        scenes = [
            Scene(
                index=1,
                heading="EXT. ТЕРМЫ БАЙИ - ДЕНЬ",
                location_id="loc_1",
                time_of_day="ДЕНЬ",
                character_ids=["ch_1", "ch_2", "ch_3"],
                summary="Купальщик заходит в воду, при этом шумно плещется. Как писал Сенека, курорт был полон развлечений при Клавдии.",
                mood="",
                duration_sec=5
            )
        ]
        story = "Купальщик заходит в воду, при этом шумно плещется. Как писал Сенека, курорт был полон развлечений при Клавдии."
        analysis = AnalysisResult(
            registry=registry,
            scenes=scenes,
            warnings=[],
            stats=Stats(tokens_in=10, tokens_out=10, cost_usd=0.001)
        )
        app_data = export_to_app_analysis(analysis, story_text=story)
        scene = app_data["scenes"][0]

        self.assertIn("Купальщик", scene["characters"])
        self.assertNotIn("Сенека", scene["characters"])
        self.assertNotIn("Клавдий", scene["characters"])
        self.assertTrue(any("Сенека" in m for m in scene["mentioned_characters"]))
        self.assertTrue(any("Клавдий" in m for m in scene["mentioned_characters"]))

    def test_scene_without_location_reference_passes_preflight_and_compiles_prompt(self):
        """Verify that a scene with an unlocked location (or without a locked location reference)
        passes preflight without 409 error and compiles a valid prompt."""
        project = {
            "id": "test-proj",
            "title": "Test Project",
            "mode": "characters",
            "story": "Текст сценария.",
            "characters": [],
            "locations": [
                {
                    "id": "loc-1",
                    "name": "Врачебный кабинет",
                    "description": "Светлый кабинет врача XIX века",
                    "int_ext": "int",
                    "visual_description": "Старинная мебель и медицинские склянки",
                    "references": [],
                    "locked": False,
                }
            ],
            "scenes": [
                {
                    "id": "s-1",
                    "position": 1,
                    "current_revision": 1,
                    "active_generation_id": None,
                    "stale": False,
                    "data": {
                        "heading": "INT. ВРАЧЕБНЫЙ КАБИНЕТ - ДЕНЬ",
                        "beat": "Врач принимает пациента",
                        "summary": "Консультация",
                        "characters": [],
                        "character_ids": [],
                        "location": "Врачебный кабинет",
                        "location_id": "loc-1",
                        "visible_people": 0,
                        "source_quote": "Текст сценария.",
                        "source_start": 0,
                        "source_end": 15,
                    }
                }
            ],
            "props": [],
            "voice_workflow": None,
        }
        # Preflight should pass
        preflight = validate_project_preflight(project)
        self.assertTrue(preflight["ok"])
        self.assertEqual(len(preflight["errors"]), 0)

        # Compiling prompt should succeed without 409 error
        prompt, refs = compile_prompt(
            project,
            project["scenes"][0]["data"],
            characters=[],
            locations=project["locations"],
            props=[],
        )
        self.assertIsInstance(prompt, str)
        self.assertTrue(len(prompt) > 0)
        # Location description should be included in the compiled text
        self.assertIn("кабинет", prompt.casefold())

    def test_text_boundaries_and_cast_survive_db_persistence(self):
        """Verify text boundaries [start:end] and cast data survive DB persistence round-trip."""
        temp_dir = tempfile.mkdtemp()
        try:
            service = Service(Path(temp_dir))
            proj = service.create_project({"title": "Persistence Test", "story": "Текст для проверки сохранения."})
            pid = proj["id"]

            analysis = {
                "world": "Исторический мир",
                "characters": [
                    {
                        "name": "Анна",
                        "description": "Молодая женщина",
                        "role": "supporting",
                        "aliases": ["Анна"],
                        "wardrobe": {},
                    }
                ],
                "locations": [
                    {
                        "name": "Будуар",
                        "description": "Комната с туалетным столиком",
                    }
                ],
                "props": [],
                "scenes": [
                    {
                        "heading": "INT. БУДУАР - ВЕЧЕР",
                        "beat": "Анна готовится к выходу",
                        "characters": ["Анна"],
                        "location": "Будуар",
                        "source_quote": "Текст для проверки сохранения.",
                        "source_start": 0,
                        "source_end": 30,
                    }
                ]
            }

            service.import_analysis(pid, analysis, proj['version'])

            # Read back detail
            det = service.detail(pid)
            saved_scenes = det["scenes"]
            self.assertEqual(len(saved_scenes), 1)
            s0 = saved_scenes[0]["data"]

            self.assertEqual(s0["characters"], ["Анна"])
            self.assertEqual(s0["location"], "Будуар")
            self.assertEqual(s0["source_quote"], "Текст для проверки сохранения.")
            self.assertEqual(s0["source_start"], 0)
            self.assertEqual(s0["source_end"], 30)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_pinned_references_apply_only_to_assigned_scenes(self):
        """Verify that pinned reference assets are scoped only to scenes where that entity is present."""
        char_anna = {
            "id": "c_anna",
            "cast_id": "c_anna",
            "name": "Анна",
            "canonical_name": "Анна",
            "role": "supporting",
            "description": "Молодая женщина с тёмными волосами",
            "revision": 1,
            "locked": True,
            "references": {"portrait": "asset_anna_123"},
            "portrait": "asset_anna_123",
        }
        char_clara = {
            "id": "c_clara",
            "cast_id": "c_clara",
            "name": "Клара",
            "canonical_name": "Клара",
            "role": "supporting",
            "description": "Девушка в светлом платье",
            "revision": 1,
            "locked": True,
            "references": {"portrait": "asset_clara_456"},
            "portrait": "asset_clara_456",
        }
        characters = [char_anna, char_clara]
        project = {
            "id": "test-refs-proj",
            "mode": "characters",
            "story": "Анна и Клара.",
        }

        # Scene with only Anna
        scene_anna = {
            "heading": "INT. КОМНАТА АННЫ",
            "beat": "Анна сидит у окна",
            "characters": ["Анна"],
            "character_ids": ["c_anna"],
            "visible_people": 1,
            "source_quote": "Анна сидит у окна.",
        }
        prompt_anna, refs_anna = compile_prompt(project, scene_anna, characters)
        ref_ids_anna = [r.get("asset_id") for r in refs_anna if isinstance(r, dict)]

        # Anna's portrait should be present, Clara's must NOT be present
        self.assertIn("asset_anna_123", ref_ids_anna)
        self.assertNotIn("asset_clara_456", ref_ids_anna)

        # Scene with only Clara
        scene_clara = {
            "heading": "INT. КОМНАТА КЛАРЫ",
            "beat": "Клара читает книгу",
            "characters": ["Клара"],
            "character_ids": ["c_clara"],
            "visible_people": 1,
            "source_quote": "Клара читает книгу.",
        }
        prompt_clara, refs_clara = compile_prompt(project, scene_clara, characters)
        ref_ids_clara = [r.get("asset_id") for r in refs_clara if isinstance(r, dict)]

        # Clara's portrait should be present, Anna's must NOT be present
        self.assertIn("asset_clara_456", ref_ids_clara)
        self.assertNotIn("asset_anna_123", ref_ids_clara)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imagegen.analyzer_v2.models import (
    Entity, EntityType, EntityModality, SourceSpan, StoryAnalysisV2Result, Shot, ShotInstance
)
from imagegen.analyzer_v2.events import EventSpan, build_event_spans
from imagegen.analyzer_v2.coreference import (
    generate_russian_name_inflections,
    entity_is_mentioned_in_text,
    run_narrative_coreference_pass,
    ContextualCoreferenceResolver,
    DiscourseContext,
)
from imagegen.analyzer_v2.pipeline import StoryAnalyzerV2Pipeline
from imagegen.analyzer_v2.validator import validate_semantic_consistency, DiagnosticSeverity, DiagnosticCode
from imagegen.providers import GroqProvider


class TestGlobalCoreferenceResolution(unittest.TestCase):
    """Test suite covering universal morphology, discourse context, Q&A topics, and diverse story counterexamples."""

    def test_russian_name_morphology(self):
        """Verify inflection generation across Russian masculine, feminine, fleeting-vowel, and surname patterns."""
        # 1. Fleeting vowel masculine name: Павел
        pavel_forms = generate_russian_name_inflections("Павел")
        self.assertIn("павел", pavel_forms)
        self.assertIn("павла", pavel_forms)
        self.assertIn("павлу", pavel_forms)
        self.assertIn("павлом", pavel_forms)
        self.assertIn("павле", pavel_forms)

        # 2. Masculine name in -ей: Алексей, Сергей
        alexey_forms = generate_russian_name_inflections("Алексей")
        self.assertIn("алексей", alexey_forms)
        self.assertIn("алексея", alexey_forms)
        self.assertIn("алексею", alexey_forms)
        self.assertIn("алексеем", alexey_forms)
        self.assertIn("алексее", alexey_forms)

        # 3. Feminine name in -а: Вера
        vera_forms = generate_russian_name_inflections("Вера")
        self.assertIn("вера", vera_forms)
        self.assertIn("веры", vera_forms)
        self.assertIn("вере", vera_forms)
        self.assertIn("веру", vera_forms)
        self.assertIn("верой", vera_forms)

        # 4. Multi-word name with surname: Алексей Морозов
        morozov_forms = generate_russian_name_inflections("Алексей Морозов")
        self.assertIn("алексей", morozov_forms)
        self.assertIn("морозов", morozov_forms)
        self.assertIn("морозова", morozov_forms)
        self.assertIn("морозову", morozov_forms)
        self.assertIn("алексея морозова", morozov_forms)

        # 5. Text mention matching with prepositions
        pavel_entity = Entity("e_pavel", EntityType.PERSON, "Павел", aliases=["Павел"], confirmed_attributes=["мужчина"])
        self.assertTrue(entity_is_mentioned_in_text(pavel_entity, "Алексей спросил о Павле."))
        self.assertTrue(entity_is_mentioned_in_text(pavel_entity, "Виктор ничего не знал про Павла."))
        self.assertTrue(entity_is_mentioned_in_text(pavel_entity, "Насчёт Павла всё было решено."))
        self.assertFalse(entity_is_mentioned_in_text(pavel_entity, "В комнате стояла тишина."))

    def test_control_story_qa_dialogue_pavel_resolution(self):
        """Verify the exact failing scenario (Event 175-179): pronoun 'он' resolves to Pavel without Groq."""
        entities = {
            "e_alexey": Entity("e_alexey", EntityType.PERSON, "Алексей", aliases=["Алексей Морозов"], confirmed_attributes=["мужчина"]),
            "e_victor": Entity("e_victor", EntityType.PERSON, "Виктор", aliases=["Виктор"], confirmed_attributes=["мужчина"]),
            "e_pavel": Entity("e_pavel", EntityType.PERSON, "Павел", aliases=["Павел"], confirmed_attributes=["мужчина"]),
        }

        ev1 = EventSpan(175, 0, 35, "Алексей позвонил Виктору домой.", "sec_1")
        ev2 = EventSpan(176, 36, 80, "— Сколько раз звонили? — сразу спросил тот.", "sec_1")
        ev3 = EventSpan(177, 81, 150, "Алексей спросил, почему ему не рассказали о Павле. Виктор помолчал.", "sec_1")
        ev4 = EventSpan(178, 151, 196, "— Потому что он тоже видел себя на камерах.", "sec_1")
        ev5 = EventSpan(179, 197, 212, "А потом исчез.", "sec_1")

        contracts = run_narrative_coreference_pass("", [ev1, ev2, ev3, ev4, ev5], entities)
        
        # Event 177: pronoun 'ему' -> Alexey
        c_177 = next((c for c in contracts if c.event_index == 177), None)
        self.assertIsNotNone(c_177)
        self.assertEqual(c_177.referent_entity_id, "e_alexey")

        # Event 178: pronoun 'он' -> Pavel (Question topic continuation)
        c_178 = next((c for c in contracts if c.event_index == 178), None)
        self.assertIsNotNone(c_178)
        self.assertEqual(c_178.referent_entity_id, "e_pavel")
        self.assertGreaterEqual(c_178.confidence, 0.90)
        self.assertEqual(c_178.visual_policy, "MENTION_ONLY")
        self.assertIn("QUESTION_TOPIC", c_178.reason_codes)
        self.assertIn("ANSWER_CONTINUATION", c_178.reason_codes)

    def test_inanimate_object_pronouns(self):
        """Verify inanimate plural pronoun 'их' does not resolve to human entity groups."""
        entities = {
            "e_alexey": Entity("e_alexey", EntityType.PERSON, "Алексей", aliases=["Алексей"], confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 30, "Алексей достал ключи из кармана.", "sec_1")
        ev2 = EventSpan(2, 31, 60, "Он открыл их и вошёл внутрь.", "sec_1")

        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        # Event 2 has 'Он' (Alexey) and 'их' (keys)
        c_on = next((c for c in contracts if c.pronoun.lower() == "он"), None)
        c_ih = next((c for c in contracts if c.pronoun.lower() == "их"), None)

        self.assertIsNotNone(c_on)
        self.assertEqual(c_on.referent_entity_id, "e_alexey")

        self.assertIsNotNone(c_ih)
        self.assertIn("INANIMATE_OBJECT_REFERENT", c_ih.reason_codes)
        self.assertEqual(c_ih.visual_policy, "SAFE_NEUTRAL")

    def test_safe_neutral_visualization_non_blocking_warning(self):
        """Verify truly ambiguous pronoun on inanimate action generates non-blocking WARNING."""
        entities = {
            "e_andrey": Entity("e_andrey", EntityType.PERSON, "Андрей", confirmed_attributes=["мужчина"]),
            "e_sergey": Entity("e_sergey", EntityType.PERSON, "Сергей", confirmed_attributes=["мужчина"]),
        }
        story = "Андрей и Сергей вышли из кабинета. Он оставил ключ на столе в тишине."
        ev1 = EventSpan(1, 0, 34, "Андрей и Сергей вышли из кабинета.", "sec_1")
        ev2 = EventSpan(2, 35, 69, "Он оставил ключ на столе в тишине.", "sec_1")

        contracts = run_narrative_coreference_pass(story, [ev1, ev2], entities)
        c_ev2 = next((c for c in contracts if c.event_index == 2), None)
        self.assertIsNotNone(c_ev2)
        self.assertEqual(c_ev2.visual_policy, "SAFE_NEUTRAL")

        # Atmospheric neutral shot with 0 visible people
        neutral_shot = Shot(
            shot_id=2,
            event_id="ev_2",
            framing="atmosphere",
            shot_description="Крупный план ключа, лежащего на столе.",
            source_quote="Он оставил ключ на столе в тишине.",
            source_spans=[SourceSpan("doc", 35, 69, "Он оставил ключ на столе в тишине.")],
            place_id="loc_1",
            flow_prompt="Close up of an old metal key lying on wooden desk in empty office",
            visible_instances=[],
            needs_review=False
        )
        res = StoryAnalysisV2Result(
            schema_version=2,
            document_id="doc",
            story_text_hash="abc",
            world_description="",
            entities=list(entities.values()),
            places=[],
            facts=[],
            events=[],
            shots=[neutral_shot],
            pronoun_resolutions=contracts
        )
        val_rep = validate_semantic_consistency(res, [ev1, ev2], "Андрей и Сергей вышли из кабинета. Он оставил ключ на столе в тишине.")
        self.assertTrue(val_rep.is_valid)
        self.assertTrue(any(iss.code == DiagnosticCode.COREFERENCE_UNRESOLVED_SAFE.value for iss in val_rep.issues))

    # --- 12 Independent Story Counterexamples ---

    def test_case_01_detective_interrogation(self):
        """1. Detective suspect interrogation."""
        entities = {
            "e_det": Entity("e_det", EntityType.PERSON, "Следователь", confirmed_attributes=["мужчина"]),
            "e_wit": Entity("e_wit", EntityType.PERSON, "Свидетель", confirmed_attributes=["мужчина"]),
            "e_sus": Entity("e_sus", EntityType.PERSON, "Грачёв", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 40, "Следователь спросил о Грачёве.", "sec_1")
        ev2 = EventSpan(2, 41, 90, "— Потому что он соврал о своём алиби, — ответил свидетель.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_sus")
        self.assertEqual(c.visual_policy, "MENTION_ONLY")

    def test_case_02_family_story(self):
        """2. Family story (mother asks about daughter)."""
        entities = {
            "e_mom": Entity("e_mom", EntityType.PERSON, "Мать", confirmed_attributes=["женщина"]),
            "e_dtr": Entity("e_dtr", EntityType.PERSON, "Маша", confirmed_attributes=["девушка"]),
            "e_doc": Entity("e_doc", EntityType.PERSON, "Доктор", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 40, "Мать поинтересовалась насчёт Маши.", "sec_1")
        ev2 = EventSpan(2, 41, 85, "— Она уже спит в палате, — успокоил доктор.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "она")
        self.assertEqual(c.referent_entity_id, "e_dtr")

    def test_case_03_space_crew_radio(self):
        """3. Space crew radio communication."""
        entities = {
            "e_cmd": Entity("e_cmd", EntityType.PERSON, "Командир", confirmed_attributes=["мужчина"]),
            "e_eng": Entity("e_eng", EntityType.PERSON, "Инженер Соколов", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 50, "Командир спросил по рации, где Соколов.", "sec_1")
        ev2 = EventSpan(2, 51, 95, "— Он в шлюзовом отсеке проверяет давление.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_eng")

    def test_case_04_military_radio_dispatch(self):
        """4. Military radio dispatch."""
        entities = {
            "e_hq": Entity("e_hq", EntityType.PERSON, "Штаб", confirmed_attributes=["мужчина"]),
            "e_sct": Entity("e_sct", EntityType.PERSON, "Дозорный Новиков", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 45, "Штаб запросил сведения про Новикова.", "sec_1")
        ev2 = EventSpan(2, 46, 85, "— Он занял наблюдательный пункт на высоте.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_sct")

    def test_case_05_historical_chronicle(self):
        """5. Historical chronicle."""
        entities = {
            "e_king": Entity("e_king", EntityType.PERSON, "Король", confirmed_attributes=["мужчина"]),
            "e_gen": Entity("e_gen", EntityType.PERSON, "Герцог Блан", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 50, "Король долго размышлял о Блане.", "sec_1")
        ev2 = EventSpan(2, 51, 95, "Ведь он командовал сильнейшей армией запада.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_gen")

    def test_case_06_automotive_inanimate_review(self):
        """6. Automotive review with inanimate pronouns."""
        entities = {
            "e_dvr": Entity("e_dvr", EntityType.PERSON, "Максим", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 40, "Максим осмотрел новые фары машины.", "sec_1")
        ev2 = EventSpan(2, 41, 75, "Он включил их на полную мощность.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c_ih = next(c for c in contracts if c.pronoun.lower() == "их")
        self.assertEqual(c_ih.visual_policy, "SAFE_NEUTRAL")

    def test_case_07_stickman_story(self):
        """7. Stickman animation story."""
        entities = {
            "e_stk": Entity("e_stk", EntityType.PERSON, "Стикмен", confirmed_attributes=["мужчина"]),
            "e_bob": Entity("e_bob", EntityType.PERSON, "Боб", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 45, "Стикмен спросил у прохожего о Бобе.", "sec_1")
        ev2 = EventSpan(2, 46, 80, "— Он побежал в сторону башни.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_bob")

    def test_case_08_multiple_men_disambiguation(self):
        """8. Multiple men disambiguation by subject action chain."""
        entities = {
            "e_dmitry": Entity("e_dmitry", EntityType.PERSON, "Дмитрий", confirmed_attributes=["мужчина"]),
            "e_igor": Entity("e_igor", EntityType.PERSON, "Игорь", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 30, "Дмитрий стоял у входа в зал.", "sec_1")
        ev2 = EventSpan(2, 31, 70, "Игорь быстро подошёл к окну.", "sec_1", has_action=True, has_movement=True)
        ev3 = EventSpan(3, 71, 105, "Он распахнул деревянные створки.", "sec_1", has_action=True)
        contracts = run_narrative_coreference_pass("", [ev1, ev2, ev3], entities)
        c = next(c for c in contracts if c.event_index == 3 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_igor")

    def test_case_09_first_person_narrative(self):
        """9. First-person narrative."""
        entities = {
            "e_prof": Entity("e_prof", EntityType.PERSON, "Профессор Ильин", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 45, "Я спросил коллегу о профессоре Ильине.", "sec_1")
        ev2 = EventSpan(2, 46, 85, "— Он уехал на международный симпозиум.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_prof")

    def test_case_10_nested_quote_witness(self):
        """10. Nested quote / hearsay."""
        entities = {
            "e_anna": Entity("e_anna", EntityType.PERSON, "Анна", confirmed_attributes=["женщина"]),
            "e_elena": Entity("e_elena", EntityType.PERSON, "Елена", confirmed_attributes=["женщина"]),
        }
        ev1 = EventSpan(1, 0, 40, "Анна вспомнила разговор о Елене.", "sec_1")
        ev2 = EventSpan(2, 41, 80, "Ведь она предупреждала об опасности.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "она")
        self.assertEqual(c.referent_entity_id, "e_elena")

    def test_case_11_flashback_memory(self):
        """11. Flashback memory of absent person."""
        entities = {
            "e_hero": Entity("e_hero", EntityType.PERSON, "Николай", confirmed_attributes=["мужчина"]),
            "e_dad": Entity("e_dad", EntityType.PERSON, "Отец", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 45, "Николай часто вспоминал об отце.", "sec_1")
        ev2 = EventSpan(2, 46, 85, "Он учил его никогда не отступать.", "sec_1")
        contracts = run_narrative_coreference_pass("", [ev1, ev2], entities)
        c_on = next(c for c in contracts if c.event_index == 2 and c.pronoun.lower() == "он")
        self.assertEqual(c_on.referent_entity_id, "e_dad")

    def test_case_12_temporal_section_break(self):
        """12. Section jump and temporal break."""
        entities = {
            "e_peter": Entity("e_peter", EntityType.PERSON, "Пётр", confirmed_attributes=["мужчина"]),
            "e_ivan": Entity("e_ivan", EntityType.PERSON, "Иван", confirmed_attributes=["мужчина"]),
        }
        ev1 = EventSpan(1, 0, 35, "Пётр уехал из города навсегда.", "sec_1")
        ev2 = EventSpan(2, 36, 80, "Спустя десять лет Иван вошёл в старый дом.", "sec_2", has_time_shift=True)
        ev3 = EventSpan(3, 81, 115, "Он зажёг свечу на пыльном столе.", "sec_2", has_action=True)
        contracts = run_narrative_coreference_pass("", [ev1, ev2, ev3], entities)
        c = next(c for c in contracts if c.event_index == 3 and c.pronoun.lower() == "он")
        self.assertEqual(c.referent_entity_id, "e_ivan")


if __name__ == "__main__":
    unittest.main()

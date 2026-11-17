"""Tests for Story Analyzer V3 Stage 4: DialogueGraph & Coreference Pass.

Verifies:
1. Control story E2E (Pavel inquiry & explanation resolution without LLM).
2. Universal name morphology across Russian case paradigms.
3. Dialogue inquiry/topic tracking and speaker attribution.
4. Inanimate vs animate pronoun disambiguation (e.g. 'открыл их' -> doors/keys, not humans).
5. Safe visual fallback on genuine ambiguity (SAFE_NEUTRAL).
6. 12 independent counterexamples spanning genres, modes, and discourse structures.
"""
import unittest
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.models import (
    CoreferenceStatus,
    EntityModalityV3,
)


class TestCoreferenceV3(unittest.TestCase):
    """Test suite for Narrative Coreference Resolution and DialogueGraph V3."""

    def setUp(self):
        self.pipeline = StoryAnalyzerV3Pipeline()

    # -------------------------------------------------------------------------
    # 1. Control Story E2E (Pavel Case from Task Specification)
    # -------------------------------------------------------------------------
    def test_control_story_pavel_coreference_and_visual_policy(self):
        """E2E verification of events 175-179:
        - 'он' and 'себя' in explanation resolve to Pavel (topic of question).
        - 'ему' resolves to Alexey (antecedent in same clause).
        - 'тот' resolves to Victor (remote interlocutor/addressee).
        - Pavel has MENTIONED modality and MENTION_ONLY visual policy.
        - Alexey and Victor are NOT physical actors of the explanation or disappearance.
        """
        text = (
            "Алексей позвонил Виктору домой.\n"
            "— Сколько раз звонили? — сразу спросил тот.\n"
            "Алексей спросил, почему ему не рассказали о Павле. Виктор помолчал.\n"
            "— Потому что он тоже видел себя на камерах.\n"
            "А потом исчез."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        # 1. Verify Entity Modalities
        alexey = next(e for e in doc.entities if "алексей" in e.canonical_name.lower())
        victor = next(e for e in doc.entities if "виктор" in e.canonical_name.lower())
        pavel = next(e for e in doc.entities if "павел" in e.canonical_name.lower())

        self.assertEqual(alexey.modality, EntityModalityV3.PHYSICAL)
        self.assertEqual(victor.modality, EntityModalityV3.PHYSICAL)
        self.assertEqual(pavel.modality, EntityModalityV3.MENTIONED)

        # 2. Verify Coreference Decisions
        coref_by_text = {}
        for cd in doc.coreferences:
            m = next(men for men in doc.mentions if men.mention_id == cd.mention_id)
            coref_by_text[m.text.lower()] = cd

        # 'тот' -> Victor
        self.assertIn("тот", coref_by_text)
        cd_tot = coref_by_text["тот"]
        self.assertIn(victor.entity_id, cd_tot.resolved_entity_ids)

        # 'ему' -> Alexey
        self.assertIn("ему", coref_by_text)
        cd_emu = coref_by_text["ему"]
        self.assertIn(alexey.entity_id, cd_emu.resolved_entity_ids)

        # 'он' -> Pavel
        self.assertIn("он", coref_by_text)
        cd_on = coref_by_text["он"]
        self.assertIn(pavel.entity_id, cd_on.resolved_entity_ids)
        self.assertEqual(cd_on.visual_policy, "MENTION_ONLY")
        self.assertIn("QUESTION_TOPIC", cd_on.reason_codes)
        self.assertIn("ANSWER_CONTINUATION", cd_on.reason_codes)

        # 'себя' -> Pavel
        self.assertIn("себя", coref_by_text)
        cd_sebya = coref_by_text["себя"]
        self.assertIn(pavel.entity_id, cd_sebya.resolved_entity_ids)

        # 3. Verify Actor Isolation: neither Alexey nor Victor are actors in the explanation event
        ev_explanation = next(ev for ev in doc.events if "Потому что он тоже" in ev.span.text)
        self.assertNotIn(alexey.entity_id, ev_explanation.actor_ids)
        self.assertNotIn(victor.entity_id, ev_explanation.actor_ids)

    # -------------------------------------------------------------------------
    # 2. Universal Name Morphology
    # -------------------------------------------------------------------------
    def test_name_morphology_declension_clustering(self):
        """Universal Russian case paradigms cluster into the same entity canonical name."""
        # Pavel across cases
        text1 = "Павел шёл по улице. Анна встретила Павла. Она подошла к Павлу. Она восхищалась Павлом. Все говорили о Павле."
        doc1 = self.pipeline.analyze_structural(text1)
        self.assertEqual(len(doc1.validate_invariants(text1)), 0)
        pavels = [e for e in doc1.entities if "павел" in e.canonical_name.lower()]
        self.assertEqual(len(pavels), 1, "All case forms of Pavel must merge into a single 'Павел' entity.")
        self.assertEqual(pavels[0].canonical_name, "Павел")

        # Nikolay across cases
        text2 = "Николай вошёл в зал. Мы заметили Николая. Я подошёл к Николаю. Мы гордились Николаем. Все помнили о Николае."
        doc2 = self.pipeline.analyze_structural(text2)
        self.assertEqual(len(doc2.validate_invariants(text2)), 0)
        nikolays = [e for e in doc2.entities if "николай" in e.canonical_name.lower()]
        self.assertEqual(len(nikolays), 1)
        self.assertEqual(nikolays[0].canonical_name, "Николай")

        # Vera across cases
        text3 = "Вера открыла окно. Иван увидел Веру. Он обратился к Вере. Он восхищался Верой. Он думал о Вере."
        doc3 = self.pipeline.analyze_structural(text3)
        self.assertEqual(len(doc3.validate_invariants(text3)), 0)
        veras = [e for e in doc3.entities if "вера" in e.canonical_name.lower()]
        self.assertEqual(len(veras), 1)
        self.assertEqual(veras[0].canonical_name, "Вера")

    # -------------------------------------------------------------------------
    # 3. Inanimate Pronoun Disambiguation
    # -------------------------------------------------------------------------
    def test_inanimate_plural_pronoun_does_not_resolve_to_humans(self):
        """'открыл их' where 'их' refers to doors/keys must not create or resolve to a human group."""
        text = "Олег подошёл к массивным дверям. Он аккуратно открыл их и вошёл внутрь."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        oleg = next(e for e in doc.entities if "олег" in e.canonical_name.lower())

        # Check 'он'
        cd_on = next(cd for cd in doc.coreferences if any(m.text.lower() == "он" for m in doc.mentions if m.mention_id == cd.mention_id))
        self.assertIn(oleg.entity_id, cd_on.resolved_entity_ids)

        # Check 'их'
        cd_ih = next(cd for cd in doc.coreferences if any(m.text.lower() == "их" for m in doc.mentions if m.mention_id == cd.mention_id))
        self.assertEqual(cd_ih.visual_policy, "SAFE_NEUTRAL")
        self.assertNotIn(oleg.entity_id, cd_ih.resolved_entity_ids)

    def test_vehicle_inanimate_pronoun_binding(self):
        """Vehicle masculine noun binds to 'он' correctly."""
        text = "BMW обогнал Audi. Он мчался на предельной скорости."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        bmw = next(e for e in doc.entities if "bmw" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if any(m.text.lower() == "он" for m in doc.mentions if m.mention_id == cd.mention_id))
        self.assertIn(bmw.entity_id, cd_on.resolved_entity_ids)

    # -------------------------------------------------------------------------
    # 4. Safe Visual Policy on Ambiguity
    # -------------------------------------------------------------------------
    def test_safe_neutral_policy_on_genuine_ambiguity(self):
        """Genuine ambiguous pronoun with non-vital object action produces SAFE_NEUTRAL."""
        text = "Ключ лежал на столе. Тот блестел в лучах утреннего солнца."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        cd_tot = doc.coreferences[0]
        self.assertEqual(cd_tot.visual_policy, "SAFE_NEUTRAL")

    # -------------------------------------------------------------------------
    # 5. The 12 Independent Genre & Discourse Counterexamples
    # -------------------------------------------------------------------------
    def test_counterexample_1_detective_interrogation(self):
        """1. Detective: Question about suspect in interrogation room."""
        text = (
            "Следователь Фомин посмотрел на свидетеля.\n"
            "— Где вы видели подозреваемого Смирнова? — спросил он.\n"
            "— Он прятался в старом ангаре, — тихо ответил свидетель."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        smirnov = next(e for e in doc.entities if "смирнов" in e.canonical_name.lower())
        # The 'он' in witness answer must refer to Smirnov
        cd_ans = next(cd for cd in doc.coreferences if "он прятался" in doc.events[cd.event_index].span.text.lower())
        self.assertIn(smirnov.entity_id, cd_ans.resolved_entity_ids)
        self.assertEqual(cd_ans.visual_policy, "MENTION_ONLY")

    def test_counterexample_2_family_discussion(self):
        """2. Family story: Parents discussing their absent son."""
        text = (
            "Отец налил чай и посмотрел на мать.\n"
            "— Что нового слышно про Мишу? — спросил он.\n"
            "— Потому что он успешно сдал все экзамены, — с улыбкой ответила она."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        misha = next(e for e in doc.entities if "миш" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if "он успешно сдал" in doc.events[cd.event_index].span.text.lower())
        self.assertIn(misha.entity_id, cd_on.resolved_entity_ids)
        self.assertEqual(cd_on.visual_policy, "MENTION_ONLY")

    def test_counterexample_3_space_crew_radio(self):
        """3. Space crew: Radio exchange about astronaut on EVA."""
        text = (
            "Командир нажал кнопку радиосвязи.\n"
            "— Центр, что происходит с Гагариным? — спросил он.\n"
            "— Потому что он благополучно вышел в открытый космос, — ответил Центр."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        gagarin = next(e for e in doc.entities if "гагарин" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if "он благополучно вышел" in doc.events[cd.event_index].span.text.lower())
        self.assertIn(gagarin.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_4_automotive_review_pronouns(self):
        """4. Automotive review with inanimate pronouns."""
        text = (
            "Внедорожник выехал на песчаный холм.\n"
            "Он легко преодолел крутой подъем.\n"
            "Водитель нажал тормоз и заглушил мотор."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        car = next(e for e in doc.entities if e.entity_type == "vehicle")
        cd_on = doc.coreferences[0]
        self.assertIn(car.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_5_military_radio_comm(self):
        """5. Military communication about scout."""
        text = (
            "Капитан подошёл к рации.\n"
            "— Где сержант Орлов? — передал он.\n"
            "— Потому что он закрепился на высоте, — доложил штаб."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        orlov = next(e for e in doc.entities if "орлов" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if "он закрепился" in doc.events[cd.event_index].span.text.lower())
        self.assertIn(orlov.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_6_historical_chronicle(self):
        """6. Historical chronicle with clear antecedent."""
        text = (
            "Император Пётр основал новую крепость.\n"
            "Он лично руководил строительством верфи.\n"
            "Затем государь вернулся в столицу."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        peter = next(e for e in doc.entities if "пётр" in e.canonical_name.lower() or "петр" in e.canonical_name.lower())
        cd_on = doc.coreferences[0]
        self.assertIn(peter.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_7_stickman_story(self):
        """7. Stickman story with simple named figures."""
        text = (
            "Красный стикмен нарисовал круг на доске.\n"
            "Он сделал шаг назад и улыбнулся."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        stickman = doc.entities[0]
        cd_on = doc.coreferences[0]
        self.assertIn(stickman.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_8_multiple_same_gender_characters(self):
        """8. Multiple men in the scene: subject continuity vs recent actor."""
        text = (
            "Дмитрий вошёл в библиотеку и увидел Максима.\n"
            "Максим читал древнюю книгу.\n"
            "Дмитрий подошёл к полке. Он взял словарь."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        dmitry = next(e for e in doc.entities if "дмитрий" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if "он взял словарь" in doc.events[cd.event_index].span.text.lower())
        self.assertIn(dmitry.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_9_first_person_narrative(self):
        """9. First-person narrative."""
        text = (
            "Я шёл по темному переулку и встретил Бориса.\n"
            "Он молча кивнул мне и пошёл дальше."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        boris = next(e for e in doc.entities if "борис" in e.canonical_name.lower())
        cd_on = doc.coreferences[0]
        self.assertIn(boris.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_10_nested_quotation(self):
        """10. Nested quotation."""
        text = (
            "Анна вспомнила слова Виктора: «Николай вернётся завтра».\n"
            "Она верила, что он обязательно придет."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        anna = next(e for e in doc.entities if "анна" in e.canonical_name.lower())
        nikolay = next(e for e in doc.entities if "николай" in e.canonical_name.lower())

        corefs = {next(m.text.lower() for m in doc.mentions if m.mention_id == cd.mention_id): cd for cd in doc.coreferences}
        self.assertIn(anna.entity_id, corefs["она"].resolved_entity_ids)
        self.assertIn(nikolay.entity_id, corefs["он"].resolved_entity_ids)

    def test_counterexample_11_flashback_memory(self):
        """11. Flashback / memory mention."""
        text = (
            "Мария сидела у окна и вспоминала об Иване.\n"
            "Он всегда дарил ей полевые цветы."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        ivan = next(e for e in doc.entities if "иван" in e.canonical_name.lower())
        cd_on = next(cd for cd in doc.coreferences if any(m.text.lower() == "он" for m in doc.mentions if m.mention_id == cd.mention_id))
        self.assertIn(ivan.entity_id, cd_on.resolved_entity_ids)

    def test_counterexample_12_section_boundary_discourse(self):
        """12. Section boundary discourse continuation."""
        text = (
            "# Глава 1\n"
            "Артём открыл лабораторию.\n"
            "Он включил главный компьютер.\n"
            "## Запуск системы\n"
            "Артём проверил индикаторы. Он нажал кнопку пуска."
        )
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        artem = next(e for e in doc.entities if "артём" in e.canonical_name.lower() or "артем" in e.canonical_name.lower())
        for cd in doc.coreferences:
            self.assertIn(artem.entity_id, cd.resolved_entity_ids)

    def test_prepositional_pronoun_forms_resolution(self):
        """Verifies resolution of Russian prepositional pronoun forms (него, неё, нему, ней) obeying Principle B."""
        text_masc = "Алексей посмотрел на Виктора. Он подошел к нему и встал возле него."
        doc_masc = self.pipeline.analyze_structural(text_masc)
        self.assertEqual(len(doc_masc.validate_invariants(text_masc)), 0)

        alexey = next(e for e in doc_masc.entities if "алексей" in e.canonical_name.lower())
        victor = next(e for e in doc_masc.entities if "виктор" in e.canonical_name.lower())

        coref_by_word_masc = {
            next(m.text.lower() for m in doc_masc.mentions if m.mention_id == cd.mention_id): cd
            for cd in doc_masc.coreferences
        }
        self.assertIn("он", coref_by_word_masc)
        self.assertIn("нему", coref_by_word_masc)
        self.assertIn("него", coref_by_word_masc)

        # Subject pronoun 'он' refers to prior subject Alexey
        self.assertEqual(coref_by_word_masc["он"].resolved_entity_ids, [alexey.entity_id])
        # Prepositional/oblique forms 'нему' and 'него' refer to Victor via Principle B (disjoint reference from local subject)
        self.assertEqual(coref_by_word_masc["нему"].resolved_entity_ids, [victor.entity_id])
        self.assertEqual(coref_by_word_masc["нему"].visual_policy, "REFERENT_CONFIRMED")
        self.assertEqual(coref_by_word_masc["него"].resolved_entity_ids, [victor.entity_id])
        self.assertEqual(coref_by_word_masc["него"].visual_policy, "REFERENT_CONFIRMED")

        text_fem = "Анна посмотрела на Марию. Она подошла к ней и посмотрела на неё."
        doc_fem = self.pipeline.analyze_structural(text_fem)
        self.assertEqual(len(doc_fem.validate_invariants(text_fem)), 0)

        anna = next(e for e in doc_fem.entities if "анна" in e.canonical_name.lower())
        maria = next(e for e in doc_fem.entities if "мария" in e.canonical_name.lower())

        coref_by_word_fem = {
            next(m.text.lower() for m in doc_fem.mentions if m.mention_id == cd.mention_id): cd
            for cd in doc_fem.coreferences
        }
        self.assertIn("она", coref_by_word_fem)
        self.assertIn("ней", coref_by_word_fem)
        self.assertIn("неё", coref_by_word_fem)

        # Subject pronoun 'она' refers to prior subject Anna
        self.assertEqual(coref_by_word_fem["она"].resolved_entity_ids, [anna.entity_id])
        # Prepositional/oblique forms 'ней' and 'неё' refer to Maria via Principle B
        self.assertEqual(coref_by_word_fem["ней"].resolved_entity_ids, [maria.entity_id])
        self.assertEqual(coref_by_word_fem["ней"].visual_policy, "REFERENT_CONFIRMED")
        self.assertEqual(coref_by_word_fem["неё"].resolved_entity_ids, [maria.entity_id])
        self.assertEqual(coref_by_word_fem["неё"].visual_policy, "REFERENT_CONFIRMED")

    def test_genuine_ambiguity_two_same_gender_candidates(self):
        """Two equal antecedents of the same gender produce UNRESOLVED_SAFE with SAFE_NEUTRAL visual policy."""
        text = "Андрей и Сергей вышли из кабинета. Он оставил ключ на столе в тишине."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        cd_on = next(
            cd for cd in doc.coreferences
            if any(m.text.lower() == "он" for m in doc.mentions if m.mention_id == cd.mention_id)
        )
        self.assertEqual(cd_on.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(cd_on.visual_policy, "SAFE_NEUTRAL")
        self.assertEqual(cd_on.resolved_entity_ids, [])
        self.assertIn("AMBIGUOUS_CANDIDATES", cd_on.reason_codes)

    def test_subject_object_competition_without_context_masculine(self):
        """Competition between masculine subject and object of previous sentence yields UNRESOLVED_SAFE / SAFE_NEUTRAL."""
        text = "Алексей посмотрел на Виктора. Он улыбнулся."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        cd_on = next(
            cd for cd in doc.coreferences
            if any(m.text.lower() == "он" for m in doc.mentions if m.mention_id == cd.mention_id)
        )
        self.assertEqual(cd_on.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(cd_on.visual_policy, "SAFE_NEUTRAL")
        self.assertEqual(cd_on.resolved_entity_ids, [])
        self.assertIn("AMBIGUOUS_CANDIDATES", cd_on.reason_codes)

    def test_subject_object_competition_without_context_feminine(self):
        """Competition between feminine subject and object of previous sentence yields UNRESOLVED_SAFE / SAFE_NEUTRAL."""
        text = "Анна посмотрела на Марию. Она улыбнулась."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        cd_ona = next(
            cd for cd in doc.coreferences
            if any(m.text.lower() == "она" for m in doc.mentions if m.mention_id == cd.mention_id)
        )
        self.assertEqual(cd_ona.status, CoreferenceStatus.UNRESOLVED_SAFE)
        self.assertEqual(cd_ona.visual_policy, "SAFE_NEUTRAL")
        self.assertEqual(cd_ona.resolved_entity_ids, [])
        self.assertIn("AMBIGUOUS_CANDIDATES", cd_ona.reason_codes)

    def test_candidate_and_rejected_invariants_fail_closed(self):
        """Verifies that validate_invariants strictly rejects non-existent candidate and rejected entity IDs."""
        text = "Алексей посмотрел на Виктора. Он подошел к нему."
        doc = self.pipeline.analyze_structural(text)
        self.assertEqual(len(doc.validate_invariants(text)), 0)

        # 1. Non-existent candidate entity ID
        doc.coreferences[0].candidate_entity_ids.append("fake_cand_999")
        errors = doc.validate_invariants(text)
        self.assertTrue(any("fake_cand_999" in e for e in errors))
        doc.coreferences[0].candidate_entity_ids.pop()

        # 2. Non-existent rejected entity ID
        doc.coreferences[0].rejected_candidates.append({
            "entity_id": "fake_rej_888",
            "canonical_name": "Fake",
            "score": 0.1
        })
        errors = doc.validate_invariants(text)
        self.assertTrue(any("fake_rej_888" in e for e in errors))


if __name__ == "__main__":
    unittest.main()

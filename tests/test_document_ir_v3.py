"""Comprehensive Unit and Invariant Tests for DocumentIR, DocumentModeClassifier, and DocumentSegmenter (Stage 2 Acceptance)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imagegen.analyzer_v3.classifier import DocumentModeClassifier
from imagegen.analyzer_v3.models import (
    AnalysisOutcome,
    CoreferenceDecision,
    CoreferenceStatus,
    DocumentIR,
    DocumentMode,
    EntityIR,
    EntityModalityV3,
    EventIR,
    FactIR,
    MentionIR,
    SectionIR,
    SourceSpan,
)
from imagegen.analyzer_v3.nlp import RussianNLPAdapter
from imagegen.analyzer_v3.segmenter import DocumentSegmenter


class TestDocumentIRStage2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nlp = RussianNLPAdapter.get_instance()
        cls.classifier = DocumentModeClassifier(cls.nlp)
        cls.segmenter = DocumentSegmenter(cls.nlp, cls.classifier)

        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "v3_reference_corpus.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            cls.corpus = data if isinstance(data, list) else data.get("documents", [])

    def test_sourcespan_invariants(self):
        """Tests that SourceSpan enforces non-negative indices and validates against raw text."""
        raw = "Катя вошла в комнату. Она увидела старинные часы."
        span = SourceSpan(start=0, end=4, text="Катя")
        self.assertTrue(span.validate_against(raw))

        bad_span = SourceSpan(start=0, end=4, text="Оля")
        self.assertFalse(bad_span.validate_against(raw))

        with self.assertRaises(ValueError):
            SourceSpan(start=-1, end=5, text="Катя")

        with self.assertRaises(ValueError):
            SourceSpan(start=10, end=5, text="Катя")

    def test_document_mode_classification_corpus(self):
        """Tests that DocumentModeClassifier correctly categorizes documents in the benchmark corpus."""
        mode_mapping = {
            "narrative": DocumentMode.NARRATIVE,
            "fact_list": DocumentMode.FACT_LIST,
            "multi_subject_review": DocumentMode.MULTI_SUBJECT_REVIEW,
            "instructional": DocumentMode.INSTRUCTIONAL,
            "mixed": DocumentMode.MIXED,
        }

        for doc in self.corpus:
            doc_id = doc["id"]
            expected_mode_str = doc.get("expected_mode") or doc.get("mode")
            expected_mode = mode_mapping[expected_mode_str]
            text = doc["text"]

            decision = self.classifier.classify(text)
            self.assertEqual(
                decision.mode,
                expected_mode,
                f"Failed mode classification for {doc_id} ('{doc['title']}'). Expected {expected_mode}, got {decision.mode}. Rationale: {decision.rationale}"
            )
            self.assertGreaterEqual(decision.confidence, 0.70)

    def test_deterministic_segmentation_invariants(self):
        """Tests that DocumentSegmenter creates valid DocumentIR with 0 coordinate drift for all corpus docs."""
        for doc in self.corpus:
            doc_id = doc["id"]
            text = doc["text"]

            doc_ir = self.segmenter.segment(text)

            # Invariant check: all spans match raw text exactly
            errors = doc_ir.validate_invariants(text)
            self.assertEqual(len(errors), 0, f"Invariant validation errors for {doc_id}: {errors}")

            # Check that events are non-empty
            self.assertGreater(len(doc_ir.events), 0, f"No events generated for {doc_id}")
            self.assertGreater(len(doc_ir.sections), 0, f"No sections generated for {doc_id}")

            # Verify fact list count recovery (solving the V2 baseline defect)
            if doc_id == "fact_list_01_30_space_facts":
                self.assertEqual(len(doc_ir.events), 30, f"Expected 30 space facts events, got {len(doc_ir.events)}")
            elif doc_id == "fact_list_02_15_deep_sea_facts":
                self.assertEqual(len(doc_ir.events), 15, f"Expected 15 deep sea facts events, got {len(doc_ir.events)}")
            elif doc_id == "instructional_01_20_steps_carpentry":
                self.assertEqual(len(doc_ir.events), 20, f"Expected 20 carpentry steps, got {len(doc_ir.events)}")
            elif doc_id == "instructional_02_20_steps_server_setup":
                self.assertEqual(len(doc_ir.events), 20, f"Expected 20 server setup steps, got {len(doc_ir.events)}")

    def test_id_determinism_and_stability(self):
        """Tests that repeated segmentation of identical text produces 100% deterministic IDs and structures."""
        sample_text = (
            "# Введение\nСолнце ярко светило над полями.\n\n"
            "# Шаги сборки\n1. Подготовьте детали корпуса.\n2. Закрутите четыре винта.\n3. Проверьте фиксацию."
        )
        doc1 = self.segmenter.segment(sample_text)
        doc2 = self.segmenter.segment(sample_text)

        self.assertEqual(doc1.story_hash, doc2.story_hash)
        self.assertEqual(doc1.mode, doc2.mode)
        self.assertEqual(len(doc1.sections), len(doc2.sections))
        self.assertEqual(len(doc1.events), len(doc2.events))

        for s1, s2 in zip(doc1.sections, doc2.sections):
            self.assertEqual(s1.section_id, s2.section_id)
            self.assertEqual(s1.span.start, s2.span.start)
            self.assertEqual(s1.span.end, s2.span.end)
            self.assertEqual(s1.span.text, s2.span.text)
            self.assertEqual(s1.mode, s2.mode)

        for e1, e2 in zip(doc1.events, doc2.events):
            self.assertEqual(e1.event_id, e2.event_id)
            self.assertEqual(e1.span.start, e2.span.start)
            self.assertEqual(e1.span.end, e2.span.end)
            self.assertEqual(e1.span.text, e2.span.text)
            self.assertEqual(e1.event_type, e2.event_type)

    def test_edge_cases_empty_crlf_emoji_no_punct(self):
        """Tests robust handling of edge cases: empty strings, CRLF, emojis, no punctuation, repeated sentences."""
        # 1. Empty string
        empty_doc = self.segmenter.segment("")
        self.assertEqual(empty_doc.validate_invariants(""), [])

        # 2. Whitespace only
        ws_text = "   \r\n\t  \n  "
        ws_doc = self.segmenter.segment(ws_text)
        self.assertEqual(ws_doc.validate_invariants(ws_text), [])

        # 3. Windows CRLF
        crlf_text = "# Инструкция\r\n1. Возьмите деталь.\r\n2. Установите в паз.\r\n3. Закрепите винтом.\r\n"
        crlf_doc = self.segmenter.segment(crlf_text)
        self.assertEqual(crlf_doc.validate_invariants(crlf_text), [])
        for ev in crlf_doc.events:
            self.assertEqual(crlf_text[ev.span.start:ev.span.end], ev.span.text)

        # 4. Unicode Emoji and astral plane characters (multi-byte / non-BMP)
        emoji_text = "1. Ракета 🚀 полетела на Марс 🪐.\n2. Спутник 🛰️ вышел на орбиту 🌍."
        emoji_doc = self.segmenter.segment(emoji_text)
        self.assertEqual(emoji_doc.validate_invariants(emoji_text), [])
        for ev in emoji_doc.events:
            self.assertEqual(emoji_text[ev.span.start:ev.span.end], ev.span.text)
            self.assertTrue(ev.span.text.startswith(("1.", "2.")))

        # 5. Repeated sentences
        rep_text = "Тьма накрыла город. Тьма накрыла город. Тьма накрыла город."
        rep_doc = self.segmenter.segment(rep_text)
        self.assertEqual(rep_doc.validate_invariants(rep_text), [])
        self.assertEqual(len(rep_doc.events), 3)
        # Ensure distinct non-overlapping spans
        for i in range(len(rep_doc.events) - 1):
            self.assertLessEqual(rep_doc.events[i].span.end, rep_doc.events[i + 1].span.start)

        # 6. Text without punctuation
        nopunct_text = "солнце встало над горизонтом птицы запели в лесу"
        nopunct_doc = self.segmenter.segment(nopunct_text)
        self.assertEqual(nopunct_doc.validate_invariants(nopunct_text), [])
        self.assertGreater(len(nopunct_doc.events), 0)

        # 7. Nested bullet points
        nested_text = "1. Главный пункт\n  - подпункт один\n  - подпункт два\n2. Второй пункт"
        nested_doc = self.segmenter.segment(nested_text)
        self.assertEqual(nested_doc.validate_invariants(nested_text), [])

    def test_text_coverage_and_non_overlapping_spans(self):
        """Tests that within every section, event spans are strictly ordered and non-overlapping."""
        for doc in self.corpus:
            text = doc["text"]
            doc_ir = self.segmenter.segment(text)

            prev_end = 0
            for ev in doc_ir.events:
                self.assertGreaterEqual(
                    ev.span.start,
                    prev_end,
                    f"Overlapping event spans in {doc['id']}: prev_end {prev_end} > start {ev.span.start} ('{ev.span.text}')"
                )
                self.assertLessEqual(ev.span.start, ev.span.end)
                self.assertEqual(text[ev.span.start:ev.span.end], ev.span.text)
                prev_end = ev.span.end

    def test_regression_additional_documents_suite(self):
        """Tests DocumentModeClassifier against an independent regression test set of 12 challenging documents."""
        regression_suite = [
            {
                "id": "regr_01_numbered_chapters_novel",
                "text": "# Глава 1. Встреча\nМихаил шёл по набережной, глядя на темные волны реки.\n# Глава 2. Тайна\nАнна ждала его у старого чугунного фонаря и тревожно оглядывалась.",
                "expected_mode": DocumentMode.NARRATIVE,
            },
            {
                "id": "regr_02_review_without_headers",
                "text": "Смартфон Альфа отличается высокой производительностью и ярким AMOLED экраном с частотой 120 Гц. По сравнению с ним модель Бета обладает более мощным аккумулятором на 6000 мАч и доступной ценой. В отличие от них, флагман Гамма предлагает титановый корпус и продвинутую оптику камеры.",
                "expected_mode": DocumentMode.MULTI_SUBJECT_REVIEW,
            },
            {
                "id": "regr_03_continuous_instruction",
                "text": "Сначала возьмите заготовку и аккуратно отшлифуйте поверхность наждачной бумагой. Затем нанесите первый слой грунтовки и дайте ему высохнуть. После этого покройте деталь лаком.",
                "expected_mode": DocumentMode.INSTRUCTIONAL,
            },
            {
                "id": "regr_04_facts_with_dashes",
                "text": "- Скорость света составляет около 300 000 км/с.\n- Гравитация Луны в шесть раз слабее земной.\n- Венера вращается в противоположную сторону.",
                "expected_mode": DocumentMode.FACT_LIST,
            },
            {
                "id": "regr_05_narrative_with_incidental_facts",
                "text": "Археолог Иван осторожно поднял древний артефакт. Этот кувшин был изготовлен в третьем веке до нашей эры из редкой глины. Иван показал находку профессору и улыбнулся.",
                "expected_mode": DocumentMode.NARRATIVE,
            },
            {
                "id": "regr_06_recipe_steps",
                "text": "1. Нарежьте овощи кубиками.\n2. Разогрейте сковороду с маслом.\n3. Обжарьте лук до золотистого цвета.\n4. Добавьте специи и тушите 15 минут.",
                "expected_mode": DocumentMode.INSTRUCTIONAL,
            },
            {
                "id": "regr_07_borderline_fact_vs_review",
                "text": "1. Золото — благородный металл желтого цвета с плотностью 19,3 г/см³.\n2. Серебро — пластичный металл с высокой электропроводностью.\n3. Платина — тугоплавкий металл, стойкий к коррозии.",
                "expected_mode": DocumentMode.FACT_LIST,
            },
            {
                "id": "regr_08_story_with_dialogue_dashes",
                "text": "— Ты пойдешь со мной? — спросил Сергей.\n— Только если ты покажешь карту, — ответила Ольга.",
                "expected_mode": DocumentMode.NARRATIVE,
            },
            {
                "id": "regr_09_mixed_theory_and_steps",
                "text": "# Теория\nЭлектрический ток представляет собой направленное движение заряженных частиц.\n# Практика\n1. Подключите источник питания.\n2. Установите вольтметр параллельно нагрузке.",
                "expected_mode": DocumentMode.MIXED,
            },
            {
                "id": "regr_10_short_single_dialogue",
                "text": "— Беги! — крикнул он.",
                "expected_mode": DocumentMode.NARRATIVE,
            },
            {
                "id": "regr_11_top_list_review",
                "text": "# Топ 3 кофемашины\n## DeLonghi Magnifica\nКомпактная модель с давлением 15 бар и ручным капучинатором.\n## Philips Series 2200\nОбладает керамическими жерновами и сенсорным дисплеем.",
                "expected_mode": DocumentMode.MULTI_SUBJECT_REVIEW,
            },
            {
                "id": "regr_12_explainer_article",
                "text": "Почему небо голубое? Дело в том, что солнечный свет рассеивается в атмосфере Земли. Таким образом, коротковолновые синие лучи рассеиваются сильнее красных, что объясняет цвет дневного неба.",
                "expected_mode": DocumentMode.EXPLAINER,
            },
        ]

        for item in regression_suite:
            doc_id = item["id"]
            text = item["text"]
            expected_mode = item["expected_mode"]

            decision = self.classifier.classify(text)
            self.assertEqual(
                decision.mode,
                expected_mode,
                f"Regression classification failure for {doc_id}. Expected {expected_mode}, got {decision.mode}. Rationale: {decision.rationale}"
            )

    def test_audit_text_coverage_all_corpus(self):
        """Tests that 100% of characters in all 20 corpus documents are accounted for with zero unsegmented gaps."""
        for doc in self.corpus:
            doc_id = doc["id"]
            text = doc["text"]
            doc_ir = self.segmenter.segment(text)

            audit = doc_ir.audit_text_coverage(text)
            self.assertTrue(
                audit["is_fully_covered"],
                f"Coverage audit failed for {doc_id}. Uncovered gaps: {audit['gaps']}"
            )
            self.assertEqual(len(audit["gaps"]), 0)
            self.assertEqual(audit["counts"]["UNCOVERED_GAP"], 0)

    def test_multi_subject_comparison_event_contract(self):
        """Tests that EventIR supports multiple subject entities across sections for comparative events."""
        comp_event = EventIR(
            event_id="ev_comp_1",
            section_id="sec_toyota",
            span=SourceSpan(start=0, end=40, text="Toyota Land Cruiser мощнее, чем BMW X5."),
            event_type="comparison",
            actor_ids=[],
            mentioned_entity_ids=["ent_toyota", "ent_bmw"],
            subject_entity_ids=["ent_toyota", "ent_bmw"],
            comparison_target_ids=["ent_bmw"],
            visualizable=True,
        )

        d = comp_event.to_dict()
        self.assertEqual(d["subject_entity_ids"], ["ent_toyota", "ent_bmw"])
        self.assertEqual(d["comparison_target_ids"], ["ent_bmw"])

        restored = EventIR.from_dict(d)
        self.assertEqual(restored.subject_entity_ids, ["ent_toyota", "ent_bmw"])
        self.assertEqual(restored.comparison_target_ids, ["ent_bmw"])

    def test_serialization_round_trip(self):
        """Tests full JSON serialization and deserialization of DocumentIR."""
        doc = self.corpus[0]
        text = doc["text"]
        doc_ir = self.segmenter.segment(text)

        doc_dict = doc_ir.to_dict()
        restored_ir = DocumentIR.from_dict(doc_dict)

        self.assertEqual(doc_ir.story_hash, restored_ir.story_hash)
        self.assertEqual(doc_ir.mode, restored_ir.mode)
        self.assertEqual(len(doc_ir.sections), len(restored_ir.sections))
        self.assertEqual(len(doc_ir.events), len(restored_ir.events))

        for orig_ev, rest_ev in zip(doc_ir.events, restored_ir.events):
            self.assertEqual(orig_ev.event_id, rest_ev.event_id)
            self.assertEqual(orig_ev.span.start, rest_ev.span.start)
            self.assertEqual(orig_ev.span.end, rest_ev.span.end)
            self.assertEqual(orig_ev.span.text, rest_ev.span.text)


if __name__ == "__main__":
    unittest.main()

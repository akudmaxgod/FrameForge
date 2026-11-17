import unittest
from imagegen.forgotten_cities.prompts import build_prompt_packet
from imagegen.forgotten_cities.grounding import enforce_source_constraints
from imagegen.forgotten_cities.topics import source_topic
from imagegen.forgotten_cities.fidelity import apply_review


class EditorialTests(unittest.TestCase):
    def packet(self, story, quote, anchors=()):
        return build_prompt_packet({'anchors': list(anchors)}, 'ev', quote, 'Город',
                                   story_text=story, source_quote=quote, source_start=story.index(quote))

    def test_textile_context_does_not_leak_eye_across_paragraphs(self):
        quote='На каждом этапе требуется труд, когда мы смотрим на готовый материал.'
        story='Искусственный глаз.\n\nЕсть вещи, связанные с тканью и прядением.\n\nВолокно нужно подготовить. Из него получить нить. '+quote
        packet=self.packet(story, quote, [{'anchor_id':'eye','anchor_type':'artifact','canonical_name':'Искусственный глаз'}])
        self.assertEqual(packet['required_artifacts'], [])
        self.assertEqual(packet['source_topic'], 'textile')
        self.assertIn('spinning thread', packet['visual_direction'])
        self.assertEqual(packet['source_binding'], 'artifact_action')

    def test_animal_burial_is_not_bound_to_human_skull(self):
        quote='Молодое животное положили в отдельную могилу вместе с сосудом.'
        packet=self.packet('Череп девочки.\n\nЭто захоронение макаки-резуса. '+quote,quote,
            [{'anchor_id':'skull','anchor_type':'artifact','canonical_name':'Череп девочки'}])
        self.assertEqual(packet['source_binding'],'buried_remains')
        self.assertEqual(packet['required_artifacts'],[])
        self.assertIn('rhesus macaque',packet['visual_direction'])
        self.assertIn('no human burial',packet['visual_direction'])

    def test_cross_sentence_vessel_preserves_one_subject(self):
        quote='Художник распределил движение между несколькими положениями тела.'
        story='Ещё ближе её делает небольшой сосуд.\n\nНа нём изображён козёл рядом с деревом. '+quote
        packet=self.packet(story,quote,[{'anchor_id':'vessel','anchor_type':'artifact','canonical_name':'Сосуд с изображениями козла'}])
        self.assertEqual(packet['required_artifacts'],['Сосуд с изображениями козла'])
        self.assertIn('five successive poses',packet['visual_direction'])

    def test_new_topic_stops_animal_and_textile_inheritance(self):
        self.assertEqual(source_topic('На поверхности обозначен зрачок.','Волокно. Искусственный глаз. На поверхности обозначен зрачок.'),'')
        self.assertEqual(source_topic('Имена исчезли.','Макака-резус. Имена исчезли.'),'')

    def test_board_count_and_eye_anatomical_scale_are_source_guards(self):
        story='Искусственный глаз. Деревянную игровую доску нашли. На доске — двадцать круглых игровых полей.'
        for name,term in [('Искусственный глаз','never palm-sized'),('Игровая доска','EXACTLY twenty')]:
            packet=self.packet(story,'Искусственный глаз.',[{'anchor_id':'a','anchor_type':'artifact','canonical_name':name}])
            packet['required_artifacts']=[name]
            guarded=enforce_source_constraints(packet,story)
            self.assertIn(term,guarded['compiled_prompt'])

    def test_cached_generic_plan_cannot_erase_resolved_textile_topic(self):
        quote='Из него получить нить.'
        packet=self.packet('Волокно нужно подготовить. '+quote,quote)
        self.assertEqual(apply_review(packet,{'status':'pass','subject_state':'context','required_artifacts':[]}),packet)

    def test_modern_chess_comparison_does_not_inherit_city_or_ancient_board(self):
        quote='Где-то легко узнать шахматы.'
        story='Игровая доска. Представьте наши настольные игры. '+quote
        packet=self.packet(story,quote,[{'anchor_id':'board','anchor_type':'artifact','canonical_name':'Игровая доска'}])
        self.assertEqual(packet['required_artifacts'],[])
        self.assertEqual(packet['source_binding'],'artifact_detail')
        guarded=enforce_source_constraints(packet,story)
        self.assertIn('modern chessboard',guarded['compiled_prompt'])
        self.assertNotIn('city or landscape is the subject',guarded['compiled_prompt'])


if __name__ == '__main__':
    unittest.main()

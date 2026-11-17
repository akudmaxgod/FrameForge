import unittest
from imagegen.voice_reference_rules import guard_plan


class ReferenceRulesTests(unittest.TestCase):
    anchors=[{'anchor_id':'anchor_city_master','canonical_name':'Город — главный вид','time_layer':'historical_reconstruction'},
        {'anchor_id':'anchor_great_bath_historical_reconstruction','canonical_name':'Бассейн — реконструкция','time_layer':'historical_reconstruction'},
        {'anchor_id':'anchor_great_bath_modern_ruins','canonical_name':'Бассейн — современные руины','time_layer':'modern_ruins'}]

    def plan(self,**changes):
        return {'subject':'Dry riverbed','visual_direction':'A dry riverbed in a rural landscape',
            'subject_state':'context','time_layer':'historical_reconstruction','location_ref':'Бассейн — реконструкция',
            'expected_people':0,'roles':[],'required_artifacts':[],'forbidden':[],'reason':'source',**changes}

    def test_river_cannot_inherit_bath_but_actual_bath_keeps_reference(self):
        self.assertEqual(guard_plan(self.plan(),'Река пересохла.','',self.anchors)['location_ref'],'')
        bath=self.plan(subject='Great Bath reservoir',visual_direction='Brick reservoir steps')
        self.assertEqual(guard_plan(bath,'К воде вели две лестницы.','',self.anchors)['location_ref'],'Бассейн — реконструкция')

    def test_capacity_today_does_not_assert_present_filled_water(self):
        result=guard_plan(self.plan(subject='Великий бассейн'),
            'Бассейн способен удерживать воду даже сегодня.','',self.anchors)
        self.assertEqual(result['time_layer'],'modern_ruins')
        self.assertEqual(result['location_ref'],'Бассейн — современные руины')
        self.assertIn('not evidence of its current water level',result['visual_direction'])

    def test_migration_cutaway_requires_adjacent_migration_context(self):
        artifact=self.plan(subject='Bronze statue',visual_direction='A closeup of the bronze object',location_ref='',required_artifacts=['Статуя'])
        quote='Аграрные технологии стали основой культуры.'
        self.assertEqual(guard_plan(artifact,quote,'Статую нашли археологи.',self.anchors),artifact)
        result=guard_plan(artifact,quote,'Жители ушли на восток, унося с собой навыки.',self.anchors)
        self.assertEqual(result['required_artifacts'],[])
        self.assertEqual(result['location_ref'],'')
        self.assertEqual(result['contextual_source_quote'],'Жители ушли на восток, унося с собой навыки.')


if __name__=='__main__':unittest.main()

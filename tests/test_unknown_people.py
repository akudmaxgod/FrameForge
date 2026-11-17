import unittest,tempfile
from unittest.mock import patch
from imagegen.domain import compile_prompt,compile_flow_prompt,people_count_unresolved
from imagegen.script_analysis.storyboard import plan

STYLE='Minimalist Marker Explainer'
def scene(quote):
    return {'analysis_authority':'deepseek-v2','population_policy':'scene_participants',
        'beat':quote,'source_quote':quote,'source_start':0,'source_end':len(quote),
        'characters':[],'character_ids':[],'visible_people':0,'raw_visible_people':0,
        'episodic_people':[],'location':'Исходная обстановка','location_ref':'','time':'',
        'shot':'medium shot','emotion':'source state','wardrobe':{},'props':[],'negative':''}

class UnknownPeopleTests(unittest.TestCase):
    def assertNoEmptyBan(self,text):
        for phrase in ('no people or body parts','Uninhabited scene','UNINHABITED SCENE','no human silhouette','no hands','empty of inhabitants','exactly 0'):
            self.assertNotIn(phrase,text)
    def test_sourceless_cast_zero_is_not_verified_absence(self):
        data=scene('Человеку трудно поднять руку.');self.assertTrue(people_count_unresolved(data))
        for mode in ('characters','dynamic','objects','illustration'):
            project={'style':STYLE,'story':data['source_quote'],'world':'','mode':mode}
            local,_=compile_prompt(project,data,[]);flow=compile_flow_prompt(project,data)
            self.assertNoEmptyBan(local);self.assertNoEmptyBan(flow)
            self.assertIn('body parts',flow);self.assertIn('Human presence has no verified exact census',flow)
    def test_medical_fragment_retains_exact_required_illness_without_empty_room_instruction(self):
        quote='Под мышкой появляется плотная болезненная припухлость.';data=scene(quote)
        flow=compile_flow_prompt({'style':STYLE,'mode':'dynamic'},data)
        self.assertIn(quote,flow);self.assertNoEmptyBan(flow)
        self.assertIn('source-required',flow)
    def test_cross_genre_source_required_people_are_not_erased(self):
        for quote in ('Рабочие несут камни.','Путник протягивает руку.','Ребёнок открывает дверь.','The patient is shivering.','A worker carries a basket.'):
            data=scene(quote);text=compile_flow_prompt({'style':STYLE,'mode':'dynamic'},data)
            self.assertNoEmptyBan(text)
    def test_explicit_object_only_policy_keeps_zero_people_contract(self):
        data=scene('На столе лежит кувшин.');data['population_policy']='object_only'
        self.assertFalse(people_count_unresolved(data))
        text=compile_flow_prompt({'style':STYLE,'mode':'objects'},data)
        self.assertIn('Uninhabited scene',text);self.assertIn('no people or body parts',text)
    def test_named_reference_and_signed_or_manual_presence_remain_authoritative(self):
        for changes in ({'characters':['Анна'],'character_ids':['a'],'visible_people':1},
                        {'population_policy':'source_reviewed_participants'},
                        {'visual_content':{'version':'story-visual-plan/1'}},
                        {'participant_identity':{'asset_id':'ref'}},
                        {'source_animals':[{'name':'собака'}]},
                        {'subject_state':'buried_remains'}):
            data={**scene('Исходный текст.'),**changes};self.assertFalse(people_count_unresolved(data))
    def test_legacy_non_deepseek_empty_scene_is_not_reinterpreted(self):
        data=scene('Пустой зал.');data.pop('analysis_authority');self.assertFalse(people_count_unresolved(data))
    def test_future_density_plans_record_uncertainty_instead_of_zero_proof(self):
        quote='Человеку трудно поднять руку. Кувшин стоит на столе.'
        result=plan({'analyzer_version':'deepseek','characters':[],'locations':[],'scenes':[scene(quote)]},quote,2)
        self.assertEqual(result['scenes'][0]['population_policy'],'source_unspecified')
        self.assertTrue(people_count_unresolved(result['scenes'][0]))
    def test_full_scene_snapshot_does_not_reject_source_person_as_empty_setting(self):
        from imagegen.service import Service
        from imagegen.db import dump,uid,now
        with tempfile.TemporaryDirectory() as temp:
            service=Service(temp);quote='Человеку трудно поднять руку.'
            p=service.create_project({'title':'Presence','story':quote,'style':STYLE,'mode':'dynamic'})
            sid=uid()
            with service.db.transaction() as db:
                db.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)',(sid,p['id'],1))
                db.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(sid,1,dump(scene(quote)),now()))
            with patch.object(service,'profile',return_value={'width':1024,'height':576}):
                snap=service.scene_snapshot(service.scene(sid),allow_unlocked=True)
            self.assertTrue(snap['people_count_unresolved']);self.assertFalse(snap['uninhabited'])
            self.assertNoEmptyBan(snap['prompt'])
            # Marker geometry cannot impose a fabricated zero census.
            from imagegen.portrait_contract import marker_scene_review
            with patch('imagegen.portrait_contract.marker_head_count',return_value=1):
                self.assertTrue(marker_scene_review(snap,object()))

if __name__=='__main__':unittest.main()

import unittest, copy, json
from imagegen.entity_identity import mentions,gender
from imagegen.script_analysis.invariants import normalize_registry
from imagegen.script_analysis.schemas import Registry
from imagegen.script_analysis.storyboard import plan
from imagegen.domain import resolve_scene_presence_contract,DomainError
from tests import test_text_storyboard

def char(name,aliases=None,**extra):return {'name':name,'canonical_name':name,'aliases':aliases or [name],'description':'','cast_id':name,'wardrobe':{},**extra}
class RootIdentityTests(unittest.TestCase):
    def test_unknown_full_name_not_short_alias(self):
        self.assertEqual(mentions('Анна Петрова вошла.',[char('Анна Иванова',['Анна'])]),[])
        self.assertEqual(mentions('Anna Petrova entered.',[char('Anna Ivanova',['Anna'])]),[])
    def test_longest_full_name_wins_over_another_short_name(self):
        chars=[char('Анна Иванова',['Анна']),char('Анна Петрова',['Анна'])]
        self.assertEqual([x[0] for x in mentions('Анна Петрова вошла.',chars)],[1])
        self.assertEqual([x[0] for x in mentions('Анне Ивановой дали книгу.',chars)],[0])
        self.assertEqual(mentions('Анна вошла.',chars),[])
    def test_shared_title_and_first_name_do_not_merge(self):
        people=[]
        for i,name in enumerate(['Иван Петров','Иван Сидоров','Борис Орлов']):
            people.append({'id':f'ch_{i+1}','name':name,'aliases':['врач','отец','командующий','Иван'],'description':'','arc':'','role':'supporting'})
        registry=Registry.model_validate({'title':'','logline':'','genre':'','characters':people,'locations':[],'props':[]})
        result,remap,warnings=normalize_registry(registry)
        self.assertEqual(len(result.characters),3)
        self.assertNotIn('отец',result.characters[0].aliases)
        self.assertEqual(len(set(remap.values())),3)
    def test_gender_uses_given_name_not_surname_or_wife(self):
        self.assertEqual(gender(char('Иван Иванова',description='Его жена Анна живёт дома.')),'masc')
        self.assertEqual(gender(char('Анна Петров',description='Её муж Иван работает врачом.')),'femn')
    def test_cross_paragraph_pronoun_and_ambiguous_referent(self):
        story='Анна вошла.\n\nОна открыла книгу.'
        source=test_text_storyboard.fixture(story)
        self.assertEqual(plan(source,story,2)['scenes'][1]['characters'],['Анна'])
        story='Анна и Мария вошли. Она открыла книгу.'
        source['characters']=[char('Анна'),char('Мария')]
        self.assertEqual(plan(source,story,2)['scenes'][1]['characters'],[])
    def test_mentions_not_physical_but_actual_death_is_action(self):
        chars=[char('Анна'),char('Борис')]
        for story in ['Анна читает письмо Бориса.','Анна вспоминает Бориса.','После смерти Бориса Анна уехала.']:
            result=plan({'characters':chars,'scenes':[]},story,1)['scenes'][0]
            self.assertNotIn('Борис',result['characters'],story)
            self.assertIn('Борис',result['mentioned_characters'],story)
        self.assertIn('Борис',plan({'characters':chars,'scenes':[]},'Борис умер.',1)['scenes'][0]['characters'])
    def test_manual_review_is_not_reinterpreted_by_name_mining(self):
        chars=[char('Иван Иванова',role='supporting'),char('Анна Петров',role='supporting')]
        scene={'characters':['Иван Иванова'],'character_ids':['Иван Иванова'],'source_quote':'Он вспомнил Анну Петров.','beat':'Иван Иванова вспоминает прошлое.','visible_people':1,'project_review':{'version':'reviewed'},'analysis_authority':'project_specific_human_review'}
        contract=resolve_scene_presence_contract(scene,chars)
        self.assertEqual(contract['visible_characters'],['Иван Иванова'])
        self.assertFalse(contract['semantic_errors'])
    def test_presence_does_not_restore_wrong_short_name_via_gender_fallback(self):
        chars=[char('Анна Иванова',['Анна'],role='supporting')]
        result=resolve_scene_presence_contract({'source_quote':'Анна Петрова вошла в зал.','beat':'Анна Петрова вошла в зал.','characters':[],'visible_people':0},chars)
        self.assertEqual(result['visible_characters'],[])
    def test_reviewed_project_rebuild_blocked_before_model_request(self):
        from tests import test_veo_animation
        fixture=test_veo_animation.TestVeoAnimationPlan();fixture.setUp()
        try:
            pid=fixture._create_sample_project(1)
            with fixture.service.db.transaction() as conn:
                conn.execute('UPDATE projects SET analysis_metadata=? WHERE id=?',(json.dumps({'project_analysis_review':{'version':'fixture'}}),pid))
            with self.assertRaises(DomainError) as error:
                fixture.app.dispatch('POST',f'/api/projects/{pid}/analyse',{'target':5,'rebuild':True})
            self.assertEqual(error.exception.status,409)
            self.assertFalse(fixture.service.db.one("SELECT id FROM generations WHERE project_id=? AND purpose='analysis'",(pid,)))
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()

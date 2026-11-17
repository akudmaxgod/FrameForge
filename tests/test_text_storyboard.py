import copy,json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from imagegen.script_analysis.storyboard import plan
from imagegen.script_analysis.adapter import export_to_app_analysis
from imagegen.script_analysis.analyzer import AnalysisResult
from imagegen.script_analysis.schemas import Registry,Scene
from imagegen.source_contract import coverage

def fixture(story):
    return {'analyzer_version':'deepseek','characters':[
        {'cast_id':'a','name':'Анна','aliases':['Анна'],'description':'Анна.','wardrobe':{'default':'Пальто'}},
        {'cast_id':'b','name':'Борис','aliases':['Борис'],'description':'Борис.','wardrobe':{'default':'Куртка'}}],
        'locations':[],'props':[],'world':'', 'scenes':[
            {'beat':'Wrong coarse summary','source_start':0,'source_end':len(story),'source_quote':story,
             'characters':['Анна','Борис'],'location':'Wrong era/place','duration_sec':10}]}

class TextStoryboardTests(unittest.TestCase):
    def check(self,story,result,count):
        frames=result['scenes'];self.assertEqual(len(frames),count)
        self.assertTrue(coverage(story,frames)['ok'])
        self.assertEqual(len({(s['source_start'],s['source_end']) for s in frames}),count)
        self.assertTrue(all(story[s['source_start']:s['source_end']]==s['source_quote'] for s in frames))
    def test_long_story_requested_hundreds_are_not_coarse_event_count(self):
        story='\n\n'.join(f'В зале номер {n} Анна открывает старую книгу. Она изучает страницы, затем закрывает книгу и уходит.' for n in range(120))
        self.check(story,plan(fixture(story),story,469),469)
    def test_target_changes_reuse_structural_analysis_without_extra_model_calls(self):
        story='Анна идёт к дому. Она открывает дверь. Борис читает книгу. Он закрывает книгу.'
        coarse=fixture(story);before=copy.deepcopy(coarse)
        two=plan(coarse,story,2);seven=plan(two,story,7)
        self.check(story,two,2);self.check(story,seven,7)
        self.assertEqual(coarse,before);self.assertEqual(seven['diagnostics']['storyboard']['semantic_episodes_count'],1)
        self.assertEqual(seven['diagnostics']['storyboard']['additional_model_requests'],0)
    def test_no_duplicate_or_empty_padding_for_impossible_request(self):
        story='Анна спит.';result=plan(fixture(story),story,2000)
        self.check(story,result,2)
        self.assertEqual(result['diagnostics']['storyboard']['requested_frames'],2000)
        self.assertTrue(any('2000' in w for w in result['warnings']))
    def test_case_inflections_and_local_pronoun_keep_reference_identity(self):
        story='Анна подошла к Борису. Она передала письмо Борису.'
        result=plan(fixture(story),story,3)
        self.assertTrue(all(s['character_ids']==['a','b'] for s in result['scenes']))
    def test_unrelated_paragraph_does_not_inherit_named_person_or_old_era(self):
        story='Анна открывает дверь.\n\nСтарая башня стоит у реки.'
        result=plan(fixture(story),story,2)
        self.assertEqual(result['scenes'][1]['characters'],[])
        self.assertNotIn('Wrong',result['scenes'][1]['location'])
        self.assertNotIn('Wrong',result['scenes'][1]['beat'])
    def test_attribution_and_portrait_do_not_become_living_actors(self):
        story='По словам Анны, город пуст. Портрет Бориса висит на стене.'
        result=plan(fixture(story),story,2)
        self.assertTrue(all(not s['characters'] for s in result['scenes']))
    def test_identical_fragments_at_different_offsets_are_retained(self):
        story='Дождь идёт. Дождь идёт. Дождь идёт.'
        self.check(story,plan(fixture(story),story,3),3)
    def test_source_text_is_not_normalized_or_truncated(self):
        story='  Анна\tчитает…\r\n\r\nБорис: «Жду!»  '
        self.check(story,plan(fixture(story),story,4),4)
    def test_all_modes_share_same_density_without_changing_registry(self):
        story='Анна идёт. Борис стоит. Часы тикают.'
        for mode in ('characters','dynamic','objects','illustration'):
            source=fixture(story);source['mode']=mode;result=plan(source,story,4)
            self.check(story,result,4);self.assertEqual(result['characters'],source['characters'])
    def test_requested_target_none_retains_cli_compatibility(self):
        source=fixture('Анна идёт.');self.assertIs(plan(source,'Анна идёт.',None),source)
    def test_invalid_target_is_rejected_instead_of_ignored(self):
        for target in (0,-1,2001,True,2.2,'12'):
            with self.assertRaises(ValueError):plan(fixture('Анна идёт.'),'Анна идёт.',target)
    def test_projection_survives_json_checkpoint_without_reexpansion(self):
        story='Анна идёт. Борис ждёт. Анна открывает дверь.'
        result=plan(fixture(story),story,5);loaded=json.loads(json.dumps(result))
        self.assertIs(plan(loaded,story,5),loaded);self.check(story,loaded,5)
    def test_adapter_receives_requested_target_after_cache_result(self):
        story='Анна идёт. Анна читает книгу. Анна закрывает книгу.'
        registry=Registry.model_validate({'title':'Test','logline':'','genre':'','characters':[{'id':'ch_1','name':'Анна','aliases':['Анна'],'role':'protagonist','description':'Анна.','arc':''}],'locations':[],'props':[]})
        scene=Scene.model_validate({'index':0,'heading':'INT. ДОМ - ДЕНЬ','location_id':'','time_of_day':'ДЕНЬ','character_ids':['ch_1'],'summary':'Анна читает книгу.','mood':'calm','duration_sec':20})
        result=AnalysisResult(registry,[scene]);result.stats.result_cache_hit=True
        exported=export_to_app_analysis(result,story,target=5)
        self.check(story,exported,5);self.assertTrue(exported['diagnostics']['result_cache_hit'])
    def test_worker_checkpoint_applies_requested_count_before_project_save(self):
        from imagegen.service import Service
        from imagegen.worker import Worker
        from imagegen.db import dump
        with tempfile.TemporaryDirectory() as temp:
            service=Service(temp)
            project=service.create_project({'title':'Test','story':'Анна идёт. Борис читает книгу. Анна открывает дверь.','style':'marker','mode':'dynamic'})
            analysis=fixture(project['story'])
            snapshot={'project':project,'profile':{},'target':5,'pending_analysis':analysis}
            with service.db.transaction() as db:
                gid=service.insert_generation(db,project['id'],'analysis',snapshot)
            generation=service.db.one('SELECT * FROM generations WHERE id=?',(gid,))
            with patch('imagegen.script_analysis.compact_staging.stage', side_effect=lambda value, *args, **kwargs: value) as staging:
                Worker(service)._save_analysis(generation,analysis,project['version'])
                self.assertEqual(len(staging.call_args.args[0]['scenes']), 5)
            saved=service.detail(project['id'])
            self.assertEqual(len(saved['scenes']),5)
            self.assertEqual(saved['analysis_metadata']['diagnostics']['storyboard']['requested_frames'],5)
            self.assertTrue(coverage(project['story'],saved['scenes'])['ok'])
            self.assertEqual(service.db.one('SELECT state FROM generations WHERE id=?',(gid,))['state'],'succeeded')
            # The user's actual trigger: generating a character after a dense
            # storyboard must not replace the frame plan with semantic episodes.
            before=[dict(r) for r in service.db.all('SELECT * FROM scenes WHERE project_id=? ORDER BY position',(project['id'],))]
            with patch.object(service,'profile',return_value={'width':768,'height':432,'image_backend':'flow'}):
                service.cast(saved['characters'][0]['id'],1)
            after=[dict(r) for r in service.db.all('SELECT * FROM scenes WHERE project_id=? ORDER BY position',(project['id'],))]
            self.assertEqual(before,after)

if __name__=='__main__':unittest.main()

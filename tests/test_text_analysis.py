import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imagegen import narrative_frame_focus as focus, story_review
from imagegen.story_visual_plan import validate_scenes
from imagegen.worker import Worker, AnalysisPaused


class ReviewReached(Exception):
    pass


class TextAnalysisTests(unittest.TestCase):
    def test_default_review_uses_source_content_without_voice_options(self):
        args=({},'Текст.',object(),Path('unused'))
        with patch('imagegen.story_visual_plan.review_analysis',return_value={'checked':True}) as current, \
                patch('imagegen.legacy_story_review.review_analysis') as legacy:
            self.assertEqual(story_review.review_analysis(*args),{'checked':True})
            current.assert_called_once_with(*args,None)
            legacy.assert_not_called()

    def test_explicit_legacy_caller_remains_compatible(self):
        with patch('imagegen.story_visual_plan.review_analysis') as current, \
                patch('imagegen.legacy_story_review.review_analysis',return_value={'legacy':True}) as legacy:
            self.assertEqual(story_review.review_analysis({},'',None,Path('.'),use_visual_contract=False),{'legacy':True})
            legacy.assert_called_once()
            current.assert_not_called()

    def test_worker_text_route_does_not_read_old_voice_workflow(self):
        for profile in (None,{'profile_type':'forgotten_cities'},{'profile_type':'other'}):
            with self.subTest(profile=profile):
                project={'id':'p','story':'Рабочий несёт корзину.',
                         'channel_profile_id':'channel' if profile else None}
                queries=[]
                def one(sql,params):
                    queries.append(sql)
                    if 'channel_profiles' in sql:return profile
                    raise AssertionError('Text review consulted unrelated workflow: '+sql)
                service=SimpleNamespace(root=Path('unused'),project=lambda pid:project,db=SimpleNamespace(one=one))
                worker=Worker.__new__(Worker);worker.service=service;worker.db=service.db;worker.update=Mock()
                generation={'id':'g','project_id':'p','snapshot':json.dumps({'profile':{'text_backend':'nvidia'}})}
                client=SimpleNamespace(model='fixture')
                with patch('imagegen.nvidia_text.NvidiaStoryClient.from_settings',return_value=client), \
                        patch('imagegen.groq_assist.GroqAssistClient.from_settings',return_value=None), \
                        patch('imagegen.story_review.review_analysis',side_effect=ReviewReached) as review, \
                        patch('imagegen.narrative_storyboard.review_analysis') as audio_sequence:
                    with self.assertRaises(ReviewReached):
                        worker._save_analysis(generation,{'analyzer_version':'v3','scenes':[]},1)
                    self.assertIs(review.call_args.kwargs['use_visual_contract'],True)
                    self.assertEqual(review.call_args.args[1],project['story'])
                    audio_sequence.assert_not_called()
                    self.assertFalse(any('voice_workflows' in query for query in queries))

    def test_old_paused_voice_job_does_not_pause_text_review(self):
        def one(sql,params):
            self.assertNotIn('voice_workflows',sql)
            return {'cancel_requested':0}
        worker=Worker.__new__(Worker);worker.db=SimpleNamespace(one=one);worker.stop_event=threading.Event()
        worker._check_source_review({'id':'g','project_id':'p'})

    def test_text_cancellation_still_stops_review(self):
        for row,stop in (({'cancel_requested':1},False),(None,False),({'cancel_requested':0},True)):
            with self.subTest(row=row,stop=stop):
                worker=Worker.__new__(Worker);worker.db=SimpleNamespace(one=lambda *args:row)
                worker.stop_event=threading.Event()
                if stop:worker.stop_event.set()
                with self.assertRaises(AnalysisPaused):worker._check_source_review({'id':'g','project_id':'p'})

    def test_full_source_review_accepts_text_only_across_genres(self):
        cases=[('Рабочий несёт корзину.','корзину',['рабочий'],'human','fact','A worker carrying a basket.'),
               ('Собака тащит сани.','Собака',['собака'],'animal','fact','A dog pulling a sled.'),
               ('Врач не открыл дверь.','дверь',[],'human','negated','A closed door.'),
               ('Почему город опустел?','город',[],'human','question','A still view of the city.')]
        for story,subject,names,kind,status,view in cases:
            with self.subTest(story=story), tempfile.TemporaryDirectory() as folder:
                original={'analyzer_version':'v3','characters':[],'locations':[],'scenes':[
                    {'source_start':0,'source_end':len(story),'source_quote':story,
                     'beat':'Visual enactment of: '+story,'composition':'Setting / Environment',
                     'characters':[],'episodic_people':[],'visible_people':0,'time':'unspecified'}]}
                before=copy.deepcopy(original)
                def request(instructions,items,**kwargs):
                    if kwargs.get('review'):
                        return {'reviews':[{'key':item['key'],'checks':[{'category':category,'verdict':'supported',
                            'evidence':[item['narration']],'reason':'Подтверждено источником.'}
                            for category in focus.CATEGORIES]} for item in items]}
                    return {'frames':[{'key':item['key'],'subject':subject,'action':'Действие или удержание по источнику',
                        'participants':[{'name':name,'kind':kind,'role':'actor','evidence':item['narration']} for name in names],
                        'place':'Место источником не уточнено','period':item['parent']['period'],
                        'time_layer':item['parent']['time_layer'],'state':'Состояние по источнику',
                        'subject_state':'living' if names else 'context','source_status':status,'visual_direction':view,
                        'location_ref':'','required_artifacts':[],'evidence':[item['narration']]} for item in items]}
                request.identity='text-only-test'
                with patch('imagegen.story_visual_plan.request_adapter',return_value=request), \
                        patch('urllib.request.urlopen',side_effect=AssertionError('No real model or ASR requests in this test')):
                    result=story_review.review_analysis(original,story,SimpleNamespace(model='fixture'),Path(folder))
                self.assertEqual(original,before)
                scene=result['scenes'][0]
                self.assertEqual(scene['source_quote'],story)
                self.assertEqual(scene['visual_content']['subject'],subject)
                self.assertEqual(scene['source_status'],status)
                self.assertEqual(scene['visible_people'],len(names) if kind=='human' else 0)
                self.assertNotIn('voice_timing',scene)
                self.assertNotIn('timing',scene['visual_content'])
                validate_scenes(story,result['v3_metadata']['story_visual_plan'],result['scenes'])


if __name__=='__main__':unittest.main()

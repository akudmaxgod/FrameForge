import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from imagegen import narrative_frame_focus as focus
from imagegen import source_review_status as status
from imagegen.story_visual_plan import compile_scenes,parent_document,validate_scenes,validate_plan,verified_participants


def scene(story,quote):
    a=story.index(quote)
    return {'source_start':a,'source_end':a+len(quote),'source_quote':quote,'beat':quote,
            'composition':'Setting / Environment','characters':[],'episodic_people':[],'visible_people':0,'time':'unspecified'}


def proposal(item,subject='Корзина'):
    return {'key':item['key'],'subject':subject,'action':'Удержание предмета по источнику','participants':[],
            'place':'Не уточнено','period':item['parent']['period'],'time_layer':item['parent']['time_layer'],
            'state':'По источнику','subject_state':'context','source_status':'fact',
            'visual_direction':'A physical basket standing on the ground.','location_ref':'','required_artifacts':[],
            'evidence':[copy.deepcopy(item['narration'])]}


def review(item,advice=False):
    value={'key':item['key'],'checks':[{'category':c,'verdict':'supported','evidence':[copy.deepcopy(item['narration'])],
             'reason':'Подтверждено источником.'} for c in focus.CATEGORIES]}
    if advice:value['editorial_notes']=[{'code':'shot_size_preference','message':'Можно попробовать крупнее.',
                                       'evidence':[copy.deepcopy(item['narration'])]}]
    return value


class FrameReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.quotes=['Корзина стоит.','Корзина лежит.','Корзина висит.']
        self.story=' '.join(self.quotes);self.scenes=[scene(self.story,q) for q in self.quotes]
        self.document=parent_document(self.story,self.scenes)
        self.items=[focus.frame_input(self.story,self.document,{**d,'narrative_beat':{'ordinal':n+1}}) for n,d in enumerate(self.scenes)]
    def tearDown(self):self.temp.cleanup()
    def request(self,handler=None):
        calls=[]
        def request(instructions,items,**kw):
            calls.append((kw.get('review',False),[i['key'] for i in items]))
            if handler:return handler(items,kw)
            return {'reviews':[review(i) for i in items]} if kw.get('review') else {'frames':[proposal(i) for i in items]}
        request.identity={'fixture':'resume-v1'}
        return request,calls
    def compile(self,request,client=None,**kw):
        with patch('imagegen.story_visual_plan.request_adapter',return_value=request):
            return compile_scenes(self.scenes,self.story,client,self.folder,**kw)
    def report(self):return json.loads((self.folder/'review-progress.json').read_text(encoding='utf-8'))
    def test_all_eight_semantic_conflicts_have_source_evidence_and_fields(self):
        for n,category in enumerate(focus.CATEGORIES):
            with self.subTest(category=category):
                row=review(self.items[0],True);row['checks'][n].update(verdict='issue',reason='В кадре неверное содержание.')
                with self.assertRaises(focus.SourceConflict) as caught:focus.validate_review(row,self.items[0],self.story,proposal(self.items[0]))
                issue=caught.exception.source_issues[0]
                self.assertEqual(issue['kind'],'semantic');self.assertTrue(issue['required'])
                self.assertEqual(issue['code'],'source_'+category);self.assertEqual(issue['evidence'],[self.items[0]['narration']])
                self.assertTrue(issue['affected_fields']);self.assertEqual(issue['source_scope'],self.items[0]['narration'])
    def test_advice_is_saved_and_does_not_block_or_change_participants(self):
        def handler(items,kw):return {'reviews':[review(i,True) for i in items]} if kw.get('review') else {'frames':[proposal(i) for i in items]}
        request,calls=self.request(handler);frames,plan=self.compile(request)
        self.assertEqual(len(calls),6);validate_scenes(self.story,plan,frames)
        for row in self.report()['frames']:
            self.assertEqual(row['status'],'accepted');self.assertEqual(row['issues'][0]['kind'],'editorial')
            self.assertFalse(row['issues'][0]['required'])
        self.assertEqual(verified_participants(frames[0],self.story),[])
        self.assertEqual(frames[0]['source_review_advice'][0]['code'],'shot_size_preference')
    def test_advice_cannot_carry_semantic_codes(self):
        row=review(self.items[0],True);row['editorial_notes'][0]['code']='source_participants'
        with self.assertRaises(ValueError):focus.validate_review(row,self.items[0],self.story)
    def test_advice_cannot_use_future_or_fabricated_evidence(self):
        for evidence in (self.items[2]['narration'],{'start':0,'end':3,'quote':'Выдумка'}):
            row=review(self.items[0],True);row['editorial_notes'][0]['evidence']=[evidence]
            with self.assertRaises(ValueError):focus.validate_review(row,self.items[0],self.story)
    def test_old_review_without_advice_remains_valid(self):
        self.assertEqual(focus.validate_review(review(self.items[0]),self.items[0],self.story)['key'],self.items[0]['key'])
    def test_transport_schema_resolves_advice_source_ids(self):
        item=self.items[0];row=review(item);row['editorial_notes']=[{'code':'view_variety','message':'Другой ракурс.','source_ids':[0]}]
        for check in row['checks']:check['source_ids']=[0];del check['evidence']
        resolved=focus.resolve_source_ids({'reviews':[row]},[item],review=True)
        focus.validate_review(resolved['reviews'][0],item,self.story)
        self.assertEqual(row['editorial_notes'][0]['evidence'],[item['narration']])
    def test_failed_frame_does_not_interrupt_other_frames_and_resume_uses_cache(self):
        failed=True
        def handler(items,kw):
            if not kw.get('review'):return {'frames':[proposal(i) for i in items]}
            rows=[review(i) for i in items]
            if failed and items[0]['key']==self.items[1]['key']:
                rows[0]['checks'][0].update(verdict='issue',reason='Главный предмет не соответствует источнику.')
            return {'reviews':rows}
        request,calls=self.request(handler);before=copy.deepcopy(self.scenes)
        with self.assertRaises(ValueError):self.compile(request)
        report=self.report();self.assertEqual([r['status'] for r in report['frames']],['accepted','failed','accepted'])
        self.assertEqual(report['frames'][1]['issues'][0]['neighbors'],[self.items[0]['key'],self.items[2]['key']])
        self.assertEqual(report['frames'][1]['attempts'],3);self.assertEqual(self.scenes,before)
        first_calls=len(calls);failed=False;frames,plan=self.compile(request)
        self.assertEqual(calls[first_calls:],[(False,[self.items[1]['key']]),(True,[self.items[1]['key']])])
        self.assertEqual(self.report()['status'],'complete');validate_scenes(self.story,plan,frames)
    def test_critic_format_retries_do_not_rewrite_valid_proposal(self):
        count=0
        def handler(items,kw):
            nonlocal count
            if not kw.get('review'):return {'frames':[proposal(i) for i in items]}
            count+=1;rows=[review(i) for i in items]
            if count<=3:rows[0]['checks'].pop()
            return {'reviews':rows}
        request,calls=self.request(handler)
        with self.assertRaises(ValueError):self.compile(request)
        self.assertEqual(calls.count((False,[self.items[0]['key']])),1)
        self.assertEqual(calls.count((True,[self.items[0]['key']])),3)
        self.assertEqual(self.report()['frames'][0]['issues'][0]['code'],'review_contract')
        self.assertEqual(self.report()['frames'][2]['status'],'accepted')
    def test_changed_model_identity_invalidates_cached_reviews(self):
        request,calls=self.request();self.compile(request);calls.clear();request.identity={'fixture':'other-model'}
        self.compile(request);self.assertEqual(len(calls),6)
    def test_changed_rules_invalidate_cached_reviews(self):
        request,calls=self.request();self.compile(request);calls.clear()
        with patch.object(focus,'REVIEW_INSTRUCTIONS',focus.REVIEW_INSTRUCTIONS+'\nChanged source acceptance rule.'):
            self.compile(request)
        self.assertEqual(len(calls),6)
    def test_unrelated_parent_metadata_does_not_invalidate_every_frame(self):
        request,calls=self.request();self.compile(request);calls.clear();doc=copy.deepcopy(self.document)
        doc['beats'][2]['period']='Другой подтверждённый период'
        doc['content_hash']=focus.fingerprint({k:v for k,v in doc.items() if k!='content_hash'})
        self.compile(request,document=doc)
        self.assertEqual(calls,[(False,[self.items[2]['key']]),(True,[self.items[2]['key']])])
    def test_corrupt_single_cache_rechecks_only_that_frame(self):
        request,calls=self.request();self.compile(request);calls.clear();row=self.report()['frames'][1]
        (self.folder/row['cache']).write_text('{broken',encoding='utf-8');self.compile(request)
        self.assertEqual(calls,[(False,[self.items[1]['key']]),(True,[self.items[1]['key']])])
    def test_source_event_and_audio_clock_cannot_move_during_repair(self):
        frames=[{'data':{**d,'narrative_beat':{'ordinal':n+1},'voice_timing':{'start':n,'end':n+1}}} for n,d in enumerate(self.scenes)]
        request,calls=self.request()
        def bad_grounder(data,entry):return {**data,'voice_timing':{'start':999,'end':1000}}
        with self.assertRaises(ValueError) as caught:focus.prepare(self.story,{'narrative_storyboard':self.document},frames,request,self.folder,grounder=bad_grounder)
        self.assertTrue(all(p['code']=='immutable_voice_timing' for p in caught.exception.source_issues))
        self.assertEqual(self.report()['counts']['failed'],3)
    def test_serial_rate_limit_stops_without_semantic_retry(self):
        from imagegen.nvidia_text import NvidiaAPIError
        def handler(items,kw):raise NvidiaAPIError('HTTP 429 limit')
        request,calls=self.request(handler)
        with self.assertRaises(NvidiaAPIError):self.compile(request)
        self.assertEqual(len(calls),1);report=self.report()
        self.assertEqual(report['frames'][0]['issues'][0]['code'],'provider_rate_limit')
        self.assertEqual(report['counts']['pending'],2)
    def test_parallel_provider_failure_starts_no_more_than_four_frames(self):
        from imagegen.nvidia_text import NvidiaAPIError
        self.quotes=['Корзина номер '+str(n)+' стоит.' for n in range(12)]
        self.story=' '.join(self.quotes);self.scenes=[scene(self.story,q) for q in self.quotes]
        def handler(items,kw):raise NvidiaAPIError('HTTP 429 quota')
        request,calls=self.request(handler)
        with self.assertRaises(ValueError):self.compile(request,SimpleNamespace(model='nvidia/fixture'))
        self.assertLessEqual(len(calls),4);self.assertEqual(self.report()['counts']['pending'],8)
        self.assertTrue(all(r['issues'][0]['kind']=='provider' for r in self.report()['frames'][:4]))
    def test_parallel_timeout_preserves_checkpoint_and_reports_provider(self):
        request,calls=self.request(lambda items,kw:(_ for _ in ()).throw(TimeoutError('timeout')))
        with self.assertRaises(ValueError):self.compile(request,SimpleNamespace(model='nvidia/fixture'))
        self.assertTrue(all(r['issues'][0]['code']=='provider_timeout' for r in self.report()['frames']))
    def test_cancel_preserves_accepted_checkpoint(self):
        def handler(items,kw):
            if items[0]['key']==self.items[1]['key']:raise InterruptedError('pause')
            return {'reviews':[review(i) for i in items]} if kw.get('review') else {'frames':[proposal(i) for i in items]}
        request,calls=self.request(handler)
        with self.assertRaises(InterruptedError):self.compile(request)
        self.assertEqual([r['status'] for r in self.report()['frames']],['accepted','paused','pending'])
    def test_saved_advice_tampering_rejected(self):
        request,calls=self.request();frames,plan=self.compile(request)
        frames[0]['source_review_advice']=[{'message':'Подмена'}]
        with self.assertRaises(ValueError):validate_scenes(self.story,plan,frames)
    def test_subject_repair_preserves_valid_neighbor_and_image_description(self):
        self.story='Гора появилась. Так появилась насыпь.'
        self.scenes=[scene(self.story,'Гора появилась.'),scene(self.story,'Так появилась насыпь.')]
        repairs=[]
        def handler(items,kw):
            item=items[0];second=item['key'].startswith('2:')
            if kw.get('review'):
                row=review(item)
                if second and item['proposal']['subject']=='Гора':
                    row['checks'][0].update(verdict='issue',reason='Назовите предмет насыпью.')
                    row['subject_correction']={'subject':'насыпь','evidence':item['narration']}
                return {'reviews':[row]}
            subject='Гора'
            if second and kw.get('repair'):
                repairs.append(kw['repair']);subject=kw['repair']['subject_correction']['subject']
            value=proposal(item,subject);value['visual_direction']='An earthen mound.' if second else 'A mountain.'
            if second and subject=='Гора':value['evidence'].append(item['source_context'])
            return {'frames':[value]}
        request,calls=self.request(handler);frames,plan=self.compile(request)
        self.assertEqual(frames[1]['subject'],'насыпь');self.assertTrue(frames[1]['visual_direction'].startswith('An earthen mound.'))
        self.assertEqual(plan['focus']['frames'][1]['focus']['visual_direction'],'An earthen mound.')
        self.assertTrue(frames[0]['visual_direction'].startswith('A mountain.'));self.assertEqual(len(repairs),1)
        self.assertEqual(sum(not critic and keys[0].startswith('1:') for critic,keys in calls),1)
        validate_scenes(self.story,plan,frames)
    def test_secret_redaction_on_provider_report(self):
        message=status.safe_message('Authorization: Bearer gsk_abcdefghijklmnopqrstuvwxyz01234567890123456789')
        self.assertNotIn('gsk_abcdefghijklmnopqrstuvwxyz',message)


class ResumeApiTests(unittest.TestCase):
    def setUp(self):
        from imagegen.server import Application
        self.temp=tempfile.TemporaryDirectory();self.app=Application(self.temp.name);self.service=self.app.service
        self.project=self.service.create_project({'title':'Resume fixture','story':'Корзина стоит.','style':'Cinematic'})
        self.pid=self.project['id'];self.path='/api/projects/'+self.pid+'/source-review-status'
        data=scene(self.project['story'],self.project['story']);doc=parent_document(self.project['story'],[data])
        self.item=focus.frame_input(self.project['story'],doc,{**data,'narrative_beat':{'ordinal':1}})
        request=lambda *args:None;request.identity='fixture'
        self.run=status.begin(self.service.root/'voice'/self.pid/'general_source_review/story-visual-plan-1',self.project['story'],[self.item],request)
        status.update(self.run,self.item['key'],status='failed',issues=[status.issue(self.item,'source_subject','semantic','Предмет не совпадает.',evidence=[self.item['narration']])])
    def tearDown(self):self.temp.cleanup()
    def failed_generation(self):
        with self.service.db.transaction() as conn:
            gid=self.service.insert_generation(conn,self.pid,'analysis',{'project':self.project,'profile':{},'pending_analysis':{'scenes':[]}})
            conn.execute("UPDATE generations SET state='failed' WHERE id=?",(gid,))
        return gid
    def resume(self,**kw):return self.app.dispatch('POST',self.path+'/resume',{'stage':'text','run_id':self.run['run_id'],**kw})
    def test_read_report_does_not_change_project_or_queue(self):
        before=self.service.db.all('SELECT * FROM generations')
        result=self.app.dispatch('GET',self.path,None)
        self.assertEqual(result['reports'][0]['frames'][0]['source'],self.item['narration'])
        self.assertFalse(result['media_regeneration_started']);self.assertEqual(before,self.service.db.all('SELECT * FROM generations'))
    def test_resume_retries_only_saved_analysis_and_no_media(self):
        old=self.failed_generation();result=self.resume()
        self.assertIsInstance(result['generation_id'],str);self.assertNotEqual(result['generation_id'],old)
        rows=self.service.db.all('SELECT purpose,state FROM generations');self.assertEqual([r['purpose'] for r in rows],['analysis','analysis'])
        self.assertEqual(self.service.db.all('SELECT * FROM scenes'),[]);self.assertFalse(result['media_regeneration_started'])
    def test_resume_rejects_duplicate_active_analysis(self):
        from imagegen.domain import DomainError
        self.failed_generation();self.resume()
        with self.assertRaises(DomainError):self.resume()
        self.assertEqual(len(self.service.db.all('SELECT * FROM generations')),2)
    def test_resume_rejects_changed_source_or_project_revision(self):
        from imagegen.domain import DomainError
        self.failed_generation()
        with self.service.db.transaction() as conn:conn.execute('UPDATE projects SET version=version+1 WHERE id=?',(self.pid,))
        with self.assertRaises(DomainError):self.resume()
        with self.service.db.transaction() as conn:conn.execute('UPDATE projects SET story=? WHERE id=?',('Другой текст.',self.pid))
        result=self.app.dispatch('GET',self.path,None);self.assertTrue(result['reports'][0]['stale'])
        with self.assertRaises(DomainError):self.resume()
    def test_run_and_project_scope_are_checked(self):
        from imagegen.domain import DomainError
        self.failed_generation()
        with self.assertRaises(DomainError):self.resume(run_id='forged')
        other=self.service.create_project({'title':'Other','story':self.project['story'],'style':'Cinematic'})
        path='/api/projects/'+other['id']+'/source-review-status'
        self.assertEqual(self.app.dispatch('GET',path,None)['reports'],[])
        with self.assertRaises(DomainError):self.app.dispatch('POST',path+'/resume',{'stage':'text','run_id':self.run['run_id']})
    def test_saved_frames_are_not_overwritten_by_resume(self):
        from imagegen.domain import DomainError
        self.failed_generation()
        analysis={'world':'','characters':[],'locations':[],'props':[],'scenes':[scene(self.project['story'],self.project['story'])]}
        self.service.import_analysis(self.pid,analysis,self.project['version'])
        before=self.service.db.all('SELECT * FROM scenes')
        with self.assertRaises(DomainError):self.resume()
        report=self.app.dispatch('GET',self.path,None)['reports'][0];self.assertFalse(report['can_resume'])
        self.assertEqual(before,self.service.db.all('SELECT * FROM scenes'))
    def test_changed_scene_marks_existing_media_stale_without_new_generation(self):
        from tests.test_veo_animation import TestVeoAnimationPlan
        fixture=TestVeoAnimationPlan();fixture.service=self.service
        pid=fixture._create_sample_project(2);fixture._upload_scene_images(pid)
        detail=self.service.detail(pid);first=detail['scenes'][0];second=detail['scenes'][1]
        before=self.service.db.all('SELECT * FROM generations');assets=self.service.db.all('SELECT * FROM assets')
        self.service.edit_scene(first['id'],{'beat':first['data']['beat']+' A corrected posture.'},first['current_revision'])
        after=self.service.detail(pid)
        self.assertTrue(after['scenes'][0]['stale']);self.assertFalse(after['scenes'][1]['stale'])
        self.assertEqual(self.service.db.all('SELECT * FROM generations'),before);self.assertEqual(self.service.db.all('SELECT * FROM assets'),assets)
        self.assertEqual(after['scenes'][1]['current_revision'],second['current_revision'])
    def test_separate_plan_resume_disables_automatic_media_generation(self):
        from unittest.mock import Mock
        request=lambda *args:None;request.identity='fixture'
        run=status.begin(self.service.root/'voice'/self.pid/'story-visual-timed',self.project['story'],[self.item],request)
        status.update(run,self.item['key'],status='failed')
        self.app.voice=SimpleNamespace(get=Mock(return_value={'state':'failed','options':{'frame_content_focus':True,'auto_render':True}}),
                                       update=Mock(),control=Mock(return_value={'state':'queued'}))
        self.resume(stage='timed',run_id=run['run_id'])
        options=self.app.voice.update.call_args.kwargs['options_json']
        self.assertFalse(options['auto_render']);self.assertTrue(options['versioned_plan']);self.assertTrue(options['frames_only'])
        self.app.voice.control.assert_called_once_with(self.pid,'resume')


if __name__=='__main__':unittest.main()

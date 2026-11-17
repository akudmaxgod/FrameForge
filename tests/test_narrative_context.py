import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.evidence_protocol_fixture import response_for
from imagegen import narrative_frame_focus as focus
from imagegen.narrative_context import build, evidence_allowed, frame_fields, VERSION
from imagegen.nvidia_text import NvidiaStoryClient, NemotronSemanticPass
from imagegen.groq_assist import GroqAssistClient
from imagegen.story_catalog import build as catalog_build, seal
from imagegen.story_visual_plan import parent_document, compile_scenes, validate_plan
from imagegen.analyzer_v3.models import DocumentIR, EventIR, SourceSpan, MentionIR


def response(value):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}


def scene(story, quote):
    start=story.index(quote)
    return {'source_start':start,'source_end':start+len(quote),'source_quote':quote,
            'time':'unspecified','characters':[],'subject':'','beat':'','visible_people':0}


def entity(key, name, kind, quote):
    return {'key':key,'name':name,'kind':kind,'quote':quote,'aliases':[],
            'species':None,'visibility_policy':'source_only'}


def catalog(story, entities):
    return catalog_build(story, {'catalog':{'entities':entities,'relations':[],'states':[]}})


def proposal(item):
    return {'key':item['key'],'subject':'корзина','action':'Корзина стоит.',
            'participants':[],'place':'Не уточнено','period':item['parent']['period'],
            'time_layer':item['parent']['time_layer'],'state':'Не уточнено',
            'subject_state':'context','source_status':'fact','visual_direction':'A basket standing.',
            'location_ref':'','required_artifacts':[],'evidence':[item['narration']]}


def review(item):
    return {'key':item['key'],'checks':[{'category':c,'verdict':'supported',
        'evidence':[item['narration']],'reason':'По источнику.'} for c in focus.CATEGORIES]}


class ContextTests(unittest.TestCase):
    def test_control_corpus_sources_and_no_future_context_for_ten_genres(self):
        cases=json.loads((Path(__file__).parent/'fixtures/semantic_control_cases.json').read_text(encoding='utf-8'))['cases']
        self.assertEqual(len({c['id'] for c in cases}),10)
        for case in cases:
            with self.subTest(case=case['id']):
                story=case['story'];quote=case['focus_quote'];a=story.index(quote)
                self.assertEqual(story.count(quote),1)
                for fact in case['required']:self.assertIn(fact['quote'],story)
                value=build(story,a,a+len(quote))
                wire=json.dumps(value,ensure_ascii=False)
                for forbidden in case['future_only_terms']:self.assertNotIn(forbidden,wire)
                self.assertEqual(value['current']['quote'],quote)
    def test_no_future_prose_names_or_setting_proof(self):
        first='Рабочие несут корзину.';future='Позже исследователь Пётр найдёт город.'
        story=first+' '+future
        packet=catalog(story,[entity('p','Пётр','person',future)])
        value=build(story,0,len(first),catalog=packet,setting_facts=[
            {'kind':'geography','scope':'general','value':'future city','quote':future}])
        wire=json.dumps(value,ensure_ascii=False)
        self.assertNotIn('Пётр',wire);self.assertNotIn('Позже',wire)
        self.assertFalse(value['setting_facts'])

    def test_large_parent_is_bounded_and_future_description_hidden(self):
        story=('Корзина стоит. '*600)+'Он ждёт. '+'Позже случится катастрофа.'
        current=scene(story,'Он ждёт.');document=parent_document(story,[scene(story,story)])
        document.pop('frame_bindings')
        document['beats'][0].update(subject='катастрофа',visual_direction='future disaster')
        data={**current,'narrative_beat':{'ordinal':1}}
        item=focus.frame_input(story,document,data)
        self.assertLessEqual(item['narration']['start']-item['source_context']['start'],1200)
        self.assertNotIn('approved_event_view',item['parent'])
        self.assertNotIn('subject',item['parent'])
        self.assertNotIn('катастрофа',json.dumps(item,ensure_ascii=False))

    def test_expansion_adds_only_prior_source(self):
        story='Анна держит корзину. '+('Погода спокойная. '*160)+'Она ждёт. Позже придёт Борис.'
        a=story.index('Она');b=a+len('Она ждёт.')
        small=build(story,a,b);large=build(story,a,b,level=2)
        self.assertNotIn('Анна',small['source_context']['quote'])
        self.assertIn('Анна',large['source_context']['quote'])
        self.assertNotIn('Борис',json.dumps(large,ensure_ascii=False))
        self.assertNotEqual(small['content_hash'],large['content_hash'])

    def test_explicit_remote_evidence_is_bounded_and_exact(self):
        first='Анна открыла дверь.';story=first+' '+('Лес тихий. '*700)+'Она ждёт.'
        a=story.index('Она');packet=build(story,a,len(story),prior_spans=[
            {'start':0,'end':len(first),'quote':first}])
        self.assertTrue(evidence_allowed(packet,packet['prior_context'][0],story))
        self.assertFalse(evidence_allowed(packet,{'start':0,'end':len(first),'quote':'подмена'},story))

    def test_irrelevant_entities_are_not_sent(self):
        story='Анна пришла. Борис ушёл. Собака Рекс ждёт.'
        registry=catalog(story,[entity('a','Анна','person','Анна пришла.'),
            entity('b','Борис','person','Борис ушёл.'),entity('r','Рекс','animal','Собака Рекс ждёт.')])
        # More than one window separates the unrelated identity.
        middle='Погода спокойная. '*300
        story='Анна пришла. '+middle+'Борис ушёл. Собака Рекс ждёт.'
        registry=catalog(story,[entity('a','Анна','person','Анна пришла.'),
            entity('b','Борис','person','Борис ушёл.'),entity('r','Рекс','animal','Собака Рекс ждёт.')])
        a=story.index('Собака')
        packet=build(story,a,len(story),catalog=registry)
        self.assertNotIn('Анна',[e['name'] for e in packet['story_catalog']['entities']])
        self.assertIn('Рекс',[e['name'] for e in packet['story_catalog']['entities']])

    def test_uncertain_occurrence_cannot_silently_choose_named_actor(self):
        story='Анна и Мария стоят. Она ждёт.';a=story.index('Она')
        registry=catalog(story,[entity('a','Анна','person','Анна и Мария стоят.'),
            entity('m','Мария','person','Анна и Мария стоят.')])
        registry['coreferences']=[{'source':{'start':a,'end':a+3,'quote':'Она'},
            'entity_ids':[],'identity_status':'unresolved','status':'unresolved_safe'}];seal(registry)
        data=scene(story,'Она ждёт.');doc=parent_document(story,[data]);doc['story_catalog']=registry
        item=focus.frame_input(story,doc,{**data,'narrative_beat':{'ordinal':1}})
        self.assertTrue(any(u['status']=='unresolved_safe' for u in item['context_policy']['uncertainties']))
        value=proposal(item);value['subject']='Анна';value['evidence'].append(
            {'start':0,'end':len('Анна и Мария стоят.'),'quote':'Анна и Мария стоят.'})
        value['participants']=[{'name':'Анна','kind':'human','role':'actor','evidence':item['narration']}]
        with self.assertRaisesRegex(ValueError,'Неоднозначное местоимение'):
            focus.validate_focus(value,item,story)

    def test_budget_never_truncates_current_source(self):
        story='Анна ждёт. '*300+'Корзина стоит.';a=story.index('Корзина')
        packet=build(story,a,len(story),max_bytes=2500)
        self.assertEqual(packet['current']['quote'],'Корзина стоит.')
        self.assertLessEqual(len(json.dumps(packet,ensure_ascii=False).encode()),2700)
        self.assertTrue(packet['budget']['omitted'])
        with self.assertRaisesRegex(ValueError,'бюджет'):
            build(story,a,len(story),max_bytes=100)

    def test_oversized_current_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError,'слишком велик'):
            build('А'*12001,0,12001)

    def test_source_options_and_validator_share_exact_evidence(self):
        story='Анна ждёт. Корзина стоит.';a=story.index('Корзина')
        packet=build(story,a,len(story));fields=frame_fields(story,packet)
        item={'narration':packet['current'],**fields}
        for option in item['source_options']:
            exact={k:v for k,v in option.items() if k!='id'}
            focus.evidence_span(exact,item,story)
            self.assertTrue(evidence_allowed(packet,exact,story))

    def test_legacy_document_stays_readable(self):
        story='Корзина стоит.';data=scene(story,story);doc=parent_document(story,[data])
        doc.pop('context_policy_version');doc['content_hash']=focus.fingerprint({k:v for k,v in doc.items() if k!='content_hash'})
        def request(instructions,items,**kw):
            return {'reviews':[review(i) for i in items]} if kw.get('review') else {'frames':[proposal(i) for i in items]}
        request.identity='legacy'
        with tempfile.TemporaryDirectory() as directory,patch('imagegen.story_visual_plan.request_adapter',return_value=request):
            frames,plan=compile_scenes([data],story,None,directory,document=doc)
            self.assertNotIn('context_policy',plan['focus']['frames'][0]);validate_plan(story,plan)

    def test_expansion_is_persisted_validated_and_resumed_without_calls(self):
        story='Корзина стоит. Он ждёт.';data=scene(story,'Он ждёт.');doc=parent_document(story,[data])
        calls=[]
        def request(instructions,items,**kw):
            if kw.get('review'):return {'reviews':[review(i) for i in items]}
            calls.append(items[0]['context_policy']['level'])
            if calls==[0]:raise ValueError('Неоднозначный контекст местоимения.')
            value=proposal(items[0]);value['evidence'].append({'start':0,'end':14,'quote':'Корзина стоит.'})
            return {'frames':[value]}
        request.identity='expand-fixture'
        with tempfile.TemporaryDirectory() as directory,patch('imagegen.story_visual_plan.request_adapter',return_value=request):
            frames,plan=compile_scenes([data],story,None,directory,document=doc)
            self.assertEqual(calls,[0,1]);self.assertEqual(plan['focus']['frames'][0]['context_policy']['level'],1)
            validate_plan(story,plan)
            compile_scenes([data],story,None,directory,document=doc)
            self.assertEqual(calls,[0,1])
            damaged=copy.deepcopy(plan);damaged['focus']['frames'][0]['context_policy']['level']=2
            damaged['focus']['content_hash']=focus.fingerprint({k:v for k,v in damaged['focus'].items() if k!='content_hash'})
            with self.assertRaisesRegex(ValueError,'Контекст кадра'):
                validate_plan(story,damaged)

    def test_cache_changes_with_catalog_source_rules_and_model(self):
        story='Корзина стоит.';p=build(story,0,len(story))
        registry=catalog(story,[entity('b','Корзина','object',story)])
        self.assertNotEqual(p['content_hash'],build(story,0,len(story),catalog=registry)['content_hash'])
        self.assertNotEqual(p['content_hash'],build(story+' ',0,len(story))['content_hash'])
        from imagegen.narrative_context import fingerprint
        self.assertNotEqual(fingerprint({'context':p,'model':'primary'}),fingerprint({'context':p,'model':'helper'}))
        with patch('imagegen.narrative_context.RULES','changed rule'):
            self.assertNotEqual(p['content_hash'],build(story,0,len(story))['content_hash'])


class ProviderIsolationTests(unittest.TestCase):
    def test_primary_and_helper_receive_identical_packet_without_full_overview(self):
        story='Анна ждёт. Позже Борис уйдёт.';first='Анна ждёт.'
        registry=catalog(story,[entity('a','Анна','person',first),entity('b','Борис','person','Позже Борис уйдёт.')])
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(first),first))],metadata={'story_catalog':registry})
        bodies=[]
        def post(body):
            bodies.append(body);return response_for(body,{'entities':[],'events':[],'coreferences':[]})
        client=SimpleNamespace(_post_json=post,global_context='FULL FUTURE OVERVIEW Борис')
        parser=NemotronSemanticPass();parser._completed=0
        parser._request(doc,story,client,source_range=(0,len(first)))
        parser.helper_client=SimpleNamespace(_post_json=post);parser._completed=1
        parser._request(doc,story,client,source_range=(0,len(first)))
        a,b=[json.loads(v['messages'][1]['content']) for v in bodies]
        self.assertEqual(a['context'],b['context'])
        self.assertNotIn('Борис',json.dumps(bodies,ensure_ascii=False))
        self.assertTrue(all(b['_frameforge_scoped_context'] for b in bodies))

    def test_nvidia_scoped_transport_drops_global_overview_and_private_marker(self):
        sent=[]
        def opener(request,**kwargs):
            sent.append(json.loads(request.data));return io.BytesIO(json.dumps(response({'ok':True})).encode())
        with patch('urllib.request.urlopen',side_effect=opener):
            client=NvidiaStoryClient('nvapi-fake-unit-test-key');client.global_context='FUTURE_SECRET_PLOT'
            client._post_json({'_frameforge_scoped_context':True,'messages':[{'role':'user','content':'current'}]})
        self.assertNotIn('FUTURE_SECRET_PLOT',json.dumps(sent))
        self.assertNotIn('_frameforge_scoped_context',sent[0])

    def test_transport_does_not_append_plot_to_legacy_stage_requests(self):
        sent=[]
        def opener(request,**kwargs):
            sent.append(json.loads(request.data));return io.BytesIO(json.dumps(response({'ok':True})).encode())
        with patch('urllib.request.urlopen',side_effect=opener):
            client=NvidiaStoryClient('nvapi-fake-unit-test-key');client.global_context='FUTURE_SECRET_PLOT'
            client._post_json({'messages':[{'role':'user','content':'current'}]})
        self.assertNotIn('FUTURE_SECRET_PLOT',json.dumps(sent))

    def test_helper_strips_private_marker_and_preserves_it_on_fallback(self):
        sent=[]
        def opener(request,**kwargs):
            sent.append(json.loads(request.data));return io.BytesIO(json.dumps(response({'ok':True})).encode())
        primary=SimpleNamespace(_post_json=lambda body:sent.append(body) or response({'ok':True}))
        helper=GroqAssistClient(['gsk-fake-context-test'],primary,opener=opener)
        payload={'_frameforge_scoped_context':True,'messages':[{'role':'user','content':'current'}]}
        helper._post_json(payload)
        self.assertNotIn('_frameforge_scoped_context',sent[0])
        with patch.object(helper,'_pick',return_value=None):helper._post_json(payload)
        self.assertTrue(sent[-1]['_frameforge_scoped_context'])

    def test_block_can_ground_location_in_prior_context(self):
        first='Анна вошла в дом.';second='Она ждёт.';story=first+' '+second;a=len(first)+1
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(a,len(story),second))])
        value={'entities':[],'events':[{'event_id':'e1','location_name':'дом','location_quote':first,
            'presences':[],'time_shift':'same_scene'}],'coreferences':[]}
        parser=NemotronSemanticPass();client=SimpleNamespace(_post_json=lambda body:response_for(body,value),global_context='')
        result=parser._request(doc,story,client,source_range=(a,len(story)))
        self.assertEqual(result['events'][0]['location_evidence_span']['start'],0)

    def test_earlier_event_cannot_use_future_location(self):
        first='Анна ждёт.';second='Потом она войдёт в дом.';story=first+' '+second
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(first),first))])
        value={'entities':[],'events':[{'event_id':'e1','location_name':'дом','location_quote':second,
            'presences':[]}],'coreferences':[]}
        with self.assertRaises(ValueError):NemotronSemanticPass()._request(doc,story,
            SimpleNamespace(_post_json=lambda body:response_for(body,value)),source_range=(0,len(story)))

    def test_pronoun_cannot_be_bound_to_future_individual(self):
        first='Он ждёт.';second='Потом пришёл Борис.';story=first+' '+second;a=story.index(second)
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(first),first))],
            mentions=[MentionIR('m1','Он','он',SourceSpan(0,2,'Он'),event_id='e1',mention_type='pronoun')])
        value={'entities':[{'semantic_id':'p1','canonical_name':'Борис','entity_type':'person',
            'evidence_quotes':[second]}],'events':[],'coreferences':[{'mention_id':'m1',
            'resolved_semantic_ids':['p1'],'status':'resolved'}]}
        with self.assertRaisesRegex(ValueError,'будущему участнику'):
            NemotronSemanticPass()._request(doc,story,SimpleNamespace(_post_json=lambda body:response_for(body,value)),
                source_range=(0,len(story)))

    def test_later_named_person_does_not_replace_early_anonymous_group(self):
        first='Археологи ждут.';second='Позже пришёл Борис.';story=first+' '+second
        registry=catalog(story,[entity('b','Борис','person',second)])
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(first),first))],metadata={'story_catalog':registry})
        value={'entities':[{'semantic_id':'p1','canonical_name':'Борис','entity_type':'person',
            'evidence_quotes':[first]}],'events':[{'event_id':'e1','location_name':None,'location_quote':None,
            'presences':[{'semantic_id':'p1','quote':first}]}],'coreferences':[]}
        with self.assertRaisesRegex(ValueError,'позднее имя'):
            NemotronSemanticPass()._request(doc,story,SimpleNamespace(_post_json=lambda body:response_for(body,value)),
                source_range=(0,len(story)))


if __name__=='__main__':unittest.main()

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from imagegen import source_evidence as refs
from imagegen.nvidia_text import NemotronSemanticPass
from imagegen.source_quotes import locate
from imagegen.story_catalog import build,validate
from imagegen.narrative_context import build as context
from imagegen.analyzer_v3.models import DocumentIR,EventIR,SourceSpan


def menu(story):return refs.options(story,[{'start':0,'end':len(story),'text':story}])
def entry(key,name,ids):return {'key':key,'name':name,'kind':'person','quote_ids':ids,'aliases':[],'species':None}
def overview(entries):return {'timeline':'По источнику','causal_links':'Не установлены','uncertainties':'Сохранены',
    'setting_facts':[],'catalog':{'entities':entries,'relations':[],'states':[]}}
def response(value):return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}


class SourceEvidenceTests(unittest.TestCase):
    def test_screenshot_case_uses_original_without_accepting_rewritten_quote(self):
        story='Последним известным великим царём хеттов был Суппилулиума Второй.'
        with self.assertRaises(ValueError):locate(story.replace('царём','царем'),story)
        options=menu(story)
        raw=overview([entry('king','Суппилулиума Второй',[options[0]['id']])])
        bound=refs.overview(raw,options,story)
        catalog=build(story,bound)
        self.assertEqual(catalog['entities'][0]['evidence']['quote'],story)
        self.assertEqual(raw['catalog']['entities'][0]['quote_ids'],[options[0]['id']])
        validate(story,catalog)

    def test_identical_sentences_have_distinct_occurrence_ids(self):
        story='Анна ждёт. Анна ждёт.';options=menu(story)
        self.assertEqual(len(options),2);self.assertNotEqual(options[0]['id'],options[1]['id'])
        packet=build(story,refs.overview(overview([entry('a','Анна',[options[1]['id']])]),options,story))
        self.assertEqual(packet['entities'][0]['evidence']['start'],11)
        self.assertEqual(packet['entities'][0]['evidence']['end'],len(story))

    def test_source_menu_preserves_punctuation_unicode_and_all_words(self):
        story='Ёлка «зелёная» — здесь.\r\n  Анна ждёт?\nКафе́ открыто!'
        options=menu(story)
        self.assertEqual(''.join(''.join(q['text'].split()) for q in options),''.join(story.split()))
        for option in options:self.assertEqual(option['text'],story[option['start']:option['end']])

    def test_unknown_id_and_model_supplied_quote_are_rejected(self):
        story='Анна ждёт.';options=menu(story)
        with self.assertRaises(ValueError):refs.bind(['src_not_offered'],options,story)
        raw=overview([entry('a','Анна',[options[0]['id']])]);raw['catalog']['entities'][0]['quote']='Анна ждет.'
        with self.assertRaises(ValueError):refs.overview(raw,options,story)

    def test_modified_and_out_of_bounds_source_coordinates_are_rejected(self):
        story='Анна ждёт.'
        for proof in ({'start':0,'end':len(story),'text':'Анна ждет.'},{'start':-1,'end':len(story),'text':story}):
            with self.assertRaises(ValueError):locate(proof,story)

    def test_combining_available_adjacent_ids_preserves_exact_original_span(self):
        story='Анна пришла.\nОна врач.';options=menu(story)
        self.assertEqual(refs.bind([o['id'] for o in options],options,story)['text'],story)

    def test_ids_cannot_bridge_unavailable_context(self):
        story='Анна ждёт. СКРЫТО. Борис пришёл.'
        options=refs.options(story,[{'start':0,'end':10,'text':story[:10]},
            {'start':19,'end':len(story),'text':story[19:]}])
        with self.assertRaises(ValueError):refs.bind([o['id'] for o in options],options,story)

    def test_catalog_alias_species_relations_and_states_bind_application_text(self):
        story='Анна, она же Аня, держит собаку Рекс.';options=menu(story);ids=[options[0]['id']]
        raw=overview([entry('a','Анна',ids),{'key':'dog','name':'Рекс','kind':'animal','quote_ids':ids,
            'aliases':[],'species':{'name':'собака','quote_ids':ids}}])
        raw['catalog']['entities'][0]['aliases']=[{'name':'Аня','quote_ids':ids}]
        raw['catalog']['relations']=[{'subject':'a','predicate':'owns','object':'dog','source_status':'fact','quote_ids':ids}]
        raw['catalog']['states']=[{'entity':'a','value':'держит собаку','source_status':'fact','quote_ids':ids}]
        packet=build(story,refs.overview(raw,options,story));validate(story,packet)
        for row in packet['relations']+packet['states']:self.assertEqual(row['evidence']['quote'],story)

    def fixture(self):
        story='Анна ждёт. Потом Борис войдёт.';first='Анна ждёт.'
        event=EventIR('e1','s1',SourceSpan(0,len(first),first))
        doc=DocumentIR('test',events=[event]);options=menu(story)
        return story,doc,event,options

    def block(self,source_id):
        return {'entities':[],'events':[{'event_id':'e1','location_name':None,'location_source_ids':[],
            'presences':[{'semantic_id':'a','modality':'physical','role':'actor','source_id':source_id,'states':[]}]}],'coreferences':[]}

    def test_presence_and_state_cannot_borrow_another_event(self):
        story,doc,event,options=self.fixture()
        raw=self.block(options[1]['id'])
        with self.assertRaisesRegex(ValueError,'другому событию'):refs.block(raw,options,story,{'e1':event})
        raw=self.block(options[0]['id']);raw['events'][0]['presences'][0]['states']=[{'kind':'emotion','source_id':options[1]['id']}]
        with self.assertRaisesRegex(ValueError,'другому событию'):refs.block(raw,options,story,{'e1':event})

    def test_scoped_request_rejects_future_location_id(self):
        story,doc,event,options=self.fixture()
        raw={'entities':[],'events':[{'event_id':'e1','location_name':'дом','location_source_ids':[options[1]['id']],'presences':[]}],'coreferences':[]}
        with self.assertRaises(ValueError):
            NemotronSemanticPass()._request(doc,story,SimpleNamespace(_post_json=lambda body:response(raw)),source_range=(0,event.span.end))

    def test_scoped_request_uses_ids_and_original_proof(self):
        story='Анна ждёт в доме.';event=EventIR('e1','s1',SourceSpan(0,len(story),story));doc=DocumentIR('test',events=[event])
        bodies=[]
        def post(body):
            bodies.append(body);data=json.loads(body['messages'][1]['content']);sid=data['events'][0]['source_ids'][0]
            return response({'entities':[],'events':[{'event_id':'e1','location_name':'дом','location_source_ids':[sid],'presences':[]}],'coreferences':[]})
        result=NemotronSemanticPass()._request(doc,story,SimpleNamespace(_post_json=post))
        self.assertEqual(result['events'][0]['location_evidence_span']['text'],story)
        self.assertEqual(bodies[0]['response_format']['type'],'json_schema')
        self.assertNotIn('evidence_quotes',bodies[0]['messages'][0]['content'])

    def test_full_overview_route_uses_ids_without_quote_lookup(self):
        story='Суппилулиума Второй был царём.';event=EventIR('e1','s1',SourceSpan(0,len(story),story));doc=DocumentIR('test',events=[event])
        bodies=[]
        def post(body):
            bodies.append(body);payload=json.loads(body['messages'][1]['content'])
            self.assertEqual(body['response_format']['type'],'json_schema')
            if 'events' not in payload:return response(overview([entry('king','Суппилулиума Второй',[payload['source_options'][0]['id']])]))
            return response({'entities':[],'events':[{'event_id':'e1','location_name':None,'location_source_ids':[],
                'presences':[],'dialogue_speaker_semantic_id':None,'time_shift':'none'}],'coreferences':[]})
        parser=NemotronSemanticPass()
        with patch.object(parser,'locate_quote',side_effect=lambda quote,*args: locate(quote,*args)) as locator:
            self.assertTrue(parser.run(doc,story,SimpleNamespace(_post_json=post,global_context='')))
        self.assertTrue(all(isinstance(call.args[0],dict) for call in locator.call_args_list))
        self.assertEqual(len(bodies),2)

    def test_setting_fact_keeps_selected_occurrence_and_does_not_leak_future(self):
        story='Это дом. Анна ждёт. Это дом.';options=menu(story)
        raw=overview([]);raw['setting_facts']=[{'kind':'spatial','scope':'general','value':'house','quote_ids':[options[-1]['id']]}]
        bound=refs.overview(raw,options,story)
        early=context(story,0,8,setting_facts=bound['setting_facts'])
        self.assertFalse(early['setting_facts'])
        later=context(story,0,len(story),setting_facts=bound['setting_facts'])
        self.assertEqual(later['setting_facts'][0]['evidence']['start'],options[-1]['start'])

    def test_source_ids_do_not_approve_unrelated_entity_name(self):
        story='Анна ждёт.';options=menu(story)
        with self.assertRaises(ValueError):build(story,refs.overview(overview([entry('b','Борис',[options[0]['id']])]),options,story))

    def test_schema_uses_only_statuses_accepted_by_existing_validators(self):
        options=menu('Анна ждёт.')
        state=refs.schema(options)['json_schema']['schema']['properties']['catalog']['properties']['states']['items']
        self.assertEqual(set(state['properties']['source_status']['enum']),
            {'fact','hypothesis','reported','negated','comparison','conditional'})
        coref=refs.schema(options,block=True)['json_schema']['schema']['properties']['coreferences']['items']
        self.assertEqual(coref['properties']['status']['enum'],['resolved','unresolved_safe'])

    def test_checkpoint_resume_keeps_repeated_quote_occurrence_without_model_call(self):
        import tempfile
        story='Анна ждёт. Анна ждёт.';calls=[]
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,10,story[:10]))],metadata={'semantic_verification':{}})
        def post(body):
            calls.append(body);data=json.loads(body['messages'][1]['content'])
            return response(overview([entry('a','Анна',[data['source_options'][-1]['id']])]))
        with tempfile.TemporaryDirectory() as folder:
            client=SimpleNamespace(_post_json=post,global_context='',cache_dir=folder)
            parser=NemotronSemanticPass()
            with patch('imagegen.analyzer_v3.semantic_first.SemanticFirstPass.run',return_value=True):
                self.assertTrue(parser.run(doc,story,client))
                self.assertTrue(parser.run(doc,story,client))
        self.assertEqual(len(calls),1)
        self.assertTrue(doc.metadata['story_catalog_review']['checkpoint_reused'])
        self.assertEqual(doc.metadata['story_catalog']['entities'][0]['evidence']['start'],11)

    def test_incomplete_overview_retries_once_with_larger_output_budget(self):
        from imagegen.nvidia_text import NvidiaStoryClient,NvidiaAPIError
        story='Анна ждёт.';calls=[]
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(story),story))],metadata={'semantic_verification':{}})
        def post(body):
            calls.append(body)
            if len(calls)==1:
                NvidiaStoryClient._validate_response({'choices':[{'finish_reason':'length'}],'usage':{'completion_tokens':8192}})
            data=json.loads(body['messages'][1]['content'])
            return response(overview([entry('a','Анна',[data['source_options'][0]['id']])]))
        with patch('imagegen.analyzer_v3.semantic_first.SemanticFirstPass.run',return_value=True):
            self.assertTrue(NemotronSemanticPass().run(doc,story,SimpleNamespace(_post_json=post,global_context='')))
        self.assertEqual([b['max_tokens'] for b in calls],[8192,16384])
        self.assertEqual(calls[0]['messages'],calls[1]['messages'])

    def test_repeated_truncated_overview_stays_a_bounded_failure(self):
        from imagegen.nvidia_text import NvidiaStoryClient,NvidiaAPIError
        story='Анна ждёт.';calls=[]
        doc=DocumentIR('test',events=[EventIR('e1','s1',SourceSpan(0,len(story),story))])
        def post(body):
            calls.append(body);NvidiaStoryClient._validate_response({'choices':[{'finish_reason':'length'}]})
        with self.assertRaises(NvidiaAPIError):NemotronSemanticPass().run(doc,story,SimpleNamespace(_post_json=post,global_context=''))
        self.assertEqual(len(calls),2);self.assertNotIn('story_catalog',doc.metadata)

    def test_wire_size_and_schema_do_not_duplicate_source_id_table(self):
        story=' '.join('Предмет номер '+str(n)+' стоит.' for n in range(300))
        options=menu(story);wire=refs.wire_options(options)
        self.assertEqual(wire[0]['id'],'q1')
        self.assertEqual(set(wire[0]),{'id','text'})
        self.assertLess(len(json.dumps(refs.schema(options),ensure_ascii=False)),6000)
        with self.assertRaises(ValueError):refs.bind(['q999999'],options,story)


if __name__=='__main__':unittest.main()

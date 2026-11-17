import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from imagegen.story_catalog import build, match, slice_for, canonicalize, export, validate
from imagegen.frame_event_contract import build as bind, validate as check_binding, retime
from imagegen.story_visual_plan import compile_scenes, validate_scenes, frame_entities, apply_focus, unbound_participants
from imagegen.nvidia_text import NemotronSemanticPass
from tests.evidence_protocol_fixture import response_for
from imagegen.analyzer_v3.models import (DocumentIR, EntityIR, EntityType, SourceSpan, EventIR,
    EventEntityPresenceIR, EntityModalityV3, PresenceRole, MentionIR, CoreferenceDecision)
from imagegen import narrative_frame_focus as focus


def entry(key,name,kind,quote,aliases=(),species=None):
    return {'key':key,'name':name,'kind':kind,'quote':quote,'aliases':list(aliases),'species':species}


def catalogue(story, entities, relations=(), states=()):
    return build(story, {'catalog':{'entities':entities,'relations':list(relations),'states':list(states)}})


def scene(story,quote):
    a=story.index(quote)
    return {'source_start':a,'source_end':a+len(quote),'source_quote':quote,'time':'unspecified',
            'characters':[],'subject':'','beat':'','visible_people':0}


class EventFocusTests(unittest.TestCase):
    def compile(self,story,scenes):
        def request(instructions,items,**kw):
            if kw.get('review'):
                return {'reviews':[{'key':i['key'],'checks':[{'category':c,'verdict':'supported',
                    'evidence':[i['narration']],'reason':'Подтверждено цитатой.'} for c in focus.CATEGORIES]} for i in items]}
            rows=[]
            for i in items:
                subject=next(n for n in ('житель','сосед','врач','Anna') if n.casefold() in i['narration']['quote'].casefold())
                rows.append({'key':i['key'],'subject':subject,'action':i['narration']['quote'],'participants':[],
                    'place':'Не уточнено','period':i['parent']['period'],'time_layer':i['parent']['time_layer'],
                    'state':'По источнику','subject_state':'context','source_status':'fact',
                    'visual_direction':'A physical still of '+subject+'.','location_ref':'','required_artifacts':[],
                    'evidence':[i['narration']]})
            return {'frames':rows}
        request.identity='three-distinct-focuses'
        with tempfile.TemporaryDirectory() as directory, patch('imagegen.story_visual_plan.request_adapter',return_value=request):
            return compile_scenes(scenes,story,None,directory)

    def test_three_audio_focuses_retain_one_real_parent(self):
        parts=['Заболел житель.','Перестал выходить сосед.','Вскоре заболел врач.']; story=' '.join(parts)
        parent=scene(story,story); drafts=[]
        for n,q in enumerate(parts):
            d=scene(story,q);d.update(semantic_parent_source=parent,semantic_parent_ordinal=7,
                semantic_parent_title='Болезнь в квартале',voice_timing={'start':n*3,'end':n*3+3,'duration':3})
            drafts.append(d)
        frames,plan=self.compile(story,drafts)
        self.assertEqual([f['subject'] for f in frames],['житель','сосед','врач'])
        self.assertEqual(len({f['frame_focus']['parent_episode']['id'] for f in frames}),1)
        self.assertTrue(all(f['frame_focus']['parent_episode']['ordinal']==7 for f in frames))
        self.assertTrue(all(f['frame_focus']['parent_episode']['source']['quote']==story for f in frames))
        validate_scenes(story,plan,frames)
        damaged=copy.deepcopy(frames);damaged[1]['voice_timing']['start']=3.5
        with self.assertRaises(ValueError):validate_scenes(story,plan,damaged)
        damaged=copy.deepcopy(frames);damaged[1]['frame_focus']['parent_episode']['id']='wrong'
        with self.assertRaises(ValueError):validate_scenes(story,plan,damaged)

    def test_text_plan_has_own_focus_without_fake_audio(self):
        story='Anna opens a letter.';frames,plan=self.compile(story,[scene(story,story)])
        self.assertIsNone(frames[0]['frame_focus']['timing'])
        self.assertEqual(frames[0]['frame_focus']['narration']['quote'],story)
        validate_scenes(story,plan,frames)

    def test_audio_boundary_edit_keeps_source_and_updates_contract(self):
        story='Заболел житель. Вскоре заболел врач.';drafts=[]
        for n,q in enumerate(['Заболел житель.','Вскоре заболел врач.']):
            d=scene(story,q);d['voice_timing']={'start':n*3,'end':n*3+3,'duration':3};drafts.append(d)
        frames,plan=self.compile(story,drafts)
        frames[0]['voice_timing'].update(end=3.2,duration=3.2,manual_end=True)
        frames[1]['voice_timing'].update(start=3.2,duration=2.8,manual_start=True)
        retime(story,plan,frames);validate_scenes(story,plan,frames)
        self.assertEqual(frames[1]['frame_focus']['timing']['start'],3.2)

    def test_invalid_clock_and_wrong_parent_cannot_be_published(self):
        story='Заболел житель. Врач ушёл.';d=scene(story,'Заболел житель.')
        for clock in ({'start':1,'end':0},{'start':0,'end':3,'duration':4},{'start':float('nan'),'end':3}):
            with self.subTest(clock=clock),self.assertRaises(ValueError):bind(story,{**d,'voice_timing':clock},1)
        with self.assertRaises(ValueError):bind(story,{**d,'semantic_parent_source':scene(story,'Врач ушёл.')},1)


class CatalogueTests(unittest.TestCase):
    def test_alias_keeps_canonical_cast_binding_without_duplicate_extra(self):
        story,_,packet=self.fixture();quote=story.split(' Anna')[0];data=scene(story,quote)
        data['characters']=['John Snow']
        value={'key':'1:0:'+str(len(quote)),'subject':'Dr Snow','action':'enters','participants':[
            {'name':'Dr Snow','kind':'human','role':'actor','evidence':{'start':0,'end':len(quote),'quote':quote}}],
            'place':'unspecified','period':'unspecified','time_layer':'documented_event','state':'standing',
            'subject_state':'living','source_status':'fact','visual_direction':'Dr Snow enters.',
            'location_ref':'','required_artifacts':[],'evidence':[]}
        result=apply_focus(data,{'focus':value,'review':{}},packet)
        self.assertEqual(result['characters'],['John Snow'])
        self.assertEqual(result['episodic_people'],[])
        self.assertEqual(result['visible_people'],1)
        self.assertEqual(unbound_participants(result,value['participants']),[])
        from imagegen.generation_scene import compile_wire
        prompt=compile_wire({'story':story},result)['prompt']
        self.assertIn('Dr Snow = John Snow',prompt)

    def test_full_nemotron_two_block_route_preserves_one_identity(self):
        first='John Snow, also called Dr Snow, enters.';second='He sits.';story=first+' '+second
        raw_catalog={'entities':[entry('snow','John Snow','person',first,[{'name':'Dr Snow','quote':first}])], 'relations':[], 'states':[]}
        doc=DocumentIR('test',events=[EventIR('ev_0','sec_0',SourceSpan(0,len(first),first)),
            EventIR('ev_1','sec_0',SourceSpan(len(first)+1,len(story),second))])
        bodies=[]
        def response(value):return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
        def post(body):
            bodies.append(body)
            if len(bodies)==1:return response_for(body,{'characters':[{'name':'John Snow','quotes':[first]}],'catalog':raw_catalog})
            payload=json.loads(body['messages'][1]['content']);ev=payload['events'][0]
            self.assertTrue(payload['story_catalog']['entities'])
            return response_for(body,{'entities':[{'semantic_id':'p1','canonical_name':'John Snow' if ev['event_id']=='ev_0' else 'Dr Snow',
                'entity_type':'person','gender':'male','modality':'physical','existing_entity_ids':[], 'aliases':[],
                'evidence_quotes':[ev['text']]}], 'events':[{'event_id':ev['event_id'],'location_name':None,'location_quote':None,
                'presences':[{'semantic_id':'p1','modality':'physical','role':'actor','quote':ev['text'],'states':[]}],
                'dialogue_speaker_semantic_id':None,'time_shift':'none'}],'coreferences':[]})
        client=SimpleNamespace(_post_json=post,global_context='')
        parser=NemotronSemanticPass();parser.CHUNK_MAX_EVENTS=1
        self.assertTrue(parser.run(doc,story,client))
        self.assertEqual(len(bodies),3)
        self.assertEqual(len(doc.entities),1)
        self.assertEqual(doc.entities[0].canonical_name,'John Snow')
        self.assertEqual(len({p.entity_id for e in doc.events for p in e.presences}),1)
        result=export(doc.metadata['story_catalog'],doc)
        self.assertTrue(result['entities'][0]['reference_eligible'])

    def fixture(self):
        story='John Snow, also called Dr Snow, enters. Anna owns her dog Rex. The horse Thor waits. The city Petra stands in a valley. Later John Snow becomes ill.'
        entities=[entry('snow','John Snow','person','John Snow, also called Dr Snow, enters.',
            [{'name':'Dr Snow','quote':'John Snow, also called Dr Snow, enters.'}]),
            entry('anna','Anna','person','Anna owns her dog Rex.'),
            entry('rex','Rex','animal','Anna owns her dog Rex.',species={'name':'dog','quote':'Anna owns her dog Rex.'}),
            entry('thor','Thor','animal','The horse Thor waits.',species={'name':'horse','quote':'The horse Thor waits.'}),
            entry('petra','Petra','location','The city Petra stands in a valley.')]
        return story,entities,catalogue(story,entities,[{'subject':'anna','predicate':'owns','object':'rex','quote':'Anna owns her dog Rex.'}],
            [{'entity':'snow','value':'ill','quote':'Later John Snow becomes ill.'}])

    def test_aliases_and_reordering_keep_identity(self):
        story,entities,packet=self.fixture()
        self.assertEqual(match(packet,'John Snow')['id'],match(packet,'Dr Snow')['id'])
        reordered=catalogue(story,list(reversed(entities)))
        self.assertEqual({e['name']:e['id'] for e in packet['entities']},{e['name']:e['id'] for e in reordered['entities']})
        validate(story,packet)

    def test_animal_species_owner_and_city_remain_typed(self):
        story,_,packet=self.fixture()
        self.assertEqual(match(packet,'Rex')['species']['name'],'dog')
        self.assertEqual(match(packet,'Thor')['species']['name'],'horse')
        self.assertIsNone(match(packet,'Petra','person'))
        self.assertEqual(packet['relations'][0]['object_id'],match(packet,'Rex')['id'])

    def test_future_state_never_leaks_to_earlier_frame(self):
        story,_,packet=self.fixture(); early=slice_for(packet,story.index('Later'),0)
        self.assertEqual(early['states'],[])
        late=slice_for(packet,len(story),story.index('Later'))
        self.assertEqual(late['states'][0]['value'],'ill')

    def test_unsupported_alias_pronoun_and_city_person_rejected(self):
        for entity in [entry('p','Petra','person','The city Petra stands.'),
                entry('p','John','person','John enters.',[{'name':'he','quote':'John enters.'}])]:
            story=entity['quote']
            with self.assertRaises(ValueError):catalogue(story,[entity])

    def test_two_people_with_same_profession_do_not_merge(self):
        story='The first doctor waits. The second doctor enters.'
        packet=catalogue(story,[entry('a','doctor','person','The first doctor waits.'),entry('b','doctor','person','The second doctor enters.')])
        self.assertEqual(len(packet['entities']),2);self.assertIsNone(match(packet,'doctor'))
        doc=DocumentIR('test',metadata={'story_catalog':packet})
        e={'semantic_id':'p1','canonical_name':'doctor','entity_type':EntityType.PERSON,'existing_entity_ids':[]}
        self.assertEqual(canonicalize({'entities':[e]},doc),{})

    def test_repeated_alias_in_new_block_reuses_existing_actual_id(self):
        story,_,packet=self.fixture();identity=match(packet,'John Snow')['id']
        doc=DocumentIR('test',entities=[EntityIR('ent_4','John Snow')],metadata={'story_catalog':packet,'story_catalog_bindings':{identity:'ent_4'}})
        e={'semantic_id':'p1','canonical_name':'Dr Snow','entity_type':EntityType.PERSON,'existing_entity_ids':[]}
        chosen=canonicalize({'entities':[e]},doc)
        self.assertEqual(e['canonical_name'],'John Snow');self.assertEqual(e['existing_entity_ids'],['ent_4'])
        self.assertEqual(chosen['p1'],identity)

    def test_mention_and_one_off_never_create_reference_eligibility(self):
        story,_,packet=self.fixture();snow=match(packet,'John Snow')['id'];anna=match(packet,'Anna')['id']
        doc=DocumentIR('test',entities=[EntityIR('s','John Snow'),EntityIR('a','Anna')],
            metadata={'story_catalog_bindings':{snow:'s',anna:'a'}})
        for n in range(3):
            doc.events.append(SimpleNamespace(event_id='e'+str(n),presences=[
                SimpleNamespace(entity_id='s',physical_presence=n<2),SimpleNamespace(entity_id='a',physical_presence=False)]))
        result=export(packet,doc)
        self.assertTrue(match(result,'John Snow')['reference_eligible'])
        self.assertFalse(match(result,'Anna')['reference_eligible'])

    def test_pronoun_occurrence_uses_same_id_without_global_alias(self):
        story='John enters. He sits.';packet=catalogue(story,[entry('a','John','person','John enters.')]);identity=match(packet,'John')['id']
        a=story.index('He');m=MentionIR('m1','He','he',SourceSpan(a,a+2,'He'),mention_type='pronoun')
        doc=DocumentIR('test',entities=[EntityIR('j','John')],mentions=[m],coreferences=[CoreferenceDecision('m1',['j'])],metadata={'story_catalog_bindings':{identity:'j'}})
        result=export(packet,doc)
        data={**scene(story,'He sits.'),'visual_content':{'participants':[{'name':'He','kind':'human','role':'actor',
            'evidence':{'start':a,'end':a+2,'quote':'He'}}]}}
        self.assertIsNone(match(result,'He'))
        self.assertEqual(frame_entities(result,data)['participants'][0]['entity_id'],identity)

    def test_registry_sent_to_primary_and_helper_before_block_parse(self):
        story,_,packet=self.fixture(); requests=[]
        def post(body):
            requests.append(json.loads(body['messages'][1]['content']))
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'entities':[],'events':[],'coreferences':[]})}}]}
        doc=DocumentIR('test',metadata={'story_catalog':packet})
        parser=NemotronSemanticPass();client=SimpleNamespace(_post_json=post,global_context='')
        parser._completed=0;parser._request(doc,story,client)
        parser.helper_client=SimpleNamespace(_post_json=post);parser._completed=1;parser._request(doc,story,client)
        self.assertEqual(len(requests),2)
        self.assertTrue(all(r['story_catalog']['entities'] for r in requests))
        self.assertEqual(requests[0]['story_catalog'],requests[1]['story_catalog'])


if __name__=='__main__':unittest.main()

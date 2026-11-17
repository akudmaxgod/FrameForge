import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from imagegen import nvidia_text as transport
from imagegen.nvidia_text import NvidiaStoryClient,NvidiaAPIError,MODEL
from imagegen import narrative_frame_focus as focus
from imagegen.story_visual_plan import anonymous_role,request_adapter,parent_document
from imagegen.source_review_status import from_error
from tests.test_source_review_resume import scene,proposal


class Response(io.BytesIO):
    headers={'Content-Type':'application/json'}
    def __init__(self,value):super().__init__(json.dumps(value).encode())


def response():return Response({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],'usage':{'total_tokens':12}})


class RoutingTests(unittest.TestCase):
    def test_new_default_is_available_model_id(self):
        self.assertEqual(MODEL,'nvidia/nemotron-3.5-super-vl-120b-a12b')
    def test_new_wire_model_is_used_and_cache_does_not_reuse_retired_model(self):
        with tempfile.TemporaryDirectory() as folder:
            sent=[]
            def opener(request,**kw):sent.append(json.loads(request.data)['model']);return response()
            with patch.object(transport.urllib.request,'urlopen',side_effect=opener):
                with patch.object(transport,'MODEL','nvidia/nemotron-3-super-120b-a12b'):
                    NvidiaStoryClient('nvapi-test',cache_dir=folder)._post_json({'messages':[]})
                NvidiaStoryClient('nvapi-test',cache_dir=folder)._post_json({'messages':[]})
            self.assertEqual(sent,['nvidia/nemotron-3-super-120b-a12b',MODEL]);self.assertEqual(len(list(Path(folder).glob('*.json'))),2)
    def test_404_is_permanent_model_availability_error_without_retry_or_secret(self):
        error=urllib.error.HTTPError('https://integrate.api.nvidia.com/v1/chat/completions',404,'not found',{},io.BytesIO(b'nvapi-secret'))
        with patch.object(transport.urllib.request,'urlopen',side_effect=error) as opener:
            with self.assertRaises(NvidiaAPIError) as caught:NvidiaStoryClient('nvapi-test')._post_json({'messages':[]})
        self.assertEqual(opener.call_count,1);self.assertIn('модель',str(caught.exception));self.assertNotIn('nvapi-',str(caught.exception))
        item={'key':'1:0:1','narration':{'start':0,'end':1,'quote':'x'}}
        self.assertEqual(from_error(caught.exception,item)[0]['code'],'provider_model_unavailable')
    def test_410_is_retirement_error_without_retry(self):
        error=urllib.error.HTTPError('https://integrate.api.nvidia.com/v1/chat/completions',410,'gone',{},io.BytesIO())
        with patch.object(transport.urllib.request,'urlopen',side_effect=error) as opener:
            with self.assertRaisesRegex(NvidiaAPIError,'прекратила обслуживание'):NvidiaStoryClient('nvapi-test')._post_json({'messages':[]})
        self.assertEqual(opener.call_count,1)
    def test_settings_report_active_model_without_writing_credentials_or_config(self):
        from imagegen.service import Service
        with tempfile.TemporaryDirectory() as folder:
            service=Service(folder);config=Path(folder)/'settings.json'
            saved={'text_backend':'nvidia','nvidia_model':'nvidia/nemotron-3-super-120b-a12b','nvidia_api_key':'test-key'}
            config.write_text(json.dumps(saved));before=config.read_bytes();result=service.settings()
            self.assertEqual(result['nvidia_model'],MODEL);self.assertEqual(config.read_bytes(),before)
            self.assertEqual(result['nvidia_api_key'],'test-key')
    def test_other_backend_selection_is_preserved(self):
        from imagegen.service import Service
        with tempfile.TemporaryDirectory() as folder:
            service=Service(folder);(Path(folder)/'settings.json').write_text(json.dumps({'text_backend':'local'}))
            self.assertEqual(service.settings()['text_backend'],'local')


class AnonymousRoleTests(unittest.TestCase):
    def item(self,story):
        data=scene(story,story);return focus.frame_input(story,parent_document(story,[data]),{**data,'narrative_beat':{'ordinal':1}})
    def test_geographical_qualifier_is_not_a_personal_name(self):
        item=self.item('Жители Кахокии строили насыпь.')
        self.assertTrue(anonymous_role('житель Кахокии',item))
        self.assertTrue(anonymous_role('археолог',item))
    def test_personal_names_and_named_professions_are_not_duplicated(self):
        item=self.item('Илья осматривал пациента. John Snow ждал.')
        for name in ('Илья','врач Илья','John Snow','worker John'):
            with self.subTest(name=name):self.assertFalse(anonymous_role(name,item))
    def test_actual_adapter_preserves_two_anonymous_place_qualified_slots(self):
        story='Жители Кахокии строили насыпь.';item=self.item(story)
        value=proposal(item,'насыпь');value['participants']=[{'name':'житель Кахокии','kind':'human','role':'actor','evidence':item['narration']} for _ in range(2)]
        value['visual_direction']='Two residents construct an earthen mound.'
        def request(*args,**kwargs):return {'frames':[copy.deepcopy(value)]}
        request.identity='fixture'
        with patch('imagegen.story_visual_plan.focus.client_request',return_value=request):
            result=request_adapter(SimpleNamespace(model='fixture'))('unused',[item])['frames'][0]
        self.assertEqual(len(result['participants']),2);self.assertNotEqual(result['participants'][0]['name'],result['participants'][1]['name'])
        focus.validate_focus(result,item,story)
    def test_same_named_person_is_not_changed_into_two_anonymous_people(self):
        story='Илья осматривал пациента.';item=self.item(story)
        value=proposal(item,'Илья');value['participants']=[{'name':'Илья','kind':'human','role':'actor','evidence':item['narration']} for _ in range(2)]
        value['subject_state']='living';value['visual_direction']='Two people stand in the room.'
        def request(*args,**kwargs):return {'frames':[copy.deepcopy(value)]}
        request.identity='fixture'
        with patch('imagegen.story_visual_plan.focus.client_request',return_value=request):
            result=request_adapter(SimpleNamespace(model='fixture'))('unused',[item])['frames'][0]
        self.assertEqual([p['name'] for p in result['participants']],['Илья','Илья'])
        with self.assertRaisesRegex(ValueError,'повторён'):focus.validate_focus(result,item,story)


if __name__=='__main__':unittest.main()

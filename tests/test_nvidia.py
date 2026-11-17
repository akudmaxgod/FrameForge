import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from imagegen.nvidia_text import (
    MODEL, BASE_URL, PREFIX, NvidiaStoryClient, NvidiaAPIError,
    NemotronSemanticPass, protect_key, resolve_key, public_settings, story_client,
)
from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
from imagegen.analyzer_v3.two_layer_audit import TwoLayerSemanticAuditor
from imagegen.analyzer_v3.arbitration import redact_groq_secrets
from imagegen.service import Service
from imagegen.verify import verify

KEY = 'nvapi-fake-unit-test-key'

def response(content=None, reason='stop'):
    return {'choices':[{'finish_reason':reason,'message':{'content':json.dumps(content or {'ok':True})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}

def http_response(result=None):
    return io.BytesIO(json.dumps(result or response()).encode())


class NvidiaTests(unittest.TestCase):
    def test_key_is_encrypted_and_public_settings_do_not_disclose_it(self):
        protected = protect_key(KEY)
        self.assertTrue(protected.startswith(PREFIX))
        self.assertNotIn(KEY, protected)
        self.assertEqual(resolve_key(protected), KEY)
        self.assertEqual(public_settings({'nvidia_api_key':protected})['nvidia_api_key'], '')
        self.assertTrue(public_settings({'nvidia_api_key':protected})['nvidia_key_configured'])
        self.assertNotIn(KEY, redact_groq_secrets('bad ' + KEY))

    def test_settings_persist_key_and_empty_field_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory)
            service.save_settings({'text_backend':'nvidia','nvidia_api_key':KEY,'story_analyzer_version':'v2'})
            saved = service.save_settings({'nvidia_api_key':''})
            self.assertEqual(saved['text_backend'], 'nvidia')
            self.assertEqual(saved['story_analyzer_version'], 'v3')
            self.assertEqual(resolve_key(saved['nvidia_api_key']), KEY)
            self.assertNotIn(KEY, service.config_path.read_text())

    def test_nvidia_payload_uses_fixed_endpoint_model_and_no_thinking(self):
        with patch('urllib.request.urlopen', return_value=http_response()) as send:
            client = NvidiaStoryClient(KEY)
            client._post_json({'model':'wrong','max_completion_tokens':8192,'reasoning_effort':'low',
                'response_format':{'type':'json_schema','json_schema':{'schema':{'type':'object'}}},
                'messages':[{'role':'user','content':'text'}]})
            request = send.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, BASE_URL+'/chat/completions')
            self.assertEqual(payload['model'], MODEL)
            self.assertEqual(payload['max_tokens'], 8192)
            self.assertFalse(payload['chat_template_kwargs']['enable_thinking'])
            self.assertNotIn('max_completion_tokens', payload)
            self.assertNotIn('reasoning_effort', payload)
            self.assertEqual(client.get_usage()['calls'], 1)

    def test_successful_response_is_cached_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            client = NvidiaStoryClient(KEY, cache_dir=directory)
            with patch('urllib.request.urlopen', return_value=http_response()) as send:
                payload = {'messages':[{'role':'user','content':'test'}]}
                client._post_json(payload)
                client._post_json(payload)
                self.assertEqual(send.call_count, 1)
            self.assertNotIn(KEY, next(Path(directory).glob('*.json')).read_text())
            self.assertEqual(client.get_usage()['cache_hits'], 1)

    def test_incomplete_response_is_rejected_and_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('urllib.request.urlopen', return_value=http_response(response(reason='length'))):
                with self.assertRaises(NvidiaAPIError):
                    NvidiaStoryClient(KEY, cache_dir=directory)._post_json({})
            self.assertEqual(list(Path(directory).glob('*.json')), [])

    def test_authentication_error_is_not_retried_or_disclosed(self):
        error = urllib.error.HTTPError(BASE_URL, 401, 'bad '+KEY, {}, io.BytesIO(KEY.encode()))
        with patch('urllib.request.urlopen', side_effect=error) as send:
            with self.assertRaises(NvidiaAPIError) as caught:
                NvidiaStoryClient(KEY)._post_json({})
            self.assertEqual(send.call_count, 1)
            self.assertNotIn(KEY, str(caught.exception))

    def test_rate_limit_retries_are_bounded(self):
        error = urllib.error.HTTPError(BASE_URL,429,'limited',{'Retry-After':'1'},io.BytesIO())
        wait = Mock()
        with patch('urllib.request.urlopen', side_effect=[error,error,http_response()]) as send:
            NvidiaStoryClient(KEY,retry_wait=wait)._post_json({})
            self.assertEqual(send.call_count, 3)
            self.assertEqual(wait.call_count, 2)

    def test_long_rate_limit_is_reported_without_sleeping(self):
        error = urllib.error.HTTPError(BASE_URL,429,'limited',{'Retry-After':'300'},io.BytesIO())
        wait = Mock()
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(NvidiaAPIError):
                NvidiaStoryClient(KEY,retry_wait=wait)._post_json({})
        wait.assert_not_called()

    def test_pipeline_uses_nvidia_for_both_passes_even_with_other_keys(self):
        pipeline = StoryAnalyzerV3Pipeline.from_settings({'text_backend':'nvidia',
            'nvidia_api_key':KEY,'groq_api_key':'gsk_fake','story_analyzer_version':'v2',
            'v3_qwen_detailer':True,'v3_two_layer_audit':False})
        self.assertIsInstance(pipeline.arbitrator, NvidiaStoryClient)
        self.assertIsInstance(pipeline.local_semantic_client, NvidiaStoryClient)
        self.assertIsInstance(pipeline.semantic_first_pass, NemotronSemanticPass)
        self.assertIsNone(pipeline.detailer)
        self.assertIsInstance(pipeline.semantic_auditor, TwoLayerSemanticAuditor)

    def test_editorial_factory_honours_nvidia(self):
        self.assertIsInstance(story_client({'text_backend':'nvidia','nvidia_api_key':KEY},model='groq-model'), NvidiaStoryClient)

    def test_http_settings_never_return_the_stored_key(self):
        from imagegen.server import Application
        app = Application.__new__(Application)
        app.service = Mock()
        app.service.settings.return_value = {'nvidia_api_key':KEY}
        app.service.save_settings.return_value = {'nvidia_api_key':KEY}
        for method in ('GET','POST'):
            result = app.dispatch(method,'/api/settings',{})
            self.assertEqual(result['nvidia_api_key'],'')
            self.assertTrue(result['nvidia_key_configured'])

    def test_overview_cannot_create_unevidenced_character(self):
        pipeline = StoryAnalyzerV3Pipeline.from_settings({'text_backend':'nvidia','nvidia_api_key':KEY})
        with patch.object(pipeline.arbitrator, '_post_json', return_value=response({
                'characters':[{'name':'Invented','quotes':['not in source']}]})):
            with self.assertRaisesRegex(ValueError, 'источником'):
                pipeline.analyze_structural('Мария читает письмо.')

    def test_provider_failure_never_falls_back_to_local(self):
        pipeline = StoryAnalyzerV3Pipeline.from_settings({'text_backend':'nvidia','nvidia_api_key':KEY})
        start_local = Mock()
        with patch.object(pipeline.arbitrator,'_post_json', side_effect=NvidiaAPIError('unavailable')):
            with self.assertRaises(NvidiaAPIError):
                pipeline.analyze_structural('Мария читает письмо.', before_local_audit=start_local)
        start_local.assert_not_called()

    def test_cloud_only_bundle_does_not_require_local_qwen(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'data'
            folder.mkdir()
            (folder/'settings.json').write_text(json.dumps({'text_backend':'nvidia','local_image_generation_enabled':False}))
            self.assertEqual(verify(directory,full=False), 0)

    def test_quote_positions_are_computed_from_exact_source(self):
        result = NemotronSemanticPass.locate_quote('Саша', 'Мария и Саша читают.')
        self.assertEqual(result, {'start':8,'end':12,'text':'Саша'})

    def test_ambiguous_and_invented_quotes_are_rejected(self):
        for quote in ('Мария', 'Пётр'):
            with self.assertRaises(ValueError):
                NemotronSemanticPass.locate_quote(quote, 'Мария пришла. Мария ушла.')

    def test_presence_quote_cannot_borrow_a_neighbouring_event(self):
        story = 'Мария вошла. Александр вышел.'
        with self.assertRaises(ValueError):
            NemotronSemanticPass.locate_quote('Александр', story, 0, 12)

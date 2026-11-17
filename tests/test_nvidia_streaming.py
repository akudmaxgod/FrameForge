import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from imagegen.nvidia_text import NvidiaStoryClient, NvidiaAPIError
from imagegen.analyzer_v3.semantic_first import SemanticFirstPass


class Stream(io.BytesIO):
    headers = {'Content-Type':'text/event-stream; charset=utf-8'}


def streaming_response(finish='stop', usage=True):
    chunks = [
        {'choices':[{'delta':{'content':'{"имя":'},'finish_reason':None}]},
        {'choices':[{'delta':{'content':'"Мария"}'},'finish_reason':finish}]},
    ]
    if usage:
        chunks.append({'choices':[],'usage':{'prompt_tokens':40,'completion_tokens':8,'total_tokens':48}})
    data = b': heartbeat\n\n' + b''.join(b'data: '+json.dumps(c,ensure_ascii=False).encode()+b'\n\n' for c in chunks)
    return Stream(data+b'data: [DONE]\n\n')


class StreamingTests(unittest.TestCase):
    def test_stream_is_assembled_and_usage_preserved(self):
        client = NvidiaStoryClient('nvapi-test')
        with patch('urllib.request.urlopen',return_value=streaming_response()) as send:
            result = client._post_json({'messages':[]})
        self.assertEqual(json.loads(result['choices'][0]['message']['content']), {'имя':'Мария'})
        self.assertEqual(client.get_usage()['total_tokens'],48)
        self.assertTrue(json.loads(send.call_args.args[0].data)['stream'])

    def test_timeout_retries_once_then_succeeds(self):
        waits=[]
        client=NvidiaStoryClient('nvapi-test',retry_wait=waits.append)
        with patch('urllib.request.urlopen',side_effect=[TimeoutError(),streaming_response()]) as send:
            client._post_json({})
        self.assertEqual(send.call_count,2)
        self.assertEqual(waits,[2])

    def test_repeated_timeout_is_clear_and_bounded(self):
        client=NvidiaStoryClient('nvapi-test',retry_wait=lambda _:None)
        with patch('urllib.request.urlopen',side_effect=TimeoutError()) as send:
            with self.assertRaisesRegex(NvidiaAPIError,'время ожидания'):
                client._post_json({})
        self.assertEqual(send.call_count,2)

    def test_stream_cutoff_never_becomes_cached_success(self):
        with tempfile.TemporaryDirectory() as directory:
            client=NvidiaStoryClient('nvapi-test',cache_dir=directory,retry_wait=lambda _:None)
            with patch('urllib.request.urlopen',side_effect=[streaming_response(None),streaming_response(None)]):
                with self.assertRaises(NvidiaAPIError):
                    client._post_json({})
            self.assertEqual(list(Path(directory).glob('*.json')),[])

    def test_existing_nonstream_cache_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            client=NvidiaStoryClient('nvapi-test',cache_dir=directory)
            body={'model':client.model,'stream':False,'max_tokens':8192,
                  'chat_template_kwargs':{'enable_thinking':False},'messages':[]}
            import hashlib
            key=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            saved={'choices':[{'finish_reason':'stop','message':{'content':'{"cached":true}'}}]}
            (Path(directory)/(key+'.json')).write_text(json.dumps(saved))
            with patch('urllib.request.urlopen') as send:
                result=client._post_json({})
            send.assert_not_called()
            self.assertEqual(result,saved)

    def test_network_error_is_not_reported_as_source_validation_failure(self):
        client=NvidiaStoryClient('nvapi-test')
        from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
        doc=StoryAnalyzerV3Pipeline().analyze_structural('Мария вошла в дом.')
        with patch.object(client,'_post_json',side_effect=NvidiaAPIError('Сбой связи')):
            with self.assertRaisesRegex(NvidiaAPIError,'Сбой связи'):
                SemanticFirstPass().run(doc,'Мария вошла в дом.',client)

    def test_stream_has_finite_total_deadline(self):
        with self.assertRaises(TimeoutError):
            NvidiaStoryClient('nvapi-test')._read_completion(streaming_response(),time.monotonic()-241)

    def test_rejected_semantic_answer_is_not_replayed_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            client=NvidiaStoryClient('nvapi-test',cache_dir=directory)
            with patch('urllib.request.urlopen',return_value=streaming_response()):
                client._post_json({})
            self.assertEqual(len(list(Path(directory).glob('*.json'))),1)
            client.discard_last_response()
            self.assertEqual(list(Path(directory).glob('*.json')),[])

    def test_nvidia_does_not_premerge_groups_and_named_researchers(self):
        from imagegen.analyzer_v3.pipeline import StoryAnalyzerV3Pipeline
        pipeline=StoryAnalyzerV3Pipeline.from_settings({'text_backend':'nvidia','nvidia_api_key':'nvapi-test'})
        with patch.object(pipeline,'_consolidate_grounded_role_aliases') as merge:
            with patch.object(pipeline.semantic_first_pass,'run',side_effect=NvidiaAPIError('capture')):
                with self.assertRaisesRegex(NvidiaAPIError,'capture'):
                    pipeline.analyze_structural('Археологи нашли бассейн. Ли-Энн Бедал изучила каналы.')
            merge.assert_not_called()

    def test_full_sentence_evidence_is_narrowed_to_complete_current_event(self):
        from imagegen.nvidia_text import NemotronSemanticPass
        from imagegen.analyzer_v3.models import EventIR,SourceSpan
        story='Он вошёл, а она осталась.'
        event=EventIR('e','s',SourceSpan(0,9,'Он вошёл,'))
        self.assertEqual(NemotronSemanticPass.locate_presence_quote(story,story,event),event.span.to_dict())
        with self.assertRaises(ValueError):
            NemotronSemanticPass.locate_presence_quote('она осталась',story,event)

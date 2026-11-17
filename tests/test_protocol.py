"""Protocol fixtures are test-only. These tests do not measure model/image quality."""
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from PIL import Image
from imagegen.db import dump, now
from imagegen.providers import ComfyProvider, ExecutionError, JsonHTTP, ProviderError, build_workflow
from imagegen.service import Service
from imagegen.worker import Worker


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.submits = []
        self.pending = {}
        self.histories = {}
        self.failure = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, value, status=200, binary=False):
                body = value if binary else json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if outer.failure:
                    self.respond({'error':'test transport failure'},503)
                elif self.path == '/api/ps':
                    self.respond({'models':[]})
                elif self.path == '/system_stats':
                    self.respond({'system':{'comfyui_version':'test-fixture'},'devices':[]})
                elif self.path == '/queue':
                    self.respond({'queue_running':list(outer.pending.values()),'queue_pending':[]})
                elif self.path.startswith('/history/'):
                    key=self.path.split('/')[-1]
                    self.respond({key:outer.histories[key]} if key in outer.histories else {})
                elif self.path == '/history':
                    self.respond(outer.histories)
                elif self.path.startswith('/view?'):
                    b=io.BytesIO();Image.new('RGB',(1024,576),'navy').save(b,format='PNG');self.respond(b.getvalue(),binary=True)
                else:
                    self.respond({'error':'unknown'},404)

            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/prompt':
                    key='prompt-'+str(len(outer.submits)+1)
                    outer.submits.append(payload)
                    outer.pending[key]=[0,key,payload['prompt'],payload['extra_data'],['12']]
                    self.respond({'prompt_id':key})
                elif self.path == '/interrupt':
                    outer.pending.clear()
                    self.respond({})
                else:
                    self.respond({'error':'unknown'},404)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.service.save_settings({'comfy_url':self.base,'ollama_url':self.base,
            'unet':'flux-2-klein-4b.gguf','clip':'qwen3-4b.gguf','vae':'flux2-vae.safetensors'})
        project=self.service.create_project({'title':'Protocol','story':'The captain arrives.','style':'painting'})
        self.project=self.service.import_analysis(project['id'],{'characters':[],'scenes':[
            {'beat':'Captain arrives','source_quote':'The captain arrives.','characters':[],'wardrobe':{}}]},1)
        self.service.batch(project['id'],'once')
        self.worker=Worker(self.service)
        self.provider=ComfyProvider(self.base)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def generation(self):
        return self.service.db.one('SELECT * FROM generations ORDER BY rowid LIMIT 1')

    def submit(self):
        # Graph validation has separate schema contract tests; this server fixture exercises execution protocol.
        with patch.object(ComfyProvider,'validate',return_value=None):
            self.worker.tick()
        self.assertEqual('running',self.generation()['state'])

    def finish_remote(self, error=False):
        generation=self.generation()
        key=generation['provider_id']
        row=self.pending.pop(key)
        self.histories[key]={'prompt':row,'status':{'completed':True,'status_str':'error' if error else 'success',
            'messages':[['execution_error',{'exception_message':'fixture OOM'}]] if error else []},
            'outputs':{} if error else {'12':{'images':[{'filename':'test.png','subfolder':'','type':'output'}]}}}

    def test_submit_poll_fetch_and_select(self):
        self.submit()
        self.worker.tick()
        self.assertEqual('running',self.generation()['state'])
        self.finish_remote()
        self.worker.tick()
        generation=self.generation()
        self.assertEqual('succeeded',generation['state'])
        self.assertEqual(1,len(self.submits))
        self.assertIsNone(self.service.scene(generation['scene_id'])['active_generation_id'])
        self.service.select_generation(generation['id'])
        self.assertTrue(self.service.export(self.project['id']).startswith(b'PK'))

    def test_execution_error_is_terminal_not_transport_retry(self):
        self.submit();self.finish_remote(error=True);self.worker.tick()
        self.assertEqual('failed',self.generation()['state'])
        self.assertIn('fixture OOM',self.generation()['error'])
        self.worker.tick()
        self.assertEqual(1,len(self.submits))

    def test_connection_loss_does_not_resubmit(self):
        self.submit();self.failure=True;self.worker.tick()
        self.assertEqual('reconciling',self.generation()['state'])
        self.assertEqual(1,len(self.submits))
        self.failure=False;self.worker.tick()
        self.assertEqual('running',self.generation()['state'])
        self.assertEqual(1,len(self.submits))

    def test_restart_with_missing_provider_id_reconciles(self):
        self.submit()
        with self.service.db.transaction() as c:
            c.execute("UPDATE generations SET state='reconciling',provider_id=NULL")
        self.worker=Worker(self.service)
        self.worker.tick()
        self.assertEqual('prompt-1',self.generation()['provider_id'])
        self.assertEqual(1,len(self.submits))

    def test_running_cancel_waits_for_backend(self):
        self.submit();self.service.cancel_generation(self.generation()['id']);self.worker.tick()
        self.assertEqual('cancelled',self.generation()['state'])
        self.assertFalse(self.pending)

    def test_timeout_interrupt_and_resource_state(self):
        self.submit()
        with self.service.db.transaction() as c:
            c.execute("UPDATE generations SET started_at='2020-01-01T00:00:00+00:00'")
        self.worker.tick();self.worker.tick()
        self.assertEqual('resource_degraded',self.generation()['state'])
        self.assertFalse(self.pending)

    def test_missing_job_fails_without_blind_retry(self):
        self.submit();self.pending.clear();self.worker.tick()
        self.assertEqual('failed',self.generation()['state'])
        self.assertEqual(1,len(self.submits))

    def test_owned_backend_restart_gets_one_bounded_retry(self):
        class Runtime:
            def ensure_comfy(self, settings):
                pass
        self.worker.runtime = Runtime()
        self.submit();self.pending.clear();self.worker.tick()
        self.assertEqual('queued',self.generation()['state'])
        with patch.object(ComfyProvider,'validate',return_value=None):
            self.worker.tick()
        self.assertEqual('running',self.generation()['state'])
        self.assertEqual(2,len(self.submits))
        self.pending.clear();self.worker.tick()
        self.assertEqual('failed',self.generation()['state'])
        self.assertEqual(2,len(self.submits))

    def test_schema_rejects_unknown_input(self):
        provider=ComfyProvider(self.base)
        schema={'CLIPLoaderGGUF':{'input':{'required':{'clip_name':[['qwen.gguf']], 'type':[['flux2']]}}}}
        with patch.object(provider.http,'json',return_value=schema):
            with self.assertRaises(ProviderError):
                provider.validate({'1':{'class_type':'CLIPLoaderGGUF','inputs':{'clip_name':'qwen.gguf','type':'flux2','device':'cpu'}}})


if __name__=='__main__':
    unittest.main()

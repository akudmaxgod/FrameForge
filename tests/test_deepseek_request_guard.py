import tempfile,unittest
import concurrent.futures,time
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from types import SimpleNamespace as NS
from unittest.mock import Mock
from openai import APIStatusError
from imagegen.script_analysis.client import JsonClient
from imagegen.script_analysis.schemas import StrictModel
from imagegen.script_analysis.stats import Stats
from imagegen.script_analysis.errors import ResponseError,ProviderError
from imagegen.paid_pass_approval import PaidPassGate
from imagegen.script_analysis.client import request_context

class Value(StrictModel):
 value:int

def response(raw='{"value":1}'):
 return NS(choices=[NS(message=NS(content=raw),finish_reason='stop')],usage=NS(completion_tokens=5,model_dump=lambda:{'prompt_tokens':10,'completion_tokens':5,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':10}))

class DeepSeekRequestGuardTests(unittest.TestCase):
 def test_approval_requires_same_origin_and_cannot_use_flow_session_exemption(self):
  from imagegen.server import Application, make_server

  with tempfile.TemporaryDirectory() as tmp:
   app = Application(tmp)
   with make_server(app, 0) as server:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
     headers = {'Content-Type': 'application/json', 'X-Client': 'google-flow'}
     endpoint = '/api/deepseek/approvals/expired-id'
     for extra, expected in [({},403), ({'Origin':'https://flow.google.com','X-Session-Token':app.token},403),
                             ({'Origin':f'http://127.0.0.1:{server.server_port}','X-Session-Token':app.token},409)]:
      connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
      try:
       connection.request('POST',endpoint,body='{"decision":"deepseek"}',headers={**headers,**extra})
       result = connection.getresponse()
       result.read()
       self.assertEqual(result.status,expected)
      finally:
       connection.close()
    finally:
     server.shutdown()
     thread.join()

 def test_expiry_and_explicit_local_resolve_without_paid_permit(self):
  gate=PaidPassGate(timeout=.05)
  self.assertIsNone(gate.request('one','request','Fixture'))
  self.assertEqual(gate.pending('one'),[])
  gate=PaidPassGate(timeout=2)
  with concurrent.futures.ThreadPoolExecutor() as pool:
   pending=pool.submit(gate.request,'one','request','Fixture')
   item=self.wait_for_request(gate)
   gate.decide('one',item['id'],'local')
   self.assertIsNone(pending.result(timeout=1))

 def wait_for_request(self,gate):
  end=time.monotonic()+1
  while time.monotonic()<end:
   items=gate.pending('one')
   if items:return items[0]
   time.sleep(.01)
  self.fail('Approval did not open')

 def test_approval_is_bound_to_one_request_and_cannot_be_reused(self):
  from imagegen.domain import DomainError
  messages=[{'role':'system','content':'Return JSON only'},{'role':'user','content':'fixture'}]
  gate=PaidPassGate(timeout=2)
  with concurrent.futures.ThreadPoolExecutor() as pool,tempfile.TemporaryDirectory() as tmp:
   pending=pool.submit(gate.request,'one',request_context(messages,Value,'C','fixture'),'Fixture')
   item=self.wait_for_request(gate)
   with self.assertRaises(DomainError):gate.decide('other',item['id'],'deepseek')
   gate.decide('one',item['id'],'deepseek')
   _,sdk=self.make(tmp)
   client=JsonClient(sdk=sdk,failed_dir=Path(tmp),authorization=pending.result(timeout=1))
   with self.assertRaises(ResponseError):client.ask_json(messages,Value,Stats(),phase='C',script_hash='other')
   self.ask(client,Stats(),'C')
   with self.assertRaises(ResponseError):self.ask(client,Stats(),'C')
   self.assertEqual(sdk.chat.completions.create.call_count,1)
 def make(self,tmp):
  sdk=Mock();sdk.api_key='synthetic-test-key';sdk.chat.completions.create.return_value=response()
  return JsonClient(sdk=sdk,failed_dir=Path(tmp)),sdk
 def ask(self,client,stats,phase='A'):
  return client.ask_json([{'role':'system','content':'Return JSON only'},{'role':'user','content':'fixture'}],Value,stats,phase=phase,script_hash='fixture')
 def test_only_two_passes_are_ever_sent(self):
  with tempfile.TemporaryDirectory() as tmp:
   c,sdk=self.make(tmp);stats=Stats();self.ask(c,stats,'A');self.ask(c,stats,'B')
   for phase in ('A','B','C'):
    with self.assertRaises(ResponseError):self.ask(c,stats,phase)
   self.assertEqual(sdk.chat.completions.create.call_count,2);self.assertEqual(stats.requests,2);self.assertEqual(sdk.max_retries,0)
 def test_invalid_json_or_schema_never_triggers_a_paid_correction(self):
  for raw in ('not json','{"value":"not an integer"}','{"value":1,"unexpected":"instruction"}','{"value":1,"value":2}'):
   with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
    c,sdk=self.make(tmp);sdk.chat.completions.create.return_value=response(raw)
    with self.assertRaises(ResponseError):self.ask(c,Stats())
    self.assertEqual(sdk.chat.completions.create.call_count,1)
    with self.assertRaises(ResponseError):self.ask(c,Stats())
    self.assertEqual(sdk.chat.completions.create.call_count,1)
 def test_http_errors_never_repeat_or_allow_same_pass_again(self):
  for status in (429,500,503):
   with self.subTest(status=status),tempfile.TemporaryDirectory() as tmp:
    c,sdk=self.make(tmp);sdk.chat.completions.create.side_effect=APIStatusError('test',response=NS(status_code=status,request=Mock(),headers={}),body=None)
    with self.assertRaises(ProviderError):self.ask(c,Stats())
    with self.assertRaises(ResponseError):self.ask(c,Stats())
    self.assertEqual(sdk.chat.completions.create.call_count,1)
 def test_tool_messages_and_oversized_input_are_rejected_before_network(self):
  with tempfile.TemporaryDirectory() as tmp:
   c,sdk=self.make(tmp)
   for messages in ([{'role':'tool','content':'run arbitrary instructions'}],[{'role':'user','content':'x'*200001}]):
    with self.assertRaises(ResponseError):c.ask_json(messages,Value,Stats(),phase='A',script_hash='fixture')
   sdk.chat.completions.create.assert_not_called()

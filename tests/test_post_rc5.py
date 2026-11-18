import copy,json,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from imagegen.db import uid,now,dump
from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.export_worker import ExportWorker
from imagegen.server import Application,make_server
from imagegen.worker import Worker
from imagegen.project_updates import versions,poll

class PostRc5Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.service=Service(self.temp.name);self.project=self.service.create_project({'title':'Guard','story':'A test scene.','style':'test'})
  self.pid=self.project['id']
 def job(self,status='running',service=None):
  service=service or self.service;jid=uid()
  with service.db.transaction() as c:
   c.execute('INSERT INTO edit_exports(id,project_id,edit_revision,snapshot,status,progress,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(jid,self.pid,1,'{}',status,42,now(),now()))
  return jid
 def test_shutdown_refuses_queued_and_running_and_keeps_admission_open(self):
  worker=ExportWorker(self.service)
  for status in ('queued','running'):
   jid=self.job(status)
   with self.assertRaises(DomainError) as caught:worker.begin_shutdown()
   self.assertEqual(caught.exception.status,409);self.assertEqual(caught.exception.details['code'],'EXPORT_IN_PROGRESS')
   self.assertEqual(caught.exception.details['exports'][0]['progress'],42);self.assertFalse(worker.stopping)
   with self.service.db.transaction() as c:c.execute("UPDATE edit_exports SET status='completed' WHERE id=?",(jid,))
  worker.begin_shutdown();self.assertTrue(worker.stopping)
  with self.assertRaises(DomainError) as caught:worker.start(self.pid,{})
  self.assertEqual(caught.exception.details['code'],'APPLICATION_STOPPING')
 def test_shutdown_wins_against_a_start_still_validating(self):
  worker=ExportWorker(self.service);entered=threading.Event();release=threading.Event();errors=[]
  document={'schema_version':1,'clips':[{'edit_clip_id':'one','scene_id':'one','media_type':'image','source_duration_ms':1000,'trim_in_ms':0,'trim_out_ms':1000}], 'transitions':[],'audio_tracks':[],'export_settings':{'width':1280,'height':720,'fps':24}}
  edit={'revision':1,'document':document,'sources':{'one':{'missing':False,'stale':False}}}
  def validate(*args):entered.set();self.assertTrue(release.wait(5));return document
  def start():
   try:worker.start(self.pid,{'expected_revision':1})
   except Exception as e:errors.append(e)
  with patch.object(self.service.editing,'get',return_value=edit),patch.object(self.service.editing,'validate',side_effect=validate),patch('imagegen.export_worker.av.Codec'),patch('imagegen.export_preflight.require_ready'):
   thread=threading.Thread(target=start);thread.start();self.assertTrue(entered.wait(5));worker.begin_shutdown();release.set();thread.join(5)
  self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1);self.assertEqual(errors[0].details['code'],'APPLICATION_STOPPING')
  self.assertEqual(self.service.db.all('SELECT id FROM edit_exports'),[])
 def test_shutdown_http_returns_progress_before_allowing_stop(self):
  app=Application(Path(self.temp.name));jid=self.job(service=app.service);server=make_server(app,0)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  base='http://127.0.0.1:'+str(server.server_port)
  request=lambda:urllib.request.Request(base+'/api/shutdown',data=b'{}',headers={'X-Session-Token':app.token,'Content-Type':'application/json'})
  try:
   with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request())
   self.assertEqual(caught.exception.code,409);body=json.load(caught.exception);self.assertEqual(body['code'],'EXPORT_IN_PROGRESS');self.assertEqual(body['exports'][0]['progress'],42)
   self.assertEqual(urllib.request.urlopen(base+'/api/session').status,200)
   with app.service.db.transaction() as c:c.execute("UPDATE edit_exports SET status='completed' WHERE id=?",(jid,))
   self.assertEqual(urllib.request.urlopen(request()).status,200);thread.join(5);self.assertFalse(thread.is_alive())
  finally:server.shutdown();server.server_close()
 def test_unchanged_poll_is_tiny(self):
  cursor=versions(self.service,self.pid);result=poll(self.service,self.pid,**cursor)
  self.assertEqual(result,cursor);self.assertLess(len(json.dumps(result)),65)
 def test_progress_patch_has_no_snapshot_and_terminal_state_invalidates_content(self):
  gid=uid()
  with self.service.db.transaction() as c:c.execute("INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,created_at) VALUES(?,?,'analysis','running',?,0,?)",(gid,self.pid,dump({'large':'x'*500000}),now()))
  before=versions(self.service,self.pid)
  with self.service.db.transaction() as c:c.execute('UPDATE generations SET progress=? WHERE id=?',(dump({'percent':37}),gid))
  result=poll(self.service,self.pid,**before);self.assertEqual(result['content'],before['content']);self.assertGreater(result['progress'],before['progress']);self.assertEqual(result['generations'][0]['progress'],{'percent':37});self.assertLess(len(json.dumps(result)),1500)
  cursor={k:result[k] for k in ('content','progress')}
  with self.service.db.transaction() as c:c.execute("UPDATE generations SET state='succeeded' WHERE id=?",(gid,))
  result=poll(self.service,self.pid,**cursor);self.assertGreater(result['content'],cursor['content']);self.assertNotIn('generations',result)
 def test_noop_and_rollback_do_not_invalidate_clients(self):
  before=versions(self.service,self.pid)
  with self.service.db.transaction() as c:c.execute('UPDATE projects SET title=title WHERE id=?',(self.pid,))
  self.assertEqual(versions(self.service,self.pid),before)
  try:
   with self.service.db.transaction() as c:c.execute("UPDATE projects SET title='changed' WHERE id=?",(self.pid,));raise RuntimeError('rollback')
  except RuntimeError:pass
  self.assertEqual(versions(self.service,self.pid),before)
 def test_indexes_are_installed(self):
  indexes={r['name'] for r in self.service.db.all("SELECT name FROM sqlite_master WHERE type='index'")}
  self.assertTrue({'generations_project_created','generations_video_asset'}<=indexes)
 def test_shared_channel_edit_invalidates_only_linked_projects(self):
  cid=uid()
  with self.service.db.transaction() as c:
   c.execute('INSERT INTO channel_profiles(id,name,created_at,updated_at) VALUES(?,?,?,?)',(cid,'Channel',now(),now()))
   c.execute('UPDATE projects SET channel_profile_id=? WHERE id=?',(cid,self.pid))
  other=self.service.create_project({'title':'Other','story':'A different scene.','style':'test'})['id']
  before=versions(self.service,self.pid);untouched=versions(self.service,other)
  with self.service.db.transaction() as c:c.execute("UPDATE channel_profiles SET name='Changed' WHERE id=?",(cid,))
  self.assertGreater(versions(self.service,self.pid)['content'],before['content']);self.assertEqual(versions(self.service,other),untouched)
 def test_architectural_separator_is_advisory_only_with_source_evidence(self):
  worker=Worker.__new__(Worker);image=Image.new('RGB',(1024,576));image.info['_frameforge_separator']={'color':[255,255,255],'variation':0};snapshot={'scene':{'source_quote':'За оконными переплётами виден двор.'}}
  with patch.object(worker,'_has_panel_grid',return_value=True):
   self.assertFalse(worker._panel_layout_requires_rejection(snapshot,image));self.assertEqual(snapshot['panel_layout_review']['status'],'needs_review')
   self.assertTrue(worker._panel_layout_requires_rejection({'prompt':'add windows'},image))
   self.assertTrue(worker._panel_layout_requires_rejection({},image))
 def test_dark_window_crossbars_are_not_paid_retry_reasons_without_text_hints(self):
  from PIL import ImageDraw
  worker=Worker.__new__(Worker)
  for axis in ('vertical','horizontal'):
   image=Image.new('RGB',(1024,576),'#31597a');draw=ImageDraw.Draw(image)
   if axis=='vertical':draw.rectangle((515,0,1023,575),fill='#806040');draw.rectangle((509,0,514,575),fill='#101010')
   else:draw.rectangle((0,291,1023,575),fill='#806040');draw.rectangle((0,285,1023,290),fill='#101010')
   self.assertTrue(worker._has_panel_grid(image));self.assertFalse(worker._panel_layout_requires_rejection({},image))
 def test_explicit_white_contact_sheet_gutter_remains_rejected(self):
  from PIL import ImageDraw
  worker=Worker.__new__(Worker);image=Image.new('RGB',(1024,576),'#31597a');draw=ImageDraw.Draw(image);draw.rectangle((515,0,1023,575),fill='#806040');draw.rectangle((509,0,514,575),fill='white')
  self.assertTrue(worker._panel_layout_requires_rejection({},image))
 def test_manual_import_window_prompt_is_source_evidence(self):
  worker=Worker.__new__(Worker);snapshot={'scene':{'manual_prompt':'View through a window with dark mullions.'}}
  with patch.object(worker,'_has_panel_grid',return_value=True):self.assertFalse(worker._panel_layout_requires_rejection(snapshot,Image.new('RGB',(10,10))))

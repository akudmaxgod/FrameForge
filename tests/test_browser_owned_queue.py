import sqlite3,json,unittest,sys
from contextlib import contextmanager
from unittest.mock import Mock,patch
from pathlib import Path
from imagegen.worker import Worker

class DB:
    def __init__(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript('CREATE TABLE generations(id,project_id,purpose,state,snapshot,created_at,cancel_requested DEFAULT 0,batch_id,started_at,attempts DEFAULT 0,progress,error,finished_at); CREATE TABLE batches(id,state);')
    def one(self,q):
        r=self.c.execute(q).fetchone();return dict(r) if r else None
    @contextmanager
    def transaction(self):
        yield self.c;self.c.commit()

class BrowserQueueTests(unittest.TestCase):
    def setUp(self):
        self.w=Worker.__new__(Worker);self.w.db=DB();self.w.service=Mock();self.w.service.settings.return_value={'image_backend':'flow'}
        self.w.refresh_batches=Mock();self.w.resume_analysis_review=Mock(return_value=False);self.w.resume_prompt_review=Mock(return_value=False)
        self.w.prepare=Mock();self.w.runtime=Mock();self.w.provider_factory=Mock();self.w.finish=Mock()
    def add(self,id,purpose,state,snapshot):
        self.w.db.c.execute('INSERT INTO generations(id,project_id,purpose,state,snapshot,created_at) VALUES(?,?,?,?,?,?)',(id,'p',purpose,state,json.dumps(snapshot),id))
    def test_browser_portrait_does_not_block_analysis(self):
        self.add('1','casting','running',{'flow_request_id':'request'});self.add('2','analysis','queued',{})
        self.w.tick()
        self.assertEqual(self.w.prepare.call_args.args[0]['id'],'2')
        self.assertEqual(self.w.db.one("SELECT state FROM generations WHERE id='1'")['state'],'running')
        self.w.runtime.ensure_comfy.assert_not_called()
    def test_direct_monitor_never_sends_flow_to_comfy(self):
        self.w.monitor({'purpose':'casting','snapshot':json.dumps({'flow_request_id':'request'})})
        self.w.runtime.ensure_comfy.assert_not_called();self.w.provider_factory.assert_not_called()
    def test_local_job_still_monitored(self):
        self.add('1','casting','running',{});self.w.monitor=Mock();self.w.tick()
        self.assertEqual(self.w.monitor.call_args.args[0]['id'],'1');self.w.prepare.assert_not_called()
    def test_shutdown_recovery_preserves_browser_request(self):
        self.add('1','casting','running',{'flow_request_id':'request'});self.add('2','casting','running',{})
        self.w.recover_after_unexpected_shutdown()
        self.assertEqual(self.w.db.one("SELECT state FROM generations WHERE id='1'")['state'],'running')
        self.assertEqual(self.w.db.one("SELECT state FROM generations WHERE id='2'")['state'],'resource_degraded')
    def test_normal_restart_preserves_browser_request(self):
        self.add('1','casting','running',{'flow_request_id':'request'});self.add('2','casting','running',{})
        self.w.reconcile_cancelled_visual_reviews=Mock()
        with patch('imagegen.worker.threading.Thread'):
            self.w.start()
        self.assertEqual(self.w.db.one("SELECT state FROM generations WHERE id='1'")['state'],'running')
        self.assertEqual(self.w.db.one("SELECT state FROM generations WHERE id='2'")['state'],'reconciling')

if __name__=='__main__':unittest.main()


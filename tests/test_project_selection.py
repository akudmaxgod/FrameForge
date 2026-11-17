import json
import tempfile
import time
import unittest
from imagegen.server import Application
from imagegen.domain import DomainError
from imagegen.prompt_import import create


class ProjectSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Application(self.temp.name);self.service=self.app.service
        self.a=create(self.service,{'title':'Demo','story':'Корабль стоит.', 'prompts_text':'A rusty ship on sand.'})['id']
        self.b=create(self.service,{'title':'Working','story':'Судно стоит.', 'prompts_text':'A fishing boat on sand.'})['id']
        self.app.dispatch('POST','/api/projects/current',{'project_id':self.b})

    def get(self,path):return self.app.dispatch('GET','/api'+path,None)

    def test_background_reads_do_not_steal_active_project(self):
        for path in (f'/projects/{self.a}',f'/projects/{self.a}/prompts',
                     f'/projects/{self.a}/preflight',f'/projects/{self.a}/source-review-status'):
            self.get(path)
            self.assertEqual(self.get('/projects/current')['project_id'],self.b)

    def test_alternating_two_tab_polling_keeps_explicit_selection(self):
        for pid in (self.a,self.b,self.a,self.b,self.a):
            self.get('/projects/'+pid)
        self.assertEqual(self.get('/projects/current')['project_id'],self.b)
        self.app.dispatch('POST','/api/projects/current',{'project_id':self.a})
        self.get('/projects/'+self.b)
        self.assertEqual(self.get('/projects/current')['project_id'],self.a)

    def test_invalid_selection_preserves_current_project(self):
        for value in (None,'','unknown',3):
            with self.subTest(value=value),self.assertRaises(DomainError):
                self.app.dispatch('POST','/api/projects/current',{'project_id':value})
            self.assertEqual(self.get('/projects/current')['project_id'],self.b)

    def test_initial_fallback_is_stable_after_first_read(self):
        self.service.active_project_id=None
        picked=self.get('/projects/current')['project_id']
        self.get('/projects/'+self.a)
        self.assertEqual(self.get('/projects/current')['project_id'],picked)

    def test_new_project_explicitly_becomes_active(self):
        result=self.app.dispatch('POST','/api/projects',{'title':'New','story':'Корабль ждёт.'})
        self.assertEqual(self.get('/projects/current')['project_id'],result['id'])
        self.get('/projects/'+self.a)
        self.assertEqual(self.get('/projects/current')['project_id'],result['id'])

    def test_import_creation_also_selects_its_new_project(self):
        result=self.app.dispatch('POST','/api/projects/from-prompts',{'title':'Import','story':'Корабль.', 'prompts_text':'A ship.'})
        self.assertEqual(self.get('/projects/current')['project_id'],result['id'])

    def test_reads_do_not_modify_jobs_admissions_or_assets(self):
        tables=('projects','scenes','scene_revisions','generations','assets')
        before={t:self.service.db.all('SELECT * FROM '+t+' ORDER BY rowid') for t in tables}
        for _ in range(3):self.get('/projects/'+self.a);self.get('/projects/'+self.b)
        self.assertEqual(before,{t:self.service.db.all('SELECT * FROM '+t+' ORDER BY rowid') for t in tables})

    def test_background_read_does_not_break_bridge_start_or_retarget_command(self):
        self.app.flow_bridge_v1.update(last_seen=time.time(),adapter_connected=True,
            adapter_instance_id='fixture',project_id=self.b,scoped_scene_start=True,quota_admission=True)
        self.get('/projects/'+self.a)
        self.app.dispatch('POST','/api/bridge/v1/command',{'action':'start','project_id':self.b})
        self.assertEqual(self.app.flow_bridge_v1['command']['project_id'],self.b)
        self.assertEqual(self.service.db.all('SELECT * FROM generations'),[])

    def test_explicit_switch_keeps_foreign_project_guard(self):
        self.app.flow_bridge_v1.update(last_seen=time.time(),adapter_connected=True,
            adapter_instance_id='fixture',project_id=self.b,scoped_scene_start=True)
        self.app.dispatch('POST','/api/projects/current',{'project_id':self.a})
        with self.assertRaises(DomainError):
            self.app.dispatch('POST','/api/bridge/v1/command',{'action':'start','project_id':self.b})
        self.assertIsNone(self.app.flow_bridge_v1['command'])

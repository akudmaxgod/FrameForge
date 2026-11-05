import tempfile
import unittest
from pathlib import Path
from imagegen.flow_admission import FlowAdmission
from imagegen.domain import DomainError

class Service:
    def __init__(self,root): self.root=Path(root);self.revision=1
    def settings(self): return {'google_queue_enabled':True}
    def scene(self,sid): return {'id':sid,'project_id':'project','current_revision':self.revision}
    def google_generation_capacity(self,pid): return {'pending':0}

class AdmissionReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=Service(self.temp.name);self.admission=FlowAdmission(self.service)
    def reserve(self,rid,sid='s'):
        with self.admission.db() as db:
            db.execute('INSERT INTO admissions VALUES(?,?,?,?,?,?)',(rid,'project',sid,1,'hash','reserved'))
    def states(self):
        with self.admission.db() as db: return dict(db.execute('SELECT request_id,state FROM admissions'))
    def test_specific_remote_failure_releases_only_that_request(self):
        self.reserve('rejected');self.reserve('unknown')
        self.assertEqual(self.admission.fail('s','rejected','FLOW_SERVICE_UNAVAILABLE')['state'],'failed')
        self.assertEqual(self.states(),{'rejected':'failed','unknown':'reserved'})
    def test_confirmed_codes_supported_but_generic_unknown_and_transport_rejected(self):
        for code in ('ERR_GEN_FAILED','COMPOSITION_REJECTED','FLOW_SERVICE_UNAVAILABLE','ERR_UNSUPPORTED_MODEL','ERR_TOO_MANY_REFS','FLOW_BUSY','PREFLIGHT_REJECTED','NOT_SENT'):
            self.reserve(code);self.admission.fail('s',code,code)
        for code in ('FLOW_UNKNOWN_ERROR','TIMEOUT','NETWORK_ERROR',None):
            self.reserve(str(code))
            with self.assertRaises(DomainError):self.admission.fail('s',str(code),code)
            self.assertEqual(self.states()[str(code)],'reserved')
    def test_repeated_ack_is_idempotent_and_survives_restart(self):
        self.reserve('one');self.admission.fail('s','one','FLOW_SERVICE_UNAVAILABLE')
        reopened=FlowAdmission(self.service)
        self.assertEqual(reopened.fail('s','one','FLOW_SERVICE_UNAVAILABLE')['state'],'failed')
    def test_no_reservation_is_safe_ack_without_creating_rows(self):
        self.assertEqual(self.admission.fail('s','absent','NOT_SENT')['state'],'not_reserved');self.assertEqual(self.states(),{})
    def test_confirmed_old_revision_closes_exact_old_attempt(self):
        self.reserve('old');self.service.revision=2
        self.assertEqual(self.admission.fail('s','old','FLOW_SERVICE_UNAVAILABLE')['state'],'failed')
    def test_wrong_scene_cannot_close_reservation(self):
        self.reserve('one')
        with self.assertRaises(DomainError):self.admission.fail('other','one','FLOW_SERVICE_UNAVAILABLE')
        self.assertEqual(self.states()['one'],'reserved')
    def test_completed_upload_cannot_be_reclassified_as_failed(self):
        self.reserve('one')
        with self.admission.db() as db:db.execute("UPDATE admissions SET state='uploaded' WHERE request_id='one'")
        with self.assertRaises(DomainError):self.admission.fail('s','one','FLOW_SERVICE_UNAVAILABLE')
        self.assertEqual(self.states()['one'],'uploaded')
    def test_already_rejected_upload_keeps_its_terminal_outcome(self):
        self.reserve('one');self.admission.finish('s','one','rejected')
        self.assertEqual(self.admission.fail('s','one','COMPOSITION_REJECTED')['state'],'rejected')

if __name__=='__main__':unittest.main()

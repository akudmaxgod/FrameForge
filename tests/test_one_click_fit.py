import copy
import time
import unittest
from unittest.mock import patch
import av
from imagegen.domain import DomainError
from imagegen.editing import normalize
from imagegen.export_worker import render
from imagegen.montage_voice_fit import propose_auto
from tests import test_montage_voice_fit as voice_tests
pure_fixture=voice_tests.pure_fixture
from tests import test_insert_coverage as coverage_tests

class AutomaticClockTests(unittest.TestCase):
    def test_shorten_extend_trim_offset_and_no_mutation(self):
        for target in (1200,8500):
            snap,_=pure_fixture();before=copy.deepcopy(snap)
            snap['audio_binding'].update(trim_in_ms=200,trim_out_ms=target,offset_ms=200)
            result=propose_auto(snap)
            self.assertTrue(result['can_apply'],result['conflicts'])
            self.assertAlmostEqual(result['timeline']['duration_ms'],target)
            self.assertEqual([c['edit_clip_id'] for c in result['document']['clips']],['0','1','2'])
            self.assertEqual(result['document']['audio_tracks'],before['document']['audio_tracks'])
            self.assertTrue(all(.8<=c.get('playback_rate',1)<=1 for c in result['document']['clips']))
    def test_stills_have_full_camera_time_without_freeze(self):
        snap,_=pure_fixture()
        for c in snap['document']['clips']:c.update(media_type='image',source_duration_ms=86400000,trim_out_ms=1000)
        r=propose_auto(snap)
        self.assertTrue(r['can_apply']);self.assertEqual([c['hold_last_ms'] for c in r['document']['clips']],[0,0,0])

class AutomaticIntegrationTests(unittest.TestCase):
    setUp=voice_tests.IntegrationTests.setUp
    # Inherit fixture only; load these named tests explicitly.
    def auto(self):
        job=self.manager.start(self.pid,{'expected_revision':self.editor.get(self.pid)['revision'],'timing_mode':'auto'})
        for _ in range(150):
            job=self.manager.get(self.pid,job['id'])
            if job['status'] not in ('queued','recognizing','ready'):break
            time.sleep(.02)
        self.assertEqual(job['status'],'applied',job)
        return job
    def test_one_click_reuse_rollback_and_later_edit_guard(self):
        before=self.editor.get(self.pid);job=self.auto();applied=self.editor.get(self.pid)
        self.assertEqual(self.auto()['id'],job['id']);self.assertEqual(self.editor.get(self.pid),applied)
        self.manager.rollback(self.pid,job['id'],{'expected_revision':applied['revision']})
        self.assertEqual(self.editor.get(self.pid)['document'],before['document'])
        job=self.auto();current=self.editor.get(self.pid);doc=copy.deepcopy(current['document']);doc['clips'][0]['framing']='fill'
        changed=self.editor.save(self.pid,{'expected_revision':current['revision'],'document':doc})
        with self.assertRaises(DomainError):self.manager.rollback(self.pid,job['id'],{'expected_revision':changed['revision']})
        self.assertEqual(self.editor.get(self.pid)['document'],changed['document'])
    def test_actual_mp4_both_directions(self):
        for target in (1200,4500):
            current=self.editor.get(self.pid);doc=copy.deepcopy(current['document']);doc['audio_tracks'][0].update(trim_in_ms=100,trim_out_ms=target,offset_ms=100)
            for c in doc['clips']:c.update(trim_out_ms=1000,hold_last_ms=0)
            self.editor.save(self.pid,{'expected_revision':current['revision'],'document':doc})
            job=self.auto();candidate=self.editor.get(self.pid)['document'];out=self.service.root/f'auto-{target}.mp4'
            render(self.service,candidate,out,lambda *a:None,lambda:False,size=(160,90))
            with av.open(str(out)) as media:
                self.assertLessEqual(abs(len(list(media.decode(video=0)))/24-target/1000),1/24)

if __name__=='__main__':unittest.main()



class PlacementContractTests(unittest.TestCase):
    setUp=coverage_tests.CoverageServiceTests.setUp
    record=coverage_tests.CoverageServiceTests.record
    def test_middle_end_no_zero_no_length_change_and_rollback(self):
        before=self.s.editing.get(self.pid)
        result=self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p3','p5'],coverage={'mode':'limited','percent':40,'distribution':'spread'})
        clips=result['document']['clips']
        self.assertEqual([i for i,c in enumerate(clips) if c.get('footage_overlay')],[3,5])
        self.assertEqual(normalize(result['document'])['duration_ms'],6000)
        self.assertEqual(result['document']['audio_tracks'],before['document']['audio_tracks'])
        restored=self.s.montage_inserts.rollback_proposal(self.pid,self.proposal_id)
        self.assertEqual(restored['document'],before['document'])
    def test_no_match_and_unapproved_results_leave_montage_unchanged(self):
        before=self.s.editing.get(self.pid)
        for status in ('rejected','needs_review'):
            choices=copy.deepcopy(self.choices)
            for p in choices:p['match_status']=status
            pid=self.record(choices)
            result=self.s.montage_inserts.apply_proposals(self.pid,pid,coverage={'mode':'limited','percent':40})
            self.assertEqual(result['applied_count'],0)
            self.assertEqual(self.s.editing.get(self.pid),before)
    def test_later_manual_overlay_edit_blocks_rollback(self):
        result=self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p3'],coverage={'mode':'limited','percent':40})
        doc=copy.deepcopy(result['document']);doc['clips'][3]['footage_overlay']['enabled']=False
        saved=self.s.editing.save(self.pid,{'expected_revision':result['new_revision'],'document':doc})
        with self.assertRaises(DomainError):self.s.montage_inserts.rollback_proposal(self.pid,self.proposal_id)
        self.assertEqual(self.s.editing.get(self.pid)['document'],saved['document'])

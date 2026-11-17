import io
import copy
import unittest
from PIL import Image
from imagegen.domain import DomainError
from imagegen.server import Application
from tests.test_forgotten_cities_contracts import TestForgottenCitiesChannelProfile

class StillApprovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture=TestForgottenCitiesChannelProfile();self.fixture.setUp()
        self.service=self.fixture.service;self.app=Application(self.fixture.temp_dir)
    def tearDown(self):
        self.fixture.doCleanups();self.fixture.tearDown()
    def project(self,count=1,variants=1):return self.fixture._project_with_rendered_scene(count,variants)
    def payload(self,pid):
        return {'scenes':[{'scene_id':s['id'],'revision':s['current_revision'],'generation_id':s['active_generation_id']} for s in self.service.detail(pid)['scenes']]}
    def add_variant(self,sid,color=(99,88,77)):
        buf=io.BytesIO();Image.new('RGB',(1280,720),color).save(buf,format='PNG')
        return self.service.upload_scene_image(sid,buf.getvalue())
    def test_one_variant_is_ready_without_creating_an_approval(self):
        pid,ids,gens=self.project()
        before=self.service.db.all('SELECT * FROM reviews')
        detail=self.service.detail(pid);approval=detail['scenes'][0]['still_approval']
        self.assertEqual(approval['status'],'pending_review')
        self.assertFalse(detail['still_approval_required'])
        self.assertEqual(before,self.service.db.all('SELECT * FROM reviews'))
        self.assertTrue(self.service.queue_single_scene_omni(ids[0])['queued'])
    def test_new_alternative_requires_choice_and_bulk_approves_only_selected(self):
        pid,ids,gens=self.project(3,2)
        detail=self.service.detail(pid);self.assertTrue(all(s['still_approval']['status']=='pending_review' for s in detail['scenes']))
        revisions={s['id']:s['current_revision'] for s in detail['scenes']}
        result=self.app.dispatch('POST',f'/api/projects/{pid}/stills/approve-all',self.payload(pid))
        self.assertEqual(set(result['approved']),set(ids));self.assertEqual(result['failed'],[])
        reviews=self.service.db.all("SELECT generation_id FROM reviews WHERE status='approved'")
        self.assertEqual({r['generation_id'] for r in reviews},{s['active_generation_id'] for s in detail['scenes']})
        after=self.service.detail(pid);self.assertTrue(all(s['still_approval']['status']=='approved' for s in after['scenes']))
        self.assertEqual(revisions,{s['id']:s['current_revision'] for s in after['scenes']})
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])
        again=self.service.approve_selected_stills(pid,self.payload(pid))
        self.assertEqual(again['approved'],[]);self.assertEqual(set(again['already_approved']),set(ids))
    def test_auto_approval_does_not_override_rejection_and_can_be_explicitly_approved(self):
        pid,ids,gens=self.project()
        self.service.review_generation(gens[0],'rejected')
        self.assertEqual(self.service.detail(pid)['scenes'][0]['still_approval']['status'],'rejected')
        self.assertEqual(self.service.approve_selected_stills(pid,self.payload(pid))['approved'],ids)
    def test_second_variant_disables_auto_but_failed_attempts_and_old_variants_do_not(self):
        pid,ids,gens=self.project();sid=ids[0]
        self.add_variant(sid)
        self.assertEqual(self.service.detail(pid)['scenes'][0]['still_approval']['status'],'pending_review')
        scene=self.service.scene(sid);data=copy.deepcopy(scene['data']);data['shot']+=' updated view'
        self.service.edit_scene(sid,data,scene['current_revision'])
        self.assertNotEqual(self.service.detail(pid)['scenes'][0]['still_approval']['status'],'approved')
        self.add_variant(sid,(22,33,44))
        self.assertFalse(self.service.detail(pid)['still_approval_required'])
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])
    def test_batch_view_conflict_is_atomic_and_foreign_scene_is_rejected(self):
        pid,ids,gens=self.project(2,2);payload=self.payload(pid)
        added=self.add_variant(ids[1]);self.service.select_generation(added['generation_id'])
        with self.assertRaises(DomainError) as err:self.service.approve_selected_stills(pid,payload)
        self.assertEqual(err.exception.status,409);self.assertEqual(self.service.db.all('SELECT * FROM reviews'),[])
        other_pid,_,_=self.project();foreign=self.payload(other_pid)
        with self.assertRaises(DomainError):self.service.approve_selected_stills(pid,foreign)
    def test_missing_image_is_pending_and_single_variants_are_skipped_in_batch(self):
        pid,ids,_=self.project(2)
        result=self.service.approve_selected_stills(pid,self.payload(pid))
        self.assertEqual(set(result['approved']),set(ids));self.assertEqual(result['already_approved'],[])
        with self.service.db.transaction() as conn:conn.execute('UPDATE scenes SET active_generation_id=NULL WHERE id=?',(ids[0],))
        self.assertNotEqual(self.service.detail(pid)['scenes'][0]['still_approval']['status'],'approved')
    def test_ready_still_is_returned_for_animation_without_queued_image_generation(self):
        pid,ids,_=self.project(2)
        self.assertEqual(self.service.project_prompts(pid)['scenes'],[])
        self.service.select_scene_animation_engine(ids[0],'omni_flash')
        self.service.start_animation_generation(pid,scene_ids=[ids[0]])
        jobs=self.service.animation_jobs(pid)['jobs']
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['scene_id'],ids[0]);self.assertEqual(jobs[0]['engine'],'omni_flash');self.assertTrue(jobs[0]['prompt']);self.assertTrue(jobs[0]['source_image_asset_id'])
        self.assertEqual(self.service.project_prompts(pid)['scenes'],[])
        self.assertEqual(self.service.db.all("SELECT id FROM generations WHERE purpose='scene' AND state='queued'"),[])
        self.service.select_scene_animation_engine(ids[1],'veo_lite')
        result=self.service.start_animation_generation(pid,scene_ids=[ids[1]])
        self.assertEqual(result['scenes'][1]['status'],'queued_veo')
        self.assertEqual({j['engine'] for j in self.service.animation_jobs(pid)['jobs']},{'omni_flash','veo_lite'})

    def test_missing_last_file_blocks_batch_before_any_animation_write(self):
        pid,ids,gens=self.project(3,2)
        for sid in ids:self.service.select_scene_animation_engine(sid,'omni_flash')
        generation=self.service.db.one('SELECT asset_id FROM generations WHERE id=?',(self.service.scene(ids[-1])['active_generation_id'],))
        asset=self.service.asset(generation['asset_id'])
        (self.service.root/asset['path']).unlink()
        before={sid:self.service.scene(sid) for sid in ids}
        generations=self.service.db.all('SELECT * FROM generations WHERE project_id=?',(pid,))
        with self.assertRaises(DomainError):self.service.start_animation_generation(pid)
        self.assertEqual(before,{sid:self.service.scene(sid) for sid in ids})
        self.assertEqual(generations,self.service.db.all('SELECT * FROM generations WHERE project_id=?',(pid,)))
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])

    def test_explicit_approved_subset_is_not_blocked_by_an_unselected_frame(self):
        pid,ids,gens=self.project(2,2)
        self.service.review_generation(gens[0],'approved')
        self.service.select_scene_animation_engine(ids[0],'omni_flash')
        before=self.service.scene(ids[1])
        result=self.service.start_animation_generation(pid,scene_ids=[ids[0]])
        self.assertEqual(result['total_scenes'],2)
        self.assertEqual(self.service.scene(ids[1]),before)
        jobs=self.service.db.all("SELECT scene_id FROM veo_jobs WHERE engine='omni_flash'")
        self.assertEqual([j['scene_id'] for j in jobs],[ids[0]])

    def test_unplanned_frame_does_not_require_approval(self):
        pid,ids,gens=self.project(2,2)
        self.service.start_animation_generation(pid)
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])

    def test_pending_and_historically_rejected_stills_queue_without_review_writes(self):
        pid,ids,gens=self.project(3,2)
        self.service.review_generation(gens[1],'rejected')
        before=self.service.db.all('SELECT * FROM reviews')
        for sid in ids:self.service.select_scene_animation_engine(sid,'omni_flash')
        self.service.start_animation_generation(pid)
        self.assertEqual(before,self.service.db.all('SELECT * FROM reviews'))
        jobs=self.service.animation_jobs(pid)['jobs']
        self.assertEqual({j['scene_id'] for j in jobs},set(ids))
        for job in jobs:
            selected=self.service.db.one('SELECT asset_id FROM generations WHERE id=?',
                (self.service.scene(job['scene_id'])['active_generation_id'],))
            self.assertEqual(job['source_image_asset_id'],selected['asset_id'])

    def test_stale_selected_image_cannot_queue(self):
        pid,ids,_=self.project()
        self.service.select_scene_animation_engine(ids[0],'omni_flash')
        scene=self.service.scene(ids[0])
        self.service.edit_scene(ids[0],{'beat':'A different story action.'},scene['current_revision'])
        with self.assertRaisesRegex(DomainError,'устарело'):
            self.service.start_animation_generation(pid)
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])

    def test_modified_source_file_cannot_queue(self):
        pid,ids,gens=self.project()
        self.service.select_scene_animation_engine(ids[0],'omni_flash')
        generation=self.service.db.one('SELECT asset_id FROM generations WHERE id=?',(gens[0],))
        asset=self.service.asset(generation['asset_id'])
        (self.service.root/asset['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(DomainError,'изменён'):
            self.service.start_animation_generation(pid)
        self.assertEqual(self.service.db.all('SELECT id FROM veo_jobs'),[])

    def test_received_duration_is_measured_without_overwriting_requested_duration(self):
        import av
        pid,ids,_=self.project();sid=ids[0]
        self.service.select_scene_animation_engine(sid,'omni_flash',duration_seconds=6)
        buf=io.BytesIO()
        with av.open(buf,mode='w',format='mp4') as media:
            stream=media.add_stream('libx264',rate=10);stream.width=128;stream.height=72;stream.pix_fmt='yuv420p'
            frame=av.VideoFrame.from_image(Image.new('RGB',(128,72),(35,45,55)))
            for _ in range(80):
                for packet in stream.encode(frame):media.mux(packet)
            for packet in stream.encode():media.mux(packet)
        payload=buf.getvalue()
        result=self.service.upload_scene_video(sid,payload,filename='video.mp4',idempotency_key='duration-test')
        self.assertEqual(result['animation']['duration_seconds'],6)
        self.assertEqual(result['animation']['actual_duration_seconds'],8)
        detail=self.service.detail(pid)
        self.assertEqual(detail['scenes'][0]['video_metadata']['duration_seconds'],8)
        # Existing clips are also measured by detail, independently of revision metadata.
        from imagegen.db import dump
        scene=self.service.scene(sid);data=copy.deepcopy(scene['data']);data['animation'].pop('actual_duration_seconds')
        with self.service.db.transaction() as conn:
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',(dump(data),sid,scene['current_revision']))
        before=self.service.scene(sid)['current_revision']
        self.assertEqual(self.service.detail(pid)['scenes'][0]['video_metadata']['duration_seconds'],8)
        self.assertEqual(self.service.scene(sid)['current_revision'],before)
        count=len(self.service.db.all('SELECT id FROM assets'))
        self.service.upload_scene_video(sid,payload,filename='video.mp4',idempotency_key='duration-test')
        self.assertEqual(len(self.service.db.all('SELECT id FROM assets')),count)

if __name__=='__main__':unittest.main()

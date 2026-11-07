import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from imagegen.db import uid, now, dump
from imagegen.domain import DomainError
from imagegen.editing import normalize
from imagegen.insert_coverage import select, policy, assert_budget, is_animated
from imagegen.media_segment_placement import apply_full_segments
from tests import test_veo_animation as fixtures


def document(durations):
    return {'schema_version': 1, 'clips': [{'edit_clip_id': str(i), 'scene_id': 's' + str(i), 'media_type': 'image',
                      'trim_in_ms': 0, 'trim_out_ms': duration, 'playback_rate': 1, 'hold_last_ms': 0}
                     for i, duration in enumerate(durations)], 'transitions': [], 'audio_tracks': []}


def proposals(doc):
    timeline = normalize(doc)
    return [{'proposal_id': 'p' + c['edit_clip_id'], 'edit_clip_id': c['edit_clip_id'],
             'replace_clip_ids': [c['edit_clip_id']], 'match_status': 'matched',
             'retrieval_score': .7, 'insert_duration_ms': c['duration_ms']} for c in timeline['clips']]


def limited(percent=30, distribution='spread'):
    return {'mode': 'limited', 'percent': percent, 'distribution': distribution}


class CoverageSelectionTests(unittest.TestCase):
    def test_thirty_percent_uses_time_and_spreads_over_whole_video(self):
        doc = document([1000] * 10)
        picked = select(doc, proposals(doc), limited())
        self.assertEqual(picked['coverage']['planned_covered_ms'], 3000)
        self.assertEqual(picked['selected_proposal_ids'], ['p1', 'p4', 'p8'])
        self.assertEqual(picked, select(doc, proposals(doc), limited()))

    def test_variable_durations_not_clip_count_and_never_trims(self):
        doc = document([6000, 1000, 1000, 1000, 1000])
        before = copy.deepcopy(doc)
        picked = select(doc, proposals(doc), limited())
        self.assertNotIn('p0', picked['selected_proposal_ids'])
        self.assertEqual(picked['coverage']['actual_percent'], 30)
        self.assertEqual(len(picked['selected_proposal_ids']), 3)
        self.assertEqual(doc, before)

    def test_matched_mode_fills_only_real_matches_and_manual_review_needs_selection(self):
        doc = document([1000] * 10)
        choices = proposals(doc)
        choices[1]['match_status'] = 'needs_review'
        choices[2]['match_status'] = 'rejected'
        automatic = select(doc, choices[:3], {'mode': 'matched'})
        self.assertEqual(automatic['selected_proposal_ids'], ['p0'])
        manual = select(doc, choices[:3], {'mode': 'matched'}, accepted_ids=['p0','p1','p2'])
        self.assertEqual(manual['selected_proposal_ids'], ['p0','p1'])
        self.assertEqual(manual['coverage']['actual_percent'], 20)

    def test_existing_overlay_and_independent_footage_consume_the_budget(self):
        doc = document([1000] * 10)
        doc['clips'][0]['source_kind'] = 'footage'
        doc['clips'][1]['footage_overlay'] = {'source_kind': 'footage', 'media_type': 'image',
            'trim_in_ms': 0, 'trim_out_ms': 1000, 'enabled': True}
        chosen = select(doc, proposals(doc), limited())
        self.assertEqual(chosen['coverage']['existing_percent'], 20)
        self.assertEqual(chosen['coverage']['selected_count'], 1)
        self.assertEqual(chosen['coverage']['actual_percent'], 30)
        less = select(doc, proposals(doc), limited(10))
        self.assertFalse(less['selected_proposal_ids'])
        self.assertTrue(less['coverage']['already_above_target'])
        self.assertEqual(len(doc['clips']), 10)

    def test_disabled_overlay_excluded_clip_and_animation_are_protected(self):
        doc = document([1000] * 10)
        doc['clips'][0]['source_video_asset_id'] = 'video'
        doc['clips'][1]['camera_motion'] = {'preset': 'gentle_push_in'}
        doc['clips'][2]['footage_overlay'] = {'source_kind': 'footage', 'enabled': False}
        doc['clips'][3]['media_replacement_locked'] = True
        doc['clips'][4]['excluded'] = True
        picked = select(doc, proposals(doc), {'mode': 'matched'}, scene_ids={'s5'})
        self.assertEqual(picked['selected_proposal_ids'], ['p6','p7','p8','p9'])
        self.assertEqual(picked['coverage']['animated_clips_protected'], 3)

    def test_dissolves_use_interval_union_not_sum(self):
        doc = document([1000, 1000])
        doc['transitions'] = [{'left': '0', 'right': '1', 'kind': 'dissolve', 'duration_ms': 400}]
        result = select(doc, proposals(doc), limited(50))
        self.assertEqual(result['coverage']['duration_ms'], 1600)
        self.assertEqual(result['coverage']['selected_count'], 0)
        all_matched = select(doc, proposals(doc), {'mode': 'matched'})
        self.assertEqual(all_matched['coverage']['planned_covered_ms'], 1600)
        self.assertEqual(all_matched['coverage']['actual_percent'], 100)

    def test_best_prefers_match_quality_zero_and_shortfall_never_force_fill(self):
        doc = document([1000] * 10)
        choices = proposals(doc)
        choices[8]['retrieval_score'] = .95
        result = select(doc, choices, limited(10, 'best'))
        self.assertEqual(result['selected_proposal_ids'], ['p8'])
        self.assertFalse(select(doc, choices, limited(0))['selected_proposal_ids'])
        short = select(doc, choices[:1], limited(80))
        self.assertTrue(short['coverage']['below_target'])
        self.assertEqual(short['coverage']['actual_percent'], 10)

    def test_policy_rejects_invalid_values_and_guard_rejects_overfill(self):
        for p in [-1, 101, float('nan'), float('inf'), True, '30']:
            with self.subTest(p=p), self.assertRaises(DomainError):
                policy(limited(p))
        with self.assertRaises(DomainError):
            policy({'mode': 'unknown'})
        with self.assertRaises(DomainError):
            select(document([1000]), [], limited(), accepted_ids='p0')
        before = document([1000] * 10)
        after = copy.deepcopy(before)
        for clip in after['clips'][:4]:
            clip['source_kind'] = 'footage'
        with self.assertRaises(DomainError):
            assert_budget(before, after, limited())


class CoverageServiceTests(unittest.TestCase):
    def setUp(self):
        self.fix = fixtures.TestVeoAnimationPlan()
        self.fix.setUp()
        self.addCleanup(self.fix.tearDown)
        self.s = self.fix.service
        self.pid = self.fix._create_sample_project(6)
        self.scenes = self.s.detail(self.pid)['scenes']
        self.doc = document([1000] * 6)
        source = Path(self.fix.temp_dir.name) / 'base.png'
        for i, clip in enumerate(self.doc['clips']):
            Image.new('RGB', (160, 90), (30+i*20, 50, 80)).save(source)
            imported = self.s.editing.import_image_file(self.pid, source, 'base.png')['clip']
            clip.update(source_kind='scene', scene_id=self.scenes[i]['id'],
                        source_image_asset_id=imported['source_image_asset_id'],
                        source_video_asset_id=None, source_scene_revision=1, muted=True, framing='fit')
        self.saved = self.s.editing.save(self.pid, {'expected_revision': 0, 'document': self.doc})
        Image.new('RGB', (160, 90), 'red').save(source)
        self.item = self.s.media_library.import_file(self.pid, source)
        self.choices = proposals(self.doc)
        for i, p in enumerate(self.choices):
            p.update(scene_id=self.scenes[i]['id'], phrase_quote='Test phrase',
                     insert_start_ms=i*1000,insert_end_ms=(i+1)*1000,
                     narration_origin_clip_id=str(i), candidate_segment={
                         'item_id': self.item['id'], 'media_type': 'image', 'original_name': 'insert.png',
                         'file_path':self.item['file_path'],'thumbnail_path':self.item['thumbnail_path']})
        self.proposal_id = self.record(self.choices)

    def record(self, choices):
        proposal_id = uid()
        rev = self.s.editing.get(self.pid)['revision']
        voice = self.s.media_matching.compute_voice_hash(self.pid)
        with self.s.db.transaction() as db:
            db.execute("INSERT INTO media_match_proposals(id,project_id,base_edit_revision,voice_binding_hash,status,proposals_json,target_document_json,created_at,updated_at) VALUES(?,?,?,?,'proposed',?,?,?,?)",
                (proposal_id,self.pid,rev,voice,dump(choices),dump(self.doc),now(),now()))
        return proposal_id

    def test_opt_in_overlay_preserves_camera_motion_and_protects_video(self):
        from imagegen.montage_media import camera_plan
        current = self.s.editing.get(self.pid)
        doc = current['document']
        doc['clips'][3]['camera_motion'] = camera_plan(160, 90, 'gentle_push_in')
        self.s.editing.save(self.pid, {'expected_revision': current['revision'], 'document': doc})
        self.s.import_scene_video(self.scenes[5]['id'],
            self.fix._create_valid_mp4(width=160, height=90, duration=1), filename='animation.mp4')
        proposal = self.record([self.choices[3], self.choices[5]])
        default = self.s.montage_inserts.preview_proposals(self.pid, proposal, limited(40))
        self.assertEqual(default['selected_proposal_ids'], [])
        result = self.s.montage_inserts.apply_proposals(
            self.pid, proposal, coverage={**limited(40), 'cover_photo_motion': True})
        self.assertEqual(result['selected_proposal_ids'], ['p3'])
        clips = result['document']['clips']
        self.assertTrue(clips[3]['footage_overlay'])
        self.assertEqual(clips[3]['camera_motion'], doc['clips'][3]['camera_motion'])
        self.assertNotIn('footage_overlay', clips[5])
        restored = self.s.montage_inserts.rollback_proposal(self.pid, proposal)
        self.assertEqual(restored['document']['clips'][3], doc['clips'][3])

    def test_preview_apply_and_rollback_preserve_base_duration_and_audio(self):
        before = self.s.editing.get(self.pid)
        preview = self.s.montage_inserts.preview_proposals(self.pid, self.proposal_id, limited(40))
        self.assertEqual(preview['coverage']['selected_count'], 2)
        self.assertEqual(self.s.editing.get(self.pid), before)
        result = self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,
            [p['proposal_id'] for p in self.choices],coverage=limited(40))
        self.assertEqual(result['applied_count'], 2)
        self.assertLessEqual(result['coverage']['actual_percent'], 40)
        self.assertEqual(normalize(result['document'])['duration_ms'], 6000)
        self.assertEqual(sum(bool(c.get('footage_overlay')) for c in result['document']['clips']), 2)
        self.assertTrue(all(c['footage_overlay'].get('camera_motion') for c in result['document']['clips'] if c.get('footage_overlay')))
        self.assertEqual(len({c['footage_overlay']['camera_motion']['preset'] for c in result['document']['clips'] if c.get('footage_overlay')}),2)
        restored = self.s.montage_inserts.rollback_proposal(self.pid,self.proposal_id)
        self.assertEqual(restored['document'],before['document'])

    def test_animation_created_after_preview_cannot_be_covered_by_old_proposals(self):
        self.s.montage_inserts.preview_proposals(self.pid,self.proposal_id,limited(100))
        self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='animation.mp4')
        before = self.s.editing.get(self.pid)
        result = self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p0'],coverage=limited(100))
        self.assertEqual(result['status'],'no_changes')
        self.assertEqual(self.s.editing.get(self.pid),before)
        with self.assertRaises(DomainError):
            apply_full_segments(self.s,self.pid,self.proposal_id,[self.choices[0]],before)

    def test_final_guard_rechecks_animation_created_during_apply(self):
        original_save = self.s.editing.save
        def race(pid,data,**kwargs):
            self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='animation.mp4')
            return original_save(pid,data,**kwargs)
        with patch.object(self.s.editing,'save',side_effect=race), self.assertRaises(DomainError):
            self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p0'],coverage=limited(100))
        self.assertEqual(self.s.editing.get(self.pid)['revision'],self.saved['revision'])

    def test_stale_and_foreign_proposals_rejected_and_new_plan_skips_animation(self):
        foreign = self.s.create_project({'title':'Other','story':'Other story','style':'Cinematic'})['id']
        with self.assertRaises(DomainError) as error:
            self.s.montage_inserts.preview_proposals(foreign,self.proposal_id,limited())
        self.assertEqual(error.exception.status,404)
        self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='animation.mp4')
        plan = self.s.material_search.create_plan(self.pid)
        ids = [cid for r in plan['requirements'] for cid in r['origin_clip_ids']]
        self.assertNotIn('0',ids)
        current = self.s.editing.get(self.pid)
        self.s.editing.save(self.pid,{'expected_revision':current['revision'],'document':current['document']})
        with self.assertRaises(DomainError) as error:
            self.s.montage_inserts.preview_proposals(self.pid,self.proposal_id,limited())
        self.assertEqual(error.exception.status,409)

    def test_automatic_matching_skips_animated_frames_before_any_visual_verification(self):
        self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='animation.mp4')
        segment = self.item['segments'][0]
        with self.s.db.transaction() as db:
            db.execute("UPDATE media_segments SET analysis_status='completed', dominant_subject='coast', analysis_summary='coast', objects_json='[\"coast\"]' WHERE id=?",(segment['id'],))
        with patch('imagegen.media_matching.verify_candidate_with_qwen',return_value={'status':'matched','status_label':'подходит','reason':'test'}):
            result = self.s.media_matching.propose_inserts(self.pid,{'coverage':limited(100)})
        self.assertFalse(any(p['scene_id']==self.scenes[0]['id'] for p in result['proposals']))

    def test_batch_motion_changes_only_automatic_stills_preserves_manual_motion_and_is_idempotent(self):
        from imagegen.insert_photo_motion import apply_to_auto_photos
        from imagegen.montage_media import camera_plan
        self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p0','p1','p2'],coverage={**limited(100),'photo_motion':'off'})
        before=self.s.editing.get(self.pid)
        doc=copy.deepcopy(before['document'])
        manual_plan=camera_plan(160,90,'gentle_pull_back')
        doc['clips'][1]['footage_overlay']['camera_motion']=manual_plan
        doc['clips'][2]['footage_overlay']['placement_mode']='manual'
        doc['clips'][2]['footage_overlay'].pop('placement_policy',None)
        doc=self.s.editing.save(self.pid,{'expected_revision':before['revision'],'document':doc})
        result=apply_to_auto_photos(self.s,self.pid,{'expected_revision':doc['revision'],'photo_motion':'auto'})
        self.assertEqual(result['changed_count'],1)
        self.assertEqual(result['document']['clips'][1]['footage_overlay']['camera_motion'],manual_plan)
        self.assertNotIn('camera_motion',result['document']['clips'][2]['footage_overlay'])
        self.assertEqual(normalize(result['document'])['duration_ms'],6000)
        repeat=apply_to_auto_photos(self.s,self.pid,{'expected_revision':result['revision'],'photo_motion':'auto'})
        self.assertEqual(repeat['status'],'no_changes')
        self.assertEqual(repeat['revision'],result['revision'])

    def test_batch_motion_protects_new_animation_under_old_overlay(self):
        from imagegen.insert_photo_motion import apply_to_auto_photos
        self.s.montage_inserts.apply_proposals(self.pid,self.proposal_id,['p0'],coverage={**limited(100),'photo_motion':'off'})
        self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='new-animation.mp4')
        before=self.s.editing.get(self.pid)
        result=apply_to_auto_photos(self.s,self.pid,{'expected_revision':before['revision'],'photo_motion':'auto'})
        self.assertEqual(result['changed_count'],0)
        self.assertEqual(result['protected_count'],1)
        self.assertEqual(self.s.editing.get(self.pid),before)

    def test_moving_photo_renders_actual_motion_without_stretching_or_duration_change(self):
        from imagegen.montage_media import StillReader, camera_plan
        from imagegen.export_worker import render
        import av
        import numpy as np
        source=Path(self.fix.temp_dir.name)/'moving.png'
        image=Image.new('RGB',(160,90),'#395564')
        draw=ImageDraw.Draw(image)
        for x in range(0,160,10):draw.line((x,0,159-x,89),fill=(x,200,220),width=3)
        image.save(source)
        self.item=self.s.media_library.import_file(self.pid,source)
        for choice in self.choices:
            choice['candidate_segment']['item_id']=self.item['id']
        proposal_id=self.record(self.choices)
        applied=self.s.montage_inserts.apply_proposals(self.pid,proposal_id,['p0'],coverage=limited(20))
        path=Path(self.fix.temp_dir.name)/'motion.mp4'
        render(self.s,applied['document'],path,lambda *_:None,lambda:False,size=(160,90))
        with av.open(str(path)) as media:
            frames=[f.to_ndarray(format='rgb24') for f in media.decode(video=0)]
        self.assertEqual(len(frames),144)
        self.assertGreater(float(np.abs(frames[0].astype(float)-frames[20]).mean()),2)
        portrait=Path(self.fix.temp_dir.name)/'portrait.png'
        Image.new('RGB',(100,200),'white').save(portrait)
        reader=StillReader(portrait,{'camera_motion':camera_plan(100,200,'gentle_push_in'),'base_duration_ms':1000})
        moved=np.asarray(reader.image(.5,(160,90),fill=False))
        self.assertEqual(int(moved[:,0].max()),0)
        self.assertGreater(int(moved[:,80].min()),240)
        filled=np.asarray(reader.image(.5,(160,90),fill=True))
        self.assertGreater(int(filled.min()),240)
        reader.close()

    def test_folder_analysis_preserves_montage_even_for_old_auto_apply_requests(self):
        from imagegen.media_folder_workflow import MediaFolderWorkflow
        folder = Path(self.fix.temp_dir.name) / 'input'
        folder.mkdir()
        Image.new('RGB',(160,90),'blue').save(folder/'footage.png')
        options = {'auto_apply':True,'recursive':True,'coverage':limited(40),
                   'voice_hash':self.s.media_matching.compute_voice_hash(self.pid),'placement_policy':'whole-narration-segment/1'}
        workflow = MediaFolderWorkflow(self.s)
        job_id = uid()
        with self.s.db.transaction() as db:
            db.execute("INSERT INTO media_folder_jobs(id,project_id,folder_path,state,step,options_json,base_edit_revision,created_at,updated_at) VALUES(?,?,?,'queued','scanning',?,?,?,?)",
                (job_id,self.pid,str(folder),dump(options),self.saved['revision'],now(),now()))
        generated={'proposal_id':self.proposal_id,'proposals':self.choices}
        with patch.object(self.s.media_analysis,'start_job',return_value={'id':'analysis','state':'completed'}), \
             patch.object(self.s.media_matching,'propose_inserts',return_value=generated) as matching:
            workflow._run(self.pid,job_id,folder,options,self.saved['revision'])
        done=workflow.latest(self.pid)
        self.assertEqual(done['state'],'completed',done['message'])
        self.assertEqual(done['applied_count'],0)
        self.assertEqual(done['proposal_id'],self.proposal_id)
        self.assertEqual(matching.call_args.args[1]['coverage'],limited(40))
        self.assertEqual(self.s.editing.get(self.pid)['revision'],self.saved['revision'])

    def test_preview_http_rejects_invalid_and_stale_policy_without_saving(self):
        from imagegen.server import Application
        app=Application(self.s.root)
        path=f'/api/projects/{self.pid}/edit/media-match/{self.proposal_id}/preview'
        before=self.s.editing.get(self.pid)
        result=app.dispatch('POST',path,{'coverage':limited(30)})
        self.assertEqual(result['coverage']['selected_count'],1)
        with self.assertRaises(DomainError):
            app.dispatch('POST',path,{'coverage':limited(130)})
        self.assertEqual(self.s.editing.get(self.pid),before)

    def test_material_search_old_candidate_blocked_before_import_when_scene_is_animated(self):
        plan=self.s.material_search.create_plan(self.pid)
        requirement=plan['requirements'][0]
        job=self.s.material_search.start_job(self.pid,plan['plan_id'],selected_providers=['local_library'])
        cid=uid()
        with self.s.db.transaction() as db:
            db.execute("""INSERT INTO material_search_candidates(id,job_id,requirement_id,result_id,provider,external_id,media_type,
                       candidate_json,semantic_status,identity_status,rights_status,reasons_json,rank_score,is_selected,is_imported,created_at)
                       VALUES(?,?,?,'result','local_library',?,'photo','{}','matched','confirmed','permission_recorded','[]',.9,0,0,?)""",
                       (cid,job['id'],requirement['id'],self.item['id'],now()))
        self.s.import_scene_video(self.scenes[0]['id'],self.fix._create_valid_mp4(width=160,height=90,duration=1),filename='new-animation.mp4')
        before=self.s.editing.get(self.pid)
        with patch.object(self.s.material_search,'import_candidate',side_effect=AssertionError('No import for protected animation')),self.assertRaises(DomainError) as error:
            self.s.material_search.apply_candidate_to_segment(self.pid,cid)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.s.editing.get(self.pid),before)


if __name__ == '__main__':
    unittest.main()

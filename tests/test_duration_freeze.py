import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import av
import numpy as np
from imagegen.montage_voice_fit import propose_duration,subtitles
from imagegen.editing import normalize
from imagegen.export_worker import render,white_fade
from tests import test_montage_voice_fit as fixtures
pure_fixture=fixtures.pure_fixture


class DurationFreezeCalculationTests(unittest.TestCase):
    def test_extra_duration_freezes_existing_trim_and_keeps_audio_source_order(self):
        snap,_=pure_fixture();original=copy.deepcopy(snap)
        for c in snap['document']['clips']:c['source_duration_ms']=6000
        original=copy.deepcopy(snap)
        p=propose_duration(snap)
        self.assertTrue(p['can_apply'],p['conflicts']);self.assertEqual(snap,original)
        self.assertEqual(p['timeline']['duration_ms'],4500)
        self.assertEqual([c['trim_out_ms'] for c in p['document']['clips']],[1000]*3)
        self.assertEqual([c['hold_last_ms'] for c in p['document']['clips']],[500]*3)
        self.assertEqual([c.get('playback_rate',1) for c in p['document']['clips']],[1]*3)
        self.assertEqual(p['document']['audio_tracks'],original['document']['audio_tracks'])
        self.assertEqual(p['document']['voice_fit']['cues'],[])
        self.assertEqual(subtitles(p['document']),'')
        self.assertFalse(p['document']['voice_fit']['phrase_alignment'])

    def test_repeated_fit_is_idempotent(self):
        snap,_=pure_fixture();p=propose_duration(snap);snap['document']=p['document']
        self.assertEqual(propose_duration(snap)['document'],p['document'])

    def test_offsets_and_trimmed_audio_are_preserved(self):
        snap,_=pure_fixture();snap['audio_binding'].update(offset_ms=500,trim_in_ms=500)
        snap['document']['audio_tracks'][0].update(offset_ms=500,trim_in_ms=500)
        p=propose_duration(snap);self.assertEqual(p['timeline']['duration_ms'],4500)
        self.assertEqual(p['document']['audio_tracks'],snap['document']['audio_tracks'])

    def test_shorter_voice_trims_video_without_slowing_audio_or_video(self):
        snap,_=pure_fixture();snap['audio_binding']['trim_out_ms']=1500
        snap['document']['audio_tracks'][0]['trim_out_ms']=1500
        p=propose_duration(snap);self.assertTrue(p['can_apply'])
        self.assertEqual([c['trim_out_ms'] for c in p['document']['clips']],[500]*3)
        self.assertFalse(any(c['hold_last_ms'] for c in p['document']['clips']))

    def test_manual_lock_and_stale_source_remain_conflicts(self):
        for mode in ('lock','stale'):
            snap,_=pure_fixture()
            if mode=='lock':snap['document']['clips'][0]['timing_locked']=True
            else:snap['scenes'][0]['current_revision']=2
            p=propose_duration(snap);self.assertFalse(p['can_apply'])

    def test_transitions_do_not_shorten_fitted_voice_clock(self):
        for kind in ('dissolve','black','white'):
            snap,_=pure_fixture();snap['document']['transitions']=[{'left':'0','right':'1','kind':kind,'duration_ms':300}]
            p=propose_duration(snap);self.assertTrue(p['can_apply']);self.assertEqual(p['timeline']['duration_ms'],4500)

    def test_excluded_clip_is_preserved_and_does_not_receive_a_freeze(self):
        snap,_=pure_fixture();snap['document']['clips'][1]['excluded']=True
        before=copy.deepcopy(snap['document']['clips'][1]);p=propose_duration(snap)
        self.assertTrue(p['can_apply']);self.assertEqual(len(p['changes']),2)
        self.assertEqual(p['document']['clips'][1],before)

    def test_real_user_snapshot_269_clips_100_second_tail_and_no_fabricated_quotes(self):
        path=Path(__file__).parent/'fixtures/local-duration-snapshot.json'
        if not path.exists():self.skipTest('Local user snapshot')
        snap=json.loads(path.read_text(encoding='utf-8'))['snapshot'];p=propose_duration(snap)
        self.assertTrue(p['can_apply'],p['conflicts']);self.assertEqual(len(p['changes']),269)
        self.assertAlmostEqual(p['timeline']['duration_ms'],1445433.469,places=5)
        self.assertAlmostEqual(sum(c['hold_last_ms'] for c in p['document']['clips']),100433.469,places=5)
        for old,new in zip(snap['document']['clips'],p['document']['clips']):
            for key in ('edit_clip_id','scene_id','source_video_asset_id','source_image_asset_id','trim_in_ms','trim_out_ms'):
                self.assertEqual(old.get(key),new.get(key))
            self.assertNotIn('voice_source_binding',new)


class DurationFreezeAPIAndRenderTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.IntegrationTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.editor=self.fixture.editor;self.manager=self.fixture.manager;self.pid=self.fixture.pid;self.service=self.fixture.service

    def proposal(self):
        with patch.object(self.manager,'recognize',side_effect=AssertionError('Duration fit must not run ASR')):
            job=self.manager.start(self.pid,{'expected_revision':self.editor.get(self.pid)['revision'],'timing_mode':'duration_freeze'})
            for _ in range(100):
                job=self.manager.get(self.pid,job['id'])
                if job['status'] not in ('queued','recognizing'):break
                time.sleep(.01)
        self.assertEqual(job['status'],'ready',job);self.assertTrue(job['proposal']['can_apply'],job['proposal']['conflicts']);return job

    def test_apply_and_rollback_keep_scenes_generations_and_audio_unchanged(self):
        # Make a short montage so this test extends clips rather than trims them.
        current=self.editor.get(self.pid)
        for c in current['document']['clips']:c['trim_out_ms']=1000
        before=self.editor.save(self.pid,{'expected_revision':current['revision'],'document':current['document']})
        rows={t:self.service.db.all('SELECT * FROM '+t+' ORDER BY rowid') for t in ('scenes','scene_revisions','generations','assets')}
        job=self.proposal();applied=self.manager.apply(self.pid,job['id'],{'expected_revision':before['revision']})
        self.assertEqual(applied['document']['audio_tracks'],before['document']['audio_tracks'])
        self.assertEqual(rows,{t:self.service.db.all('SELECT * FROM '+t+' ORDER BY rowid') for t in rows})
        restored=self.manager.rollback(self.pid,job['id'],{'expected_revision':applied['revision']})
        self.assertEqual(restored['document'],before['document'])

    def test_render_freeze_holds_last_source_frame_and_keeps_audio_tail(self):
        # Existing fixture can create tiny local MP4s; no remote generation.
        last=self.service.detail(self.pid)['scenes'][-1]
        video=self.fixture.fixture._create_valid_mp4(width=160,height=90,duration=1)
        self.service.import_scene_video(last['id'],video,filename='freeze-source.mp4')
        doc=self.editor.sync(self.pid,{'expected_revision':1})['document']
        for c in doc['clips']:c['trim_out_ms']=1000
        saved=self.editor.save(self.pid,{'expected_revision':1,'document':doc})
        job=self.proposal();candidate=self.editor.validate(self.pid,job['proposal']['document'])
        out=self.service.root/'duration-freeze.mp4';seconds=render(self.service,candidate,out,lambda *a:None,lambda:False,size=(160,90))
        self.assertAlmostEqual(seconds,4.5,delta=1/24)
        with av.open(str(out)) as media:
            frames=list(media.decode(video=0));self.assertEqual(len(frames),108)
            self.assertTrue(np.array_equal(frames[-1].to_ndarray(),frames[-2].to_ndarray()))
        with av.open(str(out)) as media:self.assertEqual(media.streams.audio[0].codec_context.name,'aac')

    def test_white_transition_renders_white_at_join_and_preserves_duration(self):
        job=self.proposal();doc=job['proposal']['document'];clips=doc['clips']
        doc['transitions']=[{'left':clips[0]['edit_clip_id'],'right':clips[1]['edit_clip_id'],'kind':'white','duration_ms':300}]
        candidate=self.editor.validate(self.pid,doc);out=self.service.root/'white-transition.mp4'
        render(self.service,candidate,out,lambda *a:None,lambda:False,size=(160,90))
        timeline=normalize(doc);join=timeline['clips'][1]['start_ms'];index=round(join/1000*24)
        with av.open(str(out)) as media:frames=list(media.decode(video=0))
        self.assertGreater(np.mean(frames[index].to_ndarray(format='rgb24')),245)
        self.assertAlmostEqual(len(frames)/24,4.5,delta=1/24)

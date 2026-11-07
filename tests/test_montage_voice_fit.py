import base64
import copy
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
import av
import numpy as np
from PIL import Image
from imagegen.domain import DomainError
from imagegen.editing import normalize
from imagegen.montage_voice_fit import propose,snapshot,subtitles,signature
from imagegen.montage_media import camera_plan,StillReader
from imagegen.export_worker import render
from imagegen.voice_alignment import reference_words,build_report
from tests import test_veo_animation as fixture_module


def pure_fixture():
    story='Анна идёт. Борис ждёт. Корзина стоит.'
    quotes=['Анна идёт.','Борис ждёт.','Корзина стоит.'];scenes=[];clips=[];words=reference_words(story)
    for i,quote in enumerate(quotes):
        a=story.index(quote);scenes.append({'id':str(i),'current_revision':1,'data':{'source_start':a,'source_end':a+len(quote),'source_quote':quote}})
        clips.append({'edit_clip_id':str(i),'scene_id':str(i),'source_scene_revision':1,'source_video_asset_id':'video-'+str(i),
            'source_duration_ms':1000,'trim_in_ms':0,'trim_out_ms':1000,'muted':True})
    times=[(.2,.5),(.6,.9),(1.7,2),(2.2,2.4),(3.2,3.4),(3.6,3.8)]
    for word,(a,b) in zip(words,times):word.update(start=a,end=b,method='exact',confidence=.99)
    voice={'asset_id':'audio','sha256':'fake-audio-hash','trim_in_ms':0,'trim_out_ms':4500,'offset_ms':0}
    doc={'schema_version':1,'clips':clips,'transitions':[],'audio_tracks':[{'role':'voice',**{k:v for k,v in voice.items() if k not in ('sha256',)},'source_duration_ms':4500,'enabled':True}], 'source_audio':False}
    return {'project_id':'pid','story':story,'scenes':scenes,'document':doc,'audio_binding':voice}, {'words':words,'audio_duration':4.5,'story_sha256':hashlib.sha256(story.encode()).hexdigest(),'metrics':{}}


class CalculationTests(unittest.TestCase):
    def test_phrase_boundaries_pauses_tail_and_no_input_mutation(self):
        snap,alignment=pure_fixture();before=copy.deepcopy(snap);result=propose(snap,alignment)
        self.assertEqual(snap,before);self.assertTrue(result['can_apply'],result['conflicts'])
        self.assertEqual(result['boundaries_ms'],[0,1700,3200,4500])
        self.assertEqual(result['timeline']['duration_ms'],4500)
        self.assertEqual([c['hold_last_ms'] for c in result['document']['clips']],[700,500,300])
        self.assertEqual(result['document']['audio_tracks'],snap['document']['audio_tracks'])

    def test_images_hold_without_video_or_generation(self):
        snap,a=pure_fixture()
        for c in snap['document']['clips']:c.update(media_type='image',source_video_asset_id=None,source_image_asset_id='image',source_duration_ms=86400000,trim_out_ms=5000)
        result=propose(snap,a);self.assertTrue(result['can_apply']);self.assertEqual([c['trim_out_ms'] for c in result['document']['clips']],[1700,1500,1300])

    def test_transition_does_not_shift_following_phrases(self):
        snap,a=pure_fixture();snap['document']['transitions']=[{'left':'0','right':'1','kind':'dissolve','duration_ms':500},{'left':'1','right':'2','kind':'black','duration_ms':300}]
        result=propose(snap,a);timeline=result['timeline'];self.assertTrue(result['can_apply'])
        self.assertEqual([c['start_ms'] for c in timeline['clips']],[0,1700,3200]);self.assertEqual(timeline['duration_ms'],4500)
        self.assertEqual(timeline['clips'][0]['end_ms'],2200)

    def test_manual_order_footage_duplicate_and_missing_source_are_conflicts(self):
        for mode in ('order','footage','duplicate','missing'):
            snap,a=pure_fixture();clips=snap['document']['clips']
            if mode=='order':clips.reverse()
            if mode=='footage':clips[0].update(source_kind='footage',scene_id=None)
            if mode=='duplicate':clips.append(copy.deepcopy(clips[0]));clips[-1]['edit_clip_id']='dup'
            if mode=='missing':clips.pop()
            before=copy.deepcopy(clips);result=propose(snap,a)
            self.assertFalse(result['can_apply'],mode);self.assertEqual([c['edit_clip_id'] for c in before],[c['edit_clip_id'] for c in result['document']['clips']])

    def test_manual_and_locked_boundaries_preserved(self):
        for field in ('timing_locked','manual_timing'):
            snap,a=pure_fixture();snap['document']['clips'][0][field]=True;before=copy.deepcopy(snap['document']['clips'][0]);result=propose(snap,a)
            self.assertFalse(result['can_apply']);self.assertEqual(result['document']['clips'][0],before)

    def test_existing_trim_requires_explicit_choice(self):
        snap,a=pure_fixture();snap['document']['clips'][0]['trim_in_ms']=100
        self.assertFalse(propose(snap,a)['can_apply'])
        snap['document']['clips'][0]['allow_voice_retime']=True
        result=propose(snap,a);self.assertTrue(result['can_apply']);self.assertEqual(result['document']['clips'][0]['trim_in_ms'],100)

    def test_short_video_speed_is_bounded_and_audio_unchanged(self):
        snap,a=pure_fixture()
        for c,d in zip(snap['document']['clips'],[1500,1350,1200]):c.update(source_duration_ms=d,trim_out_ms=d)
        result=propose(snap,a,'speed');self.assertTrue(result['can_apply']);self.assertAlmostEqual(result['document']['clips'][0]['playback_rate'],1500/1700)
        snap['document']['clips'][0]['source_duration_ms']=500;snap['document']['clips'][0]['trim_out_ms']=500
        self.assertFalse(propose(snap,a,'speed')['can_apply']);self.assertFalse(propose(snap,a,'error')['can_apply'])

    def test_uncertain_and_unmatched_words_never_get_even_timing(self):
        for method in ('interpolated','unmatched'):
            snap,a=pure_fixture();a['words'][0]['method']=method
            self.assertFalse(propose(snap,a)['can_apply'])

    def test_missing_unknown_tail_and_audio_trim_are_explicit_conflicts(self):
        snap,a=pure_fixture();snap['document']['clips'].pop();a['words'][-1].update(start=None,end=None,method='unmatched')
        self.assertFalse(propose(snap,a)['can_apply'])
        snap,a=pure_fixture();snap['audio_binding']['trim_in_ms']=2000;snap['document']['audio_tracks'][0]['trim_in_ms']=2000
        result=propose(snap,a);self.assertFalse(result['can_apply']);self.assertIn('phrase_outside_audio',[c['code'] for c in result['conflicts']])

    def test_repeated_application_does_not_accumulate_rounding_or_clips(self):
        snap,a=pure_fixture();first=propose(snap,a);snap['document']=first['document'];second=propose(snap,a)
        self.assertEqual(first['document'],second['document']);self.assertEqual(len(second['document']['clips']),3)

    def test_2_5d_uses_original_image_and_duration(self):
        snap,a=pure_fixture();scene=snap['scenes'][0]
        scene.update(image_asset_id='image',image_width=1280,image_height=720)
        scene['data']['animation']={'source':'ken_burns','ken_burns_preset':'gentle_pan_left'}
        snap['document']['clips'][0]['source_image_asset_id']='image'
        result=propose(snap,a);self.assertTrue(result['can_apply']);clip=result['document']['clips'][0]
        self.assertEqual(clip['media_type'],'image');self.assertIsNone(clip['source_video_asset_id']);self.assertEqual(clip['original_video_asset_id'],'video-0');self.assertTrue(clip['camera_motion'])

    def test_subtitles_use_montage_boundaries_including_transitions(self):
        snap,a=pure_fixture();snap['document']['transitions']=[{'left':'0','right':'1','kind':'dissolve','duration_ms':300}]
        text=subtitles(propose(snap,a)['document'])
        self.assertIn('00:00:00,000 --> 00:00:01,700',text);self.assertIn('00:00:03,200 --> 00:00:04,500',text)

    def test_audio_offset_keeps_first_image_through_initial_silence(self):
        snap,a=pure_fixture();snap['audio_binding']['offset_ms']=500;snap['document']['audio_tracks'][0]['offset_ms']=500
        result=propose(snap,a);self.assertTrue(result['can_apply']);self.assertEqual(result['boundaries_ms'],[0,2200,3700,5000])


def audio_bytes(seconds):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000)
        samples=(np.sin(np.arange(round(48000*seconds))*2*np.pi*440/48000)*9000).astype('<i2')
        out.writeframes(samples.tobytes())
    return buffer.getvalue()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.TestVeoAnimationPlan();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.service=self.fixture.service;self.app=self.fixture.app;self.pid=self.fixture._create_sample_project(3)
        self.fixture._upload_scene_images(self.pid)
        self.editor=self.service.editing;self.manager=self.app.montage_voice_fit
        result=self.editor.sync(self.pid,{'expected_revision':0,'add_only':True});self.doc=result['document']
        upload=self.editor.audio(self.pid,{'filename':'voice.wav','data':base64.b64encode(audio_bytes(4.5)).decode()})
        self.doc['audio_tracks']=[{'role':'voice','asset_id':upload['asset_id'],'source_duration_ms':4500,'trim_in_ms':0,'trim_out_ms':4500,'offset_ms':0,'volume':1,'enabled':True}]
        self.saved=self.editor.save(self.pid,{'expected_revision':0,'document':self.doc})
        story=self.service.project(self.pid)['story'];words=reference_words(story)
        self.asr={'audio_duration':4.5,'model':'test-fixture','segments':[{'words':[
            {'word':w['text'],'start':i*4.2/len(words)+.1,'end':(i+.8)*4.2/len(words)+.1,'probability':.99} for i,w in enumerate(words)]}]}

    def run_proposal(self):
        with patch.object(self.manager,'recognize',return_value=self.asr):
            job=self.manager.start(self.pid,{'expected_revision':self.editor.get(self.pid)['revision']})
            for _ in range(100):
                job=self.manager.get(self.pid,job['id'])
                if job['status'] not in ('queued','recognizing'):break
                time.sleep(.02)
        self.assertEqual(job['status'],'ready',job);self.assertTrue(job['proposal']['can_apply'],job['proposal']['conflicts'])
        return job

    def test_image_sync_validation_and_no_scene_writes_on_apply_rollback(self):
        self.assertTrue(all(c['media_type']=='image' for c in self.doc['clips']))
        before=self.service.db.all('SELECT * FROM scenes ORDER BY id');generations=self.service.db.all('SELECT * FROM generations ORDER BY id')
        job=self.run_proposal();self.assertEqual(self.editor.get(self.pid)['revision'],1)
        applied=self.manager.apply(self.pid,job['id'],{'expected_revision':1});self.assertEqual(applied['revision'],2)
        self.assertEqual(self.manager.apply(self.pid,job['id'],{'expected_revision':1})['revision'],2)
        back=self.manager.rollback(self.pid,job['id'],{'expected_revision':2});self.assertEqual(back['document'],self.saved['document'])
        self.assertEqual(before,self.service.db.all('SELECT * FROM scenes ORDER BY id'));self.assertEqual(generations,self.service.db.all('SELECT * FROM generations ORDER BY id'))

    def test_changed_edit_audio_or_project_cannot_apply_stale_preview(self):
        job=self.run_proposal();doc=copy.deepcopy(self.doc);doc['clips'].reverse();self.editor.save(self.pid,{'expected_revision':1,'document':doc})
        with self.assertRaises(DomainError):self.manager.apply(self.pid,job['id'],{'expected_revision':2})

    def test_project_change_racing_apply_is_checked_inside_write_transaction(self):
        job=self.run_proposal()
        with self.service.db.transaction() as conn:conn.execute('UPDATE projects SET version=version+1 WHERE id=?',(self.pid,))
        with self.assertRaises(DomainError):self.manager.apply(self.pid,job['id'],{'expected_revision':1})

    def test_audio_replacement_marks_old_fit_stale_but_exports_current_montage(self):
        job=self.run_proposal();applied=self.manager.apply(self.pid,job['id'],{'expected_revision':1});doc=applied['document']
        upload=self.editor.audio(self.pid,{'filename':'new.wav','data':base64.b64encode(audio_bytes(4.5)).decode()})
        doc['audio_tracks'][0]['asset_id']=upload['asset_id'];saved=self.editor.save(self.pid,{'expected_revision':2,'document':doc})
        self.assertTrue(saved['document']['voice_fit']['stale'])
        with patch('imagegen.export_worker.threading.Thread'):
            export=self.app.edit_exports.start(self.pid,{'expected_revision':3})
        row=self.service.db.one('SELECT snapshot FROM edit_exports WHERE id=?',(export['id'],))
        self.assertEqual(json.loads(row['snapshot']),saved['document'])

    def test_rollback_preserves_later_manual_changes_by_refusing_overwrite(self):
        job=self.run_proposal();applied=self.manager.apply(self.pid,job['id'],{'expected_revision':1})
        applied['document']['clips'][0]['timing_locked']=True;self.editor.save(self.pid,{'expected_revision':2,'document':applied['document']})
        with self.assertRaises(DomainError):self.manager.rollback(self.pid,job['id'],{'expected_revision':3})

    def test_real_mp4_stills_cover_full_voice_with_aac(self):
        job=self.run_proposal();document=self.editor.validate(self.pid,job['proposal']['document']);out=self.service.root/'voice-fit.mp4'
        duration=render(self.service,document,out,lambda *a:None,lambda:False,size=(160,90))
        self.assertAlmostEqual(duration,4.5,delta=1/24)
        with av.open(str(out)) as video:
            frames=list(video.decode(video=0));self.assertEqual(len(frames),108)
            self.assertGreater(float(np.mean(frames[-1].to_ndarray(format='rgb24'))),5)
        with av.open(str(out)) as audio:
            self.assertEqual(audio.streams.audio[0].codec_context.name,'aac')
            samples=np.concatenate([f.to_ndarray() for f in audio.decode(audio=0)],axis=1)
            self.assertGreater(np.max(np.abs(samples[:,-4000:])),.05)

    def test_real_short_video_hold_and_dissolve_keep_phrase_clock(self):
        last=self.service.detail(self.pid)['scenes'][-1]
        video=self.fixture._create_valid_mp4(width=160,height=90,duration=1)
        self.service.import_scene_video(last['id'],video,filename='short.mp4')
        doc=self.editor.sync(self.pid,{'expected_revision':1})['document']
        clips=doc['clips'];doc['transitions']=[{'left':clips[0]['edit_clip_id'],'right':clips[1]['edit_clip_id'],'kind':'dissolve','duration_ms':300}]
        self.editor.save(self.pid,{'expected_revision':1,'document':doc})
        job=self.run_proposal();candidate=self.editor.validate(self.pid,job['proposal']['document']);self.assertGreater(candidate['clips'][-1]['hold_last_ms'],0)
        out=self.service.root/'short-hold.mp4';duration=render(self.service,candidate,out,lambda *a:None,lambda:False,size=(160,90))
        self.assertEqual(duration,4.5)
        with av.open(str(out)) as media:
            frames=list(media.decode(video=0));self.assertEqual(len(frames),108)
            self.assertTrue(np.array_equal(frames[-1].to_ndarray(format='rgb24'),frames[-2].to_ndarray(format='rgb24')))

    def test_changed_source_invalidates_fit_and_camera_parameters_cannot_be_forged(self):
        job=self.run_proposal();doc=job['proposal']['document'];doc['clips'][0]['source_scene_revision']+=1
        self.assertTrue(self.editor.validate(self.pid,doc)['voice_fit']['stale'])
        clip=copy.deepcopy(self.doc['clips'][0]);clip['camera_motion']={'preset':'gentle_push_in','scale_start':99}
        doc=copy.deepcopy(self.doc);doc['clips'][0]=clip
        with self.assertRaises(DomainError):self.editor.validate(self.pid,doc)

    def test_motion_render_and_source_pixels_match_same_duration_phase(self):
        from imagegen.ken_burns import render_ken_burns_frames
        path=self.service.root/'motion.png';Image.fromarray(np.tile(np.arange(160,dtype=np.uint8)[None,:,None],(90,1,3))).save(path)
        plan=camera_plan(160,90,'gentle_pan_left');reader=StillReader(path,{'camera_motion':plan,'base_duration_ms':2000})
        frames,_=render_ken_burns_frames(Image.open(path),'gentle_pan_left',2,24,target_size=(160,90))
        for i in (0,20,47):self.assertTrue(np.array_equal(np.asarray(frames[i]),np.asarray(reader.image(i/24,(160,90)))))
        reader.close()

    def test_cross_project_proposal_and_invalid_paths_rejected(self):
        job=self.run_proposal();foreign=self.service.create_project({'title':'other','story':'Другой сценарий.','style':'Cinematic'})['id']
        with self.assertRaises(DomainError):self.manager.get(foreign,job['id'])
        with self.assertRaises(DomainError):self.manager.get(self.pid,'../../app.sqlite')

    def test_cancel_and_restart_preserve_montage(self):
        entered=threading.Event();release=threading.Event()
        def recognize(pid,jid,snap,event):entered.set();release.wait(2);return self.asr
        with patch.object(self.manager,'recognize',side_effect=recognize):
            job=self.manager.start(self.pid,{'expected_revision':1});self.assertTrue(entered.wait(1));self.manager.cancel(self.pid,job['id']);release.set()
            for _ in range(50):
                if job['id'] not in self.manager.running:break
                time.sleep(.02)
        self.assertEqual(self.manager.get(self.pid,job['id'])['status'],'cancelled');self.assertEqual(self.editor.get(self.pid)['revision'],1)

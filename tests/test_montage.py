import base64
import copy
import io
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import av
import numpy as np
from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.server import make_server, Application
from imagegen.editing import normalize
from imagegen.export_worker import render
from tests import veo_fixture


def wav():
    out=io.BytesIO()
    with wave.open(out,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(48000)
        samples=(np.sin(np.arange(48000)*2*np.pi*440/48000)*8000).astype('<i2')
        audio.writeframes(samples.tobytes())
    return out.getvalue()


class MontageTests(unittest.TestCase):
    def setUp(self):
        self.fixture=veo_fixture.TestVeoAnimationPlan();self.fixture.setUp()
        self.service=self.fixture.service;self.app=self.fixture.app
        self.pid=self.fixture._create_sample_project(3)
        video=self.fixture._create_valid_mp4(width=160,height=90,duration=1)
        for s in self.service.detail(self.pid)['scenes']:
            self.service.import_scene_video(s['id'],video,filename='clip.mp4')
        self.editor=self.service.editing
        self.doc=self.editor.sync(self.pid,{'expected_revision':0,'add_only':True})['document']

    def tearDown(self):
        self.fixture.tearDown()

    def save(self,doc=None,revision=0):
        return self.editor.save(self.pid,{'expected_revision':revision,'document':doc or self.doc})

    def test_persist_conflict_restart_and_no_flow_writes(self):
        before=self.service.db.all('SELECT id,current_revision,video_asset_id FROM scenes')
        count=len(self.service.db.all('SELECT id FROM generations'))
        self.doc['clips'].reverse();self.doc['clips'][0]['trim_in_ms']=250
        self.save()
        self.assertEqual(Service(self.service.root).editing.get(self.pid)['document'],self.doc)
        with self.assertRaises(DomainError) as error:self.save()
        self.assertEqual(error.exception.status,409)
        self.assertEqual(before,self.service.db.all('SELECT id,current_revision,video_asset_id FROM scenes'))
        self.assertEqual(count,len(self.service.db.all('SELECT id FROM generations')))

    def test_asset_isolation_validation_and_empty_trim(self):
        foreign=self.service.create_project({'title':'Other','story':'Another story','style':'Cinematic'})['id']
        with self.assertRaises(DomainError):self.editor.save(foreign,{'expected_revision':0,'document':self.doc})
        self.doc['clips'][0]['source_image_asset_id']=self.doc['clips'][0]['source_video_asset_id']
        with self.assertRaises(DomainError):self.save()
        self.doc['clips'][0]['source_image_asset_id']=None
        self.doc['clips'][0]['trim_out_ms']=0
        with self.assertRaises(DomainError):self.save()

    def test_explicit_sync_retains_order_exclusion_and_trim(self):
        self.doc['clips'].reverse();first=self.doc['clips'][0];first['excluded']=True;first['trim_in_ms']=250
        self.save()
        old=first['source_video_asset_id']
        self.service.import_scene_video(first['scene_id'],self.fixture._create_valid_mp4(width=160,height=90,duration=2),filename='new.mp4')
        self.assertEqual(self.editor.get(self.pid)['document']['clips'][0]['source_video_asset_id'],old)
        self.assertTrue(self.editor.get(self.pid)['sources'][first['edit_clip_id']]['new_version'])
        synced=self.editor.sync(self.pid,{'expected_revision':1})['document']
        self.assertNotEqual(synced['clips'][0]['source_video_asset_id'],old)
        self.assertTrue(synced['clips'][0]['excluded']);self.assertEqual(synced['clips'][0]['trim_in_ms'],250)
        self.assertEqual([c['edit_clip_id'] for c in synced['clips']],[c['edit_clip_id'] for c in self.doc['clips']])

    def test_timeline_overlap_black_and_short_clip_zones(self):
        clips=self.doc['clips'];clips[1]['trim_out_ms']=100
        self.doc['transitions']=[{'left':clips[0]['edit_clip_id'],'right':clips[1]['edit_clip_id'],'kind':'dissolve','duration_ms':500},{'left':clips[1]['edit_clip_id'],'right':clips[2]['edit_clip_id'],'kind':'black','duration_ms':500}]
        timeline=normalize(self.doc)
        self.assertEqual(timeline['duration_ms'],2050)
        self.assertEqual(timeline['clips'][1]['in_ms']+timeline['clips'][1]['out_ms'],100)
        self.doc['transitions'][0]['kind']='cut';self.doc['transitions'][1]['duration_ms']=0
        self.assertEqual(normalize(self.doc)['duration_ms'],2100)

    def test_audio_upload_rejects_invalid_and_mix_has_aac(self):
        with self.assertRaises(DomainError):self.editor.audio(self.pid,{'filename':'bad.wav','data':base64.b64encode(b'bad').decode()})
        aid=self.editor.audio(self.pid,{'filename':'voice.wav','data':base64.b64encode(wav()).decode()})
        self.doc['audio_tracks']=[{'role':'voice','asset_id':aid['asset_id'],'trim_in_ms':100,'trim_out_ms':900,'offset_ms':200,'volume':.5,'fade_in_ms':100,'fade_out_ms':100,'enabled':True}]
        self.doc['clips']=self.doc['clips'][:1];self.doc['clips'][0]['trim_in_ms']=100
        document=self.editor.validate(self.pid,self.doc)
        result=self.service.root/'mixed.mp4'
        duration=render(self.service,document,result,lambda *a:None,lambda:False,size=(160,90))
        self.assertAlmostEqual(duration,.9,delta=1/24)
        with av.open(str(result)) as media:
            self.assertEqual(media.streams.audio[0].codec_context.name,'aac')
            chunks=[f.to_ndarray() for f in media.decode(audio=0)]
            samples=np.concatenate(chunks,axis=1)
            self.assertLess(np.max(np.abs(samples[:,:4000])),.01)
            self.assertGreater(np.max(np.abs(samples[:,18000:24000])),.05)

    def test_stream_export_transition_duration_and_pixels(self):
        for kind,expected in [('cut',3),('black',3),('dissolve',2.5)]:
            clips=self.doc['clips'];self.doc['transitions']=[{'left':clips[i]['edit_clip_id'],'right':clips[i+1]['edit_clip_id'],'kind':kind,'duration_ms':250} for i in range(2)]
            result=self.service.root/(kind+'.mp4')
            duration=render(self.service,self.doc,result,lambda *a:None,lambda:False,size=(160,90))
            self.assertAlmostEqual(duration,expected,delta=1/24)
            with av.open(str(result)) as media:
                frames=list(media.decode(video=0))
                self.assertEqual(len(frames),round(expected*24))
                if kind=='black':
                    self.assertLess(np.mean(frames[24].to_ndarray(format='rgb24')),5)

    def test_range_and_auth(self):
        server=make_server(self.app,port=0);threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        aid=self.doc['clips'][0]['source_video_asset_id'];url=base+'/api/assets/'+aid
        try:
            with urlopen(Request(url,headers={'Range':'bytes=4-19'})) as response:
                self.assertEqual(response.status,206);self.assertEqual(len(response.read()),16)
                self.assertTrue(response.headers['Content-Range'].startswith('bytes 4-19/'))
            with urlopen(Request(url,headers={'Range':'bytes=-8'})) as response:self.assertEqual(len(response.read()),8)
            with self.assertRaises(HTTPError) as err:urlopen(Request(url,headers={'Range':'bytes=999999999-'}))
            self.assertEqual(err.exception.code,416)
            with self.assertRaises(HTTPError) as err:urlopen(Request(base+f'/api/projects/{self.pid}/edit',data=b'{}',headers={'Content-Type':'application/json'}))
            self.assertEqual(err.exception.code,403)
        finally:server.shutdown();server.server_close()

    def test_background_snapshot_dedup_cancel_and_restart(self):
        self.save();entered=threading.Event();release=threading.Event()
        def blocked(*args,**kwargs):
            entered.set();release.wait(5)
            if args[4]():raise InterruptedError()
        with patch('imagegen.export_worker.render',side_effect=blocked):
            first=self.app.edit_exports.start(self.pid,{'expected_revision':1});self.assertTrue(entered.wait(2))
            second=self.app.edit_exports.start(self.pid,{'expected_revision':1});self.assertEqual(first['id'],second['id'])
            self.doc['clips'][0]['excluded']=True;self.save(revision=1)
            row=self.service.db.one('SELECT snapshot FROM edit_exports WHERE id=?',(first['id'],))
            self.assertFalse(json.loads(row['snapshot'])['clips'][0]['excluded'])
            self.app.edit_exports.cancel(self.pid,first['id']);release.set()
            for _ in range(100):
                state=self.app.edit_exports.get(self.pid,first['id'])
                if state['status']=='cancelled':break
                time.sleep(.02)
            self.assertEqual(state['status'],'cancelled');self.assertIsNone(state['asset_id'])
        with self.service.db.transaction() as conn:
            conn.execute("UPDATE edit_exports SET status='running' WHERE id=?",(first['id'],))
        restarted=Application(self.service.root)
        self.assertEqual(restarted.edit_exports.get(self.pid,first['id'])['status'],'interrupted')

    def test_stale_only_included_clip_blocks_export(self):
        self.save()
        cid=self.doc['clips'][0]['edit_clip_id']
        original=self.editor.sources
        def stale(*args):
            states=original(*args);states[cid]['stale']=True;return states
        with patch.object(self.app.service.editing,'sources',side_effect=stale):
            with self.assertRaises(DomainError):self.app.edit_exports.start(self.pid,{'expected_revision':1})

    def test_232_clips_use_at_most_two_video_decoders(self):
        from imagegen import export_worker
        prototype=self.doc['clips'][0]
        self.doc['clips']=[dict(prototype,edit_clip_id=f'clip-{i}',trim_out_ms=125) for i in range(232)]
        self.doc['transitions']=[{'left':f'clip-{i}','right':f'clip-{i+1}','kind':'dissolve','duration_ms':40} for i in range(231)]
        saved=self.save();self.assertEqual(len(saved['document']['clips']),232)
        self.assertEqual(saved['timeline']['duration_ms'],19760)
        original=export_worker.VideoReader
        active=[0,0]
        class Tracked(original):
            def __init__(self,*args):
                super().__init__(*args);active[0]+=1;active[1]=max(active)
            def close(self):
                super().close();active[0]-=1
        with patch.object(export_worker,'VideoReader',Tracked):
            duration=render(self.service,self.doc,self.service.root/'large.mp4',lambda *a:None,lambda:False,size=(32,24))
        self.assertEqual(active[0],0);self.assertLessEqual(active[1],2);self.assertEqual(active[1],2)
        self.assertAlmostEqual(duration,19.76,delta=1/24)

    def test_background_completed_file_and_aac_upload_formats(self):
        raw=wav()
        for fmt,suffix,codec in [('mp3','.mp3','libmp3lame'),('mp4','.m4a','aac'),('mp4','.mp4','aac')]:
            path=self.service.root/('sound'+suffix)
            with av.open(str(path),'w',format=fmt) as out:
                stream=out.add_stream(codec,rate=48000);stream.layout='mono'
                frame=av.AudioFrame.from_ndarray(np.frombuffer(raw[-96000:],dtype='<i2').reshape(1,-1),format='s16',layout='mono');frame.sample_rate=48000
                for packet in stream.encode(frame):out.mux(packet)
                for packet in stream.encode():out.mux(packet)
            aid=self.editor.audio(self.pid,{'filename':path.name,'data':base64.b64encode(path.read_bytes()).decode()})
            self.assertGreater(aid['duration_ms'],900)
        self.doc['clips']=self.doc['clips'][:1]
        self.save()
        job=self.app.edit_exports.start(self.pid,{'expected_revision':1})
        for _ in range(250):
            state=self.app.edit_exports.get(self.pid,job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.02)
        self.assertEqual(state['status'],'completed',state.get('error'))
        self.assertTrue(state['url']);self.assertEqual(state['edit_revision'],1)
        asset=self.service.asset(state['asset_id'])
        with av.open(str(self.service.root/asset['path'])) as media:self.assertEqual(len(list(media.decode(video=0))),24)

    def test_excluded_missing_video_does_not_block_other_clips(self):
        self.save()
        first=self.doc['clips'][0];first['excluded']=True
        asset=self.service.asset(first['source_video_asset_id']);(self.service.root/asset['path']).unlink()
        saved=self.save(revision=1)
        self.assertTrue(saved['sources'][first['edit_clip_id']]['missing'])
        # Validate the document used by exports without requiring excluded files.
        self.editor.validate(self.pid,saved['document'])

    def test_imported_footage_is_independent_and_survives_sync(self):
        source=self.service.root/'footage.mp4';source.write_bytes(self.fixture._create_valid_mp4(width=160,height=90,duration=2))
        before=self.service.db.all('SELECT id,current_revision,video_asset_id FROM scenes')
        result=self.editor.import_video_file(self.pid,source,'пейзаж.mp4');clip=result['clip']
        self.doc['clips'].insert(1,clip);clip['trim_in_ms']=500;clip['trim_out_ms']=1500
        saved=self.save();self.assertFalse(saved['sources'][clip['edit_clip_id']]['stale'])
        synced=self.editor.sync(self.pid,{'expected_revision':1})['document']
        self.assertEqual(synced['clips'][1],clip)
        self.assertEqual(before,self.service.db.all('SELECT id,current_revision,video_asset_id FROM scenes'))
        foreign=self.service.create_project({'title':'Other','story':'Other story','style':'Cinematic'})['id']
        with self.assertRaises(DomainError):self.editor.validate(foreign,dict(self.doc,clips=[clip]))
        forged=dict(clip,source_video_asset_id=self.doc['clips'][0]['source_video_asset_id'])
        with self.assertRaises(DomainError):self.editor.validate(self.pid,dict(self.doc,clips=[forged]))

    def test_invalid_footage_does_not_register_assets(self):
        count=len(self.service.db.all('SELECT id FROM assets'))
        source=self.service.root/'audio.mp4';source.write_bytes(wav())
        with self.assertRaises(DomainError):self.editor.import_video_file(self.pid,source,'audio.mp4')
        self.assertEqual(count,len(self.service.db.all('SELECT id FROM assets')))

    def test_prepared_scene_snapshot_is_not_duplicated_by_sync(self):
        source=self.service.root/'shot.mp4';source.write_bytes(self.fixture._create_valid_mp4(width=160,height=90,duration=1))
        clip=self.editor.import_video_file(self.pid,source,'shot.mp4')['clip'];clip['origin_scene_id']=self.doc['clips'][0]['scene_id']
        self.doc['clips']=[clip];self.save()
        synced=self.editor.sync(self.pid,{'expected_revision':1,'add_only':True})['document']
        self.assertEqual(len(synced['clips']),3)
        self.assertEqual(synced['clips'][0],clip)

    def test_fast_source_checks_match_scene_checks_after_revision_changes(self):
        self.fixture._upload_scene_images(self.pid)
        self.doc=self.editor.sync(self.pid,{'expected_revision':0})['document']
        first=self.doc['clips'][0];scene=self.service.scene(first['scene_id'])
        animation=dict(scene['data'].get('animation',{}),ken_burns_preset='gentle_pan_right')
        self.service.edit_scene(scene['id'],{'animation':animation},scene['current_revision'])
        self.assertFalse(self.editor.sources(self.pid,self.doc)[first['edit_clip_id']]['stale'])
        scene=self.service.scene(scene['id'])
        self.service.edit_scene(scene['id'],{'beat':'Thomas watches the lighthouse during the storm.'},scene['current_revision'])
        detail={s['id']:s for s in self.service.detail(self.pid)['scenes']}
        self.assertTrue(detail[scene['id']]['stale'])
        self.assertTrue(self.editor.sources(self.pid,self.doc)[first['edit_clip_id']]['stale'])

    def test_raw_footage_upload_requires_session_and_cleans_temporary(self):
        server=make_server(self.app,port=0);threading.Thread(target=server.serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{server.server_port}/api/projects/{self.pid}/edit/video'
        raw=self.fixture._create_valid_mp4(width=160,height=90,duration=1)
        try:
            with self.assertRaises(HTTPError) as err:urlopen(Request(url,data=raw,headers={'Content-Type':'application/octet-stream','X-Client':'google-flow'}))
            self.assertEqual(err.exception.code,403)
            headers={'Content-Type':'application/octet-stream','X-Session-Token':self.app.token,'X-File-Name':'test.mov'}
            with urlopen(Request(url,data=raw,headers=headers)) as response:result=json.load(response)
            self.assertEqual(result['clip']['source_kind'],'footage');self.assertEqual(result['clip']['title'],'test.mov')
            self.assertFalse(list((self.service.root/'edit_uploads').iterdir()))
            with self.assertRaises(HTTPError):urlopen(Request(url,data=b'broken',headers=headers))
            self.assertFalse(list((self.service.root/'edit_uploads').iterdir()))
        finally:server.shutdown();server.server_close()

    def test_non_browser_video_is_converted_and_exports_with_voice(self):
        source=self.service.root/'source.mov'
        with av.open(str(source),'w',format='mov') as out:
            stream=out.add_stream('mpeg4',rate=24);stream.width=160;stream.height=90;stream.pix_fmt='yuv420p'
            for i in range(24):
                frame=av.VideoFrame.from_ndarray(np.full((90,160,3),80,dtype=np.uint8),format='rgb24');frame.pts=i
                for packet in stream.encode(frame):out.mux(packet)
            for packet in stream.encode():out.mux(packet)
        clip=self.editor.import_video_file(self.pid,source,'source.mov')['clip']
        path=self.service.root/self.service.asset(clip['source_video_asset_id'])['path']
        with av.open(str(path)) as media:self.assertEqual(media.streams.video[0].codec_context.name,'h264')
        clip['illustrative']=True
        clip['framing']='fill'
        voice=self.editor.audio(self.pid,{'filename':'voice.wav','data':base64.b64encode(wav()).decode()})
        self.doc['clips']=[clip];self.doc['audio_tracks']=[{'role':'voice','asset_id':voice['asset_id'],'trim_in_ms':0,'trim_out_ms':1000,'offset_ms':0,'volume':1}]
        doc=self.editor.validate(self.pid,self.doc);final=self.service.root/'footage-export.mp4'
        render(self.service,doc,final,lambda *_:None,lambda:False,size=(160,90))
        with av.open(str(final)) as media:
            self.assertEqual(len(list(media.decode(video=0))),24)
            self.assertEqual(media.streams.audio[0].codec_context.name,'aac')

    def test_wide_footage_can_fill_the_video_without_borders(self):
        from imagegen.export_worker import VideoReader
        source=self.service.root/'wide.mp4';source.write_bytes(self.fixture._create_valid_mp4(width=160,height=80,duration=1))
        reader=VideoReader(source,0)
        try:
            fitted=np.asarray(reader.image(0,(160,90)))
            filled=np.asarray(reader.image(0,(160,90),fill=True))
            self.assertEqual(fitted[0].max(),0)
            self.assertGreater(filled[0].max(),0)
            self.assertEqual(filled.shape,(90,160,3))
        finally:reader.close()

    def test_fullhd_and_hd_export_settings_are_preserved(self):
        self.doc['export_settings']={'width':1920,'height':1080,'fps':24}
        saved=self.save();self.assertEqual(saved['document']['export_settings']['height'],1080)
        self.doc['export_settings']={'width':1280,'height':720,'fps':24}
        saved=self.save(revision=1);self.assertEqual(saved['document']['export_settings']['height'],720)
        self.doc['export_settings']={'width':3840,'height':2160,'fps':24}
        with self.assertRaises(DomainError):self.save(revision=2)

    def test_background_fullhd_export_metadata_matches_the_file(self):
        self.doc['clips']=self.doc['clips'][:1];self.doc['clips'][0]['trim_out_ms']=250
        self.doc['export_settings']={'width':1920,'height':1080,'fps':24};self.save()
        job=self.app.edit_exports.start(self.pid,{'expected_revision':1})
        for _ in range(500):
            state=self.app.edit_exports.get(self.pid,job['id'])
            if state['status'] not in ('queued','running'):break
            time.sleep(.02)
        self.assertEqual(state['status'],'completed',state.get('error'))
        self.assertEqual((state['width'],state['height']),(1920,1080))
        asset=self.service.asset(state['asset_id']);self.assertEqual((asset['width'],asset['height']),(1920,1080))
        with av.open(str(self.service.root/asset['path'])) as media:
            stream=media.streams.video[0];self.assertEqual((stream.width,stream.height),(1920,1080));self.assertEqual(len(list(media.decode(stream))),6)


if __name__=='__main__':unittest.main()

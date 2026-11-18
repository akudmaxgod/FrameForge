import copy,hashlib,json,math,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import av,numpy as np
from PIL import Image
from imagegen.export_checkpoint import render_resumable,discard_completed
from imagegen.export_worker import render,RATE
from imagegen.export_preflight import require_ready
from imagegen.domain import DomainError

class ExportCheckpointTests(unittest.TestCase):
 def test_cleanup_preserves_output_unknown_files_and_neighbor_jobs(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);output=root/'done.mp4';output.write_bytes(b'completed')
   directory=root/'export-parts'/'job'/('a'*64);directory.mkdir(parents=True)
   for name in ('checkpoint.json','000000000000-000000000012.mp4','notes.txt'):(directory/name).write_text('test')
   neighbor=root/'export-parts'/'other'/('b'*64);neighbor.mkdir(parents=True);(neighbor/'checkpoint.json').write_text('other')
   discard_completed(output,'job')
   self.assertEqual(output.read_bytes(),b'completed');self.assertEqual([p.name for p in directory.iterdir()],['notes.txt']);self.assertTrue((neighbor/'checkpoint.json').exists())
   discard_completed(output,'../..');self.assertTrue(output.exists())
 def test_interruption_reuses_verified_chunks_and_preserves_audio_pts(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);Image.new('RGB',(640,360),'#52788a').save(root/'image.png');samples=49488
   sound=np.zeros((2,samples),dtype=np.float32);sound[:,-400:]=.35*np.sin(np.arange(400)*2*np.pi*900/RATE)
   with av.open(str(root/'voice.wav'),'w') as out:
    stream=out.add_stream('pcm_s16le',rate=RATE);stream.layout='stereo';frame=av.AudioFrame.from_ndarray(sound,format='fltp',layout='stereo');frame.sample_rate=RATE
    for p in stream.encode(frame):out.mux(p)
    for p in stream.encode():out.mux(p)
   service=Mock();service.root=root;service.asset.side_effect=lambda aid:{'path':'image.png' if aid=='image' else 'voice.wav','sha256':aid}
   for fps in (24,30,60):
    output=root/(str(fps)+'.mp4');job='job'+str(fps)
    doc={'schema_version':1,'clips':[{'edit_clip_id':'one','scene_id':'one','source_image_asset_id':'image','media_type':'image','source_duration_ms':1031,'trim_in_ms':0,'trim_out_ms':1031,'muted':True}], 'transitions':[], 'audio_tracks':[{'role':'voice','asset_id':'voice','trim_in_ms':0,'trim_out_ms':1031,'offset_ms':0,'volume':1,'enabled':True}], 'export_settings':{'width':1280,'height':720,'fps':fps}}
    cache=root/'export-parts'/job
    def cancel():return any(cache.glob('*/checkpoint.json'))
    with self.assertRaises(InterruptedError):render_resumable(service,doc,output,lambda *a:None,cancel,size=(640,360),job_id=job,chunk_seconds=.4)
    completed=list(cache.glob('*/*.mp4'));self.assertEqual(len(completed),1);before=(completed[0].stat().st_mtime_ns,completed[0].read_bytes())
    ranges=[]
    def record(*args,**kwargs):ranges.append(kwargs['frame_range']);return render(*args,**kwargs)
    with patch('imagegen.export_worker.render',side_effect=record):render_resumable(service,doc,output,lambda *a:None,lambda:False,size=(640,360),job_id=job,chunk_seconds=.4)
    self.assertTrue(ranges and all(a>0 for a,b in ranges));self.assertEqual((completed[0].stat().st_mtime_ns,completed[0].read_bytes()),before)
    # A damaged cached part is rebuilt rather than trusted merely by its filename.
    if fps==30:
     completed[0].write_bytes(b'corrupt');ranges.clear()
     with patch('imagegen.export_worker.render',side_effect=record):render_resumable(service,doc,output,lambda *a:None,lambda:False,size=(640,360),job_id=job,chunk_seconds=.4)
     self.assertEqual(ranges,[(0,12)])
    with av.open(str(output)) as video:
     frames=list(video.decode(video=0));self.assertEqual(len(frames),math.ceil(1.031*fps))
     for i,f in enumerate(frames):self.assertAlmostEqual(float(f.pts*f.time_base),i/fps,places=6)
    with av.open(str(output)) as video:
     audio=video.streams.audio[0];self.assertAlmostEqual(float(audio.duration*audio.time_base),1.031,delta=1/RATE)
     data=np.concatenate([f.to_ndarray() for f in video.decode(audio=0)],axis=1);self.assertGreater(float(np.sqrt(np.mean(data[:,samples-300:samples-40]**2))),.05)
 def test_insufficient_space_and_gpu_failure_are_actionable(self):
  service=Mock();service.root=Path(tempfile.gettempdir());doc={'schema_version':1,'clips':[],'transitions':[],'audio_tracks':[],'export_settings':{'width':1920,'height':1080,'fps':60}}
  with patch('imagegen.export_preflight.shutil.disk_usage',return_value=Mock(free=1)),patch('imagegen.export_preflight.gpu_encoder') as gpu:
   with self.assertRaises(DomainError) as caught:require_ready(service,doc)
   self.assertEqual(caught.exception.details['code'],'INSUFFICIENT_DISK_SPACE');gpu.assert_not_called()
  with patch('imagegen.export_preflight.shutil.disk_usage',return_value=Mock(free=10**12)),patch('imagegen.export_preflight.gpu_encoder',side_effect=RuntimeError('GPU')):
   with self.assertRaises(DomainError) as caught:require_ready(service,doc)
   self.assertEqual(caught.exception.details['code'],'GPU_ENCODER_UNAVAILABLE')

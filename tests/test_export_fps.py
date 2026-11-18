import math,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import av,numpy as np
from PIL import Image
from imagegen.export_worker import render,RATE
from imagegen.gpu_pixels import CudaImage,cuda
from imagegen.montage_media import StillReader,camera_plan

class ExportFpsTests(unittest.TestCase):
 def test_video_pts_and_last_partial_audio_interval_all_rates(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);Image.new('RGB',(640,360),'#386482').save(root/'image.png')
   samples=49488
   wave=np.zeros((2,samples),dtype=np.float32)
   wave[:,:480]=.2*np.sin(np.arange(480)*2*np.pi*660/RATE)
   wave[:,-320:]=.35*np.sin(np.arange(320)*2*np.pi*990/RATE)
   with av.open(str(root/'audio.wav'),'w') as out:
    stream=out.add_stream('pcm_s16le',rate=RATE);stream.layout='stereo'
    f=av.AudioFrame.from_ndarray(wave,format='fltp',layout='stereo');f.sample_rate=RATE
    for p in stream.encode(f):out.mux(p)
    for p in stream.encode():out.mux(p)
   service=Mock();service.root=root;service.asset.side_effect=lambda aid:{'path':('image.png' if aid=='image' else 'audio.wav')}
   for fps in (24,30,60):
    doc={'schema_version':1,'clips':[{'edit_clip_id':'one','scene_id':'one','source_image_asset_id':'image','media_type':'image','source_duration_ms':1031,'trim_in_ms':0,'trim_out_ms':1031,'muted':True}], 'transitions':[], 'audio_tracks':[{'role':'voice','asset_id':'audio','trim_in_ms':0,'trim_out_ms':1031,'offset_ms':0,'volume':1,'enabled':True}], 'export_settings':{'width':1920,'height':1080,'fps':fps}}
    output=root/(str(fps)+'.mp4');render(service,doc,output,lambda *a:None,lambda:False,size=(1920,1080))
    with av.open(str(output)) as video:
     frames=list(video.decode(video=0));self.assertEqual(len(frames),math.ceil(1.031*fps))
     for i,frame in enumerate(frames):self.assertAlmostEqual(float(frame.pts*frame.time_base),i/fps,places=6)
    with av.open(str(output)) as video:
     stream=video.streams.audio[0];self.assertEqual(stream.rate,RATE);self.assertAlmostEqual(float(stream.duration*stream.time_base),1.031,delta=1/RATE)
     decoded=np.concatenate([f.to_ndarray() for f in video.decode(audio=0)],axis=1)
     self.assertGreater(float(np.sqrt(np.mean(decoded[:,:400]**2))),.05)
     self.assertGreater(float(np.sqrt(np.mean(decoded[:,samples-280:samples-40]**2))),.05)
 def test_still_and_resize_keep_intermediate_arrays_on_gpu(self):
  cp=cuda()
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'image.png';Image.new('RGB',(400,300),'#456789').save(path)
   for moving in (False,True):
    clip={'base_duration_ms':2000,'output_fps':60}
    if moving:clip['camera_motion']=camera_plan(400,300,'gentle_push_in')
    reader=StillReader(path,clip)
    with patch.object(cp,'asnumpy',side_effect=AssertionError('Intermediate download')):
     for time in (0,.25,1.2,1.99):self.assertIsInstance(reader.gpu_image(time,(640,360)),cp.ndarray)
    reader.close()

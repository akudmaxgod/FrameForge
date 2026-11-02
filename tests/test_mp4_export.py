import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import av
import threading
from urllib.request import Request, urlopen
import json
from imagegen.assembler import assemble_storyboard_video
from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.server import make_server


class Mp4ExportTests(unittest.TestCase):
    def clip(self, path, rate, color):
        with av.open(str(path), 'w') as out:
            stream=out.add_stream('libx264',rate=rate)
            stream.width=64;stream.height=48;stream.pix_fmt='yuv420p'
            for _ in range(rate):
                for packet in stream.encode(av.VideoFrame.from_image(Image.new('RGB',(64,48),color))):out.mux(packet)
            for packet in stream.encode():out.mux(packet)

    def test_mixed_fps_preserves_duration_order_and_zero_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);a=root/'a.mp4';b=root/'b.mp4'
            self.clip(a,12,'red');self.clip(b,30,'blue')
            result=assemble_storyboard_video([a,b],root/'out.mp4',fps=24,target_size=(64,48),transition_seconds=0)
            with av.open(result['path']) as video:frames=[f.to_image().getpixel((20,20)) for f in video.decode(video=0)]
            self.assertEqual(len(frames),48);self.assertEqual(result['duration'],2)
            self.assertGreater(frames[0][0],200);self.assertGreater(frames[-1][2],200)

    def test_transition_shortens_by_overlap_and_blends(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);a=root/'a.mp4';b=root/'b.mp4'
            self.clip(a,24,'red');self.clip(b,24,'blue')
            result=assemble_storyboard_video([a,b],root/'out.mp4',fps=24,target_size=(64,48),transition_seconds=.25)
            with av.open(result['path']) as video:frames=[f.to_image().getpixel((20,20)) for f in video.decode(video=0)]
            self.assertEqual(len(frames),42)
            self.assertGreater(frames[20][0],40);self.assertGreater(frames[20][2],40)

    def test_corrupt_clip_does_not_produce_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bad=root/'bad.mp4';bad.write_bytes(b'not a video')
            with self.assertRaises(Exception):assemble_storyboard_video([bad],root/'out.mp4')
            self.assertFalse((root/'out.mp4').exists());self.assertFalse((root/'out.mp4.partial').exists())

    def test_service_explains_missing_and_stale_animation_before_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Service(directory)
            with patch.object(service,'detail',return_value={'scenes':[{'position':4,'video_asset_id':None}]}):
                with self.assertRaisesRegex(DomainError,'нет анимации.*4'):service.assemble_animation_timeline('p')
            with patch.object(service,'detail',return_value={'scenes':[{'position':4,'video_asset_id':'v','video_stale':True}]}):
                with self.assertRaisesRegex(DomainError,'обновите анимацию.*4'):service.assemble_animation_timeline('p')

    def test_http_assembly_and_download_returns_playable_mp4(self):
        from tests.veo_fixture import TestVeoAnimationPlan
        fixture=TestVeoAnimationPlan();fixture.setUp()
        server=None
        try:
            project_id=fixture._create_sample_project(scene_count=2)
            video=fixture._create_valid_mp4(width=160,height=90,duration=1)
            for scene in fixture.service.detail(project_id)['scenes']:
                fixture.service.import_scene_video(scene['id'],video,filename='clip.mp4')
            server=make_server(fixture.app,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            request=Request(base+f'/api/projects/{project_id}/animation/assemble',data=b'{"transition_seconds":0}',
                            headers={'Content-Type':'application/json','X-Session-Token':fixture.app.token})
            with urlopen(request) as response:result=json.load(response)
            with urlopen(base+result['url']+'?download=1') as response:
                self.assertEqual(response.headers['Content-Type'],'video/mp4')
                self.assertIn('attachment',response.headers['Content-Disposition'])
                payload=response.read()
            with av.open(__import__('io').BytesIO(payload)) as video:
                self.assertGreater(len(list(video.decode(video=0))),0)
        finally:
            if server:server.shutdown();server.server_close()
            fixture.tearDown()

if __name__=='__main__':unittest.main()

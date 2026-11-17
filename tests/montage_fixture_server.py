"""Isolated montage fixture; never opens the user's database."""
import base64
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests import test_veo_animation
from tests.test_montage import wav
from imagegen.server import make_server
fixture=test_veo_animation.TestVeoAnimationPlan();fixture.setUp()
pid=fixture._create_sample_project(3)
fixture._upload_scene_images(pid)
for scene in fixture.service.detail(pid)['scenes']:
    fixture.service.import_scene_video(scene['id'],fixture._create_valid_mp4(width=160,height=90,duration=1),filename='clip.mp4')
voice=fixture.service.root/'voice.wav';voice.write_bytes(wav())
server=make_server(fixture.app,port=0)
print(f'READY http://127.0.0.1:{server.server_port} {pid} {voice}',flush=True)
try:server.serve_forever()
finally:server.server_close();fixture.tearDown()

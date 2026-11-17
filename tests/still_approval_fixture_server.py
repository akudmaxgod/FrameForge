import io
import sys
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from imagegen.server import Application,make_server
from tests.test_forgotten_cities_contracts import TestForgottenCitiesChannelProfile

fixture=TestForgottenCitiesChannelProfile();fixture.setUp()
app=Application(fixture.temp_dir);fixture.service=app.service
pid,ids,_=fixture._project_with_rendered_scene(4)
for sid in ids[1:]:
    buf=io.BytesIO();Image.new('RGB',(1280,720),(75,85,95)).save(buf,format='PNG')
    app.service.upload_scene_image(sid,buf.getvalue())
server=make_server(app,0)
print(f'READY http://127.0.0.1:{server.server_port} {pid}',flush=True)
try:server.serve_forever()
finally:
    server.server_close();app.runtime.stop_all();fixture.doCleanups();fixture.tearDown()

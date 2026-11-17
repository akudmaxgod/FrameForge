"""Local populated server used only by browser click tests."""
import io
import json
import sys
import tempfile
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imagegen.server import Application, make_server
from imagegen.worker import Worker
from imagegen.db import uid, now


def png(color, width=1024, height=576):
    stream = io.BytesIO()
    Image.new('RGB', (width, height), color).save(stream, format='PNG')
    return stream.getvalue()


temporary = tempfile.TemporaryDirectory()
app = Application(temporary.name)
service = app.service
story = 'Event 0. Event 1. Event 2. Event 3.'
project = service.create_project({
    'title': 'Готовый тестовый проект', 'story': story, 'idea': '',
    'style': 'Cinematic photorealism', 'mode': 'characters',
})
analysis = {
    'world': 'Modern city',
    'characters': [{'name': 'Антон', 'description': 'Young man with short dark hair', 'wardrobe': {}}],
    'locations': [{'name': 'Дом Антона', 'description': 'Red brick two-storey house, blue front door, three upper windows.'}],
    'props': [{'name': 'Кастрюля', 'description': 'Dented steel cooking pot with two black handles.',
               'importance': 'key', 'first_appearance': 2, 'owner': 'Антон', 'default_state': 'absent'}],
    'scenes': [
        {'beat': f'Visual event {index}', 'source_quote': f'Event {index}.',
         'characters': ['Антон'], 'wardrobe': {}, 'location': 'City', 'location_ref': 'Дом Антона', 'time': 'Day',
         'shot': 'Cinematic wide shot', 'continuity_before': '', 'continuity_after': '',
         'props': ([{'name': 'Кастрюля', 'state': 'worn_on_head', 'holder': 'Антон',
                    'visible': True, 'continuity': 'continues'}] if index >= 1 else [])}
        for index in range(4)
    ],
}
# Start the first card in the Omni state so browser coverage can validate the
# compact per-engine disclosure without depending on an asynchronous bridge.
analysis['scenes'][0]['animation'] = {
    'selected_engine': 'omni_flash', 'status': 'planned_omni',
    'omni': {'resolution': '720p', 'duration_seconds': 6},
}
detail = service.import_analysis(project['id'], analysis, project['version'])
character = detail['characters'][0]
worker = Worker(service)
worker._detected_face_count = lambda _image: 1
for generation_id, color in zip(service.cast(character['id'])['generation_ids'], ('#7657b8', '#3a78a4')):
    generation = service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
    profile = json.loads(generation['snapshot'])['profile']
    worker.save_image(generation, png(color, profile['width'], profile['height']))
location = detail['locations'][0]
worker._detected_face_count = lambda _image: 0
for generation_id, color in zip(service.cast_location(location['id'])['generation_ids'], ('#887766', '#665544')):
    generation = service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
    profile = json.loads(generation['snapshot'])['profile']
    worker.save_image(generation, png(color, profile['width'], profile['height']))
prop = detail['props'][0]
service.upload_prop_image(prop['id'], png('#999999', 768, 768), lock=True)

for index, (scene, color) in enumerate(zip(detail['scenes'], ('#45346b', '#315f72', '#674238', '#345c43'))):
    with service.db.transaction() as connection:
        generation_id = service.insert_generation(
            connection, project['id'], 'scene', {'profile': service.profile(), 'prompt': scene['data']['beat']},
            scene_id=scene['id'], scene_revision=scene['current_revision'])
    generation = service.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
    worker.save_image(generation, png(color))
    with service.db.transaction() as connection:
        connection.execute('UPDATE scenes SET active_generation_id=? WHERE id=?', (generation_id, scene['id']))
    if index == 0:
        # Add a second candidate variant for scene 1 (position 1)
        with service.db.transaction() as connection:
            gen2_id = service.insert_generation(
                connection, project['id'], 'scene', {'profile': service.profile(), 'prompt': scene['data']['beat'] + ' variant 2'},
                scene_id=scene['id'], scene_revision=scene['current_revision'])
        gen2 = service.db.one('SELECT * FROM generations WHERE id=?', (gen2_id,))
        worker.save_image(gen2, png('#553377'))

# A dormant batch provides a real queue control for browser click coverage.  The
# fixture keeps the character unlocked so the selection cards remain testable, thus
# the queued fixture task is inserted without asking production batching to compile
# a locked-character prompt.  No worker is started here.
batch_id = uid()
with service.db.transaction() as connection:
    connection.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',
                       (batch_id, project['id'], 'ui-pause-controls', 'fixture', 'running', now()))
    service.insert_generation(connection, project['id'], 'scene', {'profile': service.profile(), 'prompt': 'fixture', 'template_version': 6},
                              batch_id=batch_id, scene_id=detail['scenes'][0]['id'],
                              scene_revision=detail['scenes'][0]['current_revision'])

server = make_server(app, 0)
print(f'READY http://127.0.0.1:{server.server_port} {project["id"]}', flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    app.runtime.stop_all()
    temporary.cleanup()

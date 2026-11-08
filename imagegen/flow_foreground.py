"""Flow layer jobs tied to the immutable image and current scene revision."""
import base64
import binascii
import hashlib
import io
import json
from pathlib import Path

from PIL import Image

from .db import dump, now, uid
from .domain import DomainError
from .montage_foreground import PURPOSE
from .parallax_layers import BadBackground, LAYOUT, prepare_registered

BACKGROUND_DRAFT = 'background_candidate'


def records(service, project_id):
    rows = service.db.all(
        """SELECT a.id,a.path,g.id AS generation_id,g.snapshot FROM assets a JOIN generations g ON g.id=a.generation_id
           WHERE a.project_id=? AND g.purpose=? AND json_extract(g.snapshot,'$.layout')=?
             AND g.state='succeeded'
           ORDER BY g.created_at""", (project_id, PURPOSE, LAYOUT))
    by_source = {}
    for row in rows:
        meta = json.loads(row['snapshot'])
        key = (meta['source'], meta['scene_revision'])
        by_source.setdefault(key, {})[meta['role']] = {**row, **meta}
    return by_source


def effects(service, project_id):
    return {
        (source, revision): {
            'mode': 'parallax', 'source': source, 'background': pair['background']['id'],
            'foreground': pair['foreground']['id'], 'flow_pair': True, 'layout': LAYOUT,
            'shift_x': pair['foreground']['shift_x'], 'shift_y': .010,
        }
        for (source, revision), pair in records(service, project_id).items()
        if {'background', 'foreground'} <= pair.keys()
        and all((service.root / pair[role]['path']).is_file() for role in ('background', 'foreground'))
    }


def planned(service, project_id, *, include_stale=False):
    service.project(project_id)
    rows = service.db.all(
        '''SELECT s.id,s.position,s.current_revision,s.video_asset_id,r.data,g.asset_id,a.width,a.height,a.sha256,g.scene_revision AS image_revision
           FROM scenes s JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision
           LEFT JOIN generations g ON g.id=s.active_generation_id
           LEFT JOIN assets a ON a.id=g.asset_id
           WHERE s.project_id=? ORDER BY s.position''', (project_id,))
    for row in rows:
        if (row['video_asset_id'] or
                (not include_stale and row['asset_id'] and row['image_revision'] != row['current_revision'])):
            continue
        yield row


def list_jobs(service, project_id):
    existing = records(service, project_id)
    jobs = []
    for row in planned(service, project_id):
        aid = row['asset_id']
        if not aid or not row['width'] or not row['height']:
            continue
        pair = existing.get((aid, row['current_revision']), {})
        ratio = row['width'] / row['height']
        aspect = min(('1:1', '16:9', '9:16'), key=lambda option: abs(ratio - ({'1:1':1, '16:9':16/9, '9:16':9/16}[option])))
        scene = json.loads(row['data'])
        context = str(scene.get('beat') or '')[:600]
        common = ('Match the reference canvas, camera, perspective, composition and lighting exactly. '
                  'Use the approved still as the identity and anatomy reference for every figure. '
                  'Separate by depth. Treat ALL nearest elements as one coherent foreground plane, '
                  'including whole near figures, their clothing and carried objects, and any near table, '
                  'props or support surface they touch. Keep thin silhouettes intact: blades, fingers, handles, tails, branches. '
                  'Leave the visible gaps between limbs and props in the far plane. '
                  'Also include nearby terrain, vegetation and props on the same near plane. '
                  'Keep the full horizontal extent of the near composition in one layer. '
                  'Preserve its exact screen placement and spacing. '
                  'Keep distant characters, architecture and scenery together in the far plane. ')
        prompts = {
            'background': common + 'Output the fixed far plane only. Remove the entire near plane '
                'and reconstruct what it covers. Continue distant lines naturally behind the cutout. '
                'Keep visible distant pixels in place. No cloned, mirrored or leftover outlines. '
                f'Scene context: {context}',
            'foreground': common + 'Output the entire near plane on pure flat green #00FF00. '
                'Use flat green inside every gap between the near elements. '
                'Keep the original placement, scale, face, anatomy, costume, and the connected support if near. '
                'No distant scenery, no crop, no recentering, no extra text. '
                f'Scene context: {context}',
        }
        for role, prompt in prompts.items():
            candidates = (role, BACKGROUND_DRAFT) if role == 'background' else (role,)
            if any(name in pair and (service.root / pair[name]['path']).is_file() for name in candidates):
                continue
            jobs.append({'scene_id': row['id'], 'position': row['position'], 'scene_revision': row['current_revision'],
                         'source': aid, 'role': role, 'prompt': prompt, 'image_url': '/api/assets/' + aid,
                         'source_sha256': row['sha256'], 'aspect_ratio': aspect})
    return {'jobs': jobs, 'montage_revision': service.editing.get(project_id)['revision']}


def _decode(value, size, *, rgba=False):
    if not isinstance(value, str) or not 0 < len(value) <= 16_000_000:
        raise DomainError('Слой Flow превышает лимит 12 МБ.', 413)
    try:
        with Image.open(io.BytesIO(base64.b64decode(value, validate=True))) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 25_000_000:
                raise ValueError()
            if abs((image.width / image.height) / (size[0] / size[1]) - 1) > .025:
                raise DomainError('Flow вернул слой с другим соотношением сторон.')
            return image.convert('RGBA' if rgba else 'RGB').resize(size, Image.Resampling.LANCZOS)
    except (OSError, ValueError, binascii.Error) as error:
        raise DomainError('Не удалось открыть изображение слоя Flow.') from error


def key_foreground(image, *, accept_alpha=False):
    import numpy as np

    if accept_alpha and image.mode == 'RGBA' and image.getchannel('A').getextrema()[0] < 255:
        alpha = np.asarray(image.getchannel('A'))
        if (alpha < 13).mean() < .10 or (alpha > 178).mean() < .015:
            raise DomainError('В слое нужны прозрачный фон и видимая передняя сцена.')
        return image
    image = image.convert('RGB')
    raw = np.asarray(image, dtype=np.int16)
    neutral = np.maximum(raw[:, :, 0], raw[:, :, 2])
    green = raw[:, :, 1] - neutral
    alpha = np.clip((73 - green) / 45, 0, 1)
    if (alpha < .05).mean() < .10 or (alpha > .7).mean() < .015:
        raise DomainError('Flow не отделил передний план от однотонного фона.')
    raw[:, :, 1] = np.minimum(raw[:, :, 1], neutral + np.where(green > 28, 25, 255))
    return Image.fromarray(np.dstack((raw, (alpha * 255).round())).astype('uint8'), 'RGBA')


def _prepare_foreground(image, source, background):
    import numpy as np

    foreground = key_foreground(image)
    raw = np.asarray(foreground.convert('RGB'), dtype=np.int16)
    alpha = np.asarray(foreground.getchannel('A')) / 255
    mask = alpha > .7
    # Check the assembled composition and the visible part of the fixed plane.
    composed = background.copy()
    composed.paste(foreground, (0, 0), foreground)
    before = np.asarray(source.resize((160, 90)), dtype=np.int16)
    after = np.asarray(composed.resize((160, 90)), dtype=np.int16)
    original = np.asarray(source, dtype=np.int16)
    removed = np.abs(original[mask] - np.asarray(background, dtype=np.int16)[mask]).mean(axis=1)
    far = alpha < .05
    far_difference = np.abs(original[far] - np.asarray(background,dtype=np.int16)[far]).mean(axis=1)
    if np.mean(np.abs(original[mask] - raw[mask]).mean(axis=1) < 45) < .52:
        raise DomainError('Слои Flow не совмещаются с исходным кадром.')
    if np.mean(far_difference < 28) < .68 or np.mean(removed > 22) < .25:
        raise BadBackground('Чистый фон Flow не совпадает с кадром или содержит передний план. Пересоздайте фон.')
    if np.mean(np.abs(before - after).mean(axis=2) < 27) < .78:
        raise DomainError('Слои Flow не совмещаются с исходным кадром.')
    return foreground


def save_result(service, project_id, data):
    scene_id, role, aid = (data.get(k) for k in ('scene_id', 'role', 'source'))
    if role not in ('background', 'foreground') or not isinstance(aid, str):
        raise DomainError('Неизвестный слой Flow.')
    planned_row = next((row for row in planned(service, project_id) if row['id'] == scene_id), None)
    if not planned_row or planned_row['current_revision'] != data.get('scene_revision') or planned_row['asset_id'] != aid:
        raise DomainError('Исходный кадр изменился. Слой Flow не добавлен.', 409)
    revision = data['scene_revision']
    pair = records(service, project_id).get((aid, revision), {})
    storage_role = BACKGROUND_DRAFT if role == 'background' else role
    for name in (storage_role, role):
        if name in pair and (service.root / pair[name]['path']).is_file():
            return {'asset_id': pair[name]['id'], 'reused': True}
    source_asset = service.asset(aid)
    with Image.open(service.root / source_asset['path']) as original:
        source = original.convert('RGB')
    draft = None
    shift_x = .028 if planned_row['position'] % 2 else -.028
    shift_y = .010
    image = _decode(data.get('image'), source.size)
    if role == 'foreground':
        draft = pair.get(BACKGROUND_DRAFT)
        if not draft or not (service.root / draft['path']).is_file():
            raise DomainError('Сначала сохраните чистый фон.')
        with Image.open(service.root / draft['path']) as plate:
            bg = plate.convert('RGB')
        try:
            layer = _prepare_foreground(image, source, bg)
            bg, layer = prepare_registered(source, bg, layer, shift_x, shift_y)
        except BadBackground:
            with service.db.transaction() as connection:
                # Don't invalidate a draft that a simultaneous request already finalized.
                if not _saved_layer(connection, service, project_id, aid, revision, 'foreground'):
                    connection.execute("UPDATE generations SET state='failed' WHERE id=?", (draft['generation_id'],))
            raise
        layers = [('background', bg), ('foreground', layer)]
    else:
        layers = [(BACKGROUND_DRAFT, image)]
    encoded = []
    for layer_role, layer in layers:
        output = io.BytesIO()
        layer.save(output, format='PNG')
        encoded.append((layer_role, layer.size, output.getvalue()))
    created = []
    try:
        with service.db.transaction() as connection:
            current = connection.execute('''SELECT s.current_revision,g.asset_id,s.video_asset_id FROM scenes s
                LEFT JOIN generations g ON g.id=s.active_generation_id WHERE s.id=? AND s.project_id=?''',
                (scene_id, project_id)).fetchone()
            if not current or tuple(current) != (data['scene_revision'], aid, None):
                raise DomainError('Исходный кадр изменился. Слой Flow не добавлен.', 409)
            existing = _saved_layer(connection, service, project_id, aid, revision, storage_role)
            if existing:
                return {'asset_id': existing, 'reused': True}
            if draft and _saved_layer(connection, service, project_id, aid, revision, BACKGROUND_DRAFT) != draft['id']:
                raise DomainError('Чистый фон изменился. Повторите подготовку слоёв.', 409)
            for layer_role, (width, height), raw in encoded:
                meta = {'source': aid, 'role': layer_role, 'flow_layer': True, 'scene_revision': revision,
                        'layout': LAYOUT, 'shift_x': shift_x, 'shift_y': shift_y}
                asset_id = store_layer(connection, service, project_id, (width, height), raw, meta, created)
    except Exception:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return {'asset_id': asset_id}


def store_layer(connection, service, project_id, size, raw, meta, created):
    asset_id, generation_id = uid(), uid()
    relative = Path('assets') / project_id / (asset_id + '.png')
    target = service.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    created.append(target)
    target.write_bytes(raw)
    connection.execute("""INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at)
        VALUES(?,?,?,'succeeded',?,0,?,?)""", (generation_id, project_id, PURPOSE, dump(meta), asset_id, now()))
    connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
        (asset_id, project_id, generation_id, str(relative), hashlib.sha256(raw).hexdigest(), *size, len(raw)))
    return asset_id


def _saved_layer(connection, service, project_id, source, revision, role):
    row = connection.execute("""SELECT a.id,a.path FROM assets a JOIN generations g ON g.id=a.generation_id
        WHERE a.project_id=? AND g.purpose=? AND g.state='succeeded' AND json_extract(g.snapshot,'$.source')=?
        AND json_extract(g.snapshot,'$.role')=? AND json_extract(g.snapshot,'$.layout')=?
        AND json_extract(g.snapshot,'$.scene_revision')=? ORDER BY g.created_at DESC""",
        (project_id, PURPOSE, source, role, LAYOUT, revision)).fetchone()
    return row[0] if row and (service.root / row[1]).is_file() else None


def finish(service, project_id, expected_revision, *, direct=False):
    current = service.editing.get(project_id)
    if type(expected_revision) is not int or current['revision'] != expected_revision:
        return {'applied': False, 'reason': 'Монтаж изменился. Нажмите «Добавить готовые сцены», чтобы загрузить сохранённые слои.'}
    try:
        synced = service.editing.sync(project_id, {'expected_revision': expected_revision,
                                                   'add_only': True, 'foreground_pairs': True,
                                                   'existing_only': bool(current['document']['clips']) and not direct})['document']
        if synced != current['document']:
            current = service.editing.save(project_id, {'expected_revision': expected_revision, 'document': synced})
    except DomainError as error:
        if error.status != 409:
            raise
        return {'applied': False, 'reason': 'Монтаж изменился. Слои сохранены. Нажмите «Добавить готовые сцены».'}
    return {'applied': True, 'revision': current['revision'],
            'paired': sum((clip.get('foreground_effect') or {}).get('layout') == LAYOUT for clip in synced['clips'])}

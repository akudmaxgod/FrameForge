"""Generate a clean plate and a cutout before publishing a storyboard still."""

import io
import hashlib
import json
import logging
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .camera_profiles import depth_instruction
from .db import now
from .domain import DomainError
from .flow_foreground import PURPOSE, _decode, key_foreground, planned, records, store_layer
from .flow_prompt_budget import PROVIDER_LIMIT, pack, units
from .parallax_layers import LAYOUT

SIZE = (1024, 576)
log = logging.getLogger(__name__)
DEPTH_POLICY = 'coherent-scene/3'
SCENE_PLAN = (
    'SHARED SHOT PLAN. Both layers form one place with a locked camera, single lens, horizon, scale, '
    'materials and motivated light. Near support recedes towards far space through a location-appropriate '
    'shoreline, ledge, doorway or floor. Keep the upper 35-45% readable as distant scenery, not sky alone. '
    'Preserve the stated cast, shot and action.\n'
)
PLATE = (
    'OUTPUT ONLY THE CLEAN FAR BACKGROUND. Render the FAR part of the shared plan. '
    'Use the NEAR description only to choose compatible framing, eye height, lighting and perspective. '
    'Remove every near subject and its support from this output, continuing the far environment behind them '
    'across the entire 16:9 canvas. Keep the far features readable in the upper window of the composition. '
    'Keep surface detail small-scale and softened by its actual distance. '
    'No close terrain, near props, figures, foreground shadows, silhouettes, green screen, borders or lettering.\n'
)
CUTOUT = (
    'OUTPUT ONLY THE NEAR PLANE AS A GREEN-SCREEN CUTOUT. First reference: exact far view in its original '
    'position, for alignment only. Match camera, materials and light; other references define identities. '
    'NEAR subjects and continuous support cover 55-70%. New near detail is larger and sharper than far detail. '
    'Support recedes to mid-frame and continues under the camera through most of the bottom edge. '
    'Keep the upper far window clear except for required subject silhouettes. Firm contact shadows, '
    'no floating ground, tiny island or pedestal. Never copy FAR textures or render a composite. '
    'Replace all FAR scenery and gaps with pure flat #00FF00 on at least 30% of the 16:9 canvas. No borders or lettering.\n'
)
PLANE_LABEL = re.compile(r'(?<!\w)(near(?:\s+layer)?|far(?:\s+layer)?|ближний(?:\s+слой)?|дальний(?:\s+слой)?)\s*[:—–]', re.I)


def _role_prompts(project, scene, full_prompt):
    direction = str(scene.get('manual_prompt') or '')
    labels = list(PLANE_LABEL.finditer(direction))
    planes = {}
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(direction)
        side = 'near' if label[1].lower().startswith(('near', 'ближний')) else 'far'
        planes[side] = direction[label.end():end].strip()
    style = str(project.get('style') or '').strip()
    style_line = f'Visual style: {style}.\n' if style else ''
    location = str(scene.get('location_ref') or scene.get('location') or 'the source setting')
    far = planes.get('far') or ('Distant depth beyond NEAR in ' + location + '; no near structures.')
    light = str(scene.get('time') or '').strip()
    light_line = f'Lighting and time: {light}.\n' if light else ''
    near = planes.get('near')
    near_plan = near or str(scene.get('frame_composition') or scene.get('composition') or
                            scene.get('beat') or scene.get('source_quote') or 'The source-described action and immediate support')
    camera_match = re.search(r'CAMERA: .*?(?=TWO-PLANE COMPOSITION FOR PARALLAX\.|\n|$)', full_prompt)
    actual_camera = camera_match[0].strip() if camera_match else ''
    geometry = (actual_camera or f"Viewpoint: {scene.get('shot') or 'contextual medium shot'}") + '\n'
    geometry += f"Shared location: {scene.get('location') or 'the source location'}.\n"
    camera = str(project.get('camera_custom') or '').strip() if project.get('camera_mode') == 'custom' else ''
    camera_line = f'Camera direction: {camera}\n' if camera else ''
    common_direction = direction[:labels[0].start()].strip() if labels else ''
    common_line = f'Composition direction: {common_direction}\n' if common_direction else ''
    shared = (SCENE_PLAN + style_line + light_line + geometry + camera_line + common_line +
              'NEAR plan: ' + near_plan + '\nFAR plan: ' + far + '\n')
    # The canonical scene already includes our composition boilerplate. The
    # layer-specific contract replaces it without dropping source constraints.
    near_prompt = ''
    if not near:
        context = full_prompt.replace(depth_instruction(project), '')
        if actual_camera:
            context = context.replace(actual_camera, '')
        context = pack(context, budget=0)
        # The output contract below already carries these application-owned
        # canvas rules. Keep source, identities, exclusions and editorial data.
        for repeated in (
            'Clear subject, grounded anatomy, distinct foreground, midground and background.',
            'One continuous physically coherent view; intact plausible subjects/animals. No split screen, diptych/triptych, panels, collage, contact sheet or building cross-section.',
            'Full-bleed 16:9 image, edge to edge. No borders/margins/matting/frames/rounded corners/insets/letterbox/pillarbox.',
        ):
            context = context.replace(repeated, '')
        near_prompt = context + '\n'
    background = shared + PLATE
    foreground = shared + near_prompt + CUTOUT
    return background, foreground


def _reject_copied_detail(plate, cutout):
    """Reject copied detail in the strip exposed by foreground travel."""
    light = [np.asarray(layer.convert('L'), dtype='float32') for layer in (plate, cutout)]
    details = [values - np.asarray(Image.fromarray(values.astype('uint8')).filter(ImageFilter.GaussianBlur(2)),
                                   dtype='float32') for values in light]
    alpha = np.asarray(cutout.getchannel('A')) >= 240
    mask = Image.fromarray(alpha.astype('uint8') * 255)
    dx = max(2, round(plate.width * .028 / 2) + 3)
    dy = max(2, round(plate.height * .010 / 2) + 3)
    exposed = np.zeros_like(alpha)
    for sign in (-1, 1):
        moved = mask.transform(mask.size, Image.Transform.AFFINE,
                               (1, 0, sign*dx, 0, 1, sign*dy),
                               resample=Image.Resampling.NEAREST)
        exposed |= alpha & (np.asarray(moved) == 0)
    # Frame edges are covered by renderer overscan, never by a cloned donor.
    exposed[:, :dx] = exposed[:, -dx:] = False
    exposed[:dy] = exposed[-dy:] = False
    matches, sampled = 0, 0
    for y in range(0, plate.height, 32):
        for x in range(0, plate.width, 32):
            tile = np.s_[y:y + 32, x:x + 32]
            visible = exposed[tile]
            if visible.sum() < 64:
                continue
            back, front = (layer[tile][visible] for layer in details)
            spread = back.std() * front.std()
            if spread < 20:
                continue
            sampled += 1
            corr = np.mean((back - back.mean()) * (front - front.mean())) / spread
            distance = np.abs(light[0][tile][visible] - light[1][tile][visible]).mean()
            if corr > .70 and distance < 12:
                matches += 1
    if matches >= 8 and matches > .13 * sampled:
        raise DomainError('В заднике и переднем слое повторяются одни и те же детали. Пересоздайте чистый фон и слой.', 422)


def _validate_near_plane(cutout):
    """Require a broad foreground connected to its support at the frame base."""
    alpha = cutout.getchannel('A')
    solid = np.asarray(alpha) >= 220
    coverage = solid.mean()
    bounds = alpha.point(lambda value: 255 if value >= 220 else 0).getbbox()
    if (coverage < .20 or bounds is None or bounds[2] - bounds[0] < cutout.width * .65
            or bounds[3] - bounds[1] < cutout.height * .45):
        raise DomainError('Передний план слишком мал. Пересоздайте слой с крупной ближней сценой '
                          'и широкой опорой до нижнего края.', 422)
    if (np.asarray(alpha) < 13).mean() < .10:
        raise DomainError('Передний план закрывает дальнюю сцену. Пересоздайте слой на 55–70% кадра, '
                          'сохранив видимое дальнее окружение над опорой.', 422)
    # Check a band above the edge so a thin opaque border cannot act as ground.
    mask = alpha.resize((128, 72), Image.Resampling.BOX).point(lambda value: 255 if value >= 220 else 0)
    if (np.asarray(mask)[-5:-1] > 0).mean() < .50:
        raise DomainError('У переднего плана нет широкой опоры у нижнего края. '
                          'Продолжите берег, пол или поверхность под камерой.', 422)
    # A shared extra row joins all genuine bottom contacts. Floating components
    # retain 255, so their area cannot inflate the grounded foreground coverage.
    connected = Image.new('L', (128, 73))
    connected.paste(mask, (0, 0))
    ImageDraw.Draw(connected).line((0, 72, 127, 72), fill=255)
    ImageDraw.floodfill(connected, (0, 72), 128)
    grounded = np.asarray(connected)[:-1] == 128
    if grounded.mean() < .20 or grounded.sum() < .80 * (np.asarray(mask) > 0).sum():
        raise DomainError('Основная передняя сцена оторвана от нижней опоры. '
                          'Пересоздайте цельный ближний план с непрерывной поверхностью.', 422)


def _drafts(service, project_id):
    rows = service.db.all("""SELECT a.id,a.path,a.sha256,g.id AS generation_id,g.snapshot FROM assets a
        JOIN generations g ON g.id=a.generation_id
        WHERE a.project_id=? AND g.purpose=? AND g.state='succeeded'
          AND json_extract(g.snapshot,'$.role')='direct_plate'
          AND json_extract(g.snapshot,'$.depth_policy')=? ORDER BY g.created_at""",
        (project_id, PURPOSE, DEPTH_POLICY))
    return {(meta['scene'], meta['revision']): row for row in rows
            if (service.root / row['path']).is_file() for meta in [json.loads(row['snapshot'])]}


def list_jobs(service, project_id):
    project = service.project(project_id)
    rows = {row['id']: row for row in planned(service, project_id, include_stale=True)
            if not row['asset_id'] or row['image_revision'] != row['current_revision']}
    drafts = _drafts(service, project_id)
    manifest = service.project_prompts(project_id, scene_ids=list(rows)) if rows else {'scenes': []}
    jobs, blocked = [], []
    for scene in manifest['scenes']:
        sid = scene['scene_id']
        row = rows.get(sid)
        if not row or scene['revision'] != row['current_revision']:
            continue
        revision = row['current_revision']
        draft = drafts.get((sid, revision))
        prompt = scene.get('flow_prompt') or ''
        plate_prompt, cutout_prompt = _role_prompts(project, json.loads(row['data']), prompt)
        references = scene.get('references', [])
        environment = [ref for ref in references if ref.get('type') == 'location']
        identities = [ref for ref in references if ref.get('type') != 'location']
        reason = scene.get('flow_error') if not scene.get('ready_for_flow') else None
        retry = scene.get('source_fidelity_retry') or {}
        if retry.get('exhausted'):
            reason = retry.get('reason') or 'Сцена требует проверки после предыдущих отклонений.'
        elif not prompt or not scene.get('ready_for_flow'):
            reason = reason or 'Сцена ещё не готова для генерации.'
        elif max(units(plate_prompt), units(cutout_prompt)) > PROVIDER_LIMIT:
            reason = 'Промпт слоя длиннее лимита Flow. Сократите описание кадра.'
        elif (not draft and len(environment) > 5) or len(identities) > 5:
            reason = 'Слишком много референсов для одного слоя Flow. Разделите сцену.'
        if reason:
            blocked.append({'scene_id': sid, 'position': row['position'], 'reason': reason})
            continue
        job = {
            'direct': True, 'scene_id': sid, 'position': row['position'], 'scene_revision': revision,
            'source': None, 'aspect_ratio': '16:9',
            'admission_prompt': prompt if scene.get('google_queue_enabled') else None,
        }
        if not draft:
            jobs.append({**job, 'role': 'background', 'prompt': plate_prompt, 'references': environment})
        jobs.append({**job, 'role': 'foreground', 'prompt': cutout_prompt, 'references': identities,
                     'last_error': _last_rejection(service, sid, revision),
                     **(_plate_source(draft) if draft else {})})
    return {'jobs': jobs, 'blocked': blocked, 'montage_revision': service.editing.get(project_id)['revision']}


def _png(image):
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def _rejection_folder(service, scene_id, revision):
    return service.root / 'layer-rejected' / hashlib.sha256(scene_id.encode()).hexdigest() / str(revision)


def _record_rejection(service, scene_id, revision, source, candidate, reason):
    """Keep bounded evidence without publishing a rejected scene."""
    try:
        content = _png(candidate)
        folder = _rejection_folder(service, scene_id, revision)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(content).hexdigest() + '.png')
        if path.exists() or len(list(folder.glob('*.png'))) < 6:
            path.write_bytes(content)
            path.with_suffix('.json').write_text(json.dumps({
                'scene_id': scene_id, 'revision': revision, 'source': source,
                'reason': str(reason), 'created_at': now(),
            }, ensure_ascii=False), encoding='utf-8')
    except OSError:
        log.warning('Could not save rejected layer evidence for scene %s', scene_id)


def _last_rejection(service, scene_id, revision):
    try:
        paths = list(_rejection_folder(service, scene_id, revision).glob('*.json'))
        if paths:
            path = max(paths, key=lambda item: item.stat().st_mtime_ns)
            return json.loads(path.read_text(encoding='utf-8')).get('reason')
    except (OSError, ValueError):
        log.warning('Could not read rejected layer evidence for scene %s', scene_id)
    return None


def _plate_source(draft):
    return {'source': draft['id'], 'image_url': f"/api/assets/{draft['id']}", 'source_sha256': draft['sha256']}


def _current_scene(connection, project_id, scene_id, revision):
    current = connection.execute('''SELECT s.current_revision,s.video_asset_id,g.scene_revision,g.asset_id
        FROM scenes s LEFT JOIN generations g ON g.id=s.active_generation_id
        WHERE s.project_id=? AND s.id=?''', (project_id, scene_id)).fetchone()
    if not current or current[0] != revision or current[1] or (current[2] == revision and current[3]):
        raise DomainError('Сцена изменилась или уже имеет готовый кадр.', 409)


def save_result(service, project_id, data, image_validator):
    sid, revision, role = (data.get(key) for key in ('scene_id', 'scene_revision', 'role'))
    if not isinstance(sid, str) or type(revision) is not int or role not in ('background', 'foreground'):
        raise DomainError('Некорректное задание прямой генерации слоёв.')
    row = next((row for row in planned(service, project_id, include_stale=True) if row['id'] == sid), None)
    if not row or row['current_revision'] != revision:
        raise DomainError('Сцена изменилась во время генерации слоёв.', 409)
    admission = None
    ticket = data.get('admission_request_id')
    if service.settings().get('google_queue_enabled') is True:
        from .flow_admission import FlowAdmission
        admission = FlowAdmission(service)
        admission.validate(sid, ticket)
    if row['asset_id'] and row['image_revision'] == revision:
        pair = records(service, project_id).get((row['asset_id'], revision), {})
        fg = pair.get('foreground', {})
        if role == 'foreground' and fg.get('direct_plate') == data.get('source') and fg:
            if admission:
                admission.finish(sid, ticket, 'uploaded')
            return {'asset_id': row['asset_id'], 'reused': True}
        raise DomainError('У сцены уже есть готовый кадр.', 409)
    draft = _drafts(service, project_id).get((sid, revision))
    created = []
    candidate = None
    try:
        if role == 'background':
            if data.get('source') is not None:
                raise DomainError('Для фона получен идентификатор другого слоя.', 409)
            if draft:
                return {'asset_id': draft['id'], 'reused': True, **_plate_source(draft)}
            image = _decode(data.get('image'), SIZE)
            content = _png(image)
            with service.db.transaction() as connection:
                _current_scene(connection, project_id, sid, revision)
                existing = connection.execute("""SELECT a.id,a.path,a.sha256 FROM assets a JOIN generations g ON g.id=a.generation_id
                    WHERE a.project_id=? AND g.purpose=? AND g.state='succeeded' AND json_extract(g.snapshot,'$.role')='direct_plate'
                    AND json_extract(g.snapshot,'$.depth_policy')=?
                    AND json_extract(g.snapshot,'$.scene')=? AND json_extract(g.snapshot,'$.revision')=?
                    ORDER BY g.created_at DESC LIMIT 1""", (project_id, PURPOSE, DEPTH_POLICY, sid, revision)).fetchone()
                if existing and (service.root / existing[1]).is_file():
                    return {'asset_id': existing[0], 'reused': True,
                            **_plate_source({'id': existing[0], 'sha256': existing[2]})}
                asset_id = store_layer(connection, service, project_id, SIZE, content,
                                       {'role': 'direct_plate', 'scene': sid, 'revision': revision,
                                        'depth_policy': DEPTH_POLICY}, created)
            return {'asset_id': asset_id, **_plate_source(service.asset(asset_id))}

        if not draft or draft['id'] != data.get('source'):
            raise DomainError('Сначала сохраните чистый фон этой версии сцены.', 409)
        candidate = _decode(data.get('image'), SIZE, rgba=True)
        cutout = key_foreground(candidate, accept_alpha=True)
        _validate_near_plane(cutout)
        with Image.open(service.root / draft['path']) as image:
            plate = image.convert('RGB')
        try:
            _reject_copied_detail(plate, cutout)
        except DomainError:
            with service.db.transaction() as connection:
                _current_scene(connection, project_id, sid, revision)
                connection.execute("UPDATE generations SET state='failed' WHERE id=?", (draft['generation_id'],))
            raise
        composite = Image.alpha_composite(plate.convert('RGBA'), cutout).convert('RGB')
        layer_bytes = (('background', _png(plate)), ('foreground', _png(cutout)))
        shift_x = .028 if row['position'] % 2 else -.028

        def publish(connection, source):
            _current_scene(connection, project_id, sid, revision)
            if connection.execute('SELECT state FROM generations WHERE id=?', (draft['generation_id'],)).fetchone()[0] != 'succeeded':
                raise DomainError('Чистый фон этой сцены изменился. Повторите генерацию.', 409)
            for layer_role, content in layer_bytes:
                meta = {'source': source, 'role': layer_role, 'flow_layer': True, 'scene_revision': revision,
                        'layout': LAYOUT, 'shift_x': shift_x, 'shift_y': .010, 'direct_plate': draft['id']}
                store_layer(connection, service, project_id, SIZE, content, meta, created)

        result = service.upload_scene_image(sid, _png(composite), image_validator=image_validator,
                                            expected_revision=revision, publish_layers=publish)
    except Exception as error:
        for path in created:
            path.unlink(missing_ok=True)
        if role == 'foreground' and candidate is not None and isinstance(error, DomainError) and error.status == 422:
            _record_rejection(service, sid, revision, data.get('source'), candidate, error)
        raise
    if admission:
        admission.finish(sid, ticket, 'uploaded')
    return result

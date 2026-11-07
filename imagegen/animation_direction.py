"""Compile image-grounded I2V direction, independently of narration.

No scenario-specific rewrites, word blacklists, invented actions or silent
camera-only fallback. Narrative beats and cross-scene continuity remain in
the scene document; only separately authored motion enters the video request.
"""
CONTRACT = 'image-motion/3'


def motion_instruction(data):
    animation = data.get('animation') or {}
    explicit = animation.get('visual_motion') or data.get('visual_action')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return ('Continue only the physical action already visible in the image, '
            'with natural restrained movement; keep the shot within the same moment.')


def compile_image_motion_prompt(data, camera_motion):
    parts = [
        'Animate the supplied image as the first frame of one continuous shot.',
        motion_instruction(data),
    ]
    parts.extend([
        'Camera: ' + str(camera_motion).strip().rstrip('.') + '.',
        'Preserve identity, hair, wardrobe, age and species. Preserve every person visible in the supplied image, '
        'all animals, objects, visible physical conditions, the existing setting, lighting and visual style.',
        'No new people, no identity or wardrobe changes, no new props, locations, cuts or text overlays.',
    ])
    # beat/source_quote/continuity_* are narrative metadata, not motion commands.
    # They may describe memories, off-screen events or a later scene. Do not
    # inject them even with a "context" prefix. The supplied image establishes
    # the cast, setting and visible condition; visual_motion is the opt-in action.
    return '\n'.join(parts)


def refresh_queued_direction(service, project_id, scene_id, binding):
    """Upgrade only a browser-confirmed unsent request, never an unknown one.

    The bridge first parks all submitted/unknown GM requests and excludes their
    scene IDs. This endpoint additionally checks exact revision, request key,
    source and queued state inside the write transaction.
    """
    import copy
    from .animation import compile_story_aligned_veo_prompt
    from .db import uid, now, dump
    from .domain import DomainError
    service.project(project_id)
    scene = service.scene(scene_id)
    if scene['project_id'] != project_id:
        raise DomainError('Кадр не принадлежит проекту.', 404)
    anim = copy.deepcopy(scene['data'].get('animation') or {})
    key = binding.get('idempotency_key')
    revision = binding.get('expected_scene_revision')
    asset_id, sha = binding.get('source_image_asset_id'), binding.get('source_image_sha256')
    queued = ('queued_omni', 'queued_veo')
    if (binding.get('not_sent') is not True or type(revision) is not int
            or revision != scene['current_revision'] or not key or key != anim.get('idempotency_key')
            or scene.get('video_asset_id') or anim.get('status') not in queued):
        raise DomainError('Промпт можно обновить только у ещё не отправленного задания с актуальными параметрами.', 409)
    original = service.db.one('SELECT * FROM veo_jobs WHERE project_id=? AND scene_id=? AND idempotency_key=?',
                              (project_id, scene_id, key))
    generation = service.db.one('SELECT asset_id FROM generations WHERE id=?', (scene.get('active_generation_id'),))
    asset = service.db.one('SELECT sha256,path FROM assets WHERE id=? AND project_id=?', (asset_id, project_id))
    if (not original or original['status'] not in queued or not generation or generation['asset_id'] != asset_id
            or not asset or asset['sha256'] != sha or original['source_image_asset_id'] != asset_id
            or original['source_image_sha256'] != sha or not (service.root / asset['path']).is_file()):
        raise DomainError('Исходное изображение или задание анимации изменилось. Обновите очередь.', 409)
    if anim.get('prompt_contract') == CONTRACT:
        return {'prompt_contract': CONTRACT, 'prompt': original['prompt'], 'idempotency_key': key,
                'scene_revision': revision, 'upgraded': False}
    data = copy.deepcopy(scene['data'])
    anim['prompt_contract'] = CONTRACT
    data['animation'] = anim
    camera = original.get('camera_motion') or (anim.get('veo') or {}).get('camera_motion') or anim.get('camera_motion') or 'slow cinematic push-in'
    prompt = compile_story_aligned_veo_prompt(data, camera_motion=camera, duration_seconds=original['duration_seconds'])
    if prompt == original['prompt']:
        return {'prompt_contract': CONTRACT, 'prompt': prompt, 'idempotency_key': key,
                'scene_revision': revision, 'upgraded': False}
    engine = anim.get('selected_engine')
    if engine not in ('omni_flash', 'veo_lite') or original['engine'] != engine:
        raise DomainError('Движок задания анимации изменился. Обновите очередь.', 409)
    duration, resolution = original['duration_seconds'], original['resolution']
    payload = (f'omni:{scene_id}:{resolution}:{duration}:{asset_id}:{sha}:{prompt}' if engine == 'omni_flash'
               else f'veo:{scene_id}:{camera}:{duration}:{asset_id}:{sha}:{prompt}')
    # An upgrade is a distinct request even if an earlier cancelled attempt had
    # the same compiled bytes. Never resurrect its remote request identity.
    payload += f':upgrade:{key}:{CONTRACT}'
    new_key = f"{'omni' if engine == 'omni_flash' else 'veo'}_{scene_id[:8]}_{service._animation_fingerprint(payload, anim)}"
    anim['idempotency_key'] = new_key
    with service.db.transaction() as conn:
        live = conn.execute('''SELECT s.current_revision,s.active_generation_id,s.video_asset_id,r.data
            FROM scenes s JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision
            WHERE s.id=? AND s.project_id=?''', (scene_id, project_id)).fetchone()
        import json
        current = (json.loads(live['data']).get('animation') or {}) if live else {}
        old_job = conn.execute('SELECT status FROM veo_jobs WHERE id=?', (original['id'],)).fetchone()
        if (not live or live['current_revision'] != revision or live['active_generation_id'] != scene.get('active_generation_id')
                or live['video_asset_id'] or current.get('status') not in queued
                or current.get('idempotency_key') != key or not old_job or old_job['status'] not in queued):
            raise DomainError('Задание начало выполняться или изменилось. Старый запрос сохранён.', 409)
        conn.execute("UPDATE veo_jobs SET status='cancelled',completed_at=? WHERE id=?", (now(), original['id']))
        conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, revision+1, dump(data), now()))
        conn.execute('UPDATE scenes SET current_revision=? WHERE id=?', (revision+1, scene_id))
        columns = ('id','project_id','scene_id','idempotency_key','model','status','engine','source_image_asset_id',
                   'source_image_sha256','source_generation_id','source_revision','duration_seconds','resolution',
                   'prompt','camera_motion','created_at')
        updated = {**original, 'id': uid(), 'idempotency_key': new_key, 'prompt': prompt, 'created_at': now()}
        conn.execute('INSERT INTO veo_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                     tuple(updated[c] for c in columns))
        service.db.event(conn, project_id, 'animation_direction_refreshed',
                         {'scene_id': scene_id, 'previous_request_key': key, 'idempotency_key': new_key,
                          'prompt_contract': CONTRACT})
    return {'prompt_contract': CONTRACT, 'prompt': prompt, 'story_prompt': prompt,
            'idempotency_key': new_key, 'scene_revision': revision+1, 'upgraded': True}

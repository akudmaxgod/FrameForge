"""Reversible local camera motion for automatically placed still footage only."""
import copy

from .domain import DomainError
from .montage_media import camera_plan
from .insert_coverage import PHOTO_MOTIONS, animated_scene_ids, is_animated, ensure_unanimated


def animate_photo(service, project_id, clip, motion='auto', index=0):
    if motion not in PHOTO_MOTIONS:
        raise DomainError('Неизвестный режим движения фото-вставок.')
    if motion == 'off' or clip.get('media_type') != 'image' or clip.get('camera_motion'):
        return False
    asset = service.asset(clip['source_image_asset_id'])
    if asset['project_id'] != project_id:
        raise DomainError('Фото не принадлежит проекту.', 404)
    preset = ('gentle_push_in', 'gentle_pull_back', 'gentle_pan_right', 'gentle_pan_left')[index % 4] if motion == 'auto' else motion
    clip['camera_motion'] = camera_plan(asset['width'], asset['height'], preset)
    clip['auto_photo_motion'] = True
    return True


def apply_to_auto_photos(service, project_id, data):
    service.project(project_id)
    current = service.editing.get(project_id)
    if type(data.get('expected_revision')) is not int or data['expected_revision'] != current['revision']:
        raise DomainError('Монтаж изменился. Обновите его перед добавлением движения.', 409)
    motion = data.get('photo_motion', 'auto')
    if motion not in PHOTO_MOTIONS or motion == 'off':
        raise DomainError('Выберите движение камеры для фото-вставок.')
    doc = copy.deepcopy(current['document'])
    animated = animated_scene_ids(service, project_id)
    changed, protected, existing_motion = 0, 0, 0
    changed_bases = []
    for index, base in enumerate(doc['clips']):
        if base.get('excluded'):
            continue
        layer = base.get('footage_overlay') or (base if base.get('source_kind') == 'footage' else None)
        if not layer or layer.get('media_type') != 'image' or layer.get('enabled', True) is False:
            continue
        automatic = (layer.get('placement_mode') == 'automatic' or layer.get('placement_policy') == 'whole-narration-segment/1'
                     or (layer.get('candidate_id') and layer.get('placement_mode') != 'manual'))
        if not automatic:
            continue
        if is_animated(base, animated):
            protected += 1
            continue
        if layer.get('camera_motion'):
            existing_motion += 1
            continue
        if animate_photo(service, project_id, layer, motion, changed):
            changed += 1
            changed_bases.append(base)
    if not changed:
        return {'status': 'no_changes', 'changed_count': 0, 'already_animated': existing_motion,
                'protected_count': protected, 'revision': current['revision']}
    saved = service.editing.save(project_id, {'expected_revision': current['revision'], 'document': doc},
                                 guard=lambda: ensure_unanimated(service, project_id, changed_bases))
    return {'status': 'applied', 'changed_count': changed, 'already_animated': existing_motion,
            'protected_count': protected, 'revision': saved['revision'], 'document': saved['document']}

"""Duration-based insert selection shared by folder automation and proposal apply.

Never edits a document. Uses actual timeline intervals (including transitions),
keeps whole narration segments, and protects authored animation and manual layers.
"""
import json
import math
import statistics

from .domain import DomainError
from .editing import normalize

EPS = 0.000001
PHOTO_MOTIONS = ('auto', 'off', 'gentle_push_in', 'gentle_pull_back', 'gentle_pan_left', 'gentle_pan_right')


def policy(value=None):
    if value is None:
        return {'mode': 'matched', 'percent': 100.0, 'distribution': 'spread', 'photo_motion': 'auto'}
    if not isinstance(value, dict):
        raise DomainError('Некорректные настройки покрытия вставками.')
    mode = value.get('mode', 'limited')
    distribution = value.get('distribution', 'spread')
    percent = value.get('percent', 30)
    photo_motion = value.get('photo_motion', 'auto')
    if mode not in ('limited', 'matched') or distribution not in ('spread', 'best'):
        raise DomainError('Выберите режим покрытия и распределения вставок.')
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise DomainError('Доля таймлайна должна быть числом от 0 до 100%.')
    if photo_motion not in PHOTO_MOTIONS:
        raise DomainError('Неизвестный режим движения фото-вставок.')
    settings = {'mode': mode, 'percent': float(percent), 'distribution': distribution, 'photo_motion': photo_motion}
    if value.get('cover_photo_motion') is True:
        settings['cover_photo_motion'] = True
    return settings


def animated_scene_ids(service, project_id, coverage=None):
    if not (coverage or {}).get('cover_photo_motion'):
        return {r['id'] for r in service.db.all(
            'SELECT id FROM scenes WHERE project_id=? AND video_asset_id IS NOT NULL', (project_id,))}
    rows = service.db.all(
        '''SELECT s.id, r.data FROM scenes s JOIN scene_revisions r
           ON r.scene_id=s.id AND r.revision=s.current_revision
           WHERE s.project_id=? AND s.video_asset_id IS NOT NULL''', (project_id,))
    return {r['id'] for r in rows
            if (json.loads(r['data']).get('animation') or {}).get('source') != 'ken_burns'}


def is_animated(clip, scene_ids=(), *, cover_photo_motion=False):
    if (clip.get('source_kind') or 'scene') == 'footage':
        return False
    return bool(clip.get('source_video_asset_id') or clip.get('media_type') == 'video'
                or clip.get('foreground_effect') or (clip.get('camera_motion') and not cover_photo_motion) or clip.get('scene_id') in scene_ids)


def protected_reason(clip, scene_ids=(), *, cover_photo_motion=False):
    if is_animated(clip, scene_ids, cover_photo_motion=cover_photo_motion):
        return 'animated'
    if clip.get('excluded'):
        return 'excluded'
    if clip.get('media_replacement_locked'):
        return 'locked'
    if clip.get('footage_overlay') or clip.get('source_kind') == 'footage':
        return 'existing_footage'
    return None


def ensure_unanimated(service, project_id, clips, coverage=None):
    settings = policy(coverage)
    scene_ids = animated_scene_ids(service, project_id, settings)
    if any(is_animated(c, scene_ids, cover_photo_motion=settings.get('cover_photo_motion', False)) for c in clips):
        raise DomainError('Кадр уже анимирован. Автоматические вставки не могут закрывать готовую анимацию.', 409)


def union_ms(intervals):
    end, total = -math.inf, 0.0
    for start, stop in sorted(intervals):
        if stop > max(start, end):
            total += stop - max(start, end)
        end = max(end, stop)
    return total


def timeline_state(document):
    timeline = normalize(document)
    total = max(0.0, float(timeline['duration_ms']))
    intervals = [(max(0.0, c['start_ms']), min(total, c['end_ms']))
                 for c in timeline['clips'] if c.get('source_kind') == 'footage']
    return timeline, total, intervals


def assert_budget(before, after, value):
    settings = policy(value)
    if settings['mode'] != 'limited':
        return
    _, total, old = timeline_state(before)
    _, _, new = timeline_state(after)
    maximum = total * settings['percent'] / 100
    if union_ms(new) > max(maximum, union_ms(old)) + EPS:
        raise DomainError('Вставки превысят выбранную долю таймлайна. Пересчитайте распределение.', 409)


def select(document, proposals, value=None, *, accepted_ids=None, scene_ids=()):
    settings = policy(value)
    if accepted_ids is not None and (not isinstance(accepted_ids, list) or any(not isinstance(v, str) for v in accepted_ids)):
        raise DomainError('Передайте список выбранных вставок.')
    timeline, total, existing = timeline_state(document)
    current = {c['edit_clip_id']: c for c in document['clips']}
    times = {c['edit_clip_id']: c for c in timeline['clips']}
    allowed = None if accepted_ids is None else set(accepted_ids)
    cover_photo_motion = settings.get('cover_photo_motion', False)
    candidates, skipped = [], {}
    seen = set()
    for proposal in proposals:
        pid = proposal['proposal_id']
        if allowed is not None and pid not in allowed:
            continue
        status = proposal.get('match_status')
        if status == 'rejected' or status not in ('matched', 'needs_review') or (allowed is None and status != 'matched'):
            skipped[pid] = 'unapproved_match'
            continue
        ids = proposal.get('replace_clip_ids') or [proposal.get('edit_clip_id')]
        if pid in seen or not ids or any(cid not in current or cid not in times for cid in ids):
            skipped[pid] = 'changed_segment'
            continue
        seen.add(pid)
        reasons = (protected_reason(current[cid], scene_ids, cover_photo_motion=cover_photo_motion) for cid in ids)
        reason = next((reason for reason in reasons if reason), None)
        if reason:
            skipped[pid] = reason
            continue
        intervals = [(max(0.0, times[cid]['start_ms']), min(total, times[cid]['end_ms'])) for cid in ids]
        if union_ms(intervals) <= 0:
            skipped[pid] = 'empty_segment'
            continue
        candidates.append({'proposal': proposal, 'ids': set(ids), 'intervals': intervals,
                           'center': (intervals[0][0] + intervals[-1][1]) / 2,
                           'duration': union_ms(intervals), 'status': status})

    occupied, used, chosen = list(existing), set(), []
    maximum = total * settings['percent'] / 100 if settings['mode'] == 'limited' else total
    available = sum(c['duration'] for c in candidates)

    def affordable(c):
        return not (c['ids'] & used) and union_ms(occupied + c['intervals']) <= maximum + EPS

    def take(c):
        chosen.append(c)
        occupied.extend(c['intervals'])
        used.update(c['ids'])
        candidates.remove(c)

    if settings['distribution'] == 'spread' and candidates and maximum > union_ms(existing):
        typical = statistics.median(c['duration'] for c in candidates)
        slots = min(len(candidates), max(1, int((maximum - union_ms(existing)) / typical)))
        for index in range(slots):
            target = total * (index + .5) / slots
            fits = [c for c in candidates if affordable(c)]
            if fits:
                take(min(fits, key=lambda c: (c['status'] != 'matched', abs(c['center'] - target), c['center'])))
    while True:
        fits = [c for c in candidates if affordable(c)]
        if not fits:
            break
        anchors = [c['center'] for c in chosen] + [(a + b) / 2 for a, b in existing]
        if settings['distribution'] == 'spread' and anchors:
            take(max(fits, key=lambda c: (c['status'] == 'matched', min(abs(c['center'] - anchor) for anchor in anchors), -c['center'])))
        else:
            take(min(fits, key=lambda c: (c['status'] != 'matched', -float(c['proposal'].get('retrieval_score') or 0), c['center'])))
    for c in candidates:
        skipped[c['proposal']['proposal_id']] = 'coverage_limit' if not (c['ids'] & used) else 'overlapping_segment'
    chosen.sort(key=lambda c: c['center'])
    final = union_ms(occupied)
    summary = {'policy': settings, 'duration_ms': total, 'existing_covered_ms': union_ms(existing),
               'added_covered_ms': final - union_ms(existing), 'planned_covered_ms': final,
               'actual_percent': round(100 * final / total, 2) if total else 0,
               'existing_percent': round(100 * union_ms(existing) / total, 2) if total else 0,
               'target_percent': settings['percent'] if settings['mode'] == 'limited' else None,
               'selected_count': len(chosen), 'available_candidate_ms': available,
               'animated_clips_protected': sum(is_animated(c, scene_ids, cover_photo_motion=cover_photo_motion) for c in current.values() if not c.get('excluded')),
               'below_target': final + EPS < maximum,
               'already_above_target': union_ms(existing) > maximum + EPS}
    return {'selected_proposal_ids': [c['proposal']['proposal_id'] for c in chosen],
            'coverage': summary, 'skipped': skipped}

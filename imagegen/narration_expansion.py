"""Add narration shots without changing any existing scene or media binding.

Event coverage and the spoken timeline are different contracts. Existing event
quotations remain authoritative; additional angles intentionally reuse parts of
an event. The separate narration spans partition the recording exactly once.
"""
import copy
import json
import math
import re
from uuid import NAMESPACE_URL, uuid5

from .db import dump, now, uid
from .domain import DomainError, MAX_SCENES, fingerprint
from .source_contract import coverage, source_span
from .voice_alignment import summarize_range

VERSION = 'narration-expansion/1'
ANGLES = [
    ('medium', 'A distinct medium view of the narrated subject in its established setting.'),
    ('detail', 'A closer view of a source-described physical detail of the narrated subject.'),
    ('wide', 'A complementary wider view showing the source-described spatial relationship.'),
    ('side', 'A complementary side view of the same source-backed subject and event.'),
]


def narration_frames(story, words, scenes, duration, target_seconds=7):
    if not scenes or not math.isfinite(duration) or duration <= 0:
        raise DomainError('Нужны сохранённые сцены и длительность озвучки.', 409)
    if not 4 <= target_seconds <= 20:
        raise DomainError('Длительность кадра: от 4 до 20 секунд.')
    matched = [w for w in words if w.get('start') is not None]
    if (any(not math.isfinite(w['start']) or not 0 <= w['start'] <= duration for w in matched)
            or any(a['start'] > b['start']+.001 for a, b in zip(matched, matched[1:]))):
        raise DomainError('Время слов нарушает порядок записи.', 409)
    check = coverage(story, scenes)
    if not check['ok']:
        raise DomainError('Сначала восстановите полное последовательное покрытие сценария.', 409)
    # Re-expansion must use the event anchors, not treat previous extra angles
    # as fresh events and recursively double the storyboard.
    if any(s['data'].get('narration_parent') for s in scenes):
        raise DomainError('Дополнительные сцены уже сохранены. Используйте готовый план.', 409)
    groups = []
    for scene in scenes:
        span = source_span(story, scene['data'])
        subset = [w for w in words if span['start'] <= w['char_start'] < span['end'] and w.get('start') is not None]
        if not subset:
            raise DomainError('Нет опоры в речи для кадра ' + str(scene['position']), 409)
        groups.append({'scene': scene, 'span': span, 'words': subset, 'cuts': [(span['start'], subset[0]['start'])]})
    for i, group in enumerate(groups):
        group['cuts'][0] = (group['span']['start'], 0 if i == 0 else group['cuts'][0][1])
        group['end'] = groups[i+1]['cuts'][0][1] if i+1 < len(groups) else duration
        if group['end'] <= group['cuts'][0][1]:
            raise DomainError('Время событий нарушает порядок. Текущие сцены сохранены.', 409)
    target = min(MAX_SCENES, max(len(groups), math.ceil(duration/target_seconds)))
    # Only punctuation-supported acoustic boundaries may split an event.
    # Long unsplittable events remain explicitly reported instead of inventing
    # text, padding with unrelated pictures, or silently losing audio.
    candidates = []
    for index, group in enumerate(groups):
        subset = group['words']
        for left, right in zip(subset, subset[1:]):
            between = story[left['char_end']:right['char_start']]
            if not re.search(r'[,;:.!?—]', between):
                continue
            if right.get('method') not in ('exact', 'fuzzy', 'compound', 'normalized_group'):
                continue
            if right.get('timing_group') and right['timing_group']['char_start'] < right['char_start']:
                continue
            candidates.append((index, right['char_start'], right['start']))
    count = len(groups)
    while count < target:
        best = None
        for index, char, second in candidates:
            group = groups[index]
            cuts = group['cuts'] + [(group['span']['end'], group['end'])]
            if any(c == char for c, _ in cuts):
                continue
            interval = next(((a, b) for a, b in zip(cuts, cuts[1:]) if a[0] < char < b[0]), None)
            if not interval:
                continue
            a, b = interval
            left, right = second-a[1], b[1]-second
            if min(left, right) < 2.5:
                continue
            gain = (left+right)**2-left**2-right**2
            item = (gain, -index, -char, index, char, second)
            if best is None or item > best:
                best = item
        if best is None:
            break
        _, _, _, index, char, second = best
        groups[index]['cuts'].append((char, second))
        groups[index]['cuts'].sort()
        count += 1
    frames = []
    for group in groups:
        cuts = group['cuts'] + [(group['span']['end'], group['end'])]
        for part, ((a, start), (b, end)) in enumerate(zip(cuts, cuts[1:])):
            while a < b and story[a].isspace(): a += 1
            while b > a and story[b-1].isspace(): b -= 1
            support = summarize_range(story, words, a, b)
            frames.append({'position': len(frames)+1, 'parent_scene_id': group['scene']['id'],
                'parent_position': group['scene']['position'], 'part': part+1,
                'parts': len(cuts)-1, 'preserve_existing': part == 0,
                'narration_source': {'start': a, 'end': b, 'quote': story[a:b]},
                'timing': {'start': start, 'end': end, 'duration': end-start,
                    'speech_start': support['start'], 'speech_end': support['end'],
                    'timing_needs_review': support['needs_review'], 'timing_reasons': support['reasons'],
                    'boundary_method': 'source_event_or_punctuation', 'timing_accuracy_measured': False}})
    validate_timeline(story, frames, duration)
    return frames


def validate_timeline(story, frames, duration):
    if not frames or frames[0]['timing']['start'] != 0 or abs(frames[-1]['timing']['end']-duration) > 1e-6:
        raise DomainError('План не покрывает начало и конец озвучки.', 409)
    previous = 0
    chars = set()
    for frame in frames:
        timing, span = frame['timing'], frame['narration_source']
        values = [timing.get(k) for k in ('start', 'end', 'duration')]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise DomainError('Некорректное время кадра.', 409)
        if timing['duration'] <= 0 or abs(timing['start']-previous) > 1e-6 or abs(timing['end']-timing['start']-timing['duration']) > 1e-6:
            raise DomainError('В плане есть пропуск или повтор времени.', 409)
        if not 0 <= span['start'] < span['end'] <= len(story) or story[span['start']:span['end']] != span['quote']:
            raise DomainError('Фрагмент озвучки не совпадает со сценарием.', 409)
        covered = {i for i in range(span['start'], span['end']) if story[i].isalnum()}
        if chars & covered:
            raise DomainError('В плане повторяется текст озвучки.', 409)
        chars.update(covered)
        previous = timing['end']
    if chars != {i for i, char in enumerate(story) if char.isalnum()}:
        raise DomainError('План пропустил часть текста озвучки.', 409)


def additional_scene(parent, frame):
    data = copy.deepcopy(parent['data'])
    span = frame['narration_source']
    data.update(source_start=span['start'], source_end=span['end'], source_quote=span['quote'])
    data['semantic_parent_source'] = source_span_from_data(parent['data'])
    data['intentional_visual_reuse'] = True
    data['reuse_reason'] = 'Дополнительный план того же события для отдельного фрагмента озвучки.'
    data['narration_parent'] = {'scene_id': parent['id'], 'revision': parent['current_revision'], 'part': frame['part']}
    data['environment_reference_policy'] = 'isolate_unbound_participants/1'
    data['narration_prompt_policy'] = 'preserve_parent_scope/1'
    from .scene_semantics import signals
    if not data.get('participant_identity') and {item['kind'] for item in signals(parent['data']['source_quote'])} & {'purpose', 'denied_event'}:
        data['narration_visual_mode'] = 'neutral_detail'
    angle, direction = ANGLES[(frame['part']-2) % len(ANGLES)]
    packet = copy.deepcopy(data.get('forgotten_cities_prompt_contract') or {})
    parent_direction = data.get('audit_visual_direction') or packet.get('visual_direction') or data.get('composition') or data['beat']
    visual = (direction + ' Narration detail for this shot: ' + span['quote'] +
        '\nRetain the approved interpretation of the complete parent event: ' + parent_direction +
        '\nKeep the same era, participants and place. Preserve the parent editorial exclusions; '
        'do not turn quoted speculation, negation or a metaphor into a factual event. '
        'Add no unsupported participants, modern equipment, text, labels or inscriptions.')
    data.update(composition=visual, audit_visual_direction=visual, audit_source_quote=span['quote'],
        editorial_inheritance={'parent_scene_id': parent['id'], 'parent_revision': parent['current_revision'],
                              'direction_hash': fingerprint(parent_direction)}, shot=angle)
    for key in ('source_fidelity_plan', 'editorial_plan_id'):
        data.pop(key, None)
    if packet:
        packet.update(source_quote=span['quote'], visual_direction=visual,
            visual_source=span, compiled_prompt='', shot_type=angle)
        data['forgotten_cities_prompt_contract'] = packet
    if data.get('participant_identity'):
        data['participant_identity']['source'] = copy.deepcopy(span)
    data['voice_timing'] = copy.deepcopy(frame['timing'])
    data['animation'] = {'selected_engine': 'ken_burns', 'status': 'planned_ken_burns',
        'duration_seconds': math.ceil(frame['timing']['duration']*24)/24,
        'ken_burns_mode': 'auto', 'credit_cost': 0, 'motion_scope': 'camera_only'}
    return data


def source_span_from_data(data):
    return {'start': data['source_start'], 'end': data['source_end'], 'quote': data['source_quote']}


def neutral_additional(data, story):
    """A source-bound neutral cutaway must not turn mentioned threats into actors."""
    if not data.get('narration_parent') or data.get('narration_visual_mode') != 'neutral_detail':
        return False
    source = data.get('semantic_parent_source') or {}
    start, end = source.get('start'), source.get('end')
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(story)
            or story[start:end] != source.get('quote')):
        return False
    from .scene_semantics import signals
    return bool({item['kind'] for item in signals(source['quote'])} & {'purpose', 'denied_event'})


def ground_additional(service, scene, project):
    """Resolve a cutaway through its immutable, previously grounded event.

    A clause alone may omit its actor or era. Keeping those source-backed
    bindings is essential; treating the clause as an independent new event
    would reproduce the disappearing-participant bug.
    """
    data = scene['data']
    binding = data['narration_parent']
    parent = service.scene(binding.get('scene_id'))
    if parent['project_id'] != project['id'] or parent['current_revision'] != binding.get('revision') or parent['data'].get('narration_parent'):
        raise DomainError('Исходное событие дополнительного кадра изменилось. Обновите план.', 409)
    source = source_span(project['story'], data)
    original = source_span(project['story'], parent['data'])
    if not original['start'] <= source['start'] < source['end'] <= original['end']:
        raise DomainError('Дополнительный кадр выходит за пределы исходного события.', 409)
    grounded = service._ground_forgotten_scene(parent, project)
    packet = copy.deepcopy(grounded['data'].get('forgotten_cities_prompt_contract') or {})
    direction = data['audit_visual_direction']
    packet.update(source_quote=source['quote'], visual_source=source, visual_direction=direction,
        shot_type=data['shot'], shot_contract='Authoritative narration fragment: '+source['quote']+'. Depict: '+direction)
    packet.pop('editorial_decision', None)
    packet.pop('editorial_error', None)
    packet['compiled_prompt'] += '\nADDITIONAL SOURCE-BACKED SHOT: '+packet['shot_contract']
    inherited = copy.deepcopy(grounded['data'])
    # Production bindings are inherited from the complete event; the exact
    # new source and framing are the only semantic additions.
    for key in ('source_quote', 'source_start', 'source_end', 'semantic_parent_source',
                'narration_parent', 'intentional_visual_reuse', 'reuse_reason', 'voice_timing',
                'animation', 'audit_visual_direction', 'audit_source_quote', 'editorial_inheritance',
                'shot', 'participant_identity'):
        if key in data: inherited[key] = copy.deepcopy(data[key])
    if 'environment_reference_policy' in data:
        inherited['environment_reference_policy'] = data['environment_reference_policy']
    if 'narration_prompt_policy' in data:
        inherited['narration_prompt_policy'] = data['narration_prompt_policy']
    if data.get('narration_visual_mode'):
        if not neutral_additional(data, project['story']):
            raise DomainError('Нейтральная иллюстрация не подтверждена смыслом исходного события.', 409)
        inherited['narration_visual_mode'] = 'neutral_detail'
        inherited.update(visible_people=0, raw_visible_people=0, episodic_people=[], background_roles=[],
            audit_roles=[], audit_people=0, characters=[], character_ids=[], people_policy='none')
        inherited.pop('population_policy', None)
        inherited.pop('participant_identity', None)
        packet.update(city_population='uninhabited', source_roles=[])
        packet['shot_contract'] += ' Neutral factual detail: do not enact the threatened or denied action; no living actors.'
    inherited.pop('editorial_plan_id', None)
    inherited.pop('source_fidelity_plan', None)
    inherited.update(forgotten_cities_prompt_contract=packet, composition=direction, safe_framing_explanation=direction)
    return {**scene, 'data': inherited}


class NarrationExpansion:
    def __init__(self, workflow):
        self.workflow = workflow
        self.service, self.db = workflow.service, workflow.db

    def prepare(self, pid, asset, report, target_seconds=7):
        from .voice_plans import VoicePlans
        plans = VoicePlans(self.workflow)
        base = plans.base(pid)
        existing = [self.service.scene(s['id']) for s in base['scenes']]
        project = self.service.project(pid)
        media = [{'id': s['id'], 'image': s['active_generation_id'], 'video': s['video_asset_id']} for s in existing]
        if self.db.one("SELECT id FROM generations WHERE project_id=? AND state IN ('queued','preparing','submitted','running','saving','reconciling') LIMIT 1", (pid,)):
            raise DomainError('Дождитесь завершения текущей генерации.', 409)
        duration = self.service.editing.media(pid, asset['id'], 'audio')/1000
        frames = narration_frames(project['story'], report['words'], existing, duration, target_seconds)
        by_id = {s['id']: s for s in existing}
        identities = {}
        for s in existing:
            identity = s['data'].get('participant_identity')
            if identity:
                origin = self.db.one('SELECT g.scene_id FROM assets a JOIN generations g ON g.id=a.generation_id WHERE a.id=?', (identity['asset_id'],))
                if origin:
                    identities[origin['scene_id']] = identity
        for frame in frames:
            parent = by_id[frame['parent_scene_id']]
            if frame['preserve_existing']:
                frame.update(id=parent['id'], data=copy.deepcopy(parent['data']))
            else:
                if parent['id'] in identities and not parent['data'].get('participant_identity'):
                    parent = copy.deepcopy(parent)
                    parent['data']['participant_identity'] = copy.deepcopy(identities[parent['id']])
                span = frame['narration_source']
                frame.update(id=str(uuid5(NAMESPACE_URL, f'{VERSION}:{pid}:{asset["sha256"]}:{parent["id"]}:{span["start"]}:{span["end"]}')),
                             data=additional_scene(parent, frame))
        if plans.base(pid) != base:
            raise DomainError('Проект изменился при подготовке. Текущие кадры сохранены.', 409)
        document = {'kind': VERSION, 'version': VERSION, 'project_id': pid, 'base': base,
            'audio_asset_id': asset['id'], 'audio_sha256': asset['sha256'], 'duration': duration,
            'metrics': report['metrics'], 'alignment': report, 'frames': frames, 'media_base': media,
            'conflicts': [], 'timing_accuracy_measured': False,
            'target_seconds': target_seconds, 'preserved_count': len(existing),
            'added_count': len(frames)-len(existing)}
        return plans.save(pid, document)

    def apply(self, pid, plan):
        from .voice_plans import VoicePlans
        plans = VoicePlans(self.workflow)
        with self.workflow.lock, self.db.transaction() as conn:
            doc, job = plan['document'], self.workflow.get(pid)
            if job['result'].get('active_plan_id') == plan['id'] and plan['applied']:
                return job
            if job['pause_requested'] or job['state'] not in ('plan_review', 'ready'):
                raise DomainError('Дождитесь подготовки плана и продолжите процесс.', 409)
            if job['result'].get('candidate_plan_id') != plan['id'] or plans.base(pid) != doc['base']:
                raise DomainError('Проект изменился. Старый вариант не применён.', 409)
            asset = plans.audio(pid, doc['audio_asset_id'])
            if asset['sha256'] != doc['audio_sha256'] or job['audio_asset_id'] != asset['id']:
                raise DomainError('Озвучка изменилась. Подготовьте новую версию.', 409)
            project = self.service.project(pid)
            validate_timeline(project['story'], doc['frames'], doc['duration'])
            current = [self.service.scene(s['id']) for s in doc['base']['scenes']]
            if [{'id': s['id'], 'image': s['active_generation_id'], 'video': s['video_asset_id']} for s in current] != doc['media_base']:
                raise DomainError('Выбранные изображения или видео изменились. Старый вариант не применён.', 409)
            originals = {s['id']: s for s in current}
            retained = [f for f in doc['frames'] if f['preserve_existing']]
            if len(retained) != len(originals) or {f['id'] for f in retained} != set(originals) or any(f['data'] != originals[f['id']]['data'] for f in retained):
                raise DomainError('Дополнение должно сохранять все исходные кадры без изменений.', 409)
            conn.execute('UPDATE scenes SET position=-position WHERE project_id=?', (pid,))
            for frame in doc['frames']:
                if frame['preserve_existing']:
                    conn.execute('UPDATE scenes SET position=? WHERE id=?', (frame['position'], frame['id']))
                else:
                    conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (frame['id'], pid, frame['position']))
                    conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (frame['id'], 1, dump(frame['data']), now()))
            metadata = project['analysis_metadata']
            if isinstance(metadata, str): metadata = json.loads(metadata)
            metadata['narration_timeline'] = {'version': VERSION, 'plan_id': plan['id'], 'audio_asset_id': asset['id'],
                'audio_sha256': asset['sha256'], 'duration': doc['duration'], 'frames': [
                    {'scene_id': f['id'], 'timing': f['timing'], 'narration_source': f['narration_source']} for f in doc['frames']]}
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?', (dump(metadata), pid))
            edit = self.service.editing.get(pid)
            document = edit['document']
            document['audio_tracks'] = [a for a in document['audio_tracks'] if a['role'] != 'voice'] + [
                {'role': 'voice', 'asset_id': asset['id'], 'trim_in_ms': 0, 'trim_out_ms': doc['duration']*1000, 'offset_ms': 0, 'volume': 1}]
            document = self.service.editing.validate(pid, document)
            conn.execute('INSERT INTO project_edits VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,document_json=excluded.document_json,updated_at=excluded.updated_at',
                (pid, edit['revision']+1, 1, dump(document), now()))
            result = {**job['result'], 'planned': True, 'candidate_plan_id': None, 'active_plan_id': plan['id'],
                'frame_count': len(doc['frames']), 'duration': doc['duration'], 'metrics': doc['metrics'],
                'preserved_count': doc['preserved_count'], 'added_count': doc['added_count']}
            conn.execute('INSERT INTO voice_plan_applications VALUES(?,?,?,?,?)', (uid(), pid, plan['id'], dump(doc['base']), now()))
            conn.execute("UPDATE voice_workflows SET state='ready',stage='План дополнен. Готовые кадры и клипы сохранены.',result_json=?,progress=100,updated_at=? WHERE project_id=?", (dump(result), now(), pid))
            self.db.event(conn, pid, 'narration_expansion_applied', {'plan_id': plan['id'], 'preserved_count': doc['preserved_count'], 'added_count': doc['added_count']})
        return self.workflow.get(pid)

    def montage(self, pid):
        project = self.service.project(pid)
        metadata = project['analysis_metadata']
        if isinstance(metadata, str): metadata = json.loads(metadata)
        timeline = metadata.get('narration_timeline')
        if not timeline:
            raise DomainError('Сначала подготовьте план по озвучке.', 409)
        edit = self.service.editing.get(pid)
        document = copy.deepcopy(edit['document'])
        old_clips = document['clips']
        ordered_ids = [f['scene_id'] for f in timeline['frames']]
        if (any(c.get('excluded') or c.get('source_kind', 'scene') != 'scene' for c in old_clips)
                or len({c['scene_id'] for c in old_clips}) != len(old_clips)
                or any(c['scene_id'] not in ordered_ids for c in old_clips)
                or [ordered_ids.index(c['scene_id']) for c in old_clips] != sorted(ordered_ids.index(c['scene_id']) for c in old_clips)
                or any(t.get('kind', 'cut') != 'cut' for t in document['transitions'])):
            raise DomainError('В монтаже есть ручной порядок, исключения, футажи или переходы. Сохранённый монтаж не заменён.', 409)
        by_scene = {c['scene_id']: c for c in old_clips}
        document['clips'] = []
        document['transitions'] = []
        for frame in timeline['frames']:
            scene = self.service.scene(frame['scene_id'])
            if not scene['video_asset_id']:
                raise DomainError('Не все сцены анимированы. Озвучка сохранена полностью.', 409)
            source = self.service.editing.media(pid, scene['video_asset_id'], 'video')
            wanted = frame['timing']['duration']*1000
            clip = copy.deepcopy(by_scene.get(scene['id']) or {})
            trim_in = clip.get('trim_in_ms', 0)
            trim_out = clip.get('trim_out_ms', min(source, wanted))
            if clip and clip.get('source_video_asset_id') != scene['video_asset_id']:
                raise DomainError('Источник сохранённого клипа изменился. Монтаж не заменён.', 409)
            used = trim_out-trim_in
            image = self.db.one('SELECT asset_id FROM generations WHERE id=?', (scene['active_generation_id'],))['asset_id']
            document['clips'].append({**clip, 'edit_clip_id': clip.get('edit_clip_id') or uid(), 'scene_id': scene['id'], 'source_scene_revision': scene['current_revision'],
                'source_video_asset_id': scene['video_asset_id'], 'source_image_asset_id': image,
                'trim_in_ms': trim_in, 'trim_out_ms': trim_out, 'playback_rate': used/wanted, 'muted': True, 'volume': clip.get('volume', 1), 'excluded': False})
        document = self.service.editing.validate(pid, document)
        from .editing import normalize
        if abs(normalize(document)['duration_ms']-timeline['duration']*1000) > .1:
            raise DomainError('Длительность монтажа не совпадает с озвучкой.', 409)
        if document == edit['document']:
            return edit
        return self.service.editing.save(pid, {'expected_revision': edit['revision'], 'document': document})

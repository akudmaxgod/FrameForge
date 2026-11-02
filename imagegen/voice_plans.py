"""Immutable voice plans with explicit application and preserved manual cuts."""
import copy
import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

from .db import dump, now, uid
from .domain import DomainError, fingerprint
from .source_contract import source_span


VERSION = 'voice-plan/1'


class VoicePlans:
    def __init__(self, workflow):
        self.workflow = workflow
        self.service, self.db = workflow.service, workflow.db

    def audio(self, pid, asset_id):
        asset = self.service.asset(asset_id)
        generation = self.db.one('SELECT purpose FROM generations WHERE id=?', (asset['generation_id'],))
        if asset['project_id'] != pid or not generation or generation['purpose'] != 'edit_audio':
            raise DomainError('Выберите озвучку этого проекта.')
        path = (self.service.root / asset['path']).resolve()
        if not path.is_relative_to(self.service.root.resolve()) or not path.is_file():
            raise DomainError('Файл озвучки недоступен.', 409)
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise DomainError('Файл озвучки изменился. Загрузите новую версию.', 409)
        return asset

    def base(self, pid):
        project = self.service.project(pid)
        scenes = [self.service.scene(row['id']) for row in self.db.all('SELECT id FROM scenes WHERE project_id=? ORDER BY position', (pid,))]
        return {'project_version': project['version'], 'story_hash': hashlib.sha256(project['story'].encode()).hexdigest(),
                'scenes': [{'id': s['id'], 'position': s['position'], 'revision': s['current_revision'],
                            'data_hash': fingerprint(s['data'])} for s in scenes],
                'edit_revision': self.service.editing.get(pid)['revision'],'project_state_hash':fingerprint(project)}

    @staticmethod
    def boundaries(frames, duration, preserve=None):
        """Use one value for both sides of every cut; manual choices win."""
        cuts = [0] + [f['data']['voice_timing']['start'] for f in frames[1:]] + [duration]
        manual, conflicts = {}, []
        for index, frame in enumerate(frames):
            old = (preserve or {}).get(frame['id'], {})
            for key, boundary in (('start', index), ('end', index + 1)):
                if not old.get('manual_' + key):
                    continue
                value = old.get(key)
                if boundary in manual and manual[boundary] != value:
                    conflicts.append('Соседние ручные границы не совпадают.')
                manual[boundary] = value
        for index, value in manual.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                conflicts.append('Ручная граница должна быть числом секунд.')
            elif (index == 0 and value != 0) or (index == len(frames) and value != duration):
                conflicts.append('Ручная граница не совпадает с началом или концом записи.')
            else:
                cuts[index] = value
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in cuts) or any(a >= b for a, b in zip(cuts, cuts[1:])):
            conflicts.append('Границы нарушают порядок или положительную длительность кадров.')
        if conflicts:
            return list(dict.fromkeys(conflicts))
        for index, frame in enumerate(frames):
            timing = frame['data']['voice_timing']
            timing.update(start=cuts[index], end=cuts[index + 1], duration=cuts[index + 1] - cuts[index],
                          manual_start=index in manual, manual_end=index + 1 in manual)
            if index in manual or index + 1 in manual:
                timing['boundary_method'] = 'manual'
            if frame['data'].get('frame_focus'):
                frame['data']['frame_focus']['timing']=copy.deepcopy(timing)
        return []

    def prepare(self, pid, plan, frames, duration, metrics, metadata, edit_document, alignment, expected_base=None):
        base = expected_base or self.base(pid)
        if self.base(pid)!=base:raise DomainError('Проект изменился во время подготовки. Ручные правки сохранены.',409)
        story = self.service.project(pid)['story']
        if metadata.get('story_visual_plan'):
            from .story_visual_plan import validate_scenes
            validate_scenes(story,metadata['story_visual_plan'],plan)
        existing = [self.service.scene(s['id']) for s in base['scenes']]
        exact = {(s['data'].get('source_start'), s['data'].get('source_end'), s['data'].get('source_quote')): s for s in existing}
        output, preserve, conflicts = [], {}, []
        for position, (data, frame) in enumerate(zip(plan, frames), 1):
            span = source_span(story, data)
            previous = exact.get((span['start'], span['end'], span['quote']))
            sid = previous['id'] if previous else str(uuid5(NAMESPACE_URL, f'{pid}:{base["story_hash"]}:{span["start"]}:{span["end"]}'))
            output.append({'id': sid, 'position': position, 'parent_scene_id': frame.get('parent_scene_id'), 'data': copy.deepcopy(data)})
            if previous:
                preserve[sid] = previous['data'].get('voice_timing') or {}
        matched = {f['id'] for f in output}
        for scene in existing:
            timing = scene['data'].get('voice_timing') or {}
            if (timing.get('manual_start') or timing.get('manual_end')) and scene['id'] not in matched:
                conflicts.append('Смысловые фрагменты изменились: ручная граница не перенесена автоматически.')
        conflicts += self.boundaries(output, duration, preserve)
        metadata=copy.deepcopy(metadata)
        if metadata.get('story_visual_plan'):
            from .frame_event_contract import retime
            retime(story,metadata['story_visual_plan'],output)
        job = self.workflow.get(pid)
        asset = self.audio(pid, job['audio_asset_id'])
        document = {'version': VERSION, 'project_id': pid, 'base': base, 'audio_asset_id': asset['id'],
                    'audio_sha256': asset['sha256'], 'duration': duration, 'metrics': metrics,
                    'source_scenes':existing, 'frames': output, 'metadata': metadata, 'edit_document': edit_document,
                    'alignment': alignment, 'conflicts': list(dict.fromkeys(conflicts)),
                    'timing_accuracy_measured': False}
        from .voice_boundary_audit import build
        document['boundary_audit']=build(story,alignment['words'],output,duration,asset['sha256'])
        return self.save(pid, document)

    def save(self, pid, document):
        digest = fingerprint(document)
        with self.db.transaction() as db:
            existing = db.execute('SELECT id FROM voice_plan_versions WHERE project_id=? AND content_hash=?', (pid, digest)).fetchone()
            if existing:
                plan_id = existing['id']
            else:
                revision = db.execute('SELECT coalesce(max(revision),0)+1 FROM voice_plan_versions WHERE project_id=?', (pid,)).fetchone()[0]
                plan_id = uid()
                db.execute('INSERT INTO voice_plan_versions VALUES(?,?,?,?,?,?)', (plan_id, pid, revision, digest, dump(document), now()))
        return self.get(pid, plan_id)

    def get(self, pid, plan_id):
        self.service.project(pid)
        row = self.db.one('SELECT * FROM voice_plan_versions WHERE id=? AND project_id=?', (plan_id, pid))
        if not row:
            raise DomainError('Версия плана не найдена.', 404)
        row['document'] = json.loads(row['document'])
        row['applied'] = bool(self.db.one('SELECT id FROM voice_plan_applications WHERE plan_id=?', (plan_id,)))
        reason=None
        expansion = row['document'].get('kind') == 'narration-expansion/1'
        if not expansion:
            try:
                self.workflow.assert_replaceable(pid,preserve_references=bool(row['document'].get('metadata',{}).get('narrative_storyboard')))
            except DomainError:
                reason='В проекте уже начато создание изображений, видео или монтажа. Вариант доступен для просмотра; применение к этому проекту заблокировано.'
        if not reason and not row['applied'] and self.base(pid)!=row['document']['base']:
            reason='Проект изменился после подготовки. Подготовьте новую версию; текущие сцены сохранены.'
        row['apply_blocked_reason']=reason
        return row

    def overview(self, pid):
        self.service.project(pid)
        rows = self.db.all('SELECT id,revision,created_at FROM voice_plan_versions WHERE project_id=? ORDER BY revision DESC LIMIT 50', (pid,))
        for row in rows:
            plan = self.get(pid, row['id'])
            row.update(applied=plan['applied'], frames=len(plan['document']['frames']), conflicts=plan['document']['conflicts'])
        job = self.workflow.get(pid)
        return {'versions': rows, 'candidate_id': (job or {}).get('result', {}).get('candidate_plan_id'),
                'active_id': (job or {}).get('result', {}).get('active_plan_id')}

    def choose(self, pid, plan_id):
        plan = self.get(pid, plan_id)
        job = self.workflow.get(pid)
        if not job or job['state'] in ('queued', 'recognizing', 'analyzing', 'aligning', 'images', 'animating', 'assembling', 'exporting'):
            raise DomainError('Дождитесь завершения текущей операции.', 409)
        if job['result'].get('active_plan_id')==plan_id and plan['applied']:return plan
        result = {**job['result'], 'candidate_plan_id': plan_id, 'planned': False}
        self.workflow.update(pid, state='plan_review', result_json=result, stage='Версия плана сохранена отдельно. Просмотрите и примените её.')
        return plan

    def edit_boundary(self, pid, plan_id, index, seconds):
        plan = self.get(pid, plan_id)
        if plan['document'].get('kind') == 'narration-expansion/1':
            raise DomainError('Границы дополнения привязаны к речи. Для ручной корректировки используйте монтаж.', 409)
        doc = copy.deepcopy(plan['document'])
        if type(index) is not int or not 1 <= index < len(doc['frames']) or type(seconds) not in (int, float) or not math.isfinite(seconds):
            raise DomainError('Укажите внутреннюю границу и время в секундах.')
        current = self.base(pid)
        if current['story_hash'] != doc['base']['story_hash']:
            raise DomainError('Сценарий изменился. Подготовьте новую версию плана.', 409)
        # Rebase only an applied version whose source spans still match.
        if plan['applied']:
            scenes = [self.service.scene(s['id']) for s in current['scenes']]
            if [(s['id'], s['data'].get('source_start'), s['data'].get('source_end'), s['data'].get('source_quote')) for s in scenes] != [
                    (f['id'], f['data']['source_start'], f['data']['source_end'], f['data']['source_quote']) for f in doc['frames']]:
                raise DomainError('Сцены изменились. Подготовьте новую версию плана.', 409)
            for frame, scene in zip(doc['frames'], scenes):
                frame['data'] = copy.deepcopy(scene['data'])
            doc['base'] = current
            doc['edit_document'] = self.service.editing.get(pid)['document']
        elif current != doc['base']:
            raise DomainError('Проект изменился после подготовки плана. Подготовьте новую версию.', 409)
        preserve = {f['id']: copy.deepcopy(f['data']['voice_timing']) for f in doc['frames']}
        left, right = doc['frames'][index - 1], doc['frames'][index]
        preserve[left['id']].update(end=seconds, manual_end=True)
        preserve[right['id']].update(start=seconds, manual_start=True)
        conflicts = self.boundaries(doc['frames'], doc['duration'], preserve)
        if conflicts:
            raise DomainError(' '.join(conflicts), 409)
        doc['conflicts'] = []
        if doc.get('metadata',{}).get('story_visual_plan'):
            from .frame_event_contract import retime
            retime(self.service.project(pid)['story'],doc['metadata']['story_visual_plan'],doc['frames'])
        if doc.get('boundary_audit'):
            from .voice_boundary_audit import build
            doc['boundary_audit']=build(self.service.project(pid)['story'],doc['alignment']['words'],doc['frames'],doc['duration'],doc['audio_sha256'])
        doc['parent_plan_id'] = plan_id
        result = self.save(pid, doc)
        self.choose(pid, result['id'])
        return result

    def apply(self, pid, plan_id):
        preview = self.get(pid, plan_id)
        if preview['document'].get('kind') == 'narration-expansion/1':
            from .narration_expansion import NarrationExpansion
            return NarrationExpansion(self.workflow).apply(pid, preview)
        narrative=bool(preview['document'].get('metadata',{}).get('narrative_storyboard'))
        if narrative:
            # Additional views may reuse a checked event, never introduce a
            # new visible claim under the cover of an audio-timing update.
            from .narrative_contract import verify_applied
            checked_metadata=copy.deepcopy(preview['document']['metadata'])
            checked_metadata['voice_alignment']={'duration':preview['document']['duration']}
            verify_applied({'story':self.service.project(pid)['story'],'analysis_metadata':checked_metadata},
                           preview['document']['frames'])
        with self.workflow.lock, self.db.transaction() as db:
            plan = self.get(pid, plan_id)
            doc = plan['document']
            job = self.workflow.get(pid)
            if not job or job['pause_requested']:
                raise DomainError('Сначала продолжите процесс.', 409)
            if job['result'].get('active_plan_id') == plan_id and plan['applied']:
                return self.workflow.get(pid)
            if job['state'] in ('queued','recognizing','analyzing','aligning','images','animating','assembling','exporting'):
                raise DomainError('Дождитесь завершения текущей операции.',409)
            if job['result'].get('candidate_plan_id') != plan_id:
                raise DomainError('Выберите текущий подготовленный вариант.', 409)
            if doc['conflicts']:
                raise DomainError(' '.join(doc['conflicts']), 409)
            self.workflow.assert_replaceable(pid,preserve_references=narrative)
            if self.base(pid) != doc['base']:
                raise DomainError('Проект или монтаж изменились. Старый вариант не применён.', 409)
            asset = self.audio(pid, doc['audio_asset_id'])
            if asset['sha256'] != doc['audio_sha256'] or job['audio_asset_id'] != asset['id']:
                raise DomainError('Озвучка изменилась. Старый вариант не применён.', 409)
            story = self.service.project(pid)['story']
            if doc['metadata'].get('story_visual_plan'):
                from .story_visual_plan import validate_scenes
                validate_scenes(story,doc['metadata']['story_visual_plan'],doc['frames'])
            for frame in doc['frames']:
                source_span(story, frame['data'])
            # Keep matching scene identities and all previous revisions.
            wanted = {f['id'] for f in doc['frames']}
            for scene in doc['base']['scenes']:
                if scene['id'] not in wanted:
                    db.execute('DELETE FROM scene_revisions WHERE scene_id=?', (scene['id'],))
                    db.execute('DELETE FROM scenes WHERE id=?', (scene['id'],))
            db.execute('UPDATE scenes SET position=-position WHERE project_id=?',(pid,))
            old = {s['id']: s for s in doc['base']['scenes']}
            for frame in doc['frames']:
                sid = frame['id']
                if sid in old:
                    revision = old[sid]['revision'] + 1
                    db.execute('UPDATE scenes SET position=?,current_revision=? WHERE id=?', (frame['position'], revision, sid))
                else:
                    revision = 1
                    db.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (sid, pid, frame['position']))
                db.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (sid, revision, dump(frame['data']), now()))
            result = {**job['result'], 'planned': True, 'active_plan_id': plan_id,
                      'candidate_plan_id': None, 'frame_count': len(doc['frames']), 'duration': doc['duration'], 'metrics': doc['metrics']}
            metadata = copy.deepcopy(doc['metadata'])
            metadata['voice_alignment'] = {k: result[k] for k in ('frame_count', 'duration', 'metrics', 'active_plan_id')}
            db.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?', (dump(metadata), pid))
            db.execute('INSERT INTO project_edits VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,document_json=excluded.document_json,updated_at=excluded.updated_at',
                       (pid, doc['base']['edit_revision'] + 1, 1, dump(doc['edit_document']), now()))
            db.execute('INSERT INTO voice_plan_applications VALUES(?,?,?,?,?)', (uid(), pid, plan_id, dump(doc['base']), now()))
            db.execute("UPDATE voice_workflows SET state='ready',stage='План применён. Кадры сохранены по времени озвучки.',progress=100,result_json=?,updated_at=? WHERE project_id=?", (dump(result), now(), pid))
        return self.workflow.get(pid)

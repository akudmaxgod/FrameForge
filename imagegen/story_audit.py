"""Explicit, persisted vision audit; never starts generation on its own."""
import hashlib
import json
from datetime import datetime, timezone
from .domain import DomainError
from .db import dump, now, uid
from .forgotten_cities.fidelity import SourceFidelityReviewer, source_context

AUDIT_VERSION = 'story-audit/2'

_FORMAT_REASONS = {
    'missing_frames': 'Gemini не вернул список результатов.',
    'missing_scene': 'Gemini не вернул результат для этого кадра.',
    'duplicate_scene': 'Gemini вернул несколько результатов для одного кадра.',
    'invalid_status': 'Gemini вернул неизвестный статус проверки.',
    'invalid_text': 'Gemini вернул некорректные поля описания.',
    'unknown_artifact': 'Gemini указал эталон, которого нет в этом проекте.',
    'missing_correction': 'Gemini отметил проблему, но не предоставил исправленный промпт.',
}


def _audit_schema(scene_ids, names):
    artifact_list = {'type': 'array', 'items': {'type': 'string'}}
    if names:
        artifact_list['items']['enum'] = sorted(names)
    else:
        artifact_list['maxItems'] = 0
    properties = {'scene_id': {'type': 'string', 'enum': scene_ids},
        'status': {'type': 'string', 'enum': ['keep', 'meaning', 'variety', 'uncertain']},
        'observed': {'type': 'string'}, 'reason': {'type': 'string'},
        'required_artifacts': artifact_list, 'corrected_prompt': {'type': 'string'}}
    return {'type': 'object', 'required': ['frames'], 'additionalProperties': False,
        'properties': {'frames': {'type': 'array', 'minItems': len(scene_ids), 'maxItems': len(scene_ids),
            'items': {'type': 'object', 'additionalProperties': False,
                      'required': list(properties), 'properties': properties}}}}


def _parse_audit(result, scene_ids, names):
    """Normalize optional empty fields; never guess image-to-scene mapping."""
    indexed, errors = {}, {}
    entries = result.get('frames') if isinstance(result, dict) else None
    if not isinstance(entries, list):
        return indexed, {sid: 'missing_frames' for sid in scene_ids}
    canonical = {name.strip().casefold(): name for name in names}
    for sid in scene_ids:
        matches = [e for e in entries if isinstance(e, dict) and e.get('scene_id') == sid]
        if len(matches) != 1:
            errors[sid] = 'duplicate_scene' if matches else 'missing_scene'
            continue
        entry = dict(matches[0])
        status = entry.get('status')
        if not isinstance(status, str) or status.strip().lower() not in ('keep', 'meaning', 'variety', 'uncertain'):
            errors[sid] = 'invalid_status'; continue
        entry['status'] = status.strip().lower()
        # Empty optional fields are often returned as null or omitted for keep.
        if entry['status'] in ('keep', 'uncertain') and entry.get('corrected_prompt') is None:
            entry['corrected_prompt'] = ''
        if entry.get('observed') is None:
            entry['observed'] = ''
        if not all(isinstance(entry.get(k), str) for k in ('observed', 'reason', 'corrected_prompt')) or not entry['reason'].strip() or len(entry['corrected_prompt']) > 12000:
            errors[sid] = 'invalid_text'; continue
        required = entry.get('required_artifacts') or []
        if not isinstance(required, list) or any(not isinstance(n, str) or n.strip().casefold() not in canonical for n in required):
            errors[sid] = 'unknown_artifact'; continue
        entry['required_artifacts'] = list(dict.fromkeys(canonical[n.strip().casefold()] for n in required))
        if entry['status'] in ('meaning', 'variety') and not entry['corrected_prompt'].strip():
            errors[sid] = 'missing_correction'; continue
        indexed[sid] = {k: entry[k] for k in ('scene_id', 'status', 'observed', 'reason', 'required_artifacts', 'corrected_prompt')}
    return indexed, errors


def _save_diagnostic(service, key, result, errors, attempt):
    # Only the model's JSON verdict is stored; never transport bodies or credentials.
    try:
        directory = service.root / 'story_audit_diagnostics'
        directory.mkdir(exist_ok=True)
        (directory / f'{key}-{attempt}.json').write_text(dump({'errors': errors, 'response': result}), encoding='utf-8')
    except (OSError, TypeError, ValueError):
        pass


def ensure_skull_reference(service, project_id):
    """Explicit upgrade of an existing analysis; creates no generation jobs."""
    from .forgotten_cities.artifacts import skull_evidence, skull_name, skull_constraints
    project = service.project(project_id)
    channel = service.db.one('SELECT profile_type FROM channel_profiles WHERE id=?', (project.get('channel_profile_id'),))
    if not channel or channel['profile_type'] != 'forgotten_cities':
        raise DomainError('Эталоны находок доступны в профиле «Забытые города».', 409)
    evidence = skull_evidence(project['story'])
    if not evidence:
        return {'created': False, 'reason': 'В сюжете нет подтверждённого черепа с треугольным отверстием.'}
    name = skull_name(evidence[2])
    description = evidence[2] + ' ' + ' '.join(skull_constraints(project['story']))
    scenes = service.db.all('SELECT position,data FROM scenes JOIN scene_revisions ON scenes.id=scene_id AND current_revision=revision WHERE project_id=? ORDER BY position', (project_id,))
    first = next((s['position'] for s in scenes if (json.loads(s['data']).get('source_start') or 0) + len(json.loads(s['data']).get('source_quote', '')) > evidence[0]), 1)
    with service.db.transaction() as conn:
        metadata = json.loads(conn.execute('SELECT analysis_metadata FROM projects WHERE id=?', (project_id,)).fetchone()[0])
        forgotten = metadata.get('forgotten_cities')
        if not forgotten:
            raise DomainError('Сначала выполните профильный анализ.', 409)
        existing = conn.execute('SELECT id FROM props WHERE project_id=? AND name=?', (project_id, name)).fetchone()
        prop_id = existing[0] if existing else uid()
        if not existing:
            conn.execute('INSERT INTO props(id,project_id,name,description,importance,first_appearance) VALUES(?,?,?,?,?,?)',
                         (prop_id, project_id, name, description, 'story', first))
        anchors = forgotten.setdefault('anchors', [])
        if not any(a.get('anchor_id') == 'anchor_artifact_trepanned_skull' for a in anchors):
            anchors.append({'anchor_id': 'anchor_artifact_trepanned_skull', 'anchor_type': 'artifact',
                'canonical_name': name, 'description': description, 'time_layer': 'museum_display',
                'reference_required': True, 'importance': 1.0, 'recurrence_count': 1,
                'evidence_spans': [{'start': evidence[0], 'end': evidence[1], 'text': evidence[2]}]})
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?', (dump(metadata), project_id))
    return {'created': not bool(existing), 'prop_id': prop_id}


def audit_chunk(service, project_id, scene_ids, reviewer=None):
    if not isinstance(scene_ids, list) or not 1 <= len(scene_ids) <= 4 or any(not isinstance(sid, str) for sid in scene_ids) or len(set(scene_ids)) != len(scene_ids):
        raise DomainError('Выберите 1–4 готовых кадра для пакета аудита.')
    project = service.project(project_id)
    frames, images = [], []
    metadata = project.get('analysis_metadata') or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    anchors = [a for a in (metadata.get('forgotten_cities') or {}).get('anchors', []) if a.get('anchor_type') == 'artifact']
    names = {a['canonical_name'] for a in anchors}
    for sid in scene_ids:
        scene = service.scene(sid)
        if scene['project_id'] != project_id:
            raise DomainError('Кадр принадлежит другому проекту.', 409)
        gen = service.db.one('SELECT * FROM generations WHERE id=?', (scene.get('active_generation_id'),))
        if not gen or not gen.get('asset_id'):
            raise DomainError('Кадр ещё не готов.', 409)
        asset = service.asset(gen['asset_id'])
        frames.append({'scene_id': sid, 'position': scene['position'], 'revision': scene['current_revision'],
                       'asset_id': asset['id'], 'source': source_context(scene['data'], project['story'])})
        images.append((service.root / asset['path']).read_bytes())
    key = hashlib.sha256(dump({'version': AUDIT_VERSION, 'story': project['story'], 'frames': frames}).encode()).hexdigest()
    cached = service.db.all('SELECT * FROM story_audits WHERE project_id=? AND chunk_key=?', (project_id, key))
    if len(cached) == len(frames) and all(json.loads(row['data']).get('status') != 'unverified' for row in cached):
        return {'items': [json.loads(row['data']) for row in cached], 'stop_audit': False}
    context_start = max(0, min(f['source']['start'] for f in frames) - 2400)
    context_end = max(f['source']['end'] for f in frames) + 1200
    inputs = [{**f, 'source': {k: v for k, v in f['source'].items() if k != 'context'}} for f in frames]
    prompt = ('Audit these documentary frames in the given image order (position is the actual frame number). Each image maps to exactly one input scene_id. '
              'Judge the selected quote with its neighbouring context, not just whether the image fits the city in general. '
              'Check the primary subject, concrete action, negation, metaphor, physical scale relative to anatomy, '
              'living/buried state, historical/modern layer, invented readable labels, and stable defining features across frames. '
              'A skull explicitly described as having a triangular opening cannot change to a round hole in the next frame. '
              'Check full-story object definitions: a board with twenty circular fields must not gain extra fields; '
              'one goat-painted vessel must retain its shape and five successive poses. A macaque burial must not use a human skull. '
              'Textile, fibre and spindle passages must depict textile work, not the eye from an earlier chapter. '
              'Accept minor lighting, clothing and framing differences. Enlarged macro photography alone is NOT an oversized object. '
              'Distinguish factual problems from monotonous editing: do not mark repetition as a factual contradiction. '
              'Use variety only when the replacement introduces a specific different composition or source-supported action; explain the difference. Do not propose the same existing close-up again. '
              'For variety propose only source-supported details, views or actions; never invent status, emotions, surgery or biography. '
              'Return JSON {"frames":[{"scene_id":"input id","status":"keep|meaning|variety|uncertain",'
              '"observed":"visible evidence", "reason":"specific comparison", '
              '"required_artifacts":["canonical name from anchors, only if the replacement depicts that object"], '
              '"corrected_prompt":"complete English visual direction for replacement, empty for keep/uncertain"}]}. '
              'Write observed and reason in Russian. The corrected prompt must preserve source-supported time layer, people and deceased/living state. '
              'Keep each correction within 1600 characters, with the primary subject and action first. '
              'Cover every input exactly once. Do not include generic suggestions to replace all images. '
              'No generation is requested. Data: ' + dump({'story_context': project['story'][context_start:context_end],
                  'frames': inputs, 'anchors': [{'canonical_name': a['canonical_name'], 'description': a.get('description', '')[:800]} for a in anchors]}))
    reviewer = reviewer or SourceFidelityReviewer(service.root, service.settings())
    indexed, errors, unavailable = {}, {}, False
    for attempt in range(2):
        try:
            result = reviewer.request(prompt, images, response_schema=_audit_schema(scene_ids, names))
            parsed, errors = _parse_audit(result, scene_ids, names)
            # A later malformed response must not destroy earlier valid answers.
            indexed.update({sid: entry for sid, entry in parsed.items() if sid not in indexed})
            errors = {sid: code for sid, code in errors.items() if sid not in indexed}
            if not errors:
                break
            _save_diagnostic(service, key, result, errors, attempt)
            prompt += '\nFORMAT REPAIR: Return exactly one verdict for each input scene_id and only canonical artifact names. Fix: ' + dump(errors)
        except Exception as error:
            # Never show arbitrary exception/transport text that could include credentials.
            text = str(error).casefold() if isinstance(error, DomainError) else ''
            code = 'quota_unavailable' if any(word in text for word in ('квот', 'исчерпан', 'лимит')) else 'service_unavailable'
            reason = 'Gemini сейчас недоступен или его квота исчерпана. Проверку можно продолжить позже.' if code == 'quota_unavailable' else 'Не получен ответ Gemini. Проверку можно продолжить позже.'
            errors = {sid: code for sid in scene_ids if sid not in indexed}
            unavailable = True
            break
    for sid, code in errors.items():
        indexed[sid] = {'status': 'unverified', 'observed': '', 'corrected_prompt': '',
            'audit_error_code': code, 'reason': (_FORMAT_REASONS[code] + ' Автоматический повтор не помог; можно проверить позже.') if code in _FORMAT_REASONS else reason}
    items = []
    with service.db.transaction() as conn:
        for frame in frames:
            verdict = {**indexed[frame['scene_id']], **frame, 'version': AUDIT_VERSION}
            verdict['id'] = hashlib.sha256((key + frame['scene_id']).encode()).hexdigest()
            stored = conn.execute('SELECT data FROM story_audits WHERE id=?', (verdict['id'],)).fetchone()
            if not stored:
                conn.execute('INSERT INTO story_audits VALUES(?,?,?,?,?)',
                             (verdict['id'], project_id, key, dump(verdict), now()))
            elif json.loads(stored[0]).get('status') == 'unverified':
                conn.execute('UPDATE story_audits SET data=? WHERE id=?', (dump(verdict), verdict['id']))
            else:
                verdict = json.loads(stored[0])
            items.append(verdict)
    return {'items': items, 'stop_audit': unavailable}


def audit_results(service, project_id):
    service.project(project_id)
    rows = service.db.all('SELECT data FROM story_audits WHERE project_id=? ORDER BY created_at DESC, rowid DESC', (project_id,))
    current = {s['id']: s for s in service.db.all('SELECT s.id,s.current_revision,g.asset_id,r.data FROM scenes s JOIN scene_revisions r ON r.scene_id=s.id AND r.revision=s.current_revision LEFT JOIN generations g ON g.id=s.active_generation_id WHERE s.project_id=?', (project_id,))}
    results, seen = [], set()
    for row in rows:
        entry = json.loads(row['data'])
        if entry['scene_id'] in seen:
            continue
        seen.add(entry['scene_id'])
        scene = current.get(entry['scene_id'])
        if not scene:
            continue
        pending_correction = not entry.get('batch_id') and scene['current_revision'] == entry['revision'] + 1 and json.loads(scene['data']).get('audit_origin_id') == entry['id']
        entry['stale'] = (scene['current_revision'] != entry['revision'] and not pending_correction) or scene.get('asset_id') != entry['asset_id']
        results.append(entry)
    return sorted(results, key=lambda x: x['position'])


def regenerate_audit(service, project_id, audit_id):
    row = service.db.one('SELECT * FROM story_audits WHERE id=? AND project_id=?', (audit_id, project_id))
    if not row:
        raise DomainError('Рекомендация не найдена.', 404)
    entry = json.loads(row['data'])
    dispatched = service.db.one('SELECT id FROM batches WHERE project_id=? AND idempotency_key=?', (project_id, 'audit-' + audit_id))
    if dispatched:
        return {'batch_id': dispatched['id'], 'reused': True}
    if entry.get('batch_id'):
        return {'batch_id': entry['batch_id'], 'reused': True}
    if entry['status'] not in ('meaning', 'variety'):
        raise DomainError('Нет подтверждённого промпта для замены.', 409)
    scene = service.scene(entry['scene_id'])
    gen = service.db.one('SELECT asset_id FROM generations WHERE id=?', (scene.get('active_generation_id'),)) or {}
    already_edited = scene['current_revision'] == entry['revision'] + 1 and scene['data'].get('audit_origin_id') == audit_id
    if (scene['current_revision'] != entry['revision'] and not already_edited) or gen.get('asset_id') != entry['asset_id']:
        raise DomainError('Кадр изменился после аудита. Проверьте новый вариант.', 409)
    if service.db.one("SELECT id FROM generations WHERE scene_id=? AND state IN ('queued','running') AND cancel_requested=0", (scene['id'],)):
        raise DomainError('Для этого кадра уже есть задача. Дождитесь её завершения.', 409)
    if not service.project_preflight(project_id).get('ok'):
        raise DomainError('Сначала закрепите обязательные эталоны и устраните ошибки подготовки.', 409)
    with service.db.transaction() as conn:
        current = json.loads(conn.execute('SELECT data FROM story_audits WHERE id=?', (audit_id,)).fetchone()[0])
        started = datetime.fromisoformat(current['dispatch_started']) if current.get('dispatch_started') else None
        leased = current.get('dispatching') and (not started or (datetime.now(timezone.utc) - started).total_seconds() < 600)
        if leased or current.get('batch_id'):
            raise DomainError('Перегенерация уже запускается.', 409)
        current['dispatching'] = True
        current['dispatch_started'] = now()
        conn.execute('UPDATE story_audits SET data=? WHERE id=?', (dump(current), audit_id))
    try:
        if not already_edited:
            service.edit_scene(scene['id'], {'audit_visual_direction': entry['corrected_prompt'],
                         'audit_origin_id': audit_id, 'audit_required_artifacts': entry.get('required_artifacts', []),
                         'audit_source_quote': scene['data'].get('source_quote', ''), 'manual_prompt': ''}, scene['current_revision'])
        batch = service.batch(project_id, 'audit-' + audit_id, [scene['id']])
        current.update(dispatching=False, batch_id=batch['id'])
    except Exception:
        current['dispatching'] = False
        # Retry this recommendation against its own correction revision only.
        with service.db.transaction() as conn:
            conn.execute('UPDATE story_audits SET data=? WHERE id=?', (dump(current), audit_id))
        raise
    with service.db.transaction() as conn:
        conn.execute('UPDATE story_audits SET data=? WHERE id=?', (dump(current), audit_id))
    return {'batch_id': batch['id']}

"""Immutable visual decisions bound to exact source and pinned catalogue state.

Decisions qualify an illustration; they never verify a historical claim or
rewrite narration, timing, images or a completed edit.
"""
import copy
import json
import re
from contextlib import closing

from .db import dump, now, uid
from .domain import DomainError, fingerprint
from .source_contract import source_span
from .scene_semantics import review, direction_rules, signals

VERSION = 'editorial-scene-plan/1'
MODES = {'literal', 'illustrative', 'reported_insert', 'neutral_detail', 'selected_clause'}
LAYERS = {'documented_event', 'historical_reconstruction', 'modern_ruins', 'excavation', 'museum_display'}


class EditorialPlans:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def catalog_token(self, pid):
        from .source_catalog import SourceCatalog
        catalog = SourceCatalog(self.service)
        if not catalog.path.exists():
            return fingerprint([])
        with catalog.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM links WHERE project_id=? ORDER BY anchor_id', (pid,))]
        return fingerprint(rows)

    def base(self, scene, project):
        return {'revision': scene['current_revision'], 'data_hash': fingerprint(scene['data']),
                'project_hash': fingerprint({k: v for k, v in project.items() if k != 'animation_settings'}),
                'project_hash_scope': 'source-project/1', 'catalog_hash': self.catalog_token(project['id'])}

    @staticmethod
    def project_matches(base, project):
        if base.get('project_hash_scope') == 'source-project/1':
            return base['project_hash'] == fingerprint({k: v for k, v in project.items() if k != 'animation_settings'})
        if base.get('project_hash_scope') is not None:
            return False
        # Old decisions included the unused, empty animation settings. Recover
        # only when the complete remaining project still has the exact old hash.
        return base['project_hash'] in (
            fingerprint(project), fingerprint({**project, 'animation_settings': '{}'}))

    @staticmethod
    def evidence(story, item, end):
        if (not isinstance(item, dict) or type(item.get('start')) is not int or type(item.get('end')) is not int
                or not 0 <= item['start'] < item['end'] <= end or not str(item.get('quote') or '').strip()
                or story[item['start']:item['end']] != item['quote']):
            raise DomainError('Нужно точное основание из текущего или предшествующего текста.', 409)
        return {k: item[k] for k in ('start', 'end', 'quote')}

    def preview(self, sid, payload, revision):
        scene = self.service.scene(sid)
        project = self.service.project(scene['project_id'])
        if type(revision) is not int or scene['current_revision'] != revision:
            raise DomainError('Сцена изменилась. Подготовьте решение для её текущей версии.', 409)
        story, data = project['story'], scene['data']
        source = source_span(story, data)
        channel = self.db.one('SELECT profile_type FROM channel_profiles WHERE id=?', (project.get('channel_profile_id'),))
        if not channel or channel['profile_type'] != 'forgotten_cities':
            raise DomainError('Редакторские планы этого блока доступны для документального профиля.', 409)
        if not isinstance(payload, dict) or set(payload) - {'mode', 'focus', 'subject', 'time_layer', 'time_evidence', 'roles', 'animals', 'reason', 'omit_claims'}:
            raise DomainError('Некорректные поля редакторского решения.')
        mode, layer = payload.get('mode'), payload.get('time_layer')
        if mode not in MODES or layer not in LAYERS:
            raise DomainError('Выберите допустимый способ изображения и временной слой.')
        reason = payload.get('reason')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise DomainError('Укажите основание редакторского выбора до 2000 символов.')
        focus = self.evidence(story, payload.get('focus'), source['end'])
        if focus['start'] < max(0, source['start'] - 800):
            raise DomainError('Смысловой центр находится вне ближайшего контекста сцены.', 409)
        if mode == 'selected_clause' and not (source['start'] <= focus['start'] < focus['end'] <= source['end'] and focus['quote'] != source['quote']):
            raise DomainError('Для выбора одного времени нужен отдельный фрагмент текущей цитаты.', 409)
        if (mode == 'literal' and re.search(r'\bне\b', focus['quote'], re.I)
                and re.search(r'\bпока\b', story[source['start']:focus['start']], re.I)
                and not re.search(r'\bпока\b', focus['quote'], re.I)):
            raise DomainError('Сохраните конструкцию «пока…не» целиком: её обрезка меняет смысл действия.', 409)
        subject = self.evidence(story, payload.get('subject'), focus['end'])
        if not focus['start'] <= subject['start'] < subject['end'] <= focus['end']:
            raise DomainError('Главный предмет должен находиться в выбранном смысловом центре.', 409)
        temporal = payload.get('time_evidence')
        if temporal is not None:
            temporal = self.evidence(story, temporal, source['end'])
            from .forgotten_cities.event_context import DATE, PERIOD, PRESENT, ANCIENT
            if not any(p.search(temporal['quote']) for p in (DATE, PERIOD, PRESENT, ANCIENT)):
                raise DomainError('Выбранная цитата не задаёт время события.', 409)
            if PRESENT.search(temporal['quote']) and layer not in {'modern_ruins', 'excavation', 'museum_display'}:
                raise DomainError('Современное основание противоречит выбранному времени.', 409)
            if not PRESENT.search(temporal['quote']) and layer not in {'documented_event', 'historical_reconstruction'}:
                raise DomainError('Историческое основание противоречит выбранному времени.', 409)
        if mode in {'literal', 'selected_clause'} and layer in {'documented_event', 'historical_reconstruction'} and temporal is None:
            raise DomainError('Для датированного события нужно явное основание времени.', 409)
        from .forgotten_cities.event_context import for_source
        context = for_source(story, focus['quote'], focus['start']) or {}
        visible = {}
        for key, context_key in (('roles', 'participants'), ('animals', 'animals')):
            names = payload.get(key, [])
            allowed = {p['name']: p for p in context.get(context_key, [])}
            if (not isinstance(names, list) or len(names) > 20 or not all(isinstance(n, str) for n in names)
                    or len(set(names)) != len(names) or any(n not in allowed for n in names)):
                raise DomainError('Выбранные участники должны иметь основания в смысловом центре сцены.', 409)
            visible[context_key] = [copy.deepcopy(allowed[n]) for n in names]
            for item in visible[context_key]:
                self.evidence(story, item['evidence'], source['end'])
                if item.get('link'):
                    self.evidence(story, item['link'], source['end'])
        original = self.service._ground_forgotten_scene(scene, project)['data']['forgotten_cities_prompt_contract']
        semantic = (original.get('editorial_decision') or {}).get('original_semantic_review') or review(story, source, original)
        codes = {f['code'] for f in semantic['findings']}
        if mode == 'literal' and codes & {'reported', 'hypothesis', 'conditional', 'purpose', 'capability', 'denied_event'}:
            raise DomainError('Буквальное событие не сохраняет условность или отрицание исходной фразы.', 409)
        if 'reported' in codes and mode not in {'reported_insert', 'neutral_detail', 'selected_clause'}:
            raise DomainError('Чужое утверждение требует нейтральной вставки или выбора отдельного фрагмента.', 409)
        resolved = codes - {'invalid_evidence'}
        if 'mixed_time' in resolved and mode != 'selected_clause':
            resolved.remove('mixed_time')
            resolved.discard('event_context')
        if context.get('implicit_actor') and mode not in {'neutral_detail', 'reported_insert'} and not visible['participants']:
            resolved.discard('implicit_actor')
        if mode == 'selected_clause' and len(context.get('time_layers', [])) > 1 and not (temporal and focus['start'] <= temporal['start'] < temporal['end'] <= focus['end']):
            resolved.discard('mixed_time'); resolved.discard('event_context')
        # These are explicit visual omissions, not confirmations of the claim.
        omissions = payload.get('omit_claims', [])
        from .source_catalog import SourceCatalog
        bindings = SourceCatalog(self.service).view(project['id'])['bindings'] if self.catalog_exists() else []
        relevant_ids = set(original.get('anchor_ids') or [])
        # Rebuild this selection from the full narration if a prior editorial
        # version focused only a detail. Identical measurement words do not
        # transfer a check from a door to a shaft or another catalogue object.
        from .forgotten_cities.prompts import build_prompt_packet
        metadata = project.get('analysis_metadata') or {}
        if isinstance(metadata, str):metadata=json.loads(metadata)
        source_packet = build_prompt_packet(metadata.get('forgotten_cities') or {}, '', data.get('beat',''), data.get('location',''),
            story_text=story, source_quote=source['quote'], source_start=source['start'])
        relevant_ids.update(source_packet.get('anchor_ids') or [])
        checks = [c for b in bindings if b['anchor_id'] in relevant_ids for c in b.get('document', {}).get('historical_checks', [])
                  if c.get('verdict') in {'uncertain', 'contradicted'} and c.get('claim') in source['quote']]
        if not isinstance(omissions, list) or not all(isinstance(c, str) and c in {x['claim'] for x in checks} for c in omissions):
            raise DomainError('Можно исключить только отмеченное проверкой утверждение этой сцены.', 409)
        if any(subject['quote'] == claim or claim in subject['quote'] for claim in omissions):
            raise DomainError('Исключённое утверждение не может быть главным предметом изображения.', 409)
        document = {'version': VERSION, 'scene_id': sid, 'project_id': project['id'], 'source': source,
                    'base': self.base(scene, project), 'mode': mode, 'focus': focus, 'subject': subject,
                    'time_layer': layer, 'time_evidence': temporal, 'participants': visible['participants'],
                    'animals': visible['animals'], 'reason': reason.strip(), 'resolved_codes': sorted(resolved),
                    'original_semantic_review': semantic, 'omit_claims': sorted(set(omissions)),
                    'source_historical_checks': checks,
                    'omitted_checks': [c for c in checks if c['claim'] in omissions],
                    'origin': 'editorial_review', 'historical_accuracy_verified': False, 'image_quality_measured': False}
        digest = fingerprint(document)
        with self.db.transaction() as db:
            current = self.service.scene(sid)
            if self.base(current, self.service.project(project['id'])) != document['base']:
                raise DomainError('Исходные данные изменились при подготовке решения.', 409)
            row = db.execute('SELECT id FROM editorial_scene_plans WHERE scene_id=? AND content_hash=?', (sid, digest)).fetchone()
            if row:
                plan_id = row[0]
            else:
                version = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM editorial_scene_plans WHERE scene_id=?', (sid,)).fetchone()[0]
                plan_id = uid()
                db.execute('INSERT INTO editorial_scene_plans VALUES(?,?,?,?,?,?,?)',
                           (plan_id, sid, project['id'], version, digest, dump(document), now()))
        return self.get(sid, plan_id)

    def catalog_exists(self):
        from .source_catalog import SourceCatalog
        return SourceCatalog(self.service).path.exists()

    def choices(self, sid, focus=None):
        """Read-only, exact-source suggestions. They never resolve a finding."""
        scene = self.service.scene(sid)
        project = self.service.project(scene['project_id'])
        channel = self.db.one('SELECT profile_type FROM channel_profiles WHERE id=?', (project.get('channel_profile_id'),))
        if not channel or channel['profile_type'] != 'forgotten_cities':
            raise DomainError('Выбор изображения доступен для документального профиля.', 409)
        story = project['story']
        source = source_span(story, scene['data'])
        focus = self.evidence(story, focus, source['end']) if focus is not None else source
        lower = max(0, source['start'] - 800)
        if focus['start'] < lower:
            raise DomainError('Выберите текущую цитату или её ближайший предшествующий контекст.', 409)
        from .forgotten_cities.event_context import for_source, DATE, PERIOD, PRESENT, ANCIENT
        context = for_source(story, focus['quote'], focus['start']) or {}
        def exact(item):
            try:
                return self.evidence(story, item, source['end'])
            except DomainError:
                return None
        subjects, roles, animals = [], [], []
        for key, target in (('subjects', subjects), ('participants', roles), ('animals', animals)):
            for item in context.get(key, []):
                evidence = exact(item.get('evidence'))
                link = exact(item.get('link')) if item.get('link') else None
                if not evidence or (item.get('link') and not link):
                    continue
                if evidence['start'] >= focus['start'] and evidence['end'] <= focus['end']:
                    if evidence not in subjects:
                        subjects.append(evidence)
                if key != 'subjects':
                    target.append({'name': item['name'], 'evidence': evidence, 'link': link})
        packet = self.service._ground_forgotten_scene(scene, project)['data']['forgotten_cities_prompt_contract']
        semantic = (packet.get('editorial_decision') or {}).get('original_semantic_review') or review(story, source, packet)
        codes = {item['code'] for item in semantic['findings']}
        mode = ('selected_clause' if 'mixed_time' in codes else 'reported_insert' if 'reported' in codes
                else 'neutral_detail' if codes & {'purpose', 'denied_event'}
                else 'illustrative' if codes & {'hypothesis', 'conditional', 'purpose', 'capability', 'denied_event'}
                else 'neutral_detail' if codes else 'literal')
        times = []
        for pattern in (DATE, PERIOD, PRESENT, ANCIENT):
            for match in pattern.finditer(story[:source['end']]):
                item = {'start': match.start(), 'end': match.end(), 'quote': match.group()}
                if item not in times:
                    times.append(item)
        temporal = exact((context.get('time') or {}).get('evidence'))
        if temporal and temporal not in times and any(p.search(temporal['quote']) for p in (DATE, PERIOD, PRESENT, ANCIENT)):
            times.append(temporal)
        from .source_catalog import SourceCatalog
        from .forgotten_cities.prompts import build_prompt_packet
        metadata = project.get('analysis_metadata') or {}
        if isinstance(metadata, str): metadata = json.loads(metadata)
        original = build_prompt_packet(metadata.get('forgotten_cities') or {}, '', scene['data'].get('beat',''),
            scene['data'].get('location',''), story_text=story, source_quote=source['quote'], source_start=source['start'])
        anchor_ids = set(original.get('anchor_ids') or [])
        bindings = SourceCatalog(self.service).view(project['id'])['bindings'] if self.catalog_exists() else []
        checks = [c for b in bindings if b['anchor_id'] in anchor_ids for c in b.get('document', {}).get('historical_checks', [])
                  if c.get('verdict') in {'uncertain', 'contradicted'} and c.get('claim') in source['quote']]
        with closing(self.db.connect()) as db:
            blocked = bool(self.media_blocked(project['id'], db))
        decision, error = self.bound(scene, project)
        return {'version': 'editorial-source-choices/1', 'scene_id': sid, 'revision': scene['current_revision'], 'source': source,
                'context': {'start': lower, 'end': source['end'], 'quote': story[lower:source['end']]},
                'focus': focus, 'subjects': subjects, 'roles': roles, 'animals': animals,
                'time_evidence': sorted(times, key=lambda t: (t['start'], t['end'])),
                'suggested_mode': mode, 'suggested_time_layer': (context.get('time') or {}).get('layer'),
                'semantic_review': semantic, 'historical_checks': checks, 'current_decision': decision,
                'binding_error': error, 'apply_blocked_reason': 'В проекте уже есть изображения, видео или монтаж. Доступен просмотр вариантов.' if blocked else None,
                'automatic_application': False}

    def get(self, sid, plan_id):
        row = self.db.one('SELECT * FROM editorial_scene_plans WHERE id=? AND scene_id=?', (plan_id, sid))
        if not row:
            raise DomainError('Редакторская версия этой сцены не найдена.', 404)
        row['document'] = json.loads(row['document'])
        row['applied'] = bool(self.db.one('SELECT id FROM editorial_scene_applications WHERE plan_id=?', (plan_id,)))
        with closing(self.db.connect()) as db:
            blocked = bool(self.media_blocked(row['project_id'], db))
        row['apply_blocked_reason'] = 'В проекте уже есть изображения, видео или монтаж. Доступен просмотр вариантов.' if blocked else None
        return row

    def overview(self, sid):
        self.service.scene(sid)
        return [self.get(sid, r['id']) for r in self.db.all('SELECT id FROM editorial_scene_plans WHERE scene_id=? ORDER BY revision DESC', (sid,))]

    def media_blocked(self, pid, db):
        edit = db.execute('SELECT document_json FROM project_edits WHERE project_id=?', (pid,)).fetchone()
        return (db.execute("SELECT id FROM generations WHERE project_id=? AND purpose NOT IN ('analysis','edit_audio') LIMIT 1", (pid,)).fetchone()
                or db.execute('SELECT id FROM scenes WHERE project_id=? AND (active_generation_id IS NOT NULL OR video_asset_id IS NOT NULL) LIMIT 1', (pid,)).fetchone()
                or (edit and json.loads(edit[0]).get('clips')))

    def apply(self, sid, plan_id, revision):
        plan = self.get(sid, plan_id)
        doc = plan['document']
        with self.db.transaction() as db:
            scene = self.service.scene(sid)
            previous = db.execute('SELECT target_revision FROM editorial_scene_applications WHERE plan_id=?', (plan_id,)).fetchone()
            if previous and scene['current_revision'] == previous[0] and scene['data'].get('editorial_plan_id') == plan_id:
                if self.bound(scene, self.service.project(scene['project_id']))[0] is None:
                    raise DomainError('Редакторская версия устарела: исходные данные изменились.', 409)
                return self.get(sid, plan_id)
            if type(revision) is not int or scene['current_revision'] != revision or previous:
                raise DomainError('Сцена изменилась после просмотра редакторской версии.', 409)
            project = self.service.project(scene['project_id'])
            if self.base(scene, project) != doc['base']:
                raise DomainError('Текст, каталог или версия сцены изменились. Подготовьте новый вариант.', 409)
            if self.media_blocked(project['id'], db):
                raise DomainError('В проекте уже есть изображения, видео или монтаж. Решение сохранено для просмотра.', 409)
            updated = copy.deepcopy(scene['data'])
            updated['editorial_plan_id'] = plan_id
            for key in list(updated):
                if key.startswith('audit_'):
                    updated.pop(key)
            db.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (sid, revision+1, dump(updated), now()))
            db.execute('UPDATE scenes SET current_revision=? WHERE id=?', (revision+1, sid))
            db.execute('INSERT INTO editorial_scene_applications VALUES(?,?,?,?,?,?)',
                       (uid(), plan_id, sid, revision, revision+1, now()))
        return self.get(sid, plan_id)

    def bound(self, scene, project):
        plan_id = scene['data'].get('editorial_plan_id')
        if not plan_id:
            return None, None
        try:
            # Detail and Flow normalize derived fields before compiling.
            # Bind against the persisted revision, not those transient views.
            current = self.service.scene(scene['id'])
            project = self.service.project(current['project_id'])
            if (scene['current_revision'] != current['current_revision'] or plan_id != current['data'].get('editorial_plan_id')
                    or source_span(project['story'], scene['data']) != source_span(project['story'], current['data'])):
                raise DomainError('Редакторская версия устарела: исходные данные изменились.', 409)
            scene = current
            plan = self.get(scene['id'], plan_id)
            application = self.db.one('SELECT * FROM editorial_scene_applications WHERE plan_id=?', (plan_id,))
            doc = plan['document']
            original = self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?', (scene['id'], doc['base']['revision']))
            expected = json.loads(original['data']) if original else {}
            expected['editorial_plan_id'] = plan_id
            for key in list(expected):
                if key.startswith('audit_'): expected.pop(key)
            # Appearance references and animation settings do not change a
            # source interpretation.
            # Inspect every revision since application; any other change still
            # invalidates the decision, including a change later reverted.
            target = application['target_revision'] if application else None
            revisions = self.db.all('SELECT revision,data FROM scene_revisions WHERE scene_id=? AND revision>=? AND revision<=? ORDER BY revision',
                                    (scene['id'], target, scene['current_revision'])) if target is not None else []
            def semantic_data(data):
                return {key: value for key, value in data.items() if key not in ('participant_identity', 'animation')}
            same_meaning = (target is not None and target <= scene['current_revision'] and len(revisions) == scene['current_revision'] - target + 1
                            and all(semantic_data(json.loads(row['data'])) == semantic_data(expected) for row in revisions))
            if (not same_meaning
                    or not self.project_matches(doc['base'], project)
                    or doc['base']['catalog_hash'] != self.catalog_token(project['id'])
                    or doc['source'] != source_span(project['story'], scene['data'])):
                raise DomainError('Редакторская версия устарела: исходные данные изменились.', 409)
            return {**doc, 'id': plan_id, 'hash': plan['content_hash']}, None
        except DomainError as error:
            return None, str(error)


def apply_decision(packet, decision):
    packet = copy.deepcopy(packet)
    focus = decision['focus']
    text = focus['quote']
    for claim in decision['omit_claims']:
        text = text.replace(claim, '[unspecified visual property]')
    qualifier = {'literal': 'Source-stated event', 'illustrative': 'Illustration of a possible function or hypothesis; not an established individual episode',
                 'reported_insert': 'Neutral documentary insert about a reported claim; do not enact the embedded claim',
                 'neutral_detail': 'Neutral source-backed detail; do not enact hypothetical, denied or prevented events',
                 'selected_clause': 'Illustrate only the selected clause; preserve the full narration separately'}[decision['mode']]
    direction = qualifier + '. Visual focus: ' + text + '. Main subject: ' + decision['subject']['quote'] + '.'
    direction += ' Visible human roles: ' + (', '.join(p['name'] for p in decision['participants']) or 'none') + '.'
    direction += ' ' + ' '.join(direction_rules(signals(packet['source_quote'])))
    if decision['time_evidence']:
        direction += ' Temporal evidence: ' + decision['time_evidence']['quote'] + '.'
    if decision['omit_claims']:
        direction += ' Do not assert a precise size, composition or depth for visually omitted uncertain properties; no measurement labels.'
    direction += ' No invented readable text or unrelated ruins panorama.'
    context = copy.deepcopy(packet.get('narrative_context') or {})
    context.update(source=copy.deepcopy(focus), scope=copy.deepcopy(focus), subjects=[{'name': decision['subject']['quote'], 'evidence': decision['subject']}],
                   participants=decision['participants'], animals=decision['animals'], implicit_actor=False,
                   time={'layer': decision['time_layer'], 'evidence': decision['time_evidence']} if decision['time_evidence'] else None,
                   time_layers=[decision['time_layer']], needs_review=False, review_reasons=[], place=None)
    for key in ('city_population', 'source_fidelity_plan'):
        packet.pop(key, None)
    packet.update(editorial_decision=decision, narrative_context=context, primary_subject=decision['subject']['quote'],
                  visual_source=focus, source_roles=[p['name'] for p in decision['participants']], source_action=focus['quote'],
                  visual_time_layer=decision['time_layer'], time_layer_contract='Time layer: '+decision['time_layer'],
                  source_binding='narrated_event', required_artifacts=[], required_structures=[], visual_direction=direction,
                  shot_type='main_action' if decision['participants'] else 'establishing', source_reviewed=True,
                  city_contract='Use only the selected source-backed setting and temporal state. No inherited generic city palette.',
                  negative_contract='No invented participants, dates, materials or enactment of denied events.',
                  shot_contract='Authoritative narration: '+packet['source_quote']+'. Depict: '+direction,
                  compiled_prompt=direction)
    return packet

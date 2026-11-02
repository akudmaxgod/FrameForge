"""Immutable shared catalogue; project links pin a particular revision.

Source claims retain their origin. Reuse never establishes historical truth or
copies image approval to another scene. Only explicit links cross projects.
"""
import json
import copy
import sqlite3
from urllib.parse import urlparse
from contextlib import contextmanager, closing
from pathlib import Path

from .db import dump, now, uid
from .domain import DomainError, fingerprint
from .source_contract import passport
from .catalog_evidence import comparison_value


class SourceCatalog:
    def __init__(self, service):
        self.service = service
        self.path = Path(service.root) / 'catalog' / 'source_catalog.sqlite'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(
                    entry_id TEXT NOT NULL REFERENCES entries(id), revision INTEGER NOT NULL,
                    document TEXT NOT NULL, created TEXT NOT NULL,
                    PRIMARY KEY(entry_id,revision));
                CREATE TABLE IF NOT EXISTS links(
                    project_id TEXT NOT NULL, anchor_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, story_hash TEXT NOT NULL, mode TEXT NOT NULL,
                    PRIMARY KEY(project_id,anchor_id),
                    FOREIGN KEY(entry_id,revision) REFERENCES versions(entry_id,revision));
            ''')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def packet(self, pid):
        project = self.service.project(pid)
        return project, passport(project, [], include_narrative=False)

    @staticmethod
    def document(project, item, story_hash):
        # Never promote proposed properties or descriptions to verified facts.
        return {**item, 'origin': {'project_id': project['id'], 'project_title':project['title'], 'story_hash': story_hash},
                'authority': 'source_claim', 'manual_revision': False,
                'historical_accuracy_verified': False}

    @staticmethod
    def version(db, entry_id, revision=None):
        if revision is None:
            row = db.execute('SELECT revision FROM entries WHERE id=?', (entry_id,)).fetchone()
            if not row:
                raise DomainError('Запись каталога не найдена.', 404)
            revision = row['revision']
        row = db.execute('SELECT * FROM versions WHERE entry_id=? AND revision=?', (entry_id, revision)).fetchone()
        if not row:
            raise DomainError('Версия каталога не найдена.', 404)
        return {'id': entry_id, 'revision': revision, 'document': json.loads(row['document']), 'created_at': row['created']}

    def get(self, entry_id, revision=None):
        with self.db() as db:
            return self.version(db, entry_id, revision)

    def sync(self, pid, expected_project_version):
        project, packet = self.packet(pid)
        if type(expected_project_version) is not int or project['version'] != expected_project_version:
            raise DomainError('Проект изменился. Обновите каталог после просмотра изменений.', 409)
        outcomes = []
        with self.db() as db:
            for item in packet['catalog']:
                anchor = item.get('id')
                if not isinstance(anchor, str) or not anchor or not item.get('name'):
                    continue
                document = self.document(project, item, packet['story_hash'])
                link = db.execute('SELECT * FROM links WHERE project_id=? AND anchor_id=?', (pid, anchor)).fetchone()
                if link:
                    pinned = self.version(db, link['entry_id'], link['revision'])
                    if link['mode'] != 'origin' or pinned['document'].get('manual_revision'):
                        outcomes.append({'anchor_id': anchor, 'state': 'protected', 'entry_id': link['entry_id'], 'revision': link['revision']})
                        continue
                    entry_id = link['entry_id']
                    latest = self.version(db, entry_id)
                    # Another user's edit is preserved even if this link is older.
                    if latest['revision'] != link['revision']:
                        outcomes.append({'anchor_id': anchor, 'state': 'protected', 'entry_id': entry_id, 'revision': link['revision']})
                        continue
                    if latest['document'] == document:
                        outcomes.append({'anchor_id': anchor, 'state': 'unchanged', 'entry_id': entry_id, 'revision': latest['revision']})
                        continue
                    revision = latest['revision'] + 1
                    db.execute('UPDATE entries SET revision=? WHERE id=?', (revision, entry_id))
                else:
                    entry_id, revision = uid(), 1
                    db.execute('INSERT INTO entries VALUES(?,?)', (entry_id, revision))
                db.execute('INSERT INTO versions VALUES(?,?,?,?)', (entry_id, revision, dump(document), now()))
                db.execute('INSERT INTO links VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,anchor_id) DO UPDATE SET '
                           'entry_id=excluded.entry_id,revision=excluded.revision,story_hash=excluded.story_hash,mode=excluded.mode',
                           (pid, anchor, entry_id, revision, packet['story_hash'], 'origin'))
                outcomes.append({'anchor_id': anchor, 'state': 'saved', 'entry_id': entry_id, 'revision': revision})
        return {'project_id': pid, 'outcomes': outcomes}

    @staticmethod
    def conflicts(item, document):
        conflicts = []
        if item.get('kind') != document.get('kind'):
            conflicts.append('Тип сущности отличается.')
        if item.get('identity_scope', 'specific_object') != document.get('identity_scope', 'specific_object'):
            conflicts.append('Тип конструкции нельзя объединить с конкретным объектом.')
        if item.get('source_kind') and document.get('source_kind') and item['source_kind'] != document['source_kind']:
            conflicts.append('Конструкции имеют разное назначение.')
        if item.get('time_layer') and document.get('time_layer') and item['time_layer'] != document['time_layer']:
            conflicts.append('Временное состояние локации отличается.')
        for field, prop in item.get('properties', {}).items():
            other = document.get('properties', {}).get(field, {})
            if prop.get('value') and other.get('value') and comparison_value(field, prop['value']) != comparison_value(field, other['value']):
                conflicts.append('Различаются сведения: ' + {'material':'материал','shape':'форма','scale':'размер','era':'эпоха','origin':'происхождение','distinctive_features':'отличительные признаки'}.get(field,'свойство'))
        return conflicts

    @staticmethod
    def names(item):
        names = [item.get('name')] + (item.get('aliases') if isinstance(item.get('aliases'), list) else [])
        return {name.strip().casefold() for name in names if isinstance(name, str) and name.strip()}

    def record_check(self, entry_id, expected_revision, claim, verdict, sources):
        """Persist a scoped editorial source check, never approve the whole object."""
        if verdict not in ('supported', 'contradicted', 'uncertain'):
            raise DomainError('Укажите результат проверки утверждения.')
        if not isinstance(claim, str) or not claim.strip() or not isinstance(sources, list) or not sources:
            raise DomainError('Нужны точное утверждение и источники проверки.')
        for source in sources:
            if not isinstance(source, dict):
                raise DomainError('Источник проверки должен содержать ссылку и объяснение.')
            url = urlparse(str(source.get('url', '')))
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or not isinstance(source.get('title'), str) or not source['title'].strip()
                    or not isinstance(source.get('reason'), str) or not source['reason'].strip()):
                raise DomainError('Нужны HTTPS-ссылка, название и объяснение проверки.')
        with self.db() as db:
            latest = self.version(db, entry_id)
            if type(expected_revision) is not int or latest['revision'] != expected_revision:
                raise DomainError('Каталог изменился. Сначала посмотрите новую версию.', 409)
            document = copy.deepcopy(latest['document'])
            evidence = document.get('evidence_spans') or ([document['evidence']] if document.get('evidence') else [])
            matching = [e for e in evidence if claim in e['quote']]
            if not matching:
                raise DomainError('Проверяемое утверждение отсутствует в сохранённых цитатах.', 409)
            record = {'claim':claim, 'claim_hash':fingerprint({'claim':claim, 'origin':document['origin']}),
                'verdict':verdict, 'sources':sources, 'evidence':matching, 'checked_at':now(),
                'method':'editorial_external_source_check'}
            document['historical_checks'] = [c for c in document.get('historical_checks', []) if c['claim_hash'] != record['claim_hash']] + [record]
            document['historical_accuracy_verified'] = False
            document['manual_revision'] = True
            revision = expected_revision + 1
            db.execute('INSERT INTO versions VALUES(?,?,?,?)', (entry_id, revision, dump(document), now()))
            db.execute('UPDATE entries SET revision=? WHERE id=?', (revision, entry_id))
            return self.version(db, entry_id, revision)

    def view(self, pid):
        project, packet = self.packet(pid)
        items = {i['id']: i for i in packet['catalog'] if i.get('id')}
        with self.db() as db:
            bindings = []
            for link in db.execute('SELECT * FROM links WHERE project_id=? ORDER BY anchor_id', (pid,)):
                version = self.version(db, link['entry_id'], link['revision'])
                item = items.get(link['anchor_id'])
                stale = link['story_hash'] != packet['story_hash'] or item is None
                conflicts = [] if item is None else self.conflicts(item, version['document'])
                latest_revision=db.execute('SELECT revision FROM entries WHERE id=?',(link['entry_id'],)).fetchone()[0]
                bindings.append({**dict(link), **version, 'latest_revision':latest_revision, 'binding_token':fingerprint(dict(link)), 'state': 'stale' if stale else 'conflict' if conflicts else 'bound', 'conflicts': conflicts})
            candidates = []
            for row in db.execute('SELECT id,revision FROM entries ORDER BY id'):
                version = self.version(db, row['id'], row['revision'])
                doc = version['document']
                if doc['origin']['project_id'] == pid:
                    continue
                for anchor, item in items.items():
                    if item.get('kind') == doc.get('kind') and self.names(item) & self.names(doc):
                        candidates.append({'anchor_id': anchor, **version, 'conflicts': self.conflicts(item, doc)})
        from .primary_sources import sources
        return {'project_id': pid, 'project_version': project['version'],
                'story_hash': packet['story_hash'], 'bindings': bindings, 'candidates': candidates,
                'primary_sources':sources(self.service.root, project),
                'policy': 'Explicit reuse pins a revision. Source claims are not verified historical facts. Images require a new target check.'}

    def bind(self, pid, anchor, entry_id, revision, expected_binding=None):
        _, packet = self.packet(pid)
        item = next((i for i in packet['catalog'] if i.get('id') == anchor), None)
        if item is None or type(revision) is not int or revision < 1:
            raise DomainError('Укажите существующую сущность и версию каталога.')
        with self.db() as db:
            version = self.version(db, entry_id, revision)
            doc = version['document']
            if not self.names(item) & self.names(doc):
                raise DomainError('Имена сущности не совпадают. Нужна отдельная запись каталога.', 409)
            conflicts = self.conflicts(item, doc)
            if conflicts:
                raise DomainError(' '.join(conflicts), 409)
            link = db.execute('SELECT * FROM links WHERE project_id=? AND anchor_id=?', (pid, anchor)).fetchone()
            if (fingerprint(dict(link)) if link else None) != expected_binding:
                raise DomainError('Привязка каталога изменилась. Обновите состояние.', 409)
            db.execute('INSERT INTO links VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,anchor_id) DO UPDATE SET '
                       'entry_id=excluded.entry_id,revision=excluded.revision,story_hash=excluded.story_hash,mode=excluded.mode',
                       (pid, anchor, entry_id, revision, packet['story_hash'], 'reuse'))
        return {'anchor_id': anchor, 'entry_id': entry_id, 'revision': revision, 'state': 'bound'}

    def revise(self, entry_id, expected_revision, changes):
        # Editing annotations cannot quietly turn generated content into evidence.
        allowed = {'name', 'aliases', 'description_unverified'}
        if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
            raise DomainError('Можно изменить название, варианты имени и непроверенное описание.')
        if 'name' in changes and (not isinstance(changes['name'], str) or not changes['name'].strip()):
            raise DomainError('Укажите название.')
        if 'aliases' in changes and (not isinstance(changes['aliases'], list) or not all(isinstance(a, str) and a.strip() for a in changes['aliases'])):
            raise DomainError('Укажите варианты имени списком строк.')
        if 'description_unverified' in changes and not isinstance(changes['description_unverified'], str):
            raise DomainError('Укажите описание строкой.')
        with self.db() as db:
            latest = self.version(db, entry_id)
            if type(expected_revision) is not int or latest['revision'] != expected_revision:
                raise DomainError('Каталог изменился. Сначала посмотрите новую версию.', 409)
            document = {**latest['document'], **changes, 'manual_revision': True}
            revision = expected_revision + 1
            db.execute('INSERT INTO versions VALUES(?,?,?,?)', (entry_id, revision, dump(document), now()))
            db.execute('UPDATE entries SET revision=? WHERE id=?', (revision, entry_id))
            return self.version(db, entry_id, revision)

    def context(self, pid, anchor_ids, source_end=None):
        # Reading a legacy/new scene without saved links creates no catalogue DB.
        if not self.path.exists() or not anchor_ids:
            return []
        view = self.view(pid)
        result = []
        for row in view['bindings']:
            if row['anchor_id'] not in anchor_ids:
                continue
            item = {k:copy.deepcopy(row[k]) for k in ('anchor_id', 'id', 'revision', 'document', 'state', 'conflicts')}
            doc = item['document']
            if doc.get('identity_scope') == 'source_type' and doc['origin']['project_id'] == pid and type(source_end) is int:
                # A catalogue type aggregates several structures. Only the
                # already narrated claims can interpret this earlier scene.
                evidence = [e for e in doc.get('evidence_spans', []) if e['end'] <= source_end]
                doc.update(evidence_spans=evidence, evidence=evidence[0] if evidence else None,
                           description_unverified='', scene_source_end=source_end)
                for field, prop in doc.get('properties', {}).items():
                    claims = [c for c in prop.get('claims', []) if c['evidence']['end'] <= source_end]
                    values = {comparison_value(field, c['value']):c['value'] for c in reversed(claims)}
                    prop.update(claims=claims, value=next(iter(values.values())) if len(values)==1 else None,
                                canonical_value=next(iter(values)) if len(values)==1 else None,
                                status='source_claim' if len(values)==1 else 'multiple_source_claims' if values else 'unknown',
                                proposed_unverified=None)
                doc['historical_checks'] = [c for c in doc.get('historical_checks', [])
                    if any(c['claim'] in e['quote'] for e in evidence)]
            result.append(item)
        return result

"""Apply saved image checks without sending API requests or generating media."""
import base64
import hashlib
import json
from pathlib import Path

from .db import dump, now
from .domain import DomainError, fingerprint
from .forgotten_cities.fidelity import IMAGE_VERSION, SourceFidelityReviewer

PENDING={'queued','running','waiting_quota','waiting_registry','waiting_model','retry_wait'}
TERMINAL={'error_access','error_request','error_response','transport_unknown','budget_too_small','cancelled'}


def revision_hash(data):
    value=dict(data)
    value.pop('animation',None)
    return fingerprint(value)


def reference_state(service,snapshot):
    states=[]
    for reference in snapshot.get('references',[]):
        asset=service.db.one('SELECT * FROM assets WHERE id=?',(reference.get('asset_id'),))
        if not asset:
            raise DomainError('Отсутствует эталон проверки.',409)
        path=(service.root/asset['path']).resolve()
        if not path.is_relative_to(service.root.resolve()):
            raise DomainError('Некорректный путь эталона.',409)
        state={'asset_id':asset['id'],'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        for kind,table in [('prop','props'),('location','locations'),('character','characters')]:
            if reference.get(kind+'_id'):
                item=service.db.one(f'SELECT revision,references_json FROM {table} WHERE id=?',(reference[kind+'_id'],))
                if not item:
                    raise DomainError('Эталон изменён или удалён.',409)
                state['selection']=fingerprint(item)
        states.append(state)
    return states


def review_references(service, snapshot):
    """Keep candidate/reference order identical for submission and resumption."""
    result = []
    for reference in snapshot.get('references', []):
        if not any(reference.get(kind + '_id') for kind in ('prop', 'character', 'location')):
            continue
        asset = service.asset(reference['asset_id'])
        path = (service.root / asset['path']).resolve()
        if not path.is_relative_to(service.root.resolve()):
            raise DomainError('Некорректный путь эталона.', 409)
        result.append(path.read_bytes())
    return result


def capture_binding(service,snapshot,data,story):
    revision=service.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?',
                           (snapshot.get('scene_id'),snapshot.get('scene_revision')))
    settings=service.settings()
    reviewer = SourceFidelityReviewer(service.root, settings)
    _, evidence = reviewer.image_request(snapshot, data, story, review_references(service, snapshot))
    return {'image_sha256':hashlib.sha256(data).hexdigest(),'story_hash':fingerprint(story),
            'scene_hash':revision_hash(json.loads(revision['data'])) if revision else None,
            'references':reference_state(service,snapshot),
            'model':settings.get('google_text_model') or 'gemini-3.1-flash-lite','version':IMAGE_VERSION,
            'evidence_contract_id':evidence['id']}


def current_approval(service, generation, snapshot):
    """Read-only guard for cached/completed checks, including checks without jobs."""
    review = snapshot.get('source_fidelity_review') or {}
    if review.get('status') != 'pass' or review.get('version') != IMAGE_VERSION:
        return False
    try:
        asset = service.asset(generation['asset_id'])
        path = (service.root / asset['path']).resolve()
        if not path.is_relative_to(service.root.resolve()):
            return False
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset['sha256'] or review.get('image_sha256') != asset['sha256']:
            return False
        story = service.project(generation['project_id'])['story']
        if snapshot.get('source_contract'):
            from .source_catalog import SourceCatalog
            if SourceCatalog(service).path.exists():
                fresh = service.scene_snapshot(service.scene(generation['scene_id']))
                if fresh.get('source_contract', {}).get('catalog_context') != snapshot['source_contract'].get('catalog_context'):
                    return False
        reviewer = SourceFidelityReviewer(service.root, service.settings())
        if review.get('model') != reviewer.model:
            return False
        _, evidence = reviewer.image_request(snapshot, data, story, review_references(service, snapshot))
        return reviewer.image_verdict(review, asset['sha256'], reviewer.model, evidence_contract=evidence)['status'] == 'pass'
    except (DomainError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


class ImageReviewResumer:
    def __init__(self,service,queue):
        self.service,self.queue=service,queue

    def tick(self,limit=32):
        rows=self.service.db.all('''SELECT * FROM generations WHERE purpose='scene' AND state='succeeded'
          AND asset_id IS NOT NULL AND json_extract(snapshot,'$.source_fidelity_review.job_id') IS NOT NULL
          AND json_extract(snapshot,'$.source_fidelity_review.review_state') IN
          ('queued','running','waiting_quota','waiting_registry','waiting_model','retry_wait',
           'error_access','error_request','error_response','budget_too_small','transport_unknown')
          ORDER BY coalesce(json_extract(snapshot,'$.source_fidelity_review.resumed_at'),'') ,created_at''')
        updated=0
        for generation in rows:
            if updated>=limit:
                break
            if self.queue.project_paused(generation['project_id']):
                continue
            snapshot=json.loads(generation['snapshot'])
            previous=snapshot['source_fidelity_review']
            try:
                job=self.queue.inspect(previous['job_id'])
            except KeyError:
                verdict={**previous,'status':'unverified','review_state':'error_response','reason':'Задание проверки не найдено.'}
            else:
                if job['paused']:
                    continue
                verdict=self.resolve(generation,snapshot,previous,job)
            if verdict==previous:
                continue
            if self.apply(generation,snapshot,previous,verdict):
                updated+=1
                if self.service.settings().get('source_contract_enabled') is True:
                    from .source_corrections import schedule,select_checked
                    if verdict.get('status')=='fail':schedule(self.service,generation['id'])
                    elif verdict.get('status')=='pass':select_checked(self.service,generation['id'])
        return {'updated':updated}

    def resolve(self,generation,snapshot,previous,job):
        if job['project']!=generation['project_id'] or job['category']!='source_image':
            return {**previous,'status':'unverified','review_state':'error_response','reason':'Задание принадлежит другой проверке.'}
        if job['state'] in PENDING:
            return {**previous,'review_state':job['state'],'next_retry_at':job['next_at']}
        if job['state'] in TERMINAL:
            return {**previous,'status':'unverified','review_state':job['state'],'next_retry_at':job['next_at'],
                    'reason':'Проверка остановлена: '+job['state']+'. Изображение сохранено.'}
        if job['state']!='succeeded':
            return {**previous,'status':'unverified','review_state':'error_response','reason':'Неизвестное состояние проверки.'}
        try:
            asset=self.service.asset(generation['asset_id'])
            path=(self.service.root/asset['path']).resolve()
            if not path.is_relative_to(self.service.root.resolve()):
                raise ValueError('invalid path')
            pixels=path.read_bytes()
            scene=self.service.scene(generation['scene_id'])
            if snapshot.get('source_contract'):
                from .source_catalog import SourceCatalog
                if SourceCatalog(self.service).path.exists():
                    fresh=self.service.scene_snapshot(scene)
                    if fresh.get('source_contract',{}).get('catalog_context')!=snapshot['source_contract'].get('catalog_context'):
                        return {**previous,'status':'unverified','review_state':'stale_review','reason':'Привязка каталога изменилась во время проверки. Сохранённый кадр не заменён.'}
            story=self.service.project(generation['project_id'])['story']
            if fingerprint(story) != (previous.get('binding') or {}).get('story_hash'):
                return {**previous,'status':'unverified','review_state':'stale_review','reason':'Сценарий изменился во время проверки.'}
            binding=capture_binding(self.service,{**snapshot,'scene_revision':scene['current_revision']},pixels,story)
            if binding!=previous.get('binding') or asset['sha256']!=binding['image_sha256'] or job['model']!=binding['model']:
                return {**previous,'status':'unverified','review_state':'stale_review','reason':'Изменились данные кадра, сценарий, эталон или проверяющий.'}
            payload=self.queue.request_payload(job['id'])
            images=[part['inlineData'] for content in payload.get('contents',[]) for part in content.get('parts',[]) if 'inlineData' in part]
            if not images or hashlib.sha256(base64.b64decode(images[0]['data'],validate=True)).hexdigest()!=binding['image_sha256']:
                raise ValueError('wrong candidate')
            references = review_references(self.service, snapshot)
            if len(images) != len(references) + 1 or any(
                    base64.b64decode(image['data'], validate=True) != expected
                    for image, expected in zip(images[1:], references)):
                raise ValueError('wrong references')
            reviewer = SourceFidelityReviewer(self.service.root, self.service.settings())
            expected_prompt, evidence = reviewer.image_request(snapshot, pixels, story, references)
            texts = [part['text'] for content in payload.get('contents', []) for part in content.get('parts', []) if 'text' in part]
            if texts != [expected_prompt]:
                raise ValueError('wrong evidence prompt')
            raw=SourceFidelityReviewer.parse_response(self.queue.result(job['id']))
            checked=SourceFidelityReviewer.image_verdict(raw,binding['image_sha256'],binding['model'],evidence_contract=evidence)
        except (DomainError,ValueError,TypeError,KeyError,OSError,AttributeError):
            return {**previous,'status':'unverified','review_state':'error_response','reason':'Ответ проверки или её данные не подтверждены. Изображение сохранено.'}
        return {**checked,'status':'unverified' if checked['status']=='uncertain' else checked['status'],
                'review_state':{'pass':'checked','fail':'requires_correction','uncertain':'semantic_uncertainty'}[checked['status']],
                'job_id':job['id'],'binding':binding,'next_retry_at':None}

    def apply(self,generation,snapshot,previous,verdict):
        with self.service.db.transaction() as connection:
            live=connection.execute('SELECT * FROM generations WHERE id=?',(generation['id'],)).fetchone()
            if not live or live['snapshot']!=generation['snapshot'] or live['asset_id']!=generation['asset_id']:
                return False
            # Revalidate mutable DB inputs under the commit lock. No model call
            # or media generation is performed in this transaction.
            if verdict.get('review_state') in ('checked','requires_correction','semantic_uncertainty'):
                scene=connection.execute('SELECT current_revision FROM scenes WHERE id=?',(generation['scene_id'],)).fetchone()
                current=connection.execute('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?',
                                           (generation['scene_id'],scene['current_revision'])).fetchone() if scene else None
                project=connection.execute('SELECT story FROM projects WHERE id=?',(generation['project_id'],)).fetchone()
                if (not current or revision_hash(json.loads(current['data']))!=verdict['binding']['scene_hash'] or
                    not project or fingerprint(project['story'])!=verdict['binding']['story_hash']):
                    verdict={**previous,'status':'unverified','review_state':'stale_review','reason':'Данные изменились во время применения проверки.'}
                else:
                    try:
                        asset=self.service.asset(generation['asset_id'])
                        pixels=(self.service.root/asset['path']).read_bytes()
                        if snapshot.get('source_contract'):
                            from .source_catalog import SourceCatalog
                            if SourceCatalog(self.service).path.exists():
                                fresh=self.service.scene_snapshot(self.service.scene(generation['scene_id']))
                                if fresh.get('source_contract',{}).get('catalog_context')!=snapshot['source_contract'].get('catalog_context'):
                                    raise ValueError('catalog binding changed')
                        binding=capture_binding(self.service,{**snapshot,'scene_revision':scene['current_revision']},pixels,project['story'])
                        if binding!=verdict['binding']:
                            raise ValueError('binding changed')
                    except (DomainError,ValueError,TypeError,KeyError,OSError):
                        verdict={**previous,'status':'unverified','review_state':'stale_review','reason':'Каталог, эталон, изображение или проверяющий изменились во время применения проверки.'}
            stamp=now()
            history=list(snapshot.get('source_fidelity_history') or [])
            if verdict.get('review_state') not in PENDING:
                history.append({'job_id':previous['job_id'],'previous_state':previous.get('review_state'),
                                'review_state':verdict.get('review_state'),'status':verdict.get('status'),'at':stamp})
            snapshot['source_fidelity_history']=history
            snapshot['source_fidelity_review']={**verdict,'resumed_at':stamp}
            connection.execute('UPDATE generations SET snapshot=? WHERE id=?',(dump(snapshot),generation['id']))
            self.service.db.event(connection,generation['project_id'],'source_image_review_updated',
                                  {'generation_id':generation['id'],'job_id':previous['job_id'],
                                   'status':verdict.get('status'),'review_state':verdict.get('review_state')})
        return True

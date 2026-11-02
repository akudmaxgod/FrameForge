"""Durable reference checks; background work never casts or selects media."""
import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from .domain import DomainError
from .forgotten_cities.fidelity import SourceFidelityReviewer, digest

VERSION='reference-review/2'
PENDING={'queued','running','waiting_registry','waiting_model','waiting_quota','retry_wait'}


class ReferenceReviewPending(DomainError):
    def __init__(self, record):
        super().__init__('Проверка эталона сохранена. Ожидается результат; изображение повторно не создаётся.',503)
        self.record=record
        self.review_state=record['state']
        self.job_id=record.get('job_id')


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def verdict(raw):
    if (not isinstance(raw,dict) or raw.get('status') not in ('pass','fail') or
        type(raw.get('score')) is not int or not 0<=raw['score']<=100 or
        not isinstance(raw.get('reason'),str) or not raw['reason'].strip() or
        ('correction' in raw and not isinstance(raw['correction'],str))):
        raise DomainError('Сервис вернул некорректную проверку эталона. Выбор остановлен.',503)
    return {k:raw[k] for k in ('status','score','reason','correction') if k in raw}


class ReferenceReviews:
    def __init__(self,service):
        self.service=service

    def directory(self,pid):
        return self.service.root/'voice'/pid/'reference_reviews'

    def binding(self,pid,anchor,asset_id,target=None):
        asset=self.service.asset(asset_id)
        if asset['project_id']!=pid:
            raise DomainError('Эталон принадлежит другому проекту.',409)
        path=(self.service.root/asset['path']).resolve()
        if not path.is_relative_to(self.service.root.resolve()):
            raise DomainError('Некорректный путь эталона.',409)
        pixels=path.read_bytes()
        sha=hashlib.sha256(pixels).hexdigest()
        if sha!=asset['sha256']:
            raise DomainError('Файл эталона изменён или повреждён.',409)
        binding={'project_id':pid,'asset_id':asset_id,'image_sha256':sha,
            'story_hash':digest(self.service.project(pid)['story']), 'anchor':anchor,
            'model':self.service.settings().get('google_text_model') or 'gemini-3.1-flash-lite','version':VERSION}
        if target:
            binding['target']={k:target[k] for k in ('kind','id','revision','references_json','locked')}
        return binding,pixels

    def check(self,pid,anchor,asset_id,prompt,target=None):
        from .review_policy import visual_reviews_enabled, manual_review
        if not visual_reviews_enabled():
            return manual_review()
        from .google_queue import configured_queue
        binding,pixels=self.binding(pid,anchor,asset_id,target)
        # Selection is a guard for a pending answer, not part of the pixel
        # verdict. A completed check can be reused after locking its candidate.
        identity={k:v for k,v in binding.items() if k!='target'}
        prompt+='\nSource story:\n'+self.service.project(pid)['story']
        path=self.directory(pid)/(digest(identity)+'.job.json')
        record=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if record and record['state']=='checked':
            return verdict(record['verdict'])
        queue=configured_queue(self.service.root,self.service.settings())
        if queue is None:
            raise ReferenceReviewPending(record or {'state':'disabled','binding':binding})
        if record:
            self.resolve(path,record,queue)
            record=json.loads(path.read_text(encoding='utf-8'))
            if record['state']=='checked':
                return verdict(record['verdict'])
            raise ReferenceReviewPending(record)
        reviewer=SourceFidelityReviewer(self.service.root,self.service.settings(),project_id=pid,
            category='source_reference',context=VERSION+':'+digest(identity))
        try:
            raw=reviewer.request(prompt,[pixels])
            result=verdict(raw)
        except DomainError as error:
            record={'binding':binding,'prompt_hash':digest(prompt),'state':getattr(error,'review_state',None) or 'error_response',
                'job_id':getattr(error,'job_id',None),'next_at':getattr(error,'next_at',None)}
            write(path,record)
            raise ReferenceReviewPending(record) from None
        if not self.current(binding):
            record={'binding':binding,'prompt_hash':digest(prompt),'state':'stale_review'}
            write(path,record)
            raise ReferenceReviewPending(record)
        write(path,{'binding':binding,'prompt_hash':digest(prompt),'state':'checked','verdict':result})
        return result

    def current(self,binding):
        pid=binding['project_id']
        try:
            fresh,_=self.binding(pid,binding['anchor'],binding['asset_id'])
            target=binding.get('target')
            if target:
                if target['kind'] not in ('prop','location'):
                    return False
                row=self.service.db.one(f"SELECT * FROM {target['kind']}s WHERE id=? AND project_id=?",(target['id'],pid))
                if not row or any(row[k]!=target[k] for k in ('revision','references_json','locked')):
                    return False
                fresh['target']=target
            metadata=self.service.project(pid).get('analysis_metadata') or {}
            if isinstance(metadata,str):
                metadata=json.loads(metadata)
            anchors=metadata.get('forgotten_cities',{}).get('anchors',[])
            if binding['anchor'] not in anchors:
                return False
            return fresh==binding
        except (DomainError,OSError,KeyError,ValueError):
            return False

    def resolve(self,path,record,queue):
        if record['state'] not in PENDING | {'error_access','error_request','error_response','budget_too_small','transport_unknown'}:
            return False
        original=json.dumps(record,sort_keys=True)
        binding=record['binding'];pid=binding['project_id']
        workflow=self.service.db.one('SELECT pause_requested FROM voice_workflows WHERE project_id=?',(pid,))
        if queue.project_paused(pid) or workflow and workflow['pause_requested']:
            return False
        if not self.current(binding):
            record['state']='stale_review'
        else:
            try:
                job=queue.inspect(record['job_id'])
                if (job['project']!=pid or job['category']!='source_reference' or job['model']!=binding['model']):
                    record['state']='error_response'
                elif job['state']=='succeeded':
                    payload=queue.request_payload(job['id'])
                    parts=[part for content in payload['contents'] for part in content['parts']]
                    images=[part['inlineData'] for part in parts if 'inlineData' in part]
                    text=''.join(part.get('text','') for part in parts)
                    if (len(images)!=1 or hashlib.sha256(base64.b64decode(images[0]['data'],validate=True)).hexdigest()!=binding['image_sha256'] or
                        digest(text)!=record.get('prompt_hash')):
                        raise ValueError('Request does not match reference')
                    raw=SourceFidelityReviewer.parse_response(queue.result(job['id']))
                    record['verdict']=verdict(raw)
                    # Recheck file and selection after parsing the response.
                    record['state']='checked' if self.current(binding) else 'stale_review'
                else:
                    record.update(state=job['state'],next_at=job['next_at'])
            except (DomainError,KeyError,ValueError,TypeError,AttributeError):
                record['state']='error_response'
        if json.dumps(record,sort_keys=True)==original:return False
        write(path,record)
        return record['state'] not in PENDING

    def tick(self,queue,limit=32):
        from .review_policy import visual_reviews_enabled
        if not visual_reviews_enabled():
            return {'updated': 0, 'enabled': False}
        updated=0
        for path in sorted((self.service.root/'voice').glob('*/reference_reviews/*.job.json')):
            if updated>=limit:
                break
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                if self.resolve(path,record,queue):
                    updated+=1
                    pid=record['binding']['project_id']
                    state='reference_review_ready' if record['state']=='checked' else 'reference_review_blocked'
                    stage='Проверка эталона готова. Сохранённый процесс можно продолжить.' if state=='reference_review_ready' else 'Проверка эталона требует внимания. Сохранённые изображения не изменены.'
                    with self.service.db.transaction() as connection:
                        connection.execute("UPDATE voice_workflows SET state=?,stage=?,result_json=json_set(result_json,'$.reference_review_wait.state',?,'$.reference_review_wait.next_retry_at',NULL) WHERE project_id=? AND state='waiting_reference' AND pause_requested=0 AND json_extract(result_json,'$.reference_review_wait.job_id')=?",(state,stage,record['state'],pid,record.get('job_id')))
            except (OSError,ValueError,KeyError,TypeError):
                continue
        return {'updated':updated}

    def summary(self,pid):
        entries=[]
        for path in sorted(self.directory(pid).glob('*.job.json')):
            try:
                record=json.loads(path.read_text(encoding='utf-8'));binding=record['binding']
                entries.append({'asset_id':binding['asset_id'],'anchor':binding['anchor'].get('canonical_name',''),
                    'state':record['state'],'job_id':record.get('job_id'),'next_retry_at':record.get('next_at') if record['state'] in PENDING else None,
                    'verdict':record.get('verdict') if record['state']=='checked' else None})
            except (OSError,ValueError,KeyError,TypeError):
                continue
        return entries

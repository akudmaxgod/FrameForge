"""Structured source issues and durable per-frame review checkpoints.

No generated media or scene data are written here. Reports are advisory state;
the original focus/source validators remain the acceptance authority.
"""
import copy
import hashlib
import json
import os
import threading
from pathlib import Path

VERSION='source-review-progress/1'
ISSUE_VERSION='source-issue/1'
EDITORIAL_CODES=('shot_size_preference','view_variety','duration_preference')
FIELDS={'subject':['subject'],'action':['action','visual_direction'],
        'participants':['participants'],'place':['place','visual_direction'],
        'time':['period','time_layer'],'state':['state','subject_state'],
        'modality':['source_status'],'physical_still':['visual_direction']}
_LOCK=threading.RLock()


def signature(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def safe_message(value):
    s = re.sub(r"gsk_[A-Za-z0-9_-]+", "[REDACTED_GROQ_KEY]", str(value))
    s = re.sub(r"nvapi-[A-Za-z0-9_-]+", "[REDACTED_NVIDIA_KEY]", s)
    s = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED_KEY]", s)
    return s[:1200]


def issue(item,code,kind,message,*,evidence=(),fields=(),attempts=0):
    if kind not in ('structural','semantic','editorial','provider'):raise ValueError('Unknown issue kind')
    key=item['key'];ordinal=int(key.split(':')[0])
    value={'version':ISSUE_VERSION,'frame_id':'frame:'+key,'frame_key':key,'frame_number':ordinal,
           'parent_event_id':item.get('parent',{}).get('id'),
           'code':code,'kind':kind,'severity':'advice' if kind=='editorial' else 'blocking',
           'message':safe_message(message),'source_scope':copy.deepcopy(item['narration']),
           'evidence':copy.deepcopy(list(evidence)),'affected_fields':list(fields),
           'neighbors':[],'attempts':attempts,'required':kind!='editorial'}
    value['id']=signature({k:value[k] for k in ('frame_key','code','kind','source_scope')})[:24]
    return value


def from_error(error,item,stage='contract'):
    found=getattr(error,'source_issues',None)
    if found:return copy.deepcopy(found)
    provider=getattr(error,'fatal_transport',False) or isinstance(error,(TimeoutError,ConnectionError))
    if provider:
        text=safe_message(error)
        import re
        code=next((code for pattern,code in ((r'\b429\b','provider_rate_limit'),(r'\b401\b|\b403\b','provider_access'),
             (r'\b404\b|\b410\b','provider_model_unavailable'),(r'\b413\b|too large','provider_request_size'),(r'timeout|время ожидания|deadline','provider_timeout'))
                   if re.search(pattern,text,re.I)),'provider_unavailable')
        return [issue(item,code,'provider',text)]
    kind='semantic' if stage=='content' or type(error).__name__=='SourceConflict' else 'structural'
    code='source_conflict' if kind=='semantic' else 'review_contract' if stage=='review' else 'frame_contract'
    return [issue(item,code,kind,error,fields=['visual_direction'] if kind=='semantic' else ['source','review'])]


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,path)


def begin(folder,story,items,request):
    path=Path(folder)/'review-progress.json'
    identity=signature(getattr(request,'identity','custom'))
    run=signature({'story':hashlib.sha256(story.encode()).hexdigest(),'items':items,'request':identity,'rules':VERSION})
    report={'version':VERSION,'run_id':run,'story_hash':hashlib.sha256(story.encode()).hexdigest(),
            'request_hash':identity,'status':'reviewing','frames':[{'key':i['key'],
                'frame_id':'frame:'+i['key'],'number':n+1,'source':copy.deepcopy(i['narration']),
                'neighbors':[items[j]['key'] for j in (n-1,n+1) if 0<=j<len(items)],
                'status':'pending','issues':[],'attempts':0} for n,i in enumerate(items)]}
    with _LOCK:write(path,report)
    return {'path':str(path),'run_id':run}


def update(run,key,*,status,issues=(),attempts=None,cache=None):
    with _LOCK:
        path=Path(run['path']);report=json.loads(path.read_text(encoding='utf-8'))
        if report['run_id']!=run['run_id']:return
        row=next(r for r in report['frames'] if r['key']==key)
        row.update(status=status,issues=copy.deepcopy(list(issues)))
        for problem in row['issues']:
            problem['neighbors']=row['neighbors'][:];problem['frame_number']=row['number']
        if attempts is not None:row['attempts']=attempts
        if cache:row['cache']=cache
        statuses=[r['status'] for r in report['frames']]
        report['counts']={s:statuses.count(s) for s in ('pending','reviewing','accepted','failed','paused')}
        sequence=report.get('sequence_review')
        sequence_pending=sequence is not None and sequence.get('status')!='complete'
        report['status']=('complete' if all(s=='accepted' for s in statuses) and not sequence_pending
                          else 'needs_attention' if 'failed' in statuses or sequence_pending else 'reviewing')
        write(path,report)


def public_reports(service,pid):
    service.editing.project(pid)
    current=service.project(pid)
    root=(service.root/'voice'/pid).resolve()
    reports=[]
    # Exact application-owned folders only; never accept a caller-provided path.
    for label,relative in (('text','general_source_review/story-visual-plan-1'),
                           ('timed','story-visual-timed'),('narrative','frame-focus-cache')):
        path=root/relative/'review-progress.json'
        if path.is_file():
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('version')==VERSION:
                value['stage']=label
                value['stale']=value['story_hash']!=hashlib.sha256(current['story'].encode()).hexdigest()
                value['can_resume']=not value['stale'] and value['status']!='complete'
                if service.db.one("SELECT id FROM generations WHERE project_id=? AND purpose='analysis' AND state IN ('queued','preparing','running','submitted','saving') LIMIT 1",(pid,)):
                    value['can_resume']=False;value['resume_note']='Проверка уже выполняется.'
                if label=='text' and service.db.one('SELECT id FROM scenes WHERE project_id=? LIMIT 1',(pid,)):
                    value['can_resume']=False;value['resume_note']='Есть сохранённые кадры. Новый разбор следует сохранить отдельной версией плана.'
                if value['can_resume'] and label=='text':
                    generation=service.db.one("SELECT * FROM generations WHERE project_id=? AND purpose='analysis' ORDER BY created_at DESC LIMIT 1",(pid,))
                    snapshot=json.loads(generation['snapshot']) if generation else {}
                    if (not generation or generation['state'] not in ('failed','cancelled','resource_degraded')
                            or not snapshot.get('pending_analysis')):
                        value['can_resume']=False;value['resume_note']='Нет сохранённого незавершённого разбора для продолжения.'
                    elif any(snapshot.get('project',{}).get(k)!=current[k] for k in ('id','version','story')):
                        value['can_resume']=False;value['resume_note']='Версия проекта изменилась после разбора. Нужна отдельная версия плана.'
                elif value['can_resume']:
                    job=service.db.one('SELECT * FROM voice_workflows WHERE project_id=?',(pid,))
                    if not job or job['state'] not in ('failed','paused') or not json.loads(job['options_json']).get('frame_content_focus'):
                        value['can_resume']=False;value['resume_note']='Нет приостановленной проверки отдельного плана кадров.'
                reports.append(value)
    return {'reports':reports,'media_regeneration_started':False}

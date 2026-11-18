"""Optional local visual grounding. Read paths only consume content-addressed caches."""
import base64,hashlib,io,json,logging,math,urllib.request,copy,threading,queue
from pathlib import Path
from .science_script import region_box
log=logging.getLogger(__name__)

def identity(service,plan):
    items=plan.get('illustrations') or [];steps=plan.get('reveal_steps') or []
    if len(items)!=1 or len(steps)<2 or not items[0].get('asset_id'):return None
    asset=service.asset(items[0]['asset_id']);targets=[{'key':s.get('region_key') or s['title'],'cue_quote':s['cue_quote'],'title':s['title']} for s in steps]
    key=hashlib.sha256(json.dumps({'version':1,'sha256':asset['sha256'],'targets':targets},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return key,asset,targets

def cached(service,plan):
    value=identity(service,plan)
    if not value:return {}
    key,asset,_=value;path=service.root/'science-grounding'/(key+'.json')
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('key')!=key or data.get('asset_sha256')!=asset['sha256']:return {}
        result=copy.deepcopy(data['result'])
        for region in result.get('scene_regions',[]):region['asset_id']=asset['id']
        for field in ('scene_layers','scene_paths'):
            for item in result.get(field,[]):item['source_asset_id']=asset['id']
        return result
    except (OSError,ValueError,KeyError):return {}

def iou(a,b):
    left=max(a[0],b[0]);top=max(a[1],b[1]);right=min(a[0]+a[2],b[0]+b[2]);bottom=min(a[1]+a[3],b[1]+b[3]);intersection=max(0,right-left)*max(0,bottom-top)
    return intersection/max(1e-9,a[2]*a[3]+b[2]*b[3]-intersection)

def consensus(first,second,asset,steps,targets):
    import re
    result={'scene_regions':[],'scene_layers':[],'scene_paths':[]}
    a={r.get('key'):r for r in first.get('regions',[]) if isinstance(r,dict)};b={r.get('key'):r for r in second.get('regions',[]) if isinstance(r,dict)}
    for index,target in enumerate(targets):
        key=target['key'];x=a.get(key,{});y=b.get(key,{})
        box=region_box(x.get('box'));other=region_box(y.get('box'))
        if not box or not other or x.get('visible') is not True or y.get('visible') is not True:continue
        if any(type(v.get('confidence')) not in (int,float) or not math.isfinite(v['confidence']) or v['confidence']<.92 for v in (x,y)) or iou(box,other)<.7:continue
        result['scene_regions'].append({'key':key,'box':[(c+d)/2 for c,d in zip(box,other)],'asset_id':asset['id'],'verified':True,'method':'local_vision_agreement'})
        if x.get('kind')==y.get('kind')=='rotating_part' and re.search(r'вращ|rotat|turn',steps[index]['cue_quote'],re.I):
            result['scene_layers'].append({'kind':'rotate','source_asset_id':asset['id'],'box':box,'verified':True,'step_index':index,'degrees_per_second':90,'method':'local_vision_agreement'})
    for x,y in zip(first.get('paths',[]),second.get('paths',[])):
        if not isinstance(x,dict) or not isinstance(y,dict):continue
        if x.get('step_index')!=y.get('step_index'):continue
        index=x.get('step_index')
        if type(index) is not int or not 0<=index<len(steps):continue
        points=x.get('points',[]);other=y.get('points',[])
        if not 2<=len(points)<=32 or len(points)!=len(other):continue
        try:
            if min(float(x.get('confidence',0)),float(y.get('confidence',0)))<.95:continue
            if any(len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in p) for p in points+other):continue
            if max(math.dist(a,b) for a,b in zip(points,other))>.025:continue
        except (TypeError,ValueError):continue
        result['scene_paths'].append({'source_asset_id':asset['id'],'verified':True,'points':points,'step_index':x['step_index'],'color':'#6CC8D0','period_ms':2200,'count':8,'method':'local_vision_agreement'})
    return result

def ensure(service,plan,request=None):
    value=identity(service,plan)
    if not value:return {}
    existing=cached(service,plan)
    if existing:return existing
    key,asset,targets=value
    if service.settings().get('science_auto_grounding',True) is False:return {}
    if request is None:
        from .media_analysis import LlamaServerManager
        manager=LlamaServerManager.get_manager(service)
        if not all(p.is_file() for p in (manager.binary_path,manager.base_model,manager.mmproj)):return {}
        def request(payload):
            manager.ensure_running()
            with manager.inference_lock:
                req=urllib.request.Request(f'http://127.0.0.1:{manager.port}/v1/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=120) as response:raw=json.load(response)
                return json.loads(raw['choices'][0]['message']['content'])
    from PIL import Image
    with Image.open(service.root/asset['path']) as image:
        image=image.convert('RGB');image.thumbnail((1400,1400));stream=io.BytesIO();image.save(stream,format='JPEG',quality=90)
    prompt=('Locate only clearly visible source-grounded parts. Return JSON {regions:[{key,visible,confidence,box:[x,y,width,height],kind:"part"|"rotating_part"}],paths:[{step_index,confidence,points:[[x,y],...]}]}. Coordinates normalized 0..1 relative to the full image. Unknown or ambiguous parts must be omitted. No invented anatomy, arrows, flow routes, or moving objects. Paths are allowed only along a clearly visible physical route described by that step. Rotating part must be a visibly isolated circular wheel/disc which the exact cue describes rotating. Target list: '+json.dumps(targets,ensure_ascii=False))
    payload={'model':'qwen3-vl','temperature':0,'max_tokens':1400,'response_format':{'type':'json_object'},'messages':[{'role':'system','content':prompt},{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(stream.getvalue()).decode()}}]}]}
    try:
        first=request(payload)
        if _STOP.is_set():return {}
        second=request({**payload,'messages':[{'role':'system','content':'Independently verify all coordinates; reject uncertain regions. '+prompt},payload['messages'][1]]})
        if _STOP.is_set():return {}
        result=consensus(first,second,asset,plan['reveal_steps'],targets)
    except Exception as error:
        log.warning('Scientific spatial grounding unavailable: %s',type(error).__name__);return {}
    path=service.root/'science-grounding'/(key+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps({'key':key,'asset_sha256':asset['sha256'],'result':result}),encoding='utf-8');temporary.replace(path)
    return result


_QUEUE=queue.Queue(maxsize=1024)
_JOBS={}
_DEFERRED={}
_LOCK=threading.RLock()
_STOP=threading.Event()
_THREAD=None

def available(service):
    if service.settings().get('science_auto_grounding',True) is False:return False
    from .media_analysis import LlamaServerManager
    manager=LlamaServerManager.get_manager(service)
    return all(p.is_file() for p in (manager.binary_path,manager.base_model,manager.mmproj))

def _worker():
    while not _STOP.is_set():
        try:item=_QUEUE.get(timeout=.2)
        except queue.Empty:continue
        service,pid,scene_id,plan,key=item
        with _LOCK:_JOBS[key]['state']='running'
        try:
            result=ensure(service,plan)
            if result and not _STOP.is_set():
                from .science_atlas_render import assemble_project
                assemble_project(service,pid,scene_ids=[scene_id])
            with _LOCK:_JOBS[key]['state']='done' if len(result.get('scene_regions',[]))>=2 else 'fallback'
        except Exception as error:
            with _LOCK:_JOBS[key]['state']='fallback'
            log.warning('Scientific grounding background fallback: %s',type(error).__name__)
        finally:_QUEUE.task_done()

def schedule(service,pid,scene_id,plan):
    global _THREAD
    if plan.get('presentation_motion')!='panorama_explain' or plan.get('script_scene'):return 'not_needed'
    value=identity(service,plan)
    if not value or cached(service,plan):return 'ready'
    if not available(service):return 'fallback'
    key=(str(service.root.resolve()),pid,scene_id,value[0])
    with _LOCK:
        if key in _JOBS:return _JOBS[key]['state']
        if _STOP.is_set():return 'fallback'
        if _QUEUE.full():return 'deferred'
        if len(_JOBS)>4096:
            for old in list(_JOBS):
                if _JOBS[old]['state'] not in ('queued','running'):_JOBS.pop(old)
                if len(_JOBS)<=2048:break
        _JOBS[key]={'state':'queued'};_QUEUE.put_nowait((service,pid,scene_id,copy.deepcopy(plan),key))
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD=threading.Thread(target=_worker,name='science-grounding',daemon=True);_THREAD.start()
    return 'queued'

def status(service,pid):
    root=str(service.root.resolve())
    with _LOCK:
        jobs=[value['state'] for key,value in _JOBS.items() if key[:2]==(root,pid)]
        deferred=_DEFERRED.get((root,pid),0)
    pending=sum(s in ('queued','running') for s in jobs)
    return {'pending':pending,'total':len(jobs),'deferred':deferred,'fallback':jobs.count('fallback'),'status':'running' if pending or deferred else 'ready'}

def prepare_project(service,pid):
    from .science_atlas_render import hydrate
    if service.project(pid).get('visual_format')!='science_atlas':return {'pending':0,'total':0,'deferred':0,'status':'not_applicable'}
    deferred=0
    for row in service.db.all('SELECT id FROM scenes WHERE project_id=? ORDER BY position',(pid,)):
        scene=service.scene(row['id']);raw=scene['data'].get('science_atlas') or {}
        if raw.get('presentation_motion')!='panorama_explain':continue
        result=schedule(service,pid,row['id'],hydrate(service,scene));deferred+=int(result=='deferred')
    with _LOCK:_DEFERRED[(str(service.root.resolve()),pid)]=deferred
    return status(service,pid)

def shutdown(wait=True):
    _STOP.set()
    while True:
        try:
            item=_QUEUE.get_nowait()
            with _LOCK:_JOBS[item[-1]]['state']='fallback'
            _QUEUE.task_done()
        except queue.Empty:break
    if wait and _THREAD and _THREAD is not threading.current_thread():_THREAD.join()

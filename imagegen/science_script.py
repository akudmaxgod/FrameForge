"""Source-anchored scientific beats and a shared scene-coordinate camera clock."""
import copy,math,re
from .domain import DomainError
VERSION=1

def smooth(value):
    t=max(0.,min(1.,value));return t*t*t*(t*(t*6-15)+10)

def prepare_plan(plan,source,source_start=0):
    p=copy.deepcopy(plan)
    if p.get('reveal_steps_locked') or (p.get('presentation_locked') and not p.get('reveal_steps')):return p
    if len(p.get('asset_keys',[]))!=1 or p.get('frame_density')!='filled':return p
    steps=p.get('reveal_steps') or []
    if not steps:
        clauses=[x.strip(' ,;.!?\n') for x in re.split(r'(?<=[.!?;])\s+|,\s+(?=\S)',source) if len(x.strip())>=12]
        if len(clauses)<2:return p
        notes=p.get('explanatory_labels') or p.get('labels') or []
        for i,text in enumerate(clauses[:4]):
            tokens=list(re.finditer(r'\S+',text));cue=text[:tokens[min(8,len(tokens))-1].end()];at=source.find(cue)
            if at<0 or source.count(cue)!=1:continue
            heading=notes[i] if i<len(notes) else ' '.join(text.split()[:7])
            if len(heading)>90:
                words=text.split();heading=''
                for word in words[:7]:
                    if len((heading+' '+word).strip())>90:break
                    heading=(heading+' '+word).strip()
            if not heading:continue
            steps.append({'kind':'caption','cue_quote':cue,'title':heading,'explanation':text if len(text)<=180 else '', 'region_key':''})
    valid=[];last=-1;duration=(p.get('illustration_motion') or {}).get('duration_seconds',6)*1000
    for step in steps[:4]:
        step=copy.deepcopy(step);quote=step.get('cue_quote','');pos=source.find(quote)
        if not quote or pos<0 or source.count(quote)!=1 or pos<=last:continue
        if step.get('kind') not in ('part','focus','caption'):continue
        if not str(step.get('title','')).strip():continue
        step['source_char_start']=source_start+pos
        if step.get('timing_source')!='audio_alignment':
            step['at_ms']=pos/max(1,len(source))*duration;step['timing_source']='estimated_text'
        valid.append(step);last=pos
    if len(valid)<2:return p
    p['reveal_steps']=valid;p['reveal_source']=source;p['script_reveal_version']=VERSION
    return p

def region_box(value):
    if not isinstance(value,(list,tuple)) or len(value)!=4:return None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in value):return None
    x,y,w,h=map(float,value)
    return [x,y,w,h] if x>=0 and y>=0 and w>=.015 and h>=.015 and x+w<=1.000001 and y+h<=1.000001 else None

def compile_script(plan):
    if plan.get('presentation_motion')!='panorama_explain':return None
    steps=plan.get('reveal_steps') or [];source=plan.get('reveal_source','')
    if len(steps)<2:return None
    if len(steps)>4:raise DomainError('Научная сцена содержит от 2 до 4 шагов раскрытия.')
    items=plan.get('illustrations') or []
    if len(items)!=1:return None
    primary=items[0];asset=primary.get('asset_id');regions={}
    if not asset:return None
    for region in plan.get('scene_regions',[]):
        box=region_box(region.get('box'))
        if region.get('verified') and region.get('asset_id')==asset and box:regions[region['key']]={**region,'box':box}
    result=[];last=-1;duration=max(1,(plan.get('illustration_motion') or {}).get('duration_seconds',6)*1000)
    for step in steps:
        quote=step.get('cue_quote','');pos=source.find(quote)
        if (step.get('kind') not in ('part','focus','caption') or not isinstance(step.get('title'),str) or not 0<len(step['title'])<=90 or not isinstance(step.get('explanation',''),str) or len(step.get('explanation',''))>180):raise DomainError('Шаг раскрытия требует короткий заголовок и пояснение.')
        if not quote or source.count(quote)!=1 or pos<=last:raise DomainError('Шаг раскрытия требует однозначную цитату текущей сцены по порядку.')
        at=step.get('at_ms') if step.get('timing_source')=='audio_alignment' else pos/max(1,len(source))*duration
        if type(at) not in (int,float) or not math.isfinite(at):raise DomainError('Некорректное время шага раскрытия.')
        region=regions.get(step.get('region_key') or step.get('title',''))
        result.append({**step,'at_ms':max(0,min(duration-1,at)),'region':region});last=pos
    focus=[s for s in result if s['region']]
    if len(focus)<2:return None
    layers=[]
    for layer in plan.get('scene_layers',[]):
        box=region_box(layer.get('box'))
        if not layer.get('verified') or layer.get('source_asset_id')!=asset or not box:continue
        if layer.get('kind') not in ('rotate','part'):continue
        index=layer.get('step_index',0)
        if type(index) is not int or not 0<=index<len(result):continue
        speed=layer.get('degrees_per_second',90)
        if type(speed) not in (int,float) or not math.isfinite(speed) or abs(speed)>720:continue
        layers.append({**layer,'box':box,'degrees_per_second':speed})
    for i,step in enumerate(result):
        region=step.get('region')
        if region and region.get('kind')=='rotating_part' and re.search(r'вращ|rotat|turn',step['cue_quote'],re.I) and not any(l['step_index']==i and l['kind']=='rotate' for l in layers):
            layers.append({'kind':'rotate','box':region['box'],'source_asset_id':asset,'verified':True,'step_index':i,'degrees_per_second':90})
        if step['kind']=='part' and step['region']:
            layers.append({'kind':'part','box':step['region']['box'],'source_asset_id':asset,'verified':True,'step_index':i,'degrees_per_second':0})
    paths=[]
    for path in plan.get('scene_paths',[]):
        points=path.get('points',[]);index=path.get('step_index',0)
        if not path.get('verified') or path.get('source_asset_id')!=asset or type(index) is not int or not 0<=index<len(result) or not 2<=len(points)<=32:continue
        if any(len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in p) for p in points):continue
        paths.append({**path,'period_ms':max(400,min(10000,path.get('period_ms',2200))),'count':max(1,min(24,path.get('count',8)))})
    result={'version':VERSION,'asset_id':asset,'width':primary.get('width') or 1920,'height':primary.get('height') or 1080,
        'title':plan.get('headline') or result[0]['title'],'duration_ms':duration,'steps':result,'layers':layers,'paths':paths,
        'overview':[64,210,1070,740],'window':[1192,210,664,540],'panorama':len(focus)>=2}


    if plan.get('slide_direction')=='right':
        result['overview'][0]=1920-result['overview'][0]-result['overview'][2]
        result['window'][0]=64
    return typography(result)

def typography(scene):
    from .science_atlas_render import font,lines,CONFIG
    size=48
    while len(lines(scene['title'],1776,size,600))>2 and size>22:size-=1
    title_lines=lines(scene['title'],1776,size,600)
    if len(title_lines)>2:raise DomainError('Сократите заголовок научной сцены.')
    scene['title_draw']=[{'text':line,'x':72,'y':90+i*size*1.2,'size':size,'weight':600,'color':CONFIG['text']} for i,line in enumerate(title_lines)]
    scene['colors']={**{k:CONFIG[k] for k in ('text','accent')},'muted':'#B7CAC9'}
    scene['caption_draw']=[]
    for i,step in enumerate(scene['steps']):
        size=32
        while True:
            titles=lines(step['title'],640,size,600);body=lines(step.get('explanation',''),640,size-5)
            if (len(titles)+len(body))*(size+6)<=202 or size<=21:break
            size-=1
        commands=[{'text':f"{i+1} / {len(scene['steps'])}",'x':0,'y':20,'size':18,'color':scene['colors']['muted']}];y=60
        for row in titles:commands.append({'text':row,'x':0,'y':y,'size':size,'weight':600,'color':CONFIG['text']});y+=size+6
        for row in body:commands.append({'text':row,'x':0,'y':y,'size':size-5,'color':scene['colors']['muted']});y+=size+6
        scene['caption_draw'].append(commands)
    return scene

def state(scene,seconds):
    t=max(0,min(scene['duration_ms'],seconds*1000));steps=scene['steps'];index=max([i for i,s in enumerate(steps) if s['at_ms']<=t] or [0])
    active=steps[index];region=active.get('region')
    # Keep a full view when no spatial claim has been verified.
    def camera(box):
        x,y,w,h=box;aspect=(scene['window'][2]/scene['window'][3])/(scene['width']/scene['height'])
        cw=max(w*1.65,h*1.65*aspect,.12);ch=cw/aspect
        factor=max(cw,ch,1);cw/=factor;ch/=factor
        return [max(0,min(1-cw,x+w/2-cw/2)),max(0,min(1-ch,y+h/2-ch/2)),cw,ch]
    points=[(i,s) for i,s in enumerate(steps) if s.get('region')]
    aspect=(scene['window'][2]/scene['window'][3])/(scene['width']/scene['height'])
    whole=[0,(1-1/aspect)/2,1,1/aspect] if aspect<1 else [(1-aspect)/2,0,aspect,1]
    crop=whole
    past=[(i,s) for i,s in points if s['at_ms']<=t]
    if past:
        cur_i,cur=past[-1];position=next(j for j,(i,s) in enumerate(points) if i==cur_i)
        previous=camera(points[position-1][1]['region']['box']) if position else whole
        following=points[position+1][1]['at_ms'] if position+1<len(points) else scene['duration_ms']
        span=max(1,min(1100,scene['duration_ms']*.16,max(1,following-cur['at_ms'])))
        phase=smooth((t-cur['at_ms'])/span)
        crop=[a+(b-a)*phase for a,b in zip(previous,camera(cur['region']['box']))]
    rotations=[]
    for layer in scene['layers']:
        elapsed=max(0,(t-steps[layer['step_index']]['at_ms'])/1000);ramp=.75
        # Integral of smooth acceleration; angle is absolute and seek-independent.
        u=min(1,elapsed/ramp);angle=layer['degrees_per_second']*(ramp*(u**3-.5*u**4)+max(0,elapsed-ramp))
        rotations.append({'angle':angle,'opacity':smooth(elapsed/.25)})
    return {'time_ms':t,'step_index':index,'crop':crop,'rotations':rotations,
            'caption_visible':t>=steps[0]['at_ms'],'step_progress':[smooth((t-s['at_ms'])/400) for s in steps]}

def bind_audio(plan,words,offset_ms=0):
    p=copy.deepcopy(plan)
    for step in p.get('reveal_steps',[]):
        at=step.get('source_char_start')
        word=next((w for w in words if at is not None and (w.get('char_start',-1)<=at<w.get('char_end',-1) or at<=w.get('char_start',-1)<at+len(step.get('cue_quote',''))) and w.get('start') is not None),None)
        if word:
            step['at_ms']=max(0,word['start']*1000+offset_ms);step['timing_source']='audio_alignment'
    return p

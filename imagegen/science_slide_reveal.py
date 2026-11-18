"""Opt-in scientific reveal. Coordinates are retained; time is scene-local in ms."""
import copy, math

MODE = 'slide_explain'
INITIAL = [72, 72, 1776, 936]
FINAL = [72, 240, 1070, 600]

def enabled(plan):
    from .science_sequence import MODES
    return plan.get('presentation_motion') in MODES

def duration_ms(plan):
    return max(1., float((plan.get('illustration_motion') or {}).get('duration_seconds', 6))*1000)

def timeline(plan):
    duration = duration_ms(plan)
    unit = min(1., duration/4000)
    hold, move = (700*unit,780*unit) if plan.get('presentation_motion','slide_explain')=='slide_explain' else (0.,1.)
    return {'duration': duration, 'hold': hold, 'move': move, 'end': hold+move,
            'settle': min(450*unit, duration*.15)}

def state(plan, seconds):
    timing = timeline(plan); t = max(0., seconds*1000)
    p = max(0., min(1., (t-timing['hold'])/timing['move']))
    p = p*p*p*(p*(p*6-15)+10)  # zero velocity/acceleration at both ends
    final = list((plan.get('slide_layout') or {}).get('final_rect',FINAL))
    if not plan.get('slide_layout') and plan.get('slide_direction', 'left') == 'right': final[0] = 1920-final[0]-final[2]
    # Uniform fit of the whole illustration/marker group, without cropping.
    k = min(INITIAL[2]/final[2], INITIAL[3]/final[3])
    start = [INITIAL[0]+(INITIAL[2]-final[2]*k)/2, INITIAL[1]+(INITIAL[3]-final[3]*k)/2,
             final[2]*k, final[3]*k]
    mode=plan.get('presentation_motion','slide_explain')
    if mode!='slide_explain':
        p=1.;start=final
    rect = [a+(b-a)*p for a,b in zip(start, final)]
    if mode=='focus_explain':
        q=max(0.,min(1.,t/timing['duration']));q=q*q*(3-2*q);factor=.985+.015*q
        rect=[final[0]+final[2]*(1-factor)/2,final[1]+final[3]*(1-factor)/2,final[2]*factor,final[3]*factor]
    return {'rect':rect, 'final_rect':final, 'progress':p,
            'opacity':max(0.,min(1.,t/max(1,200*min(1.,timing['duration']/4000))))}

def command_state(plan, command, seconds):
    if command.get('always_visible'):return {'start':0.,'progress':1.,'opacity':1.}
    timing = timeline(plan); duration = timing['duration']; unit = min(1., duration/4000)
    if duration < command.get('min_duration_ms',0):return {'start':duration,'progress':0.,'opacity':0.}
    group = command.get('reveal_group', 0)
    cue = command.get('cue_index'); cues = plan.get('cue_ms') or []
    anchored = cues[cue] if isinstance(cue,int) and cue < len(cues) else None
    start = float(anchored) if isinstance(anchored,(int,float)) and math.isfinite(anchored) else 0.
    steps=plan.get('reveal_steps') or [];speech_anchor=False
    if command.get('motion_group')=='support' and steps and int(group)<len(steps):
        step=steps[int(group)]
        start=max(start,float(step.get('at_ms',0)))
        speech_anchor=step.get('timing_source')=='audio_alignment'
    support = command.get('motion_group') != 'illustration'
    if support:
        mode=plan.get('presentation_motion','slide_explain')
        base=timing['end'] if mode=='slide_explain' else 80*unit if mode=='focus_explain' else 200*unit
        gap=0 if mode=='focus_explain' else 220*unit
        start=max(start,base+min(group,6)*gap)
        if mode=='detail_explain' and command.get('support_role')=='detail':start=max(120*unit,float(anchored) if isinstance(anchored,(int,float)) else 0.)
        if plan.get('support_reveal_ms'): start = max(start, float(plan['support_reveal_ms']))
    span = (180 if plan.get('presentation_motion')=='focus_explain' else 350 if command['type']=='text' else 420)*unit
    start = max(0.,start) if speech_anchor else min(max(0.,start), max(0.,duration-timing['settle']-span))
    progress = max(0., min(1., (seconds*1000-start)/max(1.,span)))
    return {'start':start, 'progress':progress, 'opacity':min(1.,progress*3) if command['type']=='text' else 1.}

def transform(command, source, target):
    """Uniformly contain a command group; leave author coordinates untouched."""
    c=copy.deepcopy(command); k=min(target[2]/source[2],target[3]/source[3])
    dx=target[0]+(target[2]-source[2]*k)/2-source[0]*k
    dy=target[1]+(target[3]-source[3]*k)/2-source[1]*k
    for name in ('x','x1','x2'): 
        if name in c:c[name]=c[name]*k+dx
    for name in ('y','y1','y2'):
        if name in c:c[name]=c[name]*k+dy
    for name in ('w','h','radius','width','size'):
        if name in c:c[name]*=k
    if 'rect' in c:
        x,y,w,h=c['rect'];c['rect']=[x*k+dx,y*k+dy,w*k,h*k]
    return c

def compile_slide(plan):
    from .science_atlas_render import font, CONFIG,lines as header_lines
    from .science_visual_render import compile_visual
    from .science_explanatory import clean_notes,valid_scheme,draw_scheme
    from .science_typography import fit_commands
    from .science_density import require_explanation,command_bounds,contain_rect
    require_explanation(plan)
    from .domain import DomainError
    if plan.get('slide_direction','left') not in ('left','right'):
        raise DomainError('Направление раскрытия: left или right.')
    items=plan.get('illustrations',[])
    if len(items)!=1:raise DomainError('Плавное раскрытие требует одну основную иллюстрацию; сравнения сохраняют свой макет.')
    result=copy.deepcopy(plan);base=copy.deepcopy(plan);base['composition_mode']='slide_source';base['layout']='hero_caption'
    raw=compile_visual(base)['commands'];commands=[];target=[64,72,1080,936]
    if plan.get('reveal_steps') and plan.get('headline'):
        title=plan['headline'];size=48
        while len(header_lines(title,1776,size,600))>2 and size>28:size-=1
        for i,text in enumerate(header_lines(title,1776,size,600)):
            commands.append({'type':'text','text':text,'x':72,'y':90+i*size*1.2,'size':size,'weight':600,'color':CONFIG['text'],'always_visible':True,'motion_group':'title'})
        target=[64,180,1080,828]
    detail_layout=plan.get('presentation_motion')=='detail_explain' and bool(plan.get('details') or plan.get('detail_assets')) and not plan.get('explanatory_scheme')
    if detail_layout:target=[64,72,820,936]
    if plan.get('slide_direction','left')=='right':target[0]=1920-target[0]-target[2]
    offset=len(plan.get('nodes',[]))+len(plan.get('elements',[]))
    primary=[c for c in raw if c['type']=='image' or isinstance(c.get('cue_index'),int) and c['cue_index']>=offset]
    elements=[c for c in raw if c not in primary]
    from .science_density import support_summary
    support=support_summary(plan);has_column=support['notes']>=2 or support['scheme'] or support['details'] or support['geometry']>=2
    if not has_column:target=[72,72,1776,936]
    if plan.get('reveal_steps') and plan.get('headline'):target=[target[0],180,target[2],min(828,target[3])]
    # The contain rectangle is reduced to actual image pixels before fitting the group.
    primary=fit_commands(primary,font)
    iw,ih=items[0].get('width') or 1000,items[0].get('height') or 1000
    for c in primary:
        if c['type']=='image':c['rect']=contain_rect([0,0,iw,ih],c['rect'])
    source=command_bounds(primary,font);final=contain_rect(source,target)
    result['slide_layout']={'version':2,'final_rect':final,'source_rect':source,'has_support_column':bool(has_column)}
    for c in primary:
        c=transform(c,source,final);c.update(motion_group='illustration',reveal_group=0,locked_typesetting=True)
        c.pop('clip_box',None);c.pop('typing_cps',None);commands.append(c)
    if not has_column:
        result['commands']=commands;result['slide_contract_version']=2;return result
    sx=(948 if detail_layout else 1192) if plan.get('slide_direction','left')=='left' else 64
    sw=908 if detail_layout else 664;notes=clean_notes(plan.get('explanatory_labels',plan.get('labels') or []),limit=4)
    scheme=plan.get('explanatory_scheme');scheme=scheme if valid_scheme(scheme,plan.get('support_quote','')) else None
    details=[d for d in plan.get('details',[]) if d.get('asset_id') and d['asset_id']!=items[0].get('asset_id')]
    # Prefer the source-bound diagram over a competing detail when space is limited.
    graphic=bool(scheme or elements or details);header=bool(plan.get('reveal_steps') and plan.get('headline'));y=190 if header else 90;note_bottom=(555 if header else 455) if graphic else 930
    note_size=36 if len(notes)>2 else 42
    def wrap(value,size,width,weight=500):
        rows=[];line='';f=font(size,weight)
        for word in value.split():
            if line and f.getlength(line+' '+word)>width:rows.append(line);line=''
            while f.getlength(word)>width and len(word)>1:
                end=1
                while end<len(word) and f.getlength(word[:end+1])<=width:end+=1
                rows.append(word[:end]);word=word[end:]
            line=(line+' '+word).strip()
        if line:rows.append(line)
        return rows
    while notes and sum(len(wrap(n,note_size+4 if i==0 else note_size,sw,650 if i==0 else 450))*(note_size+4 if i==0 else note_size)*1.28+22 for i,n in enumerate(notes))>note_bottom-y and note_size>24:note_size-=1
    for i,note in enumerate(notes):
        size=note_size+4 if i==0 else note_size
        for j,row in enumerate(wrap(note,size,sw,650 if i==0 else 450)):
            commands.append({'type':'text','text':row,'x':sx,'y':y+size,'size':size,'weight':650 if i==0 else 450,
                             'color':CONFIG['text'] if i==0 else '#B7CAC9','motion_group':'support',
                             'reveal_group':i+j*.35,'min_duration_ms':0 if i==0 else 1800+i*650,'locked_typesetting':True,'text_max_width':sw})
            y+=size*1.28
        y+=22
    gy=max(155,y+25);gh=1000-gy
    if elements:
        # Keep the exact planned local experiment/geometry, uniformly fitted to its column.
        fitted_elements=fit_commands(elements,font);element_bounds=command_bounds(fitted_elements,font)
        for c in fitted_elements:
            c=transform(c,element_bounds,[sx,gy,sw,gh]);c.update(motion_group='support',reveal_group=len(notes)+1,locked_typesetting=True)
            c.pop('clip_box',None);c.pop('typing_cps',None);commands.append(c)
    elif scheme:
        scheme=copy.deepcopy(scheme)
        if scheme['kind'] in ('signal','network'):scheme['arrows']=True
        graphic_commands=draw_scheme(scheme,[0,0,570,570])
        if scheme['kind']=='measurement':
            diameter=next(c for c in graphic_commands if c['type']=='line' and c['y1']==c['y2'] and abs(c['x2']-c['x1'])>100)
            for end,direction in ((diameter['x1'],1),(diameter['x2'],-1)):
                for off in (-7,7):graphic_commands.append({**diameter,'x1':end+12*direction,'x2':end,'y1':diameter['y1']+off})
        fitted=[]
        for c in graphic_commands:
            if c['type']!='text':fitted.append(c);continue
            c=copy.deepcopy(c);width=max(50,558-c['x']);size=round(c['size'])
            height=min(90,558-c['y'])
            for other in graphic_commands:
                if other['type']=='text' and other['y']>c['y']+10 and abs(other['x']-c['x'])<width*.8:height=min(height,other['y']-c['y']-8)
            rows=wrap(c['text'],size,width,c.get('weight',500))
            while len(rows)*size*1.2>height and size>18:
                size-=1;rows=wrap(c['text'],size,width,c.get('weight',500))
            for row_index,row in enumerate(rows):fitted.append({**c,'size':size,'text':row,'y':c['y']+row_index*size*1.2})
        scheme_bounds=command_bounds(fitted,font)
        for i,c in enumerate(fitted):
            c=transform(c,scheme_bounds,[sx,gy,sw,gh]);c.update(motion_group='support',reveal_group=len(notes)+min(i*.12,2),locked_typesetting=True)
            commands.append(c)
    elif details:
        commands.append({'type':'image','asset_id':details[0]['asset_id'],'rect':[sx,gy,sw,gh],
                         'motion_group':'support','support_role':'detail','reveal_group':len(notes)+1})
    result['commands']=commands;result['slide_contract_version']=2
    return result

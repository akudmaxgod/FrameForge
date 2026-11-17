"""Illustration-led scientific frames: narration stays in audio, labels stay concise."""
import copy,math
from .science_atlas_render import CONFIG,lines,readable_color

def compile_visual(plan):
    from .domain import DomainError
    if (plan.get('layout') not in ('hero_caption','image_left','image_right','comparison','instruction','local_board')
            or len(plan.get('elements',[]))>20 or len(plan.get('annotations',[]))>5
            or len(plan.get('illustrations',[]))>2 or len(plan.get('details',[]))>2):raise DomainError('Некорректная визуальная постановка научпопа.')
    for annotation in plan.get('annotations',[]):
        if not 0<=annotation['x']<=1 or not 0<=annotation['y']<=1 or len(annotation.get('label',''))>70:raise DomainError('Некорректное выделение научпопа.')
    plan=copy.deepcopy(plan);plan.setdefault('illustration_motion',{'kind':'push_in','strength':.07,'duration_seconds':6});commands=[]
    def text(value,x,y,width=420,size=32,color=None,cue=None):
        value=str(value).replace('≈','около ').replace('→',' — ').replace('↔',' / ').replace('≥','>=').replace('≤','<=')
        for i,line in enumerate(lines(value,width,size,500)):
            commands.append({'type':'text','text':line,'x':x,'y':y+size+i*size*1.25,'size':size,'weight':500,'color':color or CONFIG['text'],'cue_index':cue})
    illustrations=plan.get('illustrations',[])
    if plan['layout']=='comparison':
        for i,item in enumerate(illustrations[:2]):
            if item.get('asset_id'):commands.append({'type':'image','asset_id':item['asset_id'],'rect':[45+i*960,45,870,900]})
            label=(plan.get('labels') or ['',''])[i];text(label,85+i*960,965,790,34)
    elif illustrations:
        if illustrations[0].get('asset_id'):commands.append({'type':'image','asset_id':illustrations[0]['asset_id'],'rect':[32,20,1856,1040]})
    # An actual experiment or geometry occupies the frame, with no presentation heading.
    for i,e in enumerate(plan.get('elements',[])):
        x=100+e['x']*1720;y=90+e['y']*870;w=e.get('w',0)*1720;h=e.get('h',0)*870
        cue=i if e.get('source_char_start') is not None else None;color=e.get('color') or CONFIG['accent'];kind=e['kind']
        def line(a,b,c,d):commands.append({'type':'line','x1':a,'y1':b,'x2':c,'y2':d,'color':color,'width':4,'cue_index':cue})
        if kind=='text':
            background=CONFIG['background']
            for box in plan.get('elements',[]):
                if box['kind']=='box' and box.get('fill') and box['x']<=e['x']<box['x']+box.get('w',0) and box['y']<=e['y']<box['y']+box.get('h',0):background=box.get('color') or CONFIG['accent']
            text(e['text'],x,y,max(120,w or 420),36,readable_color(e.get('color'),background),cue)
        elif kind=='box':commands.append({'type':'rect','x':x,'y':y,'w':w,'h':h,'color':color,'fill':e.get('fill',False),'width':3,'cue_index':cue})
        elif kind=='dot':commands.append({'type':'circle','x':x,'y':y,'radius':max(6,min(28,abs(w)/2 or 12)),'color':color,'cue_index':cue})
        elif kind=='cross':
            r=max(10,min(30,abs(w)/2 or 18));line(x-r,y,x+r,y);line(x,y-r,x,y+r)
        elif kind in ('line','arrow'):
            line(x,y,x+w,y+h)
            if kind=='arrow':
                angle=math.atan2(h,w)
                for off in (-.55,.55):line(x+w-18*math.cos(angle+off),y+h-18*math.sin(angle+off),x+w,y+h)
        if kind!='text' and e.get('text'):text(e['text'],x,y+max(0,h)+15,max(200,abs(w)),30,cue=cue)
    offset=len(plan.get('nodes',[]))+len(plan.get('elements',[]))
    for i,a in enumerate(plan.get('annotations',[])):
        if plan.get('annotation_policy')=='verified_positions_only' and not a.get('position_verified'):continue
        x=a['x']*1920;y=a['y']*1080;cue=offset+i
        # Keep labels on their subject when a square Flow asset is contained in 16:9.
        if illustrations:
            panel=min(len(illustrations)-1,int(a['x']*2)) if plan['layout']=='comparison' else 0
            item=illustrations[panel];iw=item.get('width');ih=item.get('height')
            if iw and ih:
                bx,by,bw,bh=([45+panel*960,45,870,900] if plan['layout']=='comparison' else [32,20,1856,1040])
                ratio=min(bw/iw,bh/ih);dw,dh=iw*ratio,ih*ratio
                localx=a['x']*2-panel if plan['layout']=='comparison' else a['x']
                x=bx+(bw-dw)/2+localx*dw;y=by+(bh-dh)/2+a['y']*dh
        commands.append({'type':'ring','x':x,'y':y,'radius':48,'width':4,'color':CONFIG['accent'],'cue_index':cue})
        if a['label']:
            labelx=max(60,min(1440,x+65));labely=max(30,min(970,y-20))
            text(a['label'],labelx,labely,420,34,cue=cue)
    if plan.get('representation')=='analogy':text('Аналогия',65,30,600,25)
    plan['commands']=commands;return enrich_composition(plan)

def focus_state(plan,seconds):
    """A moving attention marker, not a literal scientific ray or pathway."""
    offset=len(plan.get('nodes',[]))+len(plan.get('elements',[]));cues=plan.get('cue_ms') or []
    rings={c.get('cue_index'):c for c in plan.get('commands',[]) if c['type']=='ring' and c.get('cue_index') is not None}
    active=[i for i in sorted(rings) if i<len(cues) and cues[i] is not None and cues[i]<=seconds*1000]
    if not active:return None
    current=active[-1];target=rings[current];previous=active[-2] if len(active)>1 else current
    t=min(1,max(0,(seconds*1000-cues[current])/350)) if previous!=current else 1
    t=t*t*(3-2*t);start=rings[previous]
    return {'index':current,'x':start['x']+(target['x']-start['x'])*t,'y':start['y']+(target['y']-start['y'])*t,'phase':round(t,3)}

def enrich_composition(plan):
    """Place independently generated detail assets; never fall back to overview crops."""
    if plan.get('composition_mode')!='explanatory' or plan['layout']=='comparison':return plan
    items=plan.get('illustrations',[])
    if len(items)!=1 or not items[0].get('asset_id') or plan.get('elements'):return plan
    details=[d for d in plan.get('details',[]) if d.get('asset_id') and d['asset_id']!=items[0]['asset_id']]
    unique={d['asset_id']:d for d in details};details=list(unique.values())[:2]
    from .science_explanatory import clean_notes,valid_scheme,draw_scheme
    notes=clean_notes(plan.get('explanatory_labels') or plan.get('labels') or [])
    scheme=plan.get('explanatory_scheme')
    if not valid_scheme(scheme,plan.get('support_quote','')):scheme=None
    if not details and not notes and not scheme:return plan
    # Remove unverified spatial claims rather than remapping them onto another view.
    commands=[dict(c,rect=[30,65,1210,950]) if c['type']=='image' else c
              for c in plan['commands'] if c['type'] not in ('ring',) and c.get('cue_index') is None]
    for slot,item in enumerate(details):
        rect=[1290,55,570,470 if scheme else 650 if notes else 880] if len(details)==1 else [1290,70+slot*500,570,410]
        commands.append({'type':'image','asset_id':item['asset_id'],'rect':rect,'at_ms':plan.get('support_reveal_ms',0)})
    if scheme and len(details)<2:
        commands.extend(draw_scheme(scheme,[1280,565 if details else 230,570,440 if details else 570]))
    if notes and len(details)<2 and not (scheme and details):
        y=104 if scheme and not details else 775 if not scheme and details else 970 if details else 350
        # An object name is a heading; a useful state is secondary explanatory text.
        if scheme and scheme['kind']=='layers':
            import re
            notes=[re.split(r'\s+(?:с|в)\s+',notes[0],maxsplit=1)[0]]
        typing_offset=0
        for i,note in enumerate(notes[:1] if scheme else notes):
            size=42 if i==0 else 30;weight=650 if i==0 else 400;color=CONFIG['text'] if i==0 else '#A7B7B8'
            for j,value in enumerate(lines(note,560,size,weight)):
                commands.append({'type':'text','text':value[0].upper()+value[1:] if value and j==0 else value,'x':1290,'y':y,'size':size,'weight':weight,'color':color,'at_ms':plan.get('support_reveal_ms',0) if details else 0})
                if plan.get('text_animation')=='typewriter':
                    commands[-1].update(typing_cps=18,typing_offset=typing_offset);typing_offset+=len(value)+1
                y+=size*1.3
            y+=32
    plan['commands']=commands;return plan

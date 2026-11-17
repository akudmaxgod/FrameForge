"""Meaningful frame occupancy: explicit filled plans and justified single subjects."""
import copy

def support_summary(plan):
    from .science_explanatory import clean_notes,valid_scheme
    notes=clean_notes(plan.get('explanatory_labels',plan.get('labels') or []),limit=4)
    scheme=valid_scheme(plan.get('explanatory_scheme'),plan.get('support_quote',''))
    details=[d for d in plan.get('details',[]) if d.get('asset_id') and all(d['asset_id']!=i.get('asset_id') for i in plan.get('illustrations',[]))]
    geometry=[e for e in plan.get('elements',[]) if e.get('kind') in ('line','arrow','box','dot','cross')]
    labels=[a for a in plan.get('annotations',[]) if a.get('position_verified') and a.get('label')]
    return {'notes':len(notes),'scheme':bool(scheme),'details':len(details),'geometry':len(geometry),'verified_labels':len(labels)}

def has_explanation(plan):
    s=support_summary(plan)
    return s['notes']>=2 or s['scheme'] or s['details']>0 or s['geometry']>=2 or s['verified_labels']>=2

def require_explanation(plan):
    from .domain import DomainError
    if not has_explanation(plan):
        raise DomainError('Для наполненного кадра добавьте минимум два разных пояснения, подтверждённую схему или готовую деталь. Один рисунок допустим только с явным объяснением, почему этого требует сцена.')

def normalize_density(plan):
    """Legacy absence is preserved; an explicit policy never falls back silently."""
    from .domain import DomainError
    density=plan.get('frame_density','legacy')
    if density not in ('legacy','filled','single_subject'):raise DomainError('Некорректное правило наполнения кадра.')
    if density=='legacy':return plan
    p=copy.deepcopy(plan)
    if density=='single_subject':
        if len(str(p.get('single_subject_reason','')).strip())<20:
            raise DomainError('Укажите содержательную причину одиночного рисунка: что именно зрителю нужно рассмотреть в этой сцене.')
        p['presentation_motion']='none';p['composition_mode']='single_subject';return p
    items=p.get('illustrations',[])
    if len(items)==1:
        from .science_sequence import MODES
        require_explanation(p)
        if p.get('presentation_motion') not in MODES:p['presentation_motion']='staged_explain'
    elif not items and not (p.get('layout') in ('instruction','local_board') and len(p.get('elements',[]))>=2 and any(e.get('kind')!='text' for e in p.get('elements',[]))) and len([e for e in p.get('elements',[]) if e.get('kind')!='text'])<2:
        raise DomainError('Наполненный кадр требует иллюстрацию с пояснениями или содержательную локальную схему.')
    return p

def command_bounds(commands,font):
    """Measure the actual vector/text footprint, including labels and stroke pads."""
    boxes=[]
    for c in commands:
        kind=c['type'];pad=c.get('width',0)/2+3
        if kind=='image':x,y,w,h=c['rect'];box=[x,y,x+w,y+h]
        elif kind=='text':
            l,t,r,b=font(max(1,round(c['size'])),c.get('weight',400)).getbbox(c.get('text',''),anchor='ls')
            box=[c['x']+l-3,c['y']+t-3,c['x']+r+3,c['y']+b+3]
        elif kind in ('circle','ring'):
            r=c['radius']+pad;box=[c['x']-r,c['y']-r,c['x']+r,c['y']+r]
        elif kind in ('line','pulse'):
            pad=max(pad,c.get('radius',0)+3);box=[min(c['x1'],c['x2'])-pad,min(c['y1'],c['y2'])-pad,max(c['x1'],c['x2'])+pad,max(c['y1'],c['y2'])+pad]
        elif kind=='rect':box=[c['x']-pad,c['y']-pad,c['x']+c['w']+pad,c['y']+c['h']+pad]
        else:continue
        boxes.append(box)
    if not boxes:return [0,0,1,1]
    left=min(b[0] for b in boxes);top=min(b[1] for b in boxes)
    return [left,top,max(1,max(b[2] for b in boxes)-left),max(1,max(b[3] for b in boxes)-top)]

def contain_rect(source,target):
    k=min(target[2]/source[2],target[3]/source[3]);w=source[2]*k;h=source[3]*k
    return [target[0]+(target[2]-w)/2,target[1]+(target[3]-h)/2,w,h]

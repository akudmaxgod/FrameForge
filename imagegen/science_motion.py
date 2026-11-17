"""Deterministic scientific camera movement and short typewriter labels."""
import math

def image_scale(plan,command,seconds):
    motion=plan.get('illustration_motion') or {};items=plan.get('illustrations') or []
    if not items or command.get('asset_id')!=items[0].get('asset_id') or command.get('crop'):return 1
    if any(a.get('position_verified') for a in plan.get('annotations',[])):return 1
    strength=max(0,min(.12,float(motion.get('strength',0))))
    if not strength:return 1
    t=max(0,min(1,seconds/max(.1,float(motion.get('duration_seconds',6)))))
    t=t*t*(3-2*t)
    # Approach the full fitted size from below: never crop anatomical details.
    return (1+strength*t)/(1+strength)

def typed_text(command,seconds,preview=False):
    value=command.get('text','');rate=command.get('typing_cps',0)
    if preview or not rate:return value
    elapsed=max(0,seconds*1000-command.get('at_ms',0))/1000
    count=max(0,min(len(value),int(elapsed*rate)-command.get('typing_offset',0)))
    return value[:count]

def pulse_position(command,seconds):
    phase=(seconds*1000/max(1,command.get('period_ms',2200))+command.get('phase',0))%1
    return (command['x1']+(command['x2']-command['x1'])*phase,command['y1']+(command['y2']-command['y1'])*phase)

def playback_commands(plan):
    """Animate every planned explanatory overlay, including legacy saved plans."""
    commands=[dict(c) for c in plan.get('commands',[])]
    if plan.get('presentation')!='visual_first':return commands
    scheme=plan.get('explanatory_scheme') or {};unit=max(.05,float((plan.get('illustration_motion') or {}).get('duration_seconds',6)))/6
    cues=[c for c in plan.get('cue_ms',[]) if isinstance(c,(int,float)) and c>=0]
    base=min(min(cues) if cues and scheme else 0,max(0,6000*unit-2500*unit))
    if scheme.get('kind')=='measurement':
        diameter=next((c for c in commands if c['type']=='line' and c['y1']==c['y2'] and abs(c['x2']-c['x1'])>100),None)
        if diameter:
            for end,direction in ((diameter['x1'],1),(diameter['x2'],-1)):
                for offset in (-7,7):commands.append({'type':'line','x1':end+12*direction,'y1':diameter['y1']+offset,'x2':end,'y2':diameter['y1'],'color':diameter['color'],'width':diameter['width'],'at_ms':round(base+1500*unit),'draw_ms':round(250*unit)})
    order=0
    for command in commands:
        if command.get('cue_index') is not None:continue
        kind=command['type']
        if kind=='text' and plan.get('text_animation','typewriter')!='none' and not command.get('typing_cps'):
            command['typing_cps']=24
            if not command.get('at_ms'):
                command['at_ms']=0 if command.get('size',0)>=38 or plan.get('layout')=='comparison' else round(base+(150 if command.get('size',0)<=23 else 1750 if scheme else 600)*unit)
        if kind in ('ring','line','circle') and not command.get('draw_ms'):
            delay=(1050+order*120) if scheme.get('kind')=='measurement' and kind=='line' else 400+min(order,12)*160
            command.setdefault('at_ms',round(base+delay*unit));command['draw_ms']=max(50,round((650 if kind=='ring' else 280)*unit));order+=1
    for c in commands:
        if c.get('typing_cps'):
            available=max(.08,(6000*unit-c.get('at_ms',0)-100*unit)/1000)
            c['typing_cps']=max(c['typing_cps'],(len(c.get('text',''))+c.get('typing_offset',0)+1)/available)
    return commands

def draw_progress(command,seconds,preview=False):
    if preview or not command.get('draw_ms'):return 1
    return max(0,min(1,(seconds*1000-command.get('at_ms',0))/command['draw_ms']))

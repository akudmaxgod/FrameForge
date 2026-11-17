"""Deterministic semantic staging across a sequence, without a model/provider pass."""
import copy,math,re
from collections import Counter

VERSION=1
MODES=('slide_explain','staged_explain','focus_explain','detail_explain')

def arrange_scenes(scenes,force=False):
    result=copy.deepcopy(scenes);candidates=[];eligible=[];previous_key=None
    for index,scene in enumerate(result):
        data=scene.get('data',scene);p=data.get('science_atlas') or {};keys=p.get('asset_keys',[])
        if (p.get('frame_density')!='filled' or len(keys)!=1 or p.get('presentation_locked')
            or data.get('animation',{}).get('selected_engine') in ('omni_flash','veo_lite')):
            previous_key=keys[0] if len(keys)==1 else None;continue
        if p.get('sequence_arrangement_version')==VERSION and not force:
            previous_key=keys[0];continue
        timing=data.get('voice_timing') or data.get('prompt_timing') or {}
        duration=float(timing.get('duration_seconds') or (p.get('illustration_motion') or {}).get('duration_seconds') or 6)
        if timing.get('end_ms') is not None and timing.get('start_ms') is not None:duration=max(.05,(timing['end_ms']-timing['start_ms'])/1000)
        focus=(p.get('focus','')+' '+p.get('support_quote','')).lower();function=p.get('function','')
        score=1.15 if function=='mechanism' else .6 if function=='structure' else 0
        if p.get('explanatory_scheme'):score+=1.8
        if re.search(r'обзорн[а-я]* маршрут|сегодня.{0,30}пройд|путь от',focus):score+=3.6
        elif re.search(r'начн[её]м|разбер[её]мся',focus):score+=1.5
        if keys[0]!=previous_key:score+=.45
        else:score-=.6
        if len(p.get('explanatory_labels') or p.get('labels') or [])>=3:score+=.4
        record={'index':index,'plan':p,'duration':duration,'function':function,'key':keys[0],'score':score}
        eligible.append(record)
        if duration>=4.2 and score>=1.55:candidates.append(record)
        previous_key=keys[0]
    # Select explanatory anchors by meaning, then enforce breathing room between them.
    anchors=set();cap=max(1,math.ceil(len(eligible)*.23))
    fixed={i for i,s in enumerate(result) if s.get('data',s).get('science_atlas',{}).get('presentation_motion')=='slide_explain'
           and s.get('data',s).get('science_atlas',{}).get('presentation_locked')}
    for record in sorted(candidates,key=lambda r:(-r['score'],r['index'])):
        i=record['index']
        if all(abs(i-j)>=3 for j in anchors|fixed):anchors.add(i)
        if len(anchors)>=cap:break
    records={r['index']:r for r in eligible};last=[];reveal_no=0;previous_key=None;previous_direction='left'
    for index,scene in enumerate(result):
        data=scene.get('data',scene);p=data.get('science_atlas') or {}
        if index not in records:
            last=[];previous_key=None;continue
        r=records[index]
        if index in anchors:
            mode='slide_explain';reason='Ключевой ввод или объяснение механизма: крупный объект раскрывает место для схемы и пояснений.'
        elif r['function'] in ('observation','summary','transition') or r['duration']<4.2:
            mode='focus_explain';reason='Наблюдение, итог или короткая реплика: спокойный крупный план с готовыми пояснениями.'
        elif p.get('detail_assets') and not p.get('explanatory_scheme'):
            mode='detail_explain';reason='В плане есть отдельная детальная иллюстрация: общий вид и деталь показаны вместе без повторного съезда.'
        else:
            mode='staged_explain';reason='Пояснения и линии раскрываются поэтапно в уже собранной композиции, без движения всей иллюстрации в сторону.'
        if len(last)>=2 and last[-1]==last[-2]==mode and mode!='slide_explain':
            mode='focus_explain' if mode!='focus_explain' else 'staged_explain'
            reason+=' Смена подачи прерывает повтор соседних постановок.'
        if mode=='slide_explain':
            direction='right' if reveal_no%3==2 else 'left';reveal_no+=1
        elif r['key']==previous_key:direction=previous_direction
        else:direction='left'
        p.update(presentation_motion=mode,slide_direction=direction,sequence_arrangement_version=VERSION,sequence_reason=reason)
        last.append(mode);last=last[-2:];previous_key=r['key'];previous_direction=direction
    counts=Counter(s.get('data',s).get('science_atlas',{}).get('presentation_motion','none') for s in result)
    return result,{'version':VERSION,'counts':dict(counts),'reveal_positions':[i+1 for i,s in enumerate(result) if s.get('data',s).get('science_atlas',{}).get('presentation_motion')=='slide_explain']}


def arrange_analysis(analysis):
    """Apply the shared scientific montage policy without changing caller data."""
    result=copy.deepcopy(analysis)
    result['scenes'],report=arrange_scenes(result.get('scenes',[]))
    result.setdefault('diagnostics',{}).setdefault('science_atlas',{})['sequence_arrangement']=report
    return result

"""Read-only boundary support and independent measurement, never auto-approval."""
import math
from .domain import DomainError, fingerprint

VERSION='voice-boundary-audit/1'

def build(story,words,scenes,duration,audio_hash):
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0:
        raise DomainError('Некорректная длительность озвучки.')
    rows=[]
    for scene in scenes:
        data=scene.get('data',scene);a,b=data['source_start'],data['source_end']
        if type(a) is not int or type(b) is not int or not 0<=a<b<=len(story) or story[a:b]!=data['source_quote']:
            raise DomainError('Цитата сцены не совпадает с текстом.',409)
        selected=[w for w in words if w['char_start']<b and w['char_end']>a]
        if not selected:raise DomainError('Нет слов для границы сцены.',409)
        w=selected[0];t=w.get('start');status='asr_supported';reasons=[]
        if t is None:status='unmatched';reasons.append('У первого слова нет времени из речи.')
        elif w['method'] not in ('exact','fuzzy','normalized_group'):
            status='approximate';reasons.append('Время отдельного слова рассчитано приблизительно.')
        if t is not None and (type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<duration):
            status='unmatched';reasons.append('Время выходит за границы записи.');t=None
        if a>w['char_start']:
            status='approximate' if t is not None else 'unmatched';reasons.append('Текстовая граница проходит внутри слова.')
        group=w.get('timing_group')
        if group and a>group['char_start']:
            status='approximate';reasons.append('Начало находится внутри числительного с общим временем.')
        elif group and status=='asr_supported':status='asr_group_supported'
        if t is not None and (w.get('confidence',0)<.55 or type(w.get('end')) not in (int,float) or not math.isfinite(w['end']) or w['end']-t<.02):
            status='approximate';reasons.append('Ненадёжная или слишком короткая опора на границе.')
        current=data.get('voice_timing') or {}
        row={'id':scene.get('id') or str(a),'position':scene.get('position',len(rows)+1),'source':{'start':a,'end':b,'quote':story[a:b]},
             'word':{'start':w['char_start'],'end':w['char_end'],'quote':story[w['char_start']:w['char_end']]},
             'predicted_speech_start':t,'current_cut':current.get('start'),'support_status':status,
             'reasons':reasons,'method':w['method'],'is_initial_scene':not rows}
        rows.append({**row,'hash':fingerprint(row)})
    # Stable temporal spread, plus every weak boundary. Label these before
    # calculating accuracy; do not select only successful observations.
    eligible=[r for r in rows if not r['is_initial_scene']]
    sample=[]
    for r in eligible:
        if r['support_status'] in ('unmatched','approximate'):sample.append(r['id'])
    for i in range(min(30,len(eligible))):
        r=eligible[round(i*(len(eligible)-1)/max(1,min(30,len(eligible))-1))]
        if r['id'] not in sample:sample.append(r['id'])
    document={'version':VERSION,'story_hash':fingerprint(story),'audio_sha256':audio_hash,'duration':duration,
              'boundaries':rows,'sample_ids':sample,'sampling':'temporal_spread_plus_all_weak_boundaries',
              'timing_accuracy_measured':False,'automatic_application':False,
              'definition':'Onset of the first spoken word of the next source scene; initial silence is not a speech boundary.'}
    return {**document,'hash':fingerprint(document)}

def measure(report,annotations):
    if not isinstance(report,dict) or report.get('hash')!=fingerprint({k:v for k,v in report.items() if k!='hash'}):
        raise DomainError('Данные проверки границ изменились. Сформируйте актуальную выборку.',409)
    if not isinstance(annotations,dict) or annotations.get('audit_hash')!=report['hash'] or annotations.get('audio_sha256')!=report['audio_sha256'] or annotations.get('story_hash')!=report['story_hash']:
        raise DomainError('Разметка относится к другой версии текста, звука или выборки.',409)
    labels=annotations.get('labels')
    if not isinstance(labels,list):raise DomainError('Нужен список независимых отметок времени.')
    by_id={r['id']:r for r in report['boundaries']};seen=set();observations=[];unmeasurable=[]
    for label in labels:
        if not isinstance(label,dict):raise DomainError('Некорректная отметка времени.')
        key=label.get('id');r=by_id.get(key)
        if not r or key in seen or label.get('boundary_hash')!=r['hash'] or label.get('origin')!='manual_listening':
            raise DomainError('Нужны уникальные отметки по прослушиванию для актуальных границ.',409)
        seen.add(key);seconds=label.get('seconds')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<=seconds<report['duration']:
            raise DomainError('Ручная отметка должна находиться внутри записи.')
        predicted=r['predicted_speech_start']
        if predicted is None:unmeasurable.append(key)
        else:observations.append({'id':key,'seconds':seconds,'error_seconds':abs(seconds-predicted)})
    selected=set(report['sample_ids']);sample=[r for r in observations if r['id'] in selected]
    errors=sorted(r['error_seconds'] for r in sample);missing=sorted(selected-seen)
    complete=bool(selected) and not missing and not any(k in selected for k in unmeasurable)
    within=sum(e<=.5 for e in errors)
    return {'audit_hash':report['hash'],'timing_accuracy_measured':bool(observations),'independent_labels':len(labels),
            'sample_size':len(selected),'measured_sample_size':len(sample),'missing_sample_ids':missing,
            'unmeasurable_ids':unmeasurable,'sample_complete':complete,'p95_error_seconds':errors[math.ceil(.95*len(errors))-1] if errors else None,
            'fraction_within_half_second':within/len(selected) if selected and sample else None,
            'target_met':complete and len(sample)>=20 and within/len(selected)>=.95,
            'observations':observations,'scope':'selected speech boundaries only; no automatic plan or image approval'}

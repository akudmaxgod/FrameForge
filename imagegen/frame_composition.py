"""Finite camera choices bound to immutable, independently reviewed content."""
import copy
from .source_review_status import signature
from .domain import DomainError

FACTS_VERSION='frame-facts/1'
VERSION='frame-composition/1'
FIELDS=('subject','action','participants','place','period','time_layer','state','subject_state',
        'source_status','location_ref','required_artifacts','evidence')
CHOICES={
 'shot_size':{'preserve':'Сохранить исходный план','wide':'Общий','medium':'Средний','close':'Крупный'},
 'camera_angle':{'preserve':'Сохранить исходный ракурс','eye_level':'На уровне предмета','high':'Сверху под углом','low':'Снизу под углом'},
 'placement':{'preserve':'Сохранить расположение','center':'По центру','thirds':'По правилу третей'}}
WORDS={
 'shot_size':{'wide':'wide shot','medium':'medium shot','close':'close shot'},
 'camera_angle':{'eye_level':'eye-level camera','high':'elevated oblique camera','low':'low oblique camera'},
 'placement':{'center':'centered framing','thirds':'rule-of-thirds framing'}}


def facts(data):
    focus=data.get('visual_content') or data.get('frame_focus')
    if not isinstance(focus,dict) or any(k not in focus for k in (*FIELDS,'visual_direction','source_review')):
        raise DomainError('Для выбора композиции нужен проверенный смысловой фокус кадра.',409)
    from .narrative_frame_focus import CATEGORIES
    checks=focus['source_review'].get('checks',[])
    if sorted(c.get('category','') for c in checks)!=sorted(CATEGORIES) or any(c.get('verdict') not in ('supported','not_applicable') for c in checks):
        raise DomainError('Содержание кадра ещё не принято по источнику.',409)
    if data.get('visual_content'):
        from .narrative_frame_focus import fingerprint
        if fingerprint(focus)!=data.get('visual_content_hash'):
            raise DomainError('Проверенное содержание кадра изменено.',409)
    value={'version':FACTS_VERSION,'key':focus.get('key'),
           'source':{k:data.get('source_'+k) for k in ('start','end','quote')},
           'event':copy.deepcopy((data.get('frame_context') or {}).get('parent_event') or data.get('semantic_parent_source') or (data.get('frame_focus') or {}).get('parent_episode')),
           'facts':{k:copy.deepcopy(focus[k]) for k in FIELDS},
           'physical_moment':focus['visual_direction'],'review_hash':signature(focus['source_review'])}
    value['content_hash']=signature(value)
    return value


def choice(packet,values=None):
    values=values or {key:'preserve' for key in CHOICES}
    if set(values)!=set(CHOICES) or any(values[k] not in CHOICES[k] for k in CHOICES):
        raise DomainError('Композиция допускает только выбранные план, ракурс и расположение.',400)
    return {'version':VERSION,'facts_hash':packet['content_hash'],**values}


def attach(data):
    result=copy.deepcopy(data);packet=facts(result);previous=result.get('frame_composition')
    result['frame_facts']=packet
    if previous and previous.get('facts_hash')==packet['content_hash']:
        validate(result)
    else:result['frame_composition']=choice(packet)
    return result


def validate(data):
    if not data.get('frame_facts') and not data.get('frame_composition'):return None
    packet=facts(data)
    if data.get('frame_facts')!=packet:
        raise DomainError('Факты кадра изменились после выбора композиции.',409)
    composition=data.get('frame_composition')
    if not isinstance(composition,dict) or set(composition)!={'version','facts_hash',*CHOICES}:
        raise DomainError('Неверная структура композиции кадра.',409)
    expected=choice(packet,{k:composition[k] for k in CHOICES})
    if composition!=expected:raise DomainError('Композиция относится к прежнему содержанию кадра.',409)
    return composition


def instruction(data, *, compact=False):
    composition=validate(data)
    if not composition:return ''
    words=[WORDS[k][composition[k]] for k in CHOICES if composition[k]!='preserve']
    if not words:return ''
    if compact:
        return ('CAMERA CHOICE: '+', '.join(words)+'. Override camera wording only; keep all approved facts and every selected participant; widen if needed. Add nothing.')
    return ('CAMERA CHOICE: '+', '.join(words)+'. This replaces camera wording only. '
            'Keep the approved subject, action phase, every selected participant, species, place, period, state and claim uncertainty. '
            'Keep all selected participants visible and intact; widen framing if needed. Add no objects, people, events or factual details.')


def inspect(service,sid):
    scene=service.scene(sid);data=scene['data'];packet=facts(data)
    project=service.project(scene['project_id']);span=packet['source']
    if (type(span['start']) is not int or type(span['end']) is not int
            or not 0<=span['start']<span['end']<=len(project['story']) or project['story'][span['start']:span['end']]!=span['quote']):
        raise DomainError('Кадр относится к другому исходному тексту.',409)
    if data.get('visual_content'):
        from .story_visual_plan import verified_participants
        verified_participants(data,project['story'])
    validate(data)
    return {'scene_id':sid,'revision':scene['current_revision'],'facts':packet,
            'composition':copy.deepcopy(data.get('frame_composition') or choice(packet)),
            'choices':copy.deepcopy(CHOICES),'media_regeneration_started':False}


def save(service,sid,payload):
    if not isinstance(payload,dict) or set(payload)!={'revision','facts_hash',*CHOICES}:
        raise DomainError('Передайте версию кадра, подпись фактов и три настройки камеры.',400)
    current=inspect(service,sid)
    if payload['revision']!=current['revision'] or payload['facts_hash']!=current['facts']['content_hash']:
        raise DomainError('Кадр изменился. Обновите композицию перед сохранением.',409)
    composition=choice(current['facts'],{k:payload[k] for k in CHOICES})
    if current['composition']==composition:return {**current,'changed':False}
    service.edit_scene(sid,{'frame_facts':current['facts'],'frame_composition':composition,
        'frame_composition_manual':current['facts']['content_hash']},current['revision'])
    return {**inspect(service,sid),'changed':True,'media_regeneration_started':False}

"""Application-owned event/source/audio identity; models cannot rewrite it."""
import copy
import hashlib
import json
import math

VERSION = 'frame-event/1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source(story, data):
    a, b = data.get('source_start'), data.get('source_end')
    if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(story)
            or story[a:b] != data.get('source_quote')):
        raise ValueError('Потеряна точная цитата события или кадра.')
    return {'start': a, 'end': b, 'quote': story[a:b]}


def timing(value):
    if value is None:
        return None  # Text-only plans never invent an audio clock.
    if not isinstance(value, dict):
        raise ValueError('Неверное время кадра в озвучке.')
    a, b = value.get('start'), value.get('end')
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in (a, b)) or not 0 <= a < b:
        raise ValueError('Время кадра должно соответствовать реальному интервалу озвучки.')
    duration = value.get('duration', b-a)
    if type(duration) not in (int, float) or not math.isfinite(duration) or abs(duration-(b-a)) > .02:
        raise ValueError('Длительность кадра расходится с границами озвучки.')
    return copy.deepcopy(value)


def build(story, data, ordinal):
    narration = source(story, data)
    inherited = data.get('semantic_parent_source')
    parent_source = source(story, inherited) if inherited else narration
    if not parent_source['start'] <= narration['start'] < narration['end'] <= parent_source['end']:
        raise ValueError('Фрагмент озвучки вышел за границы исходного события.')
    event_id = 'event_' + digest([hashlib.sha256(story.encode()).hexdigest(), parent_source])[:24]
    parent = {'id': event_id, 'source': parent_source,
              'ordinal': data.get('semantic_parent_ordinal', ordinal),
              'title': data.get('semantic_parent_title') or data.get('subject') or 'Исходное событие',
              'source_event_ids': list(data.get('source_event_ids') or [])}
    result = {'version': VERSION, 'parent_event': parent, 'narration': narration,
              'timing': timing(data.get('voice_timing'))}
    result['content_hash'] = digest(result)
    return result


def validate(story, packet, data=None):
    if (packet.get('version') != VERSION
            or packet.get('content_hash') != digest({k:v for k,v in packet.items() if k != 'content_hash'})):
        raise ValueError('Привязка кадра к событию изменена после проверки.')
    narration = packet['narration']; parent = packet['parent_event']; quote = parent['source']
    for item in (narration, quote):
        source(story, {'source_start': item['start'], 'source_end': item['end'], 'source_quote': item['quote']})
    if not quote['start'] <= narration['start'] < narration['end'] <= quote['end']:
        raise ValueError('Кадр не принадлежит исходному событию.')
    if parent['id'] != 'event_' + digest([hashlib.sha256(story.encode()).hexdigest(), quote])[:24]:
        raise ValueError('Подменён идентификатор исходного события.')
    timing(packet['timing'])
    if data is not None:
        if source(story, data) != narration or timing(data.get('voice_timing')) != packet['timing']:
            raise ValueError('Цитата или время кадра расходятся с проверенной привязкой.')


def display(value, entry):
    parent = value['parent_event']
    return {'version': 'narrative-frame-focus/1', **copy.deepcopy(entry['focus']),
            'narration': copy.deepcopy(value['narration']), 'timing': copy.deepcopy(value['timing']),
            'parent_episode': {'id': parent['id'], 'ordinal': parent['ordinal'], 'title': parent['title'],
                               'source': copy.deepcopy(parent['source']), 'source_event_ids': parent['source_event_ids']},
            'source_review': copy.deepcopy(entry['review'])}


def retime(story, plan, frames):
    """Refresh only app-owned clocks after an explicit boundary adjustment.

    No content/source/parent changes can be hidden in a timing edit.
    """
    document = plan['parent']
    if not document.get('frame_bindings'):
        return
    entries = plan['focus']['frames']
    if len(frames) != len(document['frame_bindings']) or len(entries) != len(frames):
        raise ValueError('Изменилось число кадров при корректировке времени.')
    for frame, binding, entry in zip(frames, document['frame_bindings'], entries):
        data=frame.get('data',frame)
        validate(story,binding)
        if data.get('frame_context')!=binding or source(story,data)!=binding['narration']:
            raise ValueError('При правке времени изменена смысловая привязка кадра.')
        binding['timing']=timing(data.get('voice_timing'))
        binding['content_hash']=digest({k:v for k,v in binding.items() if k!='content_hash'})
        data['frame_context']=copy.deepcopy(binding)
        data['frame_focus']=display(binding,entry)
    from .narrative_frame_focus import fingerprint
    document['content_hash']=fingerprint({k:v for k,v in document.items() if k!='content_hash'})
    packet=plan['focus'];packet['parent_content_hash']=document['content_hash']
    packet['content_hash']=fingerprint({k:v for k,v in packet.items() if k!='content_hash'})

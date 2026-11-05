"""Separate a requested human composition from fallible detector observations.

YOLO boxes and marker contours are not typed living participants: a mannequin,
statue, picture, armour stand or duplicate segment may produce the same signals.
Two raw counts can share this error, so agreement does not confirm a mismatch.
No automatic retry may be derived from these counts alone.
"""

VERSION = 'population-evidence/1'


def record_population_review(snapshot, counts, *, method, errors=()):
    expected = int(snapshot.get('expected_people', 0))
    exact = not snapshot.get('people_count_unresolved')
    values = {key: value if type(value) is int and value >= 0 else None
              for key, value in counts.items()}
    faces, people, hands = (values.get(key) for key in ('faces', 'people', 'hands'))
    excess = exact and any(value is not None and value > expected
                           for value in (faces, people))
    excess_hands = exact and hands is not None and hands > expected * 2
    missing = (exact and expected > 0 and faces is not None and people is not None
               and max(faces, people) < expected)
    flags = [name for name, value in (('possible_extra_people', excess),
             ('possible_extra_hands', excess_hands), ('people_not_confirmed', missing)) if value]
    if flags:
        status = 'needs_review'
        reason = ('Проверьте состав кадра: детектор отметил возможное расхождение. '
                  'Предметы, изображения людей и перекрытия могут давать ложный подсчёт; кадр сохранён.')
    elif errors:
        status = 'unverified'
        reason = 'Автоподсчёт людей недоступен. Кадр сохранён; проверьте состав визуально.'
    elif not exact:
        status = 'not_applicable'
        reason = 'Точное число участников не установлено; список эталонов не является общим числом людей.'
    else:
        status = 'not_flagged'
        reason = 'Детекторы не отметили расхождения; смысловая правильность кадра этим не подтверждается.'
    review = {'version': VERSION, 'status': status, 'method': method,
              'enforcement': 'advisory', 'confirmed_mismatch': False,
              'count_contract': 'requested_exact' if exact else 'source_unspecified',
              'requested_people': expected, 'detected': values, 'flags': flags,
              'errors': list(errors), 'reason': reason}
    snapshot['composition_review'] = review
    return review

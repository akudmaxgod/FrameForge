"""Isolated identity data and a narrow, deterministic marker-head check."""
import re
from functools import lru_cache

_ACTION = re.compile(r'\b(?:леч\w*|обслед\w*|принима\w*|осматри\w*|диагност\w*|выявля\w*|назнача\w*|выбира\w*|различа\w*|treat\w*|examin\w*|diagnos\w*|listens?\s+to)\b', re.I)
_VISUAL = re.compile(r'опух|от[её]к|асимметр|блед|истощ|худоб|сыпь|пятн|парали|хром|ампут|swelling|swollen|pallor|rash|wasting|limp|amputat|волос|глаз|очк|ус[ыао]|бород|одежд|жилет|пальто|халат|плать|рубаш|костюм|лыс|шрам|hair|eyes?|glasses|beard|coat|shirt|vest|dress|bald|scar', re.I)

def identity_description(description):
    """Exclude the character's interactions and medical narrative from a solo portrait."""
    clauses = re.split(r'[.!?;]', str(description or ''))
    kept = []
    for index, clause in enumerate(clauses):
        clause = _ACTION.split(clause, maxsplit=1)[0].strip(' ,:—-')
        if clause and (index == 0 or _VISUAL.search(clause)):
            kept.append(clause)
    return '. '.join(kept) or 'One original fictional person'

@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except (ImportError, OSError):
        return None

def name_gender_hint(name):
    """Only unambiguous singular given-name parses; never a suffix/surname guess."""
    morph = _morph()
    if morph is None:
        return None
    for word in re.findall(r'[А-ЯЁа-яё]+', str(name or '')):
        genders = {p.tag.gender for p in morph.parse(word)
                   if 'Name' in p.tag and 'sing' in p.tag and 'nomn' in p.tag and p.tag.gender in ('masc','femn')}
        if len(genders) == 1:
            value = 'male' if genders == {'masc'} else 'female'
            return {'value': value, 'label': 'man' if value == 'male' else 'woman', 'source': 'name_hint'}
        if genders:
            return None
    return None

def subject_gender(description, name):
    from .domain import gender_constraint
    identity = identity_description(description)
    explicit = gender_constraint(identity,name='')
    if explicit:
        return {**explicit,'source':'description'}
    if re.match(r'\s*пациентк\w*\b',str(description or ''),re.I):
        return {'value':'female','label':'woman','source':'description'}
    # A participle directly after the subject's first comma belongs to that subject,
    # unlike gender words elsewhere in the medical interaction.
    clauses = str(description or '').split('.',1)[0].split(',',1)
    if len(clauses) == 2:
        word = re.match(r'\s*([а-яё]+)\b',clauses[1],re.I)
        if word:
            word = word.group(1).casefold()
            if word.endswith(('ющая','щая','вшая')):
                return {'value':'female','label':'woman','source':'description_hint'}
            if word.endswith(('ющий','щий','вший')):
                return {'value':'male','label':'man','source':'description_hint'}
    return name_gender_hint(name)

def marker_head_count(image):
    """Count enclosed light oval faces with dark interior marks; not a gender/identity detector."""
    import cv2
    import numpy as np
    rgb = np.asarray(image.convert('RGB'))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = np.where(np.min(rgb, axis=2) > 205, 255, 0).astype('uint8')
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    count = 0
    if hierarchy is None:
        return count
    area_total = image.width * image.height
    for i, contour in enumerate(contours):
        if hierarchy[0][i][3] != -1:
            continue
        area = cv2.contourArea(contour)
        x,y,w,h = cv2.boundingRect(contour)
        if not .0015 * area_total < area < .32 * area_total or not .60 < w / max(h,1) < 1.5:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0 or 4 * np.pi * area / perimeter**2 < .42:
            continue
        # Face markings must be inside this light component, not just its border.
        child = hierarchy[0][i][2]
        marks = 0
        while child != -1:
            a = cv2.contourArea(contours[child])
            if 2 < a < area * .18:
                marks += 1
            child = hierarchy[0][child][0]
        if marks >= 2:
            count += 1
    return count

def validate_portrait(image, snapshot, *, confirmed=False):
    from .domain import DomainError
    from .visual_style import is_marker_style
    if not is_marker_style(snapshot.get('style')) or not snapshot.get('character_requirements',{}).get('one_person',True):
        return None
    count = marker_head_count(image)
    if count > 1:
        # Light contour components are not calibrated person detections: eyes,
        # hands and clothing may create additional head-like shapes. Preserve
        # the paid result for review rather than asserting a semantic fact.
        return {'status':'manual' if confirmed else 'unverified',
                'one_person':True if confirmed else None,
                'candidate_head_contours':count, 'method':'marker_head_contours',
                'identity_status':'manual_review',
                'reason':'Найдено несколько похожих на головы контуров. Это может быть ошибкой проверки. Убедитесь, что изображён один персонаж; при нескольких персонажах не фиксируйте этот вариант.'}

    if count == 0 and not confirmed:
        return {'status':'unverified', 'one_person':None,
                'reason':'Проверьте, что на портрете ровно один персонаж, перед фиксацией.'}
    return {'status':'manual' if confirmed and count == 0 else 'count_checked', 'one_person':True,
            'identity_status':'manual_review', 'method':'marker_head_contours'}

def marker_scene_review(snapshot, image):
    """ArcFace has no calibrated identity meaning for white-head marker drawings."""
    from .visual_style import is_marker_style
    from .domain import DomainError
    if not is_marker_style(snapshot.get('style')):
        return False
    count = marker_head_count(image)
    expected = int(snapshot.get('expected_people',0))
    if count > expected and (expected or snapshot.get('uninhabited')):
        raise DomainError('Количество нарисованных персонажей превышает состав сцены. Кадр не принят.',422)
    snapshot['identity_review'] = {'status':'unverified', 'method':'manual_visual_review',
        'reason':'Для рисованного стиля проверьте сходство персонажей с эталонами визуально.'}
    return True

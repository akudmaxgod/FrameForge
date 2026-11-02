"""Resolve inhabited history versus present-day ruins from the selected narration."""
import re

MODERN = re.compile(r'сегодня|современн\w*\s+(?:руин\w*|пейзаж\w*|Ирана|назван\w*|исследовател\w*)|'
                    r'раскоп\w*|археолог\w*|сохранил\w*|'
                    r'низк\w*\s+стен\w*|опустел\w*|заброш\w*|забытый\s+город|'
                    r'оставлен\w*\s+построй\w*|след\w*\s+огня|ЮНЕСКО', re.I)
HISTORY = re.compile(r'поселени\w*\s+возник\w*|развивал\w*\s+(?:крупн\w*\s+)?город\w*|'
                     r'город\w*\s+развивал\w*|основавш\w*\s+город\w*|'
                     r'(?:городск\w*|повседневн\w*)\s+жизн\w*|'
                     r'для\s+жител\w*|жител\w*[^.!?]{0,80}(?:готов\w*|работ\w*|жил\w*)|'
                     r'дома\s+строил\w*|(?:создавал\w*|переделывал\w*)[^.!?]{0,50}построй\w*|'
                     r'дом\s+приходится\s+поддерживать|выращивал\w*\s+злак\w*|'
                     r'держал\w*\s+овец|ловил\w*\s+рыбу|добывал\w*\s+водоплавающ\w*|'
                     r'жизнь\s+опиралась|(?:представить|представьте)\s+(?:город|два\s+поселения)|'
                     r'город\s+состоит\s+из\s+людей|в\s+древнем\s+городе[^.!?]{0,100}повседневн\w*', re.I)
RECONSTRUCTION = re.compile(r'мысленно\s+убрать[^.!?]{0,100}(?:лет|тысяч)|'
                            r'стен\w*\s+снова\s+появляет\w*\s+высота|'
                            r'маршрут\s+человека[^.!?]{0,100}свой\s+дом', re.I)
MODERN_ACTION = re.compile(r'(археолог\w*|исследовател\w*|посетител\w*)[^.!?]{0,90}'
                          r'(?:расчища\w*|вынима\w*|осматрива\w*|работа\w*|изуча\w*|ид[её]т|идут)', re.I)


def city_lifecycle(quote, story='', source_start=None):
    quote = str(quote or '')
    if not isinstance(source_start, int) or story[source_start:source_start + len(quote)] != quote:
        source_start = story.find(quote) if quote else -1
    prefix = ''
    if source_start >= 0:
        boundary = story.rfind('\n\n', 0, source_start)
        paragraph_start = boundary + 2 if boundary >= 0 else 0
        prefix = story[paragraph_start:source_start]
    # Selected explicit historical activity outranks a preceding excavation;
    # selected explicit ruins outrank an earlier inhabited-city paragraph.
    if RECONSTRUCTION.search(quote) or HISTORY.search(quote):
        return 'historical_reconstruction'
    if MODERN.search(quote):
        return 'modern_ruins'
    if RECONSTRUCTION.search(prefix):
        return 'historical_reconstruction'
    if MODERN.search(prefix):
        return 'modern_ruins'
    if HISTORY.search(prefix):
        return 'historical_reconstruction'
    return None


def apply_city_lifecycle(treatment, quote, story='', source_start=None):
    if treatment.get('source_binding') == 'narrated_event':
        return treatment
    if treatment.get('treatment') == 'source_backed_historical_fieldwork':
        return {**treatment, 'city_population':'source_people'}
    if treatment.get('source_binding', 'context') not in ('architecture', 'context'):
        return treatment
    layer = city_lifecycle(quote, story, source_start)
    if not layer and str(treatment.get('treatment', '')).startswith('source_backed_historical_'):
        layer = 'historical_reconstruction'
    if not layer:
        return treatment
    detail = treatment.get('shot_type') == 'prop_detail'
    if layer == 'historical_reconstruction':
        direction = (treatment['direction'] if detail else
            'Historical reconstruction of the inhabited ancient city. Show complete, maintained mud-brick homes '
            'with roofs and doorways, usable streets and courtyards. Anonymous period-appropriate residents '
            'carry out ordinary daily activities in the selected setting: walking, cooking, storing supplies or '
            'working when relevant to the narration. Natural presence, no exact headcount; the city remains '
            'the main subject. No excavation tools, modern exhibit labels, eroded ruins or museum model.')
        if not detail:
            activity = quote.casefold()
            if re.search(r'переул\w*|улиц\w*|маршрут\w*', activity):
                direction += ' Eye-level street view along the narrated inhabited lane or route, with doorways and a clear path through connected spaces.'
            elif re.search(r'двор\w*|помещени\w*|готовят|хранят', activity):
                direction += ' Human-height courtyard view connecting the source-described domestic rooms and activities.'
            if re.search(r'рыб\w*|водоплавающ\w*|озер\w*', activity):
                direction = ('Historical reconstruction of the ancient city’s lakeshore economy. Show the source-described '
                             'water environment with anonymous residents fishing or using lakeshore resources, only as '
                             'described. Do not replace this activity with dry ruins. Natural presence, no exact headcount.')
            elif re.search(r'злак\w*|пол[ея]\w*|зерн\w*|урожай|овец|коз|стад\w*', activity):
                direction = ('Historical reconstruction of everyday farming and animal husbandry around the inhabited '
                             'ancient settlement. Show anonymous residents and only the crops, livestock or supplies '
                             'described in the narration. No modern excavation. Natural presence, no exact headcount.')
            elif re.search(r'строил\w*|формовал\w*|поддерживат\w*|создавал\w*|переделывал\w*', activity):
                direction += ' Show the narrated building, brick-making or maintenance work in progress, rather than only a finished city panorama.'
        population = 'detail' if detail else 'inhabited'
    else:
        direction = (treatment['direction'] if detail else
            'Present-day documentary view of the abandoned archaeological site. Show eroded low mud-brick '
            'walls, exposed foundations and the source-described layout or traces. No ancient residents, '
            'reconstructed inhabited houses or staged ancient daily life. No museum model or invented exhibit.')
        population = 'uninhabited'
        if not detail and MODERN_ACTION.search(quote):
            population = 'source_people'
            direction += ' Include only the modern participants and activity explicitly described in the selected narration.'
    return {**treatment, 'visual_time_layer': layer, 'city_population': population,
            'treatment': 'source_backed_city_lifecycle', 'direction': direction,
            'shot_type': treatment['shot_type'] if detail else 'establishing'}

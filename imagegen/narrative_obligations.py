"""Narrated actors/actions are obligations, not optional background evidence.

Deterministic guards cover explicit source counts and common physical actions;
the independent semantic critic checks the rest, including translations.
Never repair a conflict by changing the source or deleting its participants.
"""
import re

LEGEND = re.compile(r'\b(?:предани\w*|легенд\w*|миф\w*|по\s+поверь\w*)\b', re.I)
PHYSICAL_ACTION = re.compile(r'\b(?:перемещ\w*|перенос\w*|подним\w*|строил\w*|строят|строит|'
    r'постро\w*|перестраив\w*|создавал\w*|выруб\w*|несли|н[её]с|несла|нес[её]т|несут|принос\w*|'
    r'соверша\w*|проводил\w*|проводят|исполня\w*|готовил\w*|готовят|готовит|вош\w*|вход\w*|'
    r'приш\w*|приход\w*|подош\w*|подход\w*|плыл\w*|плыв\w*|сраж\w*|'
    r'возглав\w*|составля\w*|изучал\w*|изучают|взял\w*|сопостав\w*|'
    r'расчищ\w*|осмотрел\w*|осматри\w*|ремонтир\w*|обрабатыв\w*|сажал\w*|'
    r'собирал\w*|ловил\w*|летел\w*|летели|летал\w*|парил\w*)\b', re.I)
ACTION_PAIRS = (
    (r'перемещ\w*|перенос\w*|подним\w*', r'mov\w*|transport\w*|carry\w*|carrying|lift\w*|levitat\w*|перемещ\w*|перенос\w*|подним\w*'),
    (r'обряд\w*|ритуал\w*', r'ritual\w*|ceremon\w*|rite\w*|обряд\w*|ритуал\w*'),
    (r'летал\w*|летел\w*|летели|парил\w*', r'fly\w*|flying|float\w*|levitat\w*|airborne|лет\w*|пар\w*'),
)
EXTRA_ACTIONS = (
    (r'measur\w*|измер\w*',r'измер\w*'),
    (r'tak\w* notes|making notes|record\w* notes|дела\w*\s+замет\w*|записыва\w*',r'запис\w*|замет\w*'),
    (r'sacrific\w*|жертвопринош\w*|принос\w*\s+в\s+жертв\w*',r'жертв\w*'),
    (r'kneel\w*|преклон\w*\s+колен\w*|вста\w*\s+на\s+колен\w*',r'колен\w*'),
    (r'attack\w*|напада\w*|атаку\w*',r'напад\w*|атак\w*|битв\w*|сраж\w*'),
)
ROLE_ALIASES = {'брат':r'brother\w*', 'жрец':r'priest\w*', 'монах':r'monk\w*',
    'паломник':r'pilgrim\w*', 'воин':r'warrior\w*|soldier\w*',
    'исследователь':r'researcher\w*|scientist\w*', 'археолог':r'archaeologist\w*',
    'строитель':r'builder\w*|construction worker\w*', 'капитан':r'captain\w*'}
NUMBERS = {'один':1,'одна':1,'одного':1,'два':2,'двое':2,'двух':2,'две':2,
           'оба':2,'обоих':2,'три':3,'трое':3,'трёх':3,'трех':3,'четыре':4,
           'четверо':4,'пять':5,'пятеро':5,'шесть':6,'шестеро':6}


def legend_context(context):
    source = context.get('source') or {}
    if LEGEND.search(source.get('quote','')):return True
    time = context.get('time') or {}
    if time.get('layer') in ('modern_ruins','documented_event') and time.get('evidence',{}).get('start',-1) >= source.get('start',0):
        return False
    return bool(LEGEND.search(context.get('scope',{}).get('quote','')))


def counted_roles(quote, nlp):
    slots = []
    for match in re.finditer(r'\b('+'|'.join(NUMBERS)+r'|[1-6])\s+([А-Яа-яЁё-]+)\b',quote,re.I):
        noun = nlp.analyze_token_morph(match[2]).lemma
        if noun in ROLE_ALIASES:
            count = NUMBERS.get(match[1].casefold()) or int(match[1])
            slots.extend([noun]*count)
    return slots


def obligations(story, source):
    from .forgotten_cities.event_context import for_source
    context = for_source(story, source['quote'], source['start']) or {}
    actors = context.get('participants') or []
    quote = source['quote']
    from .narrative_continuity import selected_event_source, protagonist_action
    parent = selected_event_source(story,source)
    protagonist = protagonist_action(parent['quote'])
    # A commentator answering/claiming a version is an attribution, not an
    # actor in the embedded physical event. Keep physical actions below.
    # Only an actual selected physical action establishes mandatory presence.
    # Attribution/founding summaries, carved faces and abstract commentary
    # require semantic review, not manufactured living participants.
    # Require an actual action attached to this occurrence, not a participle
    # or another clause's verb anywhere in the whole selected quote.
    physical_actors = [p for p in actors if any(a.get('asserted',True) and a['end'] <= source['end'] and PHYSICAL_ACTION.fullmatch(a['quote'])
        for a in p.get('action_evidence',[]))]
    if not any(not re.search(r'\bне\s*$', quote[:m.start()], re.I)
               for m in PHYSICAL_ACTION.finditer(quote)):
        physical_actors = []
    if protagonist:
        physical_actors += [p for p in actors if p.get('source_role')=='narrated_protagonist' and p not in physical_actors]
    # A legend often contains unknown names that morphology cannot classify.
    # The explicit kinship/count remains source evidence independently of NER.
    brothers = bool(re.search(r'\b(?:два|двое|двух|оба|обоих)\s+брат\w*', quote, re.I))
    from .nlp import RussianNLPAdapter
    nlp = RussianNLPAdapter.get_instance()
    names = []
    # Names are checked in the role slots (requested in Russian), independent
    # of the English visual description. Capitalized common roles are nouns.
    parsed = nlp.parse_document(quote)
    for p in physical_actors:
        if any(t.pos == 'PROPN' and t.text == p['name'] for t in parsed.tokens):
            names.append(p['name'])
    counted = counted_roles(quote,nlp) if physical_actors or brothers else []
    return {'participants': physical_actors, 'required_names': names,
            'required_people': bool(physical_actors) or brothers,
            'exact_people': len(counted) if counted else 2 if brothers else None,
            'legend': bool(LEGEND.search(quote)) or legend_context(context),
            'source_quote': quote,'parent_source':parent,'protagonist_action':protagonist,
            'needs_semantic_review':bool(context.get('entity_review_reasons')) or context.get('needs_review',False),
            'review_reasons':context.get('review_reasons',[])+context.get('entity_review_reasons',[])}


def participant_slots(context):
    """Explicit source cardinality outranks one aggregate NLP role."""
    actors = list(dict.fromkeys(' '.join(filter(None,(p.get('role_name'),p['name'])))
                               for p in context.get('participants', [])))
    quote = context['source']['quote']
    if re.search(r'\b(?:два|двое|двух|оба|обоих)\s+брат\w*', quote, re.I):
        names = re.search(r'брат\w*\s+([А-ЯЁ][а-яё-]+)\s+и\s+([А-ЯЁ][а-яё-]+)', quote)
        return ['брат '+names[1], 'брат '+names[2]] if names else ['первый брат', 'второй брат']
    from .nlp import RussianNLPAdapter
    slots = counted_roles(quote,RussianNLPAdapter.get_instance())
    if slots:return slots
    return actors


def errors(plan, obligation):
    from .narrative_fidelity import positive_description
    failures = []
    count = plan.get('expected_people', 0)
    visual = positive_description(str(plan.get('visual_direction', '')))
    roles = ' '.join(plan.get('roles') or [])
    if obligation.get('required_names'):
        from .nlp import RussianNLPAdapter
        nlp = RussianNLPAdapter.get_instance()
        role_words = {nlp.analyze_token_morph(w).lemma for w in re.findall(r'[А-Яа-яЁё-]+',roles)}
        for name in obligation['required_names']:
            if nlp.analyze_token_morph(name).lemma not in role_words:
                failures.append('Сохраните названного участника в roles: '+name+'. Его нельзя заменить чужим анонимным работником.')
    if obligation['required_people'] and not count:
        failures.append('Сценарий описывает действующих участников. Сохраните людей и их действие; пустая архитектура не заменяет событие.')
    if obligation['exact_people'] is not None:
        if count != obligation['exact_people']:
            failures.append('В сценарии два брата: требуется ровно два участника, а не стена или другая группа.' if re.search(r'брат\w*',obligation['source_quote'],re.I) else 'Сохраните точное число описанных участников: '+str(obligation['exact_people'])+'.')
        if re.search(r'брат\w*',obligation['source_quote'],re.I) and not re.search(r'brother\w*|брат\w*', roles + ' ' + visual, re.I):
            failures.append('Сохраните роли двух братьев; не подменяйте их анонимными рабочими другого события.')
    if obligation['required_people']:
        from .nlp import RussianNLPAdapter
        nlp = RussianNLPAdapter.get_instance()
        role_words = {nlp.analyze_token_morph(w).lemma for w in re.findall(r'[А-Яа-яЁё-]+',roles)}
        for participant in obligation['participants']:
            lemma = nlp.analyze_token_morph(participant.get('role_name') or participant['name']).lemma
            if lemma in ROLE_ALIASES and lemma not in role_words and not re.search(ROLE_ALIASES[lemma],roles,re.I):
                failures.append('Сохраните роль участника описанного действия: '+participant['name']+'.')
        if plan.get('subject_state') in ('artifact', 'buried_remains'):
            failures.append('Действующие участники требуют категории living/context; не удаляйте их ради категории предмета.')
        for source_action, visible_action in ACTION_PAIRS:
            stated = any(not re.search(r'\bне\s*$',obligation['source_quote'][:m.start()],re.I)
                         for m in re.finditer(source_action,obligation['source_quote'],re.I))
            if stated and not re.search(visible_action, visual, re.I):
                failures.append('Визуальное описание потеряло описанное действие: ' + obligation['source_quote'])
        for visible_action, source_action in EXTRA_ACTIONS:
            if re.search(visible_action,visual,re.I) and not re.search(source_action,obligation['source_quote'],re.I):
                failures.append('Добавлено действие, которого нет в выбранной озвучке. Сохраните только описанное действие: '+obligation['source_quote'])
        if obligation.get('protagonist_action'):
            event_quote=obligation['parent_source']['quote']
            for source_action, visible_action in (
                (r'\bпривяз\w*',r'\b(?:tie\w*|tying|bind\w*|fasten\w*|attach\w*|привяз\w*|закреп\w*)'),
                (r'\bснима\w*',r'\b(?:remov\w*|dismantl\w*|detach\w*|снима\w*|демонтир\w*)'),
                (r'\bкаса\w*\s+ваш\w*\s+рук\w*',r'\b(?:touch\w*|contact\w*|каса\w*|соприкаса\w*)')):
                if re.search(source_action,event_quote,re.I) and not re.search(visible_action,visual,re.I):
                    failures.append('Главный герой присутствует, но потеряно его действие. Покажите действие выбранного события: '+event_quote)
    if obligation['legend']:
        if plan.get('evidence_level') != 'illustrative_hypothesis':
            failures.append('Предание должно оставаться явно обозначенной иллюстрацией: illustrative_hypothesis.')
        if obligation['required_people'] and plan.get('time_layer') != 'historical_reconstruction':
            failures.append('Покажите событие предания как иллюстрацию, не заменяйте его современными руинами или публикациями.')
    if failures:
        failures = [f+ ' Исходная цитата: «'+obligation['source_quote']+'»' for f in failures]
    return failures


ILLUSTRATION_RULE = ('SOURCE LEGEND ILLUSTRATION, not an established historical event. '
    'Retain the narrated actors, their exact stated number, roles and action, including a '
    'source-described supernatural action. Their artistic appearance is conventional, '
    'not an authenticated portrait. Add no unmentioned magic, props or events. ')

"""Historical subject facts do not automatically date people studying that subject."""
import re
VERSION='observation-scope/2'
RESEARCH=re.compile(r'исследовател\w*|археолог\w*|уч[её]ны\w*|\b(?:researchers?|archaeologists?|scientists?)\b',re.I)
RESEARCH_ACTION=re.compile(r'examin\w*|analy[sz]\w*|measur\w*|study\w*|сравнив\w*|изуч\w*|анализир\w*',re.I)
NARRATION=re.compile(r'исследован\w*|лаборатор\w*|изотоп\w*|датиров\w*|\b(?:study|studies|analysis|laboratory|isotop\w*)\b',re.I)
UNKNOWN=re.compile(r'не\s*(?:уточн|указ|определ|извест)|неизвест|unspecified|unknown|not specified',re.I)
RULES='''Distinguish the original historical subject from the act of observing or studying it.
When the IMAGE depicts research, comparison, excavation or measurement, the old
date/age of the city, specimen or remains does not date that act or its researchers.
The origin of specimens does not prove the physical location of their study.
A publication year is not automatically the date of fieldwork or measurement.
Use an unknown period/place if the actual research episode is not dated/located.
If source explicitly dates historical research itself, retain that date; do not
force all researchers to be modern. A neutral view of the historical subject may
retain its own historical period. Preserve participants, specimens and action.
The review's time explanation and citations must describe the research episode
too. Do not certify an unknown research date by citing the old city era. When
time is unknown, say that the research date is unspecified and cite the research
sentence, rather than claiming the researchers lived in the object's era.'''


def applies(quote):return bool(RESEARCH.search(quote) or NARRATION.search(quote))


def era_terms(text):
    text=re.sub(r'\b(?:before\s+|pre[- ]?|(?:задолго\s+)?до\s*)(?:columb\w*|колумб\w*)',' era_precolumbian ',text,flags=re.I)
    text=re.sub(r'\b(?:after\s+|после\s+)(?:columb\w*|колумб\w*)',' era_postcolumbian ',text,flags=re.I)
    words=re.findall(r'\w+',text.lower())
    return {w for w in words if w.startswith('era_') or re.fullmatch(r'\d{1,4}|[ivxlcdm]+',w)
            or re.fullmatch(r'ancient|medieval|prehistoric|древн\w*|средневек\w*|доисторическ\w*',w)}


def validate(value,item):
    if not item.get('observation_policy'):return
    visual=value['visual_direction']
    researching=(RESEARCH.search(visual) or RESEARCH.search(value['subject'])
                 or RESEARCH_ACTION.search(value['action']))
    if not researching:return
    validate_attribution(value,item)
    backgrounds=[f for f in item.get('setting_facts',[]) if f.get('kind')=='era' and f.get('scope')=='historical'
                 and isinstance(f.get('evidence'),dict)]
    period=era_terms(value['period']) if not UNKNOWN.search(value['period']) else set()
    image_terms=era_terms(visual)
    for fact in backgrounds:
        terms=era_terms(fact.get('value',''))
        inherited=(period & terms) | (image_terms & terms)
        if not inherited:continue
        background=fact['evidence']
        proof=[e for e in value['evidence'] if not (background['start']<=e['start']<e['end']<=background['end'])]
        if any((RESEARCH.search(e['quote']) or RESEARCH_ACTION.search(e['quote']))
               and inherited<=era_terms(e['quote']) for e in proof):continue
        message=('Эпоха исторического объекта не датирует исследователей. Нужна отдельная цитата о времени '
                 'самого исследования; иначе укажите неизвестный период и уберите древнюю датировку '
                 'из visual_direction. Сохраните исследователей, образцы и действие; не переносите место '
                 'происхождения образца в место работы исследователей без доказательства.')
        from .source_review_status import issue
        error=ValueError(message)
        error.source_issues=[issue(item,'observation_time_scope','semantic',message,
            evidence=[item['narration'],background],fields=['period','time_layer','visual_direction'])]
        raise error


def validate_attribution(value,item):
    """Explicit specimen origin/publication dates do not locate/date study."""
    from .narrative_frame_focus import _forms
    from .frame_source_guards import fail
    proof=value.get('evidence',[])
    def forms(text):
        return set().union(*(_forms(w) for w in re.findall(r'\w+',text))) if text else set()
    period=forms(value['period'])
    if not UNKNOWN.search(value['period']):
        for e in proof:
            for publication in re.finditer(r'(?:опубликован\w*|published)\s+[^,.;]{0,120}?(?:\bгод\w*\b|\b\d{4}\b)',e['quote'],re.I):
                date=forms(publication.group())-{'опубликовать','published','в','in','год'}
                matches=period & date
                if not date or len(matches)<min(2,len(date)):continue
                separate=False
                for other in proof:
                    text=re.sub(r'(?:опубликован\w*|published)\s+[^,.;]{0,120}?(?:\bгод\w*\b|\b\d{4}\b)','',other['quote'],flags=re.I)
                    if (RESEARCH.search(text) or RESEARCH_ACTION.search(text)) and matches<=forms(text):separate=True
                if not separate:fail(item,'publication_as_research_date','Год публикации не датирует изображённое исследование. Нужна отдельная датировка самой работы; иначе время неизвестно.',('period','visual_direction'))
    if UNKNOWN.search(value['place']):return
    geography=[f for f in item.get('setting_facts',[]) if f.get('kind')=='geography' and isinstance(f.get('evidence'),dict)]
    location=forms(value['place'])-{'место','город','территория','штат','современный','на','в','у','от','недалеко','неподалёку'}
    inherited=set().union(*(location & forms(f['evidence']['quote']) for f in geography)) if geography else set()
    if not inherited:return
    # On-site discovery/excavation has a different physical relation from
    # laboratory comparison. Do not forbid an actually narrated find location.
    if re.search(r'(?:обнаруж\w*|раскоп\w*|нашли).{0,100}(?:неподал[её]ку|возле|рядом|на\s+месте)|(?:неподал[её]ку|возле|рядом).{0,100}(?:обнаруж\w*|раскоп\w*|нашли)',item['narration']['quote'],re.I):return
    for e in proof:
        text=e['quote']
        if not (RESEARCH.search(text) or RESEARCH_ACTION.search(text)):continue
        words=re.findall(r'\w+',text)
        for n,word in enumerate(words):
            if not (_forms(word)&inherited):continue
            previous=[w.casefold() for w in words[max(0,n-2):n]]
            if any(w in ('в','во','на','in','at','near') for w in previous):return
    fail(item,'specimen_origin_as_research_place','География изучаемого объекта не доказывает место работы исследователей. Нужен источник о месте самого исследования; иначе место неизвестно.',('place','visual_direction'))


def validate_review(value,item,focus):
    if not focus or not item.get('observation_policy'):return
    for check in value['checks']:
        if check['category']=='time' and check['verdict']=='supported':
            assertion={**focus,'period':check['reason'],'visual_direction':check['reason'],
                       'evidence':check['evidence']}
            validate(assertion,item)

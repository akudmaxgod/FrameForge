"""Source-first scientific staging with typed visual relations and pre-generation review."""
import copy
import hashlib
import json
import re
from dataclasses import asdict
from typing import Literal
from pydantic import Field
from .script_analysis.schemas import StrictModel
from .script_analysis.cache import DiskCache, atomic_json
from .script_analysis.client import JsonClient
from .script_analysis.stats import Stats
from .script_analysis.errors import AnalysisError

PLANNER_VERSION = 'science-staging/3'
BATCH_SIZE = 16

class Asset(StrictModel):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    subject: str = Field(min_length=12, max_length=1400)
    identity: str = Field(min_length=2, max_length=120)
    view: str = Field(min_length=2, max_length=160)
    state: str = Field(min_length=2, max_length=200)
    people: bool = False

class Node(StrictModel):
    label: str = Field(min_length=1, max_length=90)
    cue_quote: str = Field(min_length=1, max_length=200)

class Edge(StrictModel):
    source: int
    target: int
    evidence_quote: str = Field(min_length=1, max_length=700)

class Element(StrictModel):
    kind: Literal['text', 'cross', 'dot', 'line', 'arrow', 'box']
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(default=0.0, ge=-1, le=1)
    h: float = Field(default=0.0, ge=-1, le=1)
    text: str = Field(default='', max_length=120)
    cue_quote: str = Field(default='', max_length=200)
    color: str = Field(default='',pattern=r'^(|#[0-9a-fA-F]{6})$',description='Цвет локальной фигуры/текста, например #C34E45 для красного образца. Только качественная иллюстрация, не измерение яркости.')
    fill: bool = False

class Annotation(StrictModel):
    label: str = Field(default='',max_length=70)
    x: float = Field(ge=.05,le=.95)
    y: float = Field(ge=.05,le=.95)
    cue_quote: str = Field(min_length=1,max_length=200)

class ExplanatoryScheme(StrictModel):
    arrows: bool = False
    kind: Literal['layers','signal','measurement','network']
    labels: list[str] = Field(min_length=2,max_length=3)
    cue_quote: str = Field(min_length=1,max_length=1200)

class RevealStep(StrictModel):
    kind: Literal['part','focus','caption'] = 'caption'
    cue_quote: str = Field(min_length=3,max_length=180)
    title: str = Field(min_length=1,max_length=90)
    explanation: str = Field(default='',max_length=180)
    region_key: str = Field(default='',max_length=90)

class Shot(StrictModel):
    n: int
    reveal_steps: list[RevealStep] = Field(default_factory=list,max_length=4)
    layout: Literal['hero_caption','image_left','image_right','comparison','process','instruction','factors','text_card','local_board']
    function: Literal['structure','mechanism','factors','comparison','instruction','analogy','observation','summary','transition']
    representation: Literal['literal','schematic','analogy']
    focus: str = Field(min_length=4, max_length=350)
    support_quote: str = Field(min_length=1, max_length=1200)
    headline: str = Field(min_length=1, max_length=180)
    caption: str = Field(default='', max_length=350)
    asset_keys: list[str] = Field(default_factory=list, max_length=2)
    support_asset_key: str = Field(default='',max_length=100)
    support_cue_quote: str = Field(default='',max_length=200)
    text_animation: Literal['none','typewriter'] = 'typewriter'
    labels: list[str] = Field(default_factory=list, max_length=4)
    presentation_motion: Literal['none','slide_explain','staged_explain','focus_explain','detail_explain','panorama_explain'] = 'none'
    slide_direction: Literal['left','right'] = 'left'
    frame_density: Literal['legacy','filled','single_subject'] = Field(default='legacy',description='Новые сцены: filled по умолчанию. single_subject только при необходимости по смыслу. legacy — совместимость старых планов.')
    single_subject_reason: str = Field(default='',max_length=350)
    nodes: list[Node] = Field(default_factory=list, max_length=6)
    relation: Literal['none','sequence','parallel'] = 'none'
    edges: list[Edge] = Field(default_factory=list, max_length=6)
    elements: list[Element] = Field(default_factory=list, max_length=20)
    cast: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list,max_length=5)
    explanatory_scheme: ExplanatoryScheme | None = None

class Plan(StrictModel):
    assets: list[Asset]
    shots: list[Shot]

class Issue(StrictModel):
    n: int
    code: Literal['meaning','wrong_subject','relation','comparison','instruction','unsupported_claim','unusable_asset','layout','cue']
    reason: str = Field(min_length=1, max_length=700)
    correction: str = Field(min_length=1, max_length=900)

class Review(StrictModel):
    issues: list[Issue]

class Decision(StrictModel):
    issue_index: int
    upheld: bool
    reason: str = Field(min_length=1,max_length=900)

class Adjudication(StrictModel):
    decisions: list[Decision]

PROMPT = '''Ты режиссёр образовательного научпопа. Это постановка ДО генерации, качество смысла важнее числа картинок.
Верни JSON assets и shots строго по схеме. Каждый входной n один раз и в том же порядке.
Полный story задаёт контекст, narration — точный смысл текущего кадра, соседние кадры не надо пересказывать.
Начни с focus: ЧТО конкретно зритель должен увидеть и понять. support_quote — точная непустая цитата текущей narration, подтверждающая этот тезис.
Не добавляй факты и не меняй их модальность: «может», «одна из причин», «часть», отрицание, оговорки и единицы сохраняются. Заголовок до 9 слов, caption до 22 слов, каждый — законченный тезис.
Структура → конкретный видимый объект; механизм → видимые части и изменение; factors → независимые факторы; process → только явно описанный маршрут/порядок.
relation=sequence требует nodes и edges, у каждого ребра evidence_quote ТОЧНО из текущей narration, подтверждающее связь/порядок. Независимые механизмы и перечни не соединять стрелками: factors, relation=parallel, edges=[].
Все nodes.cue_quote должны буквально встречаться в текущей narration, по порядку. Не бери цитату из соседнего кадра. Для обзорной статичной схемы используй elements без временных опор.
comparison: две РАЗНЫЕ asset_keys, две labels. У каждой иллюстрации явное состояние; у сопоставимых состояний одного объекта одинаковые identity и view, но РАЗНЫЕ state и subject. Нельзя дважды вставить одну картинку или дважды картинку, внутри которой уже нарисовано сравнение. Несвязанные альтернативы вопроса лучше factors/local_board.
assets — только отдельные КОНКРЕТНЫЕ видимые иллюстрации. Для каждого identity (объект), view (проекция/масштаб) и state (состояние) обязательны. subject описывает видимые части и действие, а не абстрактный тезис и не компоновку всего кадра.
Нельзя иллюстрировать фоторецепторы внешним глазом, жир костями или обычное смотрение позой специального опыта. Переиспользование допустимо только при совпадении нужных видимых структур, проекции и состояния. Сохраняй известные assets без переопределения. Один базовый объект можно показывать много кадров; не создавай новый asset для каждой фразы/перестановки макета.
Текст, стрелки, графики, тестовые фигуры, отрицания («в голове нет экрана»), призыв подписаться — НЕ assets. Для них text_card, factors или local_board; не проси Flow нарисовать отсутствие вещи. Научные графики без предоставленных данных не придумывай; если нужна качественная схема, прямо подпиши «Условная схема» и используй локальные элементы. Исторический эксперимент не реконструировать как подлинную запись данных.
local_board — локальная доска с elements. instruction может использовать такую же доску без assets. Элементы text/cross/dot/line/arrow/box имеют x,y,w,h в диапазоне 0..1 внутри доски; у line/arrow w,h — положительные приращения к конечной точке. Размеры не выходят за доску. У text значение text обязательно; у остальных text=''. Не имитируй физические сантиметры экрана. Важные отметки и читаемые слова опыта должны реально присутствовать в elements. Без данных/оригинала не изображай график как измеренный.
hero_caption/image_left/image_right: одна иллюстрация, без nodes/edges/elements. text_card: без иллюстраций/стрелок, для завершённого тезиса/перехода. factors: 2–6 nodes, без иллюстраций/стрелок. process: 2–6 nodes с доказанными edges, без иллюстраций. local_board/instruction с elements: без иллюстраций, геометрия задана локально. instruction с иллюстрацией: одна, показывает точную позу/действие.
representation=analogy для метафоры; не выдавай её за устройство объекта. Органы, клетки, приборы не персонажи; cast обычно [], roles — только действительно необходимый видимый демонстратор.
Не чередуй лево/право механически. Выбирай макет по объяснительной задаче, избегай серий одинаковых титров и бессмысленных заголовков. Последние макеты и focus переданы в recent_shots.
'''
VISUAL_FIRST = """
КРИТИЧЕСКОЕ ОБНОВЛЕНИЕ ПОДАЧИ — ПРИОРИТЕТ НАД ПРЕЖНИМИ ПРАВИЛАМИ МАКЕТОВ:
Это высококачественное визуальное научное видео, НЕ презентация. Весь narration уже звучит голосом. Не дублируй его экранным заголовком, абзацем, текстовой карточкой или точками-понятиями.
headline/caption/focus — ТОЛЬКО редакторские метаданные: на экран не выводятся. Кадр обязан что-то ПОКАЗЫВАТЬ: объект, действие, сравнимое состояние или реальную пространственную/физическую связь.
hero_caption/image_left/image_right отображаются как крупная иллюстрация почти на весь кадр; не планируй свободное место под текст. Для структуры/механизма отдавай точную atlas-иллюстрацию с видимыми частями. Смена темы может продолжать связанный иллюстративный план без отдельного титра. Текстовые переходы, выводы и призыв подписаться сопровождай тематической иллюстрацией, а не генерацией букв/логотипов/отсутствующей вещи.
ЗАПРЕЩЕНЫ layout=text_card,process,factors. Слова «перспектива», «память», «внимание», «состав» не превращай в кружки на пустом фоне. Покажи явление: перспектива — предметы в пространстве, память — никакой выдуманной анатомической локализации, лучше тематический человеческий/зрительный план. Невидимый путь допустимо показать подробной анатомической иллюстрацией или local_board с содержательной геометрией.
local_board допустим для реальной объясняющей геометрии и опыта (лучи, проекции, перекрёст, отметки), НЕ для блоков со словами и пересказа озвучки. Если схема только переименовывает фразу, выбери иллюстрацию.
Экранный текст — только короткие обозначения частей, единицы/числа, состояния сравнения, одно действие опыта. Не выводи тезис, вопрос рассказчика, повторенную мысль или абзац. В elements каждый text не длиннее 5 слов, суммарно <=24 слов; instruction допускает короткую команду до 10 слов. Не повторяй подписи в разных местах.
annotations — 0–5 выделений поверх иллюстрации: x/y задают место в целом кадре 0..1, cue_quote — точная фраза текущего narration; label пустой для чистого выделения либо 1–3 слова (название детали, число). Это локальные кольца внимания с коротким появлением по озвучке. Не рисуй полный текст озвучки. Нельзя указывать структуру, которой нет в subject. Чтобы координаты были воспроизводимы, при создании нового asset задай в subject расположение важных частей (слева/центр/справа), но не проси буквы/стрелки в изображении. Точные анатомические подписи допустимы только если часть действительно отчётливо различима; иначе label='', выделение области без ложного указателя.
ПРИМЕР: «лицо → подпись → предмет рядом» — НЕ три кружка с этими словами. Создай одну связную иллюстрацию сцены: лицо слева, пустая вывеска/панель в центре, предмет справа; три annotations последовательно выделяют области. Подпись вывески при необходимости рисуется кодом, а не Flow. Никаких заголовка и поясняющего абзаца.
ПРИМЕР: факторы прозрачности — подробный разрез роговицы, а не точки «состав → клетки → сосуды». Путь сигнала — клетки/нервный путь, а не слова, соединённые линейкой.
Переиспользуй known_assets только когда они действительно показывают нужную мысль. Сохраняй key/subject/view/state полностью, особенно у already_generated_assets: готовые изображения уже существуют. Не создавай их копии ради перестановки макета.
"""
PROMPT += '\nНе повторяй одинаковую композицию при повторном использовании asset: показывай новую структуру или поясняющую схему по текущему focus. Для иллюстрации можно добавить explanatory_scheme: layers — ТРИ вложенных слоя с labels от внешнего к внутреннему; signal — ТРИ стадии свет, рецептор, сеть; measurement — ДВЕ labels название размера и значение с единицей. Это условные предметные схемы рядом с иллюстрацией. Для поясняющих указателей к слоям и направления сигнала можно задать arrows=true. cue_quote — точная опора из narration. Не выдумывай число, слои, направление или причинность; если тип не подходит, оставь null. Поясняющий текст и фигуры рисует код, Flow не получает букв.\n'
PROMPT += '\nВ кадрах с одним общим планом разрешён support_asset_key — ключ ДРУГОЙ поясняющей иллюстрации справа (строение, разрез, скрытый механизм). Это не сравнение состояний и не обрезка общего плана. Добавь соответствующий asset в каталог, если его ещё нет. Пояснение должно опираться на narration; не оставляй кадр пустым, когда скрытый объект или механизм можно предметно показать. Доступно также для observation и переходов, если есть содержательная опора; не заполняй экран декором.\n'
PROMPT += '\nДополнительно explanatory_scheme.kind=network: условная динамическая схема передачи сигнала нейронной сетью, со стрелками направления, ровно две короткие labels; применяй только если исходный текст или его контекст подтверждают нервную обработку. Для коротких пояснений можно задать text_animation=typewriter; не повторяй narration.\n'
PROMPT += VISUAL_FIRST+'\nВ иллюстративных кадрах labels можно использовать для 2–4 коротких пояснений схемы (до 5 слов каждое): точное название структуры, её состояние или краткая причинная связь. Например: «Мышца расслаблена», «Связки натянуты». Это отдельные пояснения к видимому объекту, а не headline/caption, заголовок слайда или пересказ narration. Не добавляй неподтверждённых фактов; не заполняй labels ради текста. При comparison labels по-прежнему обозначают две сравниваемые стороны.'

PROMPT += '\nДля одной основной иллюстрации с содержательными пояснениями и/или explanatory_scheme используй presentation_motion=slide_explain, slide_direction=left: крупный показ, плавное смещение иллюстрации влево, затем пояснения справа. Зеркальный вариант — right. До четырёх labels раскрывают разные полезные детали: часть, состояние, величину, причинную связь из narration. Они должны образовывать логичное объяснение видимого объекта, не повторять друг друга или narration абзацем. Схему добавляй только при подтверждённых отношениях; независимые факторы не превращай в причинную цепь. Если пояснить нечего или иллюстраций две — presentation_motion=none; не заполняй место выдуманным текстом.\n'

REVIEW_PROMPT = '''Ты независимый редактор смысла научпопа ДО генерации. Проверь каждый текущий shot против narration и контекста. Верни issues; если ошибок нет, [].
Проверяй: действительно ли asset показывает нужный объект/масштаб/состояние; не изменён ли факт, отрицание, степень уверенности, причина или единица в headline/caption; не стали ли параллельные факторы процессом; сравнение двух различающихся состояний; выполним ли опыт по elements; нет ли требования букв/стрелок/графика/абстрактного отсутствия к Flow; не выдуманы ли данные исторического опыта; есть ли все точные временные опоры; не потеряна ли мысль в переиспользовании картинки.
Не требуй портрет учёного только за упоминание. Не вводи новых фактов. Отмечай только конкретные проблемы с n и конкретным исправлением. Не требуй разных макетов ради разнообразия, если текущий объясняет мысль. Количество issues не ограничено.
'''

REVIEW_PROMPT += VISUAL_FIRST+'\nВ иллюстративных кадрах labels можно использовать для 2–4 коротких пояснений схемы (до 5 слов каждое): точное название структуры, её состояние или краткая причинная связь. Например: «Мышца расслаблена», «Связки натянуты». Это отдельные пояснения к видимому объекту, а не headline/caption, заголовок слайда или пересказ narration. Не добавляй неподтверждённых фактов; не заполняй labels ради текста. При comparison labels по-прежнему обозначают две сравниваемые стороны.'+'\nПроверь, что визуал дополняет голос: никаких пустых текстовых слайдов и линейных кружков-понятий. headline/caption — скрытые метаданные, не требуй их отрисовать. Не требуй экранного повторения факта только потому, что его нет в подписи. Экранные подписи — короткие обозначения, не повествование.'


DENSITY_RULE = '\nГлавное правило постановки: всегда старайся содержательно наполнить кадр. Каждый НОВЫЙ shot обязан явно задавать frame_density=filled; legacy предназначен только для сохранённых старых планов. Одна маленькая иллюстрация с большими пустыми полями не является законченной сценой. Планируй крупный основной объект и 2–4 разных пояснения, подходящую визуальную схему, сравнение или доступную независимую деталь. Избегай пустых боковых колонок и больших неиспользуемых участков сверху/снизу. Не дублируй картинку, не добавляй абзац диктора и не придумывай факты ради плотности. Одиночный рисунок допустим только при frame_density=single_subject и конкретном single_subject_reason: что именно зритель должен рассмотреть и почему пояснения помешают данной сцене. Это исключение по смыслу, а не общий вариант для всех иллюстраций. Краткое крупное вступление к раскрытию допустимо, держать весь кадр без объяснения по умолчанию нельзя.\n'
SEQUENCE_RULE='\nРежиссируй последовательность, а не одинаковый эффект в каждом кадре. presentation_motion: slide_explain — крупное вступление со смещением для ключевого ввода/механизма; staged_explain — постепенные пояснения без съезда картинки; focus_explain — спокойный крупный план с пояснениями; detail_explain — общий вид и уже запланированная независимая деталь. Чередуй подачу по смыслу и длительности. Не назначай slide_explain всему ролику или соседним сценам подряд. Сравнения и упражнения сохраняют собственные макеты. Полнота кадра не требует постоянного движения всей иллюстрации.\n'
PROMPT += SEQUENCE_RULE
REVIEW_PROMPT += SEQUENCE_RULE
PROMPT += DENSITY_RULE
REVIEW_PROMPT += DENSITY_RULE+'\nПроверь наполнение: если кадр без объясняющей функции пустует, требуй содержательную композицию. Отсутствие данных для подписи требует пересмотра постановки, а не декоративных стрелок. Проверяй обоснование каждой single_subject; не принимай формальное «так требует сцена».\n'

def source_groups(story):
    """Keep sentences, quotation marks and paragraphs intact; join short dependencies."""
    spans=[];start=0;quote_depth=0;double=False
    for i,char in enumerate(story):
        if char=='«':quote_depth+=1
        elif char=='»':quote_depth=max(0,quote_depth-1)
        elif char=='"':double=not double
        boundary=char in '.!?' and not quote_depth and not double and (i+1==len(story) or story[i+1].isspace())
        paragraph=char=='\n' and i+1<len(story) and story[i+1]=='\n'
        if boundary or paragraph:
            a,b=start,i+1
            while a<b and story[a].isspace():a+=1
            while b>a and story[b-1].isspace():b-=1
            if a<b:spans.append([a,b])
            start=i+1
    if story[start:].strip():
        a=start
        while story[a].isspace():a+=1
        spans.append([a,len(story.rstrip())])
    groups=[];i=0
    while i<len(spans):
        a,b=spans[i];i+=1
        while i<len(spans) and len(story[a:b].split())<12 and '\n\n' not in story[b:spans[i][0]] and len(story[a:spans[i][1]].split())<=65:
            b=spans[i][1];i+=1
        # Attach a short trailing clause to its own paragraph rather than make a title fragment.
        if groups and len(story[a:b].split())<7 and '\n\n' not in story[groups[-1][1]:a] and len(story[groups[-1][0]:b].split())<=65:
            groups[-1][1]=b
        else:groups.append([a,b])
    return groups

def normalize_relations(plan,registry=None,frames=None,known=None):
    # A factors layout has no causal semantics. Correct a missing enum, never invent edges.
    for shot in plan.shots:
        # Anonymous demonstration roles are not persistent character identities.
        if registry is not None and shot.roles:
            shot.cast=[cid for cid in shot.cast if cid in registry]
        if not shot.asset_keys:shot.annotations=[]
        narration=next((f['narration'] for f in (frames or []) if f['n']==shot.n),None)
        if narration is not None:
            # Optional decoration must never acquire an invented narration time.
            shot.annotations=[a for a in shot.annotations if a.cue_quote in narration]
            if shot.support_cue_quote and shot.support_cue_quote not in narration:shot.support_cue_quote=''
            if shot.explanatory_scheme:
                from .science_explanatory import valid_scheme
                if not valid_scheme(shot.explanatory_scheme.model_dump(),narration):shot.explanatory_scheme=None
            for element in shot.elements:
                if element.cue_quote and element.cue_quote not in narration:element.cue_quote=''
        if shot.layout=='factors' and not shot.edges:shot.relation='parallel'
        elif shot.layout=='process' and shot.edges:shot.relation='sequence'
    if known:
        # A reference key is immutable. The reviewer sees its actual saved description,
        # and must request a NEW key if a different picture is needed.
        plan.assets=[known.get(asset.key,asset).model_copy(deep=True) for asset in plan.assets]
    from .science_local_repair import repair_contract
    plan=repair_contract(plan,known)
    needed={key for shot in plan.shots for key in shot.asset_keys+([shot.support_asset_key] if shot.support_asset_key else [])}
    plan.assets=[asset for asset in plan.assets if asset.key in needed]
    return plan

def errors(plan, frames, known, registry,*,visual_first=True):
    from .science_local_repair import overlay_request
    found=[];merged=dict(known)
    def bad(n,reason):found.append({'n':n,'reason':reason})
    if [s.n for s in plan.shots]!=[f['n'] for f in frames]:bad(0,'Сохрани все входные n ровно один раз по порядку.')
    if len({a.key for a in plan.assets})!=len(plan.assets):bad(0,'Ключи assets повторяются.')
    for a in plan.assets:
        if a.key in merged and merged[a.key]!=a:bad(0,'Изменён известный asset '+a.key)
        if overlay_request(a.subject):
            for shot in plan.shots:
                if a.key in shot.asset_keys or a.key==shot.support_asset_key:
                    bad(shot.n,'График, текст и поясняющие стрелки должны рисоваться отдельно от иллюстрации: '+a.key)
        merged[a.key]=a
    lookup={f['n']:f for f in frames}
    for s in plan.shots:
        if s.n not in lookup:continue
        narration=lookup[s.n]['narration']
        if s.support_quote not in narration:bad(s.n,'support_quote не из текущей narration.')
        previous=-1
        for step in s.reveal_steps:
            pos=narration.find(step.cue_quote)
            if pos<=previous or narration.count(step.cue_quote)!=1:bad(s.n,'reveal_steps требуют однозначные дословные цитаты текущей narration по порядку.')
            previous=pos
        if len(s.reveal_steps)==1:bad(s.n,'Для раскрытия нужны 2–4 шага или пустой список.')
        if s.support_asset_key and (s.support_asset_key not in merged or s.support_asset_key in s.asset_keys or len(s.asset_keys)!=1 or s.layout=='comparison'):bad(s.n,'Дополнительная поясняющая иллюстрация должна иметь отдельный известный ключ и один общий план.')
        if not set(s.asset_keys)<=merged.keys():bad(s.n,'Неизвестный asset key.')
        if not set(s.cast)<=registry.keys():bad(s.n,'Неизвестный cast ID.')
        if len(s.headline.split())>14 or len(s.caption.split())>40:bad(s.n,'Заголовок/подпись слишком длинные для читаемого кадра; сохрани законченный тезис.')
        if visual_first and s.layout in ('text_card','process','factors'):bad(s.n,'Визуальное видео не допускает текстовых слайдов или цепочек слов. Покажи явление иллюстрацией либо содержательной геометрией local_board.')
        if visual_first and s.nodes:bad(s.n,'Узлы-понятия заменяются иллюстрацией или конкретной геометрией. Используй annotations для выделений по словам.')
        if visual_first and s.layout in ('local_board','instruction') and s.elements:
            text_elements=[e for e in s.elements if e.kind=='text']
            if sum(len(e.text.split()) for e in text_elements)>24 or any(len(e.text.split())>(10 if s.layout=='instruction' else 5) for e in text_elements):bad(s.n,'Сократи экранный текст до обозначений/коротких команд. Рассказ уже озвучен.')
            if s.layout=='local_board' and not (any(e.kind in ('line','arrow','dot','cross') for e in s.elements) or any(e.kind=='box' and e.fill and e.color for e in s.elements)):bad(s.n,'Local_board должен показывать геометрию/демонстрацию, а не набор текстовых блоков.')
        for annotation in s.annotations:
            if annotation.cue_quote not in narration:bad(s.n,'Выделение требует точную фразу текущей narration.')
            if len(annotation.label.split())>3:bad(s.n,'Подпись выделения — максимум 3 слова; без пересказа.')
        if s.annotations and not s.asset_keys:bad(s.n,'Выделения annotations требуют иллюстрацию.')
        if visual_first and any(len(label.split())>5 for label in s.labels):bad(s.n,'Подписи состояний сравнения — до 5 слов, без пересказа озвучки.')
        if s.frame_density=='single_subject' and len(s.single_subject_reason.strip())<20:bad(s.n,'Один рисунок требует конкретного обоснования по смыслу сцены, а не свободного места.')
        if s.frame_density=='filled' and len(s.asset_keys)==1 and len(s.labels)<2 and not s.explanatory_scheme and not s.support_asset_key and len([e for e in s.elements if e.kind!='text'])<2:bad(s.n,'Наполненный кадр: запланируй разные пояснения, содержательную схему или независимую деталь. Один рисунок не является завершённой постановкой.')
        if s.presentation_motion in ('slide_explain','staged_explain','focus_explain','detail_explain','panorama_explain') and len(s.asset_keys)!=1:bad(s.n,'Раскрытие требует одну основную иллюстрацию.')
        if s.layout=='comparison':
            if len(s.asset_keys)!=2 or len(set(s.asset_keys))!=2 or len(s.labels)!=2:bad(s.n,'Сравнение требует две разные иллюстрации и две подписи.')
            elif set(s.asset_keys)<=merged.keys():
                a,b=[merged[k] for k in s.asset_keys]
                if a.subject.strip().casefold()==b.subject.strip().casefold() or a.state.strip().casefold()==b.state.strip().casefold():bad(s.n,'Состояния сравнения должны действительно различаться.')
                if a.identity.casefold()==b.identity.casefold() and a.view.casefold()!=b.view.casefold():bad(s.n,'Для состояний одного объекта сохрани одинаковую проекцию view.')
        if s.layout in ('process','factors'):
            if s.asset_keys or not 2<=len(s.nodes)<=6:bad(s.n,'Схема требует 2–6 nodes без assets.')
            if s.layout=='process' and (s.relation!='sequence' or not s.edges):bad(s.n,'Process требует явно доказанные последовательные edges.')
            if s.layout=='factors' and (s.relation!='parallel' or s.edges):bad(s.n,'Независимые факторы не соединяй стрелками.')
        elif s.layout in ('hero_caption','image_left','image_right'):
            if len(s.asset_keys)!=1 or s.nodes or s.edges:bad(s.n,'Иллюстративный макет: одна картинка, без локальных схем.')
            if s.elements and (len(s.elements)>2 or any(e.kind!='text' or len(e.text.split())>5 for e in s.elements)):bad(s.n,'Поверх иллюстрации допустимы только 1–2 короткие надписи внутри сцены, не схема-пересказ.')
        elif s.layout=='text_card':
            if s.asset_keys or s.nodes or s.elements or s.edges:bad(s.n,'Текстовая карточка не требует assets и схем.')
        elif s.layout in ('local_board','instruction'):
            if not ((len(s.asset_keys)==1 and (not s.elements or all(e.kind=='text' and len(e.text.split())<=5 for e in s.elements))) or (not s.asset_keys and s.elements)):bad(s.n,'Покажи точную инструкцию иллюстрацией с краткими обозначениями либо локальной геометрией.')
        if s.function=='factors' and s.layout=='process':bad(s.n,'Факторы не являются последовательностью.')
        if s.layout=='process' and {(e.source,e.target) for e in s.edges}!={(i,i+1) for i in range(len(s.nodes)-1)}:bad(s.n,'Линейный process должен явно связать все соседние узлы; ветвления покажи local_board.')
        if s.relation!='sequence' and s.edges:bad(s.n,'Стрелки допустимы только для sequence.')
        for e in s.edges:
            if not 0<=e.source<e.target<len(s.nodes) or e.evidence_quote not in narration:bad(s.n,'Ребро требует допустимые индексы и дословное подтверждение текущей фразой.')
        last=-1
        for node in s.nodes:
            at=narration.find(node.cue_quote)
            if at<0 or at<last:bad(s.n,'Узлы должны иметь точные cue_quote текущего кадра по порядку.')
            last=at
        for element in s.elements:
            if element.kind not in ('line','arrow') and (element.w<0 or element.h<0):bad(s.n,'Отрицательный размер разрешён только для направленных линий и стрелок.')
            if not -.001<=element.x+element.w<=1.001 or not -.001<=element.y+element.h<=1.001:bad(s.n,'Элемент выходит за границы доски.')
            if element.kind=='text' and not element.text.strip():bad(s.n,'Локальный text должен содержать читаемый текст.')
            if element.cue_quote and element.cue_quote not in narration:bad(s.n,'Опора локального элемента не из текущей narration.')
    return found,merged

def review_shot(shot):
    """Expose actual directed endpoints so the reviewer need not guess signed vectors."""
    value=shot.model_dump();labels=[e for e in shot.elements if e.kind=='text']
    def nearest(x,y):
        if not labels:return ''
        def distance(e):
            return max(e.x-x,x-e.x-(e.w or .25),0)**2+max(e.y-y,y-e.y-max(.05,e.h),0)**2
        return min(labels,key=distance).text
    value['rendered_arrow_connections']=[{'from_label':nearest(e.x,e.y),'to_label':nearest(e.x+e.w,e.y+e.h),
        'start':[e.x,e.y],'end':[e.x+e.w,e.y+e.h]} for e in shot.elements if e.kind=='arrow']
    return value

def stage(analysis,story,*,api_key=None,client=None,cache=None,progress=None,local_review=None):
    from .science_atlas import VERSION,asset_name
    schema_text='\nJSON schema (все имена полей и значения enum обязательны): '+json.dumps(Plan.model_json_schema(),ensure_ascii=False)
    review_schema='\nВерни JSON строго по схеме: '+json.dumps(Review.model_json_schema(),ensure_ascii=False)
    spans=source_groups(story)
    frames=[{'n':i+1,'narration':story[a:b],'source_start':a,'source_end':b} for i,(a,b) in enumerate(spans)]
    registry={c.get('cast_id') or c.get('id') or c['name']:c for c in analysis.get('characters',[])}
    compatible_schema=Plan.model_json_schema()
    # Optional renderer extensions preserve the validity of already reviewed plans.
    for field in ('color','fill'):compatible_schema['$defs']['Element']['properties'].pop(field,None)
    for field in ('w','h'):compatible_schema['$defs']['Element']['properties'][field]['minimum']=0
    signature_schema='\nJSON schema (все имена полей и значения enum обязательны): '+json.dumps(compatible_schema,ensure_ascii=False)
    signature=hashlib.sha256((PLANNER_VERSION+PROMPT+signature_schema+REVIEW_PROMPT+review_schema+story+json.dumps(list(registry),ensure_ascii=False)).encode()).hexdigest()
    previous=analysis.get('diagnostics',{}).get('science_atlas',{})
    if previous.get('complete') and previous.get('source_key')==signature:return analysis
    cache=cache or DiskCache();cache.ensure_writable();owned=client is None;stats=Stats();catalog={};shots=[];audits=[];hits=0
    previous_assets={a['key']:Asset.model_validate(a) for a in previous.get('assets',[])}
    catalog=dict(previous_assets);cached_recovery=False
    try:
        for start in range(0,len(frames),BATCH_SIZE):
            batch=frames[start:start+BATCH_SIZE]
            key=hashlib.sha256((signature+json.dumps([batch,[a.model_dump() for a in catalog.values()]],ensure_ascii=False)).encode()).hexdigest()
            path=cache.directory/('science-v2-'+key+'.json');plan=None;audit_start=len(audits)
            read_path=cache.read_path(path)
            if read_path.is_file():
                saved=json.loads(read_path.read_text(encoding='utf-8'))
                candidate=normalize_relations(Plan.model_validate(saved['plan']),registry,batch,catalog);problems,merged=errors(candidate,batch,catalog,registry)
                if not problems and saved.get('reviewed') is True:
                    plan=candidate;hits+=1;audits.extend(saved.get('review_evidence') or [{'first':batch[0]['n'],'last':batch[-1]['n'],'cache_verified':True}])
            payload={'story':story,'frames':batch,'characters':[{'id':k,'name':v['name']} for k,v in registry.items()],
                'render_capabilities':'Локальные элементы line/arrow допускают отрицательные w/h для направления влево/вверх, при этом конец остаётся внутри 0..1. Локальные elements поддерживают color (#RRGGBB) и fill=true для цветных фигур. Для цветового сравнения покажи настоящие цветные образцы, явно как качественную условную схему без измеренной шкалы.',
                'cast_policy':'cast содержит ТОЛЬКО id из characters; если characters=[], cast=[] всегда. Безымянный демонстратор — roles, не cast.',
                'available_existing_assets':[a.model_dump() for a in previous_assets.values()],
                'known_assets':[a.model_dump() for a in catalog.values()],
                'recent_shots':[{'layout':s.layout,'focus':s.focus} for s in shots[-6:]]}
            draft=cache.directory/('science-v2-draft-'+key+'.json')
            accepted={};working=dict(catalog);proposed={};resume_candidate=None
            draft_read=cache.read_path(draft)
            if draft_read.is_file():
                cached_recovery=True
                partial=json.loads(draft_read.read_text(encoding='utf-8'))
                proposed={v['key']:Asset.model_validate(v) for v in partial['plan'].get('assets',[])}
                payload['previous_plan']=partial['plan'];payload['fix_required']=partial['issues']
                if partial.get('accepted_reviewed'):
                    accepted={v['n']:Shot.model_validate(v) for v in partial.get('accepted_shots',[])}
                    working.update({v['key']:Asset.model_validate(v) for v in partial.get('accepted_assets',[])})
                resume_candidate=normalize_relations(Plan.model_validate(partial['plan']),registry,batch,working)
            if plan is None:
                last_seen={s.n:s for s in (resume_candidate.shots if resume_candidate else [])}
                can_generate_locally=local_review is not None and hasattr(local_review,'generate_plan')
                for attempt in range(2 if can_generate_locally else 1):
                    remaining=[f for f in batch if f['n'] not in accepted]
                    if not remaining:break
                    payload['frames']=remaining;payload['known_assets']=[a.model_dump() for a in {**proposed,**working}.values()]
                    if progress:progress(f'Научпоп: постановка {remaining[0]["n"]}–{remaining[-1]["n"]}; принято {len(accepted)}/{len(batch)}',72)
                    candidate=None
                    if attempt==0 and resume_candidate is not None:
                        filtered=Plan(assets=resume_candidate.assets,shots=[s for s in resume_candidate.shots if s.n in {f['n'] for f in remaining}])
                        candidate=normalize_relations(filtered,registry,remaining,working)
                    if candidate is None:
                        if can_generate_locally:
                            paid_messages=[{'role':'system','content':PROMPT+schema_text},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
                            permit=None
                            if attempt==0 and hasattr(local_review,'authorize_extra_pass') and not getattr(local_review,'prefer_local',False):
                                if progress:progress('Дополнительный платный проход ожидает подтверждения. Через 60 секунд продолжит Qwen.',72)
                                permit=local_review.authorize_extra_pass(paid_messages,Plan,key)
                            if permit is not None:
                                paid_client=None
                                try:
                                    paid_client=JsonClient(api_key=api_key,authorization=permit,failed_dir=cache.directory.parent/'failures')
                                    candidate=paid_client.ask_json(paid_messages,Plan,stats,phase='C',script_hash=key)
                                except AnalysisError:
                                    local_review.prefer_local=True
                                finally:
                                    if paid_client is not None:paid_client.close()
                            if candidate is None:
                                if progress:progress(f'Локальная Qwen: постановка {remaining[0]["n"]}–{remaining[-1]["n"]}; без DeepSeek',72)
                                try:candidate=local_review.generate_plan(payload,Plan)
                                except AnalysisError as error:
                                    if hasattr(error,'science_review'):
                                        saved=error.science_review.pop('saved_in_batch',0)
                                        error.science_review.update(accepted=start+len(accepted)+saved,total=len(frames))
                                    raise
                        else:
                            if cached_recovery:raise AnalysisError('Восстановление остановлено без дополнительных запросов DeepSeek: для следующего фрагмента нет сохранённого плана. Проверенные сцены и локальные исправления сохранены.')
                            if client is None:raise AnalysisError('Для научной постановки нужна локальная Qwen. DeepSeek ограничен двумя проходами разбора; дополнительный запрос заблокирован.')
                            candidate=client.ask_json([{'role':'system','content':PROMPT+schema_text},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}],Plan,stats,phase='C',script_hash=key)
                    candidate=normalize_relations(candidate,registry,remaining,working)
                    proposed.update({a.key:a for a in candidate.assets if a.key not in working})
                    available={**proposed,**working}
                    for asset in candidate.assets:
                        if asset.key not in working:available.pop(asset.key,None)
                    problems,merged=errors(candidate,remaining,available,registry)
                    bad={p['n'] for p in problems};global_error=0 in bad
                    good_frames=[] if global_error else [f for f in remaining if f['n'] not in bad]
                    good_shots=[s for s in candidate.shots if s.n in {f['n'] for f in good_frames}]
                    if good_frames and not getattr(candidate,'_locally_reviewed',False):
                        review_client=client
                        if resume_candidate is not None or local_review is not None:
                            if local_review is None:raise AnalysisError('Сохранённый план исправлен локально. Для повторной проверки подключите локальную Qwen. Дополнительные запросы DeepSeek заблокированы.')
                            review_client=local_review
                        if progress:progress(f'Научпоп: проверка смысла {good_frames[0]["n"]}–{good_frames[-1]["n"]}',78)
                        review=review_client.ask_json([{'role':'system','content':REVIEW_PROMPT+review_schema},{'role':'user','content':json.dumps({'renderer_architecture':{'native_experiments':'Строка с настоящими читаемыми словами из elements, например ДОМ РЕКА ЛЕС МОСТ, является ПОЛНОЦЕННОЙ СТРОКОЙ опыта чтения. Это правильный локальный объект, НЕ дублирование narration. Не заменяй его Flow-asset с буквами: генерация букв в assets запрещена. Метки креста и точки также намеренно локальные. Изображать физическое закрытие глаза на экране необязательно, если зрителю даётся короткая команда выполнить это самому. Не требуй числовой шкалы реальных сантиметров на экране.','illustrative_scope':'Условная иллюстрация метода или типичного объекта допустима, если она не выдаётся за историческое фото, конкретную запись измерений или новый научный факт. Не требуй, чтобы narration буквально перечисляла чашку/прибор/позу, если это нейтральная иллюстративная обстановка. Не требуй доказать каждое озвученное слово на одном изображении: кадр может сопровождать часть мысли, если не противоречит остальному.','allowed_functions':['structure','mechanism','factors','comparison','instruction','analogy','observation','summary','transition'],'visual_first':'Визуал занимает кадр. headline/caption/focus — скрытые редакторские поля. Весь narration уже звучит: НЕ требуй повторить его текстом, в том числе в подписях сравнения.','comparison':'layout=comparison ГАРАНТИРУЕТ две картинки одновременно бок о бок; labels под каждой. Пустой elements корректен и не препятствует сравнению.','local_elements':'elements, nodes и edges рисуются КОДОМ локально. Flow получает ТОЛЬКО assets.subject/view/state. Текст, точки, стрелки и схемы в elements НЕ требуют от Flow ничего и являются правильным форматом.','process':'Схематичный process намеренно имеет 0 assets: nodes/edges уже являются его визуальным носителем. Не требуй буквальную фотографию для схемы.','capacity':'Максимум 2 иллюстрации. Не предлагай три изображения для одного кадра.','review_standard':'Ищи смысловые противоречия и невыполнимые задания. Не требуй показать измерение/доказательство каждого факта, если он уже дан исходным текстом: число можно написать в подписи, а орган показать без семи отдельных меток. Не отмечай допустимый синоним как изменение уверенности без конкретной разницы значения. Статичное пояснение и условная схема допустимы; выдуманные данные под видом измеренных — нет.'},'context':story[max(0,good_frames[0]['source_start']-900):min(len(story),good_frames[-1]['source_end']+900)],'frames':good_frames,'shots':[review_shot(s) for s in good_shots],'assets':[a.model_dump() for key,a in merged.items() if key in {k for shot in good_shots for k in shot.asset_keys+([shot.support_asset_key] if shot.support_asset_key else [])}]},ensure_ascii=False)}],Review,stats,phase='C',script_hash=key+'-review')
                        if any(i.n not in {f['n'] for f in good_frames} for i in review.issues):
                            problems.append({'n':0,'reason':'Редактор вернул неизвестный n.'});global_error=True
                        problems.extend(i.model_dump() for i in review.issues)
                    bad={p['n'] for p in problems}
                    if not global_error:
                        for shot in candidate.shots:
                            if shot.n not in bad:accepted[shot.n]=shot
                        needed={k for shot in accepted.values() for k in shot.asset_keys+([shot.support_asset_key] if shot.support_asset_key else [])}
                        working.update({k:asset for k,asset in merged.items() if k in needed})
                    last_seen.update({shot.n:shot for shot in candidate.shots});last_seen.update(accepted)
                    all_assets={**proposed,**working,**{asset.key:asset for asset in candidate.assets}}
                    draft_plan={'assets':[v.model_dump() for v in all_assets.values()],'shots':[last_seen[f['n']].model_dump() for f in batch if f['n'] in last_seen]}
                    cache.directory.mkdir(parents=True,exist_ok=True)
                    atomic_json(draft,{'plan':draft_plan,'issues':problems,'accepted_reviewed':True,'accepted_shots':[v.model_dump() for v in accepted.values()],'accepted_assets':[v.model_dump() for v in working.values()]})
                    audits.append({'first':remaining[0]['n'],'last':remaining[-1]['n'],'attempt':attempt+1,'issues':problems,'review_engine':'local_qwen' if local_review is not None else 'deepseek','paid_repair_attempts':0})
                    if progress and problems:progress('Уточняю: '+json.dumps(problems,ensure_ascii=False)[:1200],78)
                    payload['previous_plan']={'assets':[a.model_dump() for a in candidate.assets],'shots':[s.model_dump() for s in candidate.shots if s.n not in accepted]};payload['fix_required']=problems
                if len(accepted)!=len(batch):
                    error=AnalysisError(f'Принято и сохранено {start+len(accepted)} из {len(frames)} сцен. Проверьте замечания к оставшимся сценам.')
                    error.science_review={'accepted':start+len(accepted),'total':len(frames),'issues':problems}
                    raise error
                plan=Plan(assets=list(working.values()),shots=[accepted[f['n']] for f in batch]);merged=working
                path.parent.mkdir(parents=True,exist_ok=True);atomic_json(path,{'reviewed':True,'plan':plan.model_dump(),'review_evidence':audits[audit_start:]})
            catalog=merged;shots.extend(plan.shots)
        result=copy.deepcopy(analysis);result['scenes']=[]
        templates=analysis.get('scenes') or [{}]
        for f,shot in zip(frames,shots):
            template=next((s for s in templates if s.get('source_start',-1)<=f['source_start']<s.get('source_end',-1)),templates[0])
            scene=copy.deepcopy(template);nodes=[]
            for node in shot.nodes:
                n=node.model_dump();n['source_char_start']=f['source_start']+f['narration'].index(node.cue_quote);nodes.append(n)
            elements=[]
            for element in shot.elements:
                e=element.model_dump();e['source_char_start']=f['source_start']+f['narration'].index(e['cue_quote']) if e['cue_quote'] else None;elements.append(e)
            visual=shot.model_dump(exclude={'n','cast','roles','nodes','elements'});visual.update(version=VERSION,planner_version=PLANNER_VERSION,presentation='visual_first',composition_mode='explanatory',nodes=nodes,elements=elements)
            visual['annotations']=sorted([{**ann.model_dump(),'source_char_start':f['source_start']+f['narration'].index(ann.cue_quote)} for ann in shot.annotations],key=lambda ann:ann['source_char_start'])
            duration=max(3,len(f['narration'].split())/2.5)
            visual['cue_ms']=[None for _ in nodes+elements]+[round((ann['source_char_start']-f['source_start'])/max(1,len(f['narration']))*duration*1000) for ann in visual['annotations']]
            visual['cue_timing']='estimated_from_text_until_voice_fit'
            names=[registry[k]['name'] for k in shot.cast]
            direction=shot.focus+'. '+('; '.join(catalog[k].subject for k in shot.asset_keys) if shot.asset_keys else shot.headline)
            scene.update(science_atlas=visual,source_start=f['source_start'],source_end=f['source_end'],source_quote=f['narration'],
                beat=direction,composition=direction,audit_visual_direction=direction,audit_source_quote=f['narration'],
                characters=names,character_ids=shot.cast,cast=[{'character_id':k,'name':registry[k]['name']} for k in shot.cast],
                episodic_people=shot.roles,visible_people=len(names)+len(shot.roles),raw_visible_people=len(names)+len(shot.roles),
                mentioned_characters=[],offscreen_characters=[],voice_only_characters=[],background_roles=[],depicted_people=[],wardrobe={name:'default' for name in names},
                props=[],location='',location_ref='',location_id=None,population_policy='source_reviewed_participants',
                compact_staging={'version':PLANNER_VERSION,'action':direction,'kind':'comment'},
                duration_sec=round(max(3,len(f['narration'].split())/2.5),3),
                prompt_timing={'duration_seconds':min(60,max(3,len(f['narration'].split())/2.5))},
                animation={'selected_engine':'ken_burns','status':'planned_ken_burns','duration_seconds':6,'source':'science_atlas'})
            for stale in ('storyboard_frame','voice_timing','camera_motion','foreground_effect'):scene.pop(stale,None)
            result['scenes'].append(scene)
        used={k for s in shots for k in s.asset_keys+([s.support_asset_key] if s.support_asset_key else [])};catalog={k:a for k,a in catalog.items() if k in used}
        result['props']=[{'name':asset_name(a.key),'description':f'{a.subject} View: {a.view}. State: {a.state}.','importance':'story','first_appearance':next(s.n for s in shots if a.key in s.asset_keys or a.key==s.support_asset_key),'owner':'','default_state':'present'} for a in catalog.values()]
        result['locations']=[]
        for field in ('v2_metadata','v3_metadata'):
            metadata=result.get(field,{})
            for stale in ('story_visual_plan','narrative_storyboard'):metadata.pop(stale,None)
        diag=result.setdefault('diagnostics',{});diag['scenes_count']=len(frames)
        diag['science_atlas']={'version':PLANNER_VERSION,'complete':True,'source_key':signature,'reviewed':True,'assets':[a.model_dump() for a in catalog.values()],
            'usage':asdict(stats),'cache_batches':hits,'audit_rounds':audits,'previous_frame_count':len(templates),'frames':len(frames),'source_partition':'whole_sentence_groups'}
        usage=result.setdefault('deepseek_usage',{})
        for name in ('tokens_in','tokens_out','cost_usd','cache_hit_tokens','cache_miss_tokens'):usage[name]=usage.get(name,0)+getattr(stats,name)
        usage['science_atlas_cost_usd']=usage.get('science_atlas_cost_usd',0)+stats.cost_usd
        return result
    finally:
        if stats.requests:
            from datetime import datetime,timezone
            cache.directory.mkdir(parents=True,exist_ok=True)
            atomic_json(cache.directory/('science-v2-usage-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'),{'source_key':signature,**asdict(stats)})
        if owned and client is not None:client.close()

def project_preflight(project):
    """Scientific diagrams are not narrative casts: two eyes are not two actors."""
    from .source_contract import coverage
    from .domain import DomainError
    from .science_atlas_render import compile_commands
    problems=[];metadata=project.get('analysis_metadata') or {}
    if isinstance(metadata,str):metadata=json.loads(metadata)
    diagnostic=metadata.get('diagnostics',{}).get('science_atlas',{})
    if diagnostic.get('version') not in ('science-staging/2',PLANNER_VERSION) or not diagnostic.get('reviewed'):
        return None  # Legacy projects retain their existing validation.
    catalog={a['key']:Asset.model_validate(a) for a in diagnostic.get('assets',[])}
    registry={c.get('cast_id') or c.get('id') or c['name']:c for c in project.get('characters',[])}
    for n,scene in enumerate(project.get('scenes',[]),1):
        data=scene.get('data',scene);visual=data.get('science_atlas') or {}
        try:
            raw={k:visual[k] for k in Shot.model_fields if k in visual};raw['n']=n
            raw['nodes']=[{k:v[k] for k in Node.model_fields if k in v} for v in raw.get('nodes',[])]
            raw['elements']=[{k:v[k] for k in Element.model_fields if k in v} for v in raw.get('elements',[])]
            raw['annotations']=[{k:v[k] for k in Annotation.model_fields if k in v} for v in raw.get('annotations',[])]
            raw['reveal_steps']=[{k:v[k] for k in RevealStep.model_fields if k in v} for v in raw.get('reveal_steps',[])]
            shot=Shot.model_validate(raw)
            issues,_=errors(Plan(assets=[],shots=[shot]),[{'n':n,'narration':data['source_quote']}],catalog,registry,visual_first=diagnostic.get('version')==PLANNER_VERSION)
            unresolved=copy.deepcopy(visual)
            unresolved.setdefault('illustrations',[{'key':key,'asset_id':None} for key in visual.get('asset_keys',[])])
            compile_commands(unresolved)
            problems.extend({'code':'science_contract','scene_index':n,'message':f"Кадр {n}: {i['reason']}"} for i in issues)
        except DomainError as error:
            problems.append({'code':'science_contract','scene_index':n,'message':f'Кадр {n}: {error}'})
        except (ValueError,TypeError,KeyError) as error:
            problems.append({'code':'science_contract','scene_index':n,'message':f'Кадр {n}: некорректный научный макет ({type(error).__name__}).'})
    report=coverage(project['story'],project.get('scenes',[]))
    if not report['ok']:problems.append({'code':'source_coverage','message':'Научный план не покрывает исходный текст.'})
    return {'ok':not problems,'valid':not problems,'errors':problems,'warnings':[],
        'stats':{'total_scenes':len(project.get('scenes',[])),'validation':'scientific_visual_contract/2'},'source_coverage':report}

PROMPT += '\nДля подходящей сцены с одной основной иллюстрацией запланируй 2–4 reveal_steps: kind=part/focus/caption, cue_quote — короткая однозначная дословная цитата ТЕКУЩЕЙ narration, title — краткая подпись, explanation — краткая роль детали, region_key — название видимой детали. Шаги идут по порядку речи. Не выдавай координаты за проверенные: пространственная привязка выполняется отдельно по готовому изображению. Для наблюдения без различимых частей используй caption. Основной headline остаётся темой сцены с первого кадра.\n'

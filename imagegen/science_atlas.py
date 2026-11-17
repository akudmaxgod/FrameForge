"""Scientific visual planning. Replaces (does not duplicate) narrative staging."""
import copy,hashlib,json,re
from dataclasses import asdict
from pathlib import Path
from pydantic import Field
from .script_analysis.schemas import StrictModel
from .script_analysis.cache import DiskCache,atomic_json
from .script_analysis.client import JsonClient
from .script_analysis.stats import Stats
from .script_analysis.errors import AnalysisError

VERSION='science-atlas/1'
STYLE=('Dark Scientific Atlas: precise explanatory 2D atlas illustration, delicate engraved contours, '
       'matte ivory, slate and dusty rose, restrained desaturated teal. Dark petrol #09161A backdrop, '
       'readable intact subject, consistent orientation, generous margins. No lettering, captions, '
       'labels, numbers, arrows, logos, glossy CGI or fantasy anatomy. No invented pathology. '
       'Depict only the specified structures/actions; scientific accuracy needs reliable supplied references.')
LAYOUTS={'hero_caption','image_left','image_right','comparison','process','instruction','factors','text_card','local_board'}
PROMPT='''Ты визуальный редактор универсального научпопа. Верни JSON:
{"assets":[{"key":"latin_key","subject":"точное видимое содержание отдельной иллюстрации, ракурс и ограничения","people":false}],
"shots":[{"n":1,"layout":"hero_caption","function":"explanation","representation":"literal",
"headline":"тезис до 9 слов","caption":"пояснение до 22 слов","asset_keys":["latin_key"],
"labels":[],"nodes":[{"label":"короткое название","cue_quote":"точная фраза из narration или пусто"}],"cast":[],"roles":[]}]}
Каждый входной n ровно один раз и по порядку. Полный story — контекст; narration — текущая мысль.
Макеты: hero_caption, image_left, image_right (одна иллюстрация); comparison (две ОТДЕЛЬНЫЕ иллюстрации, ровно два asset_keys и две labels);
process (схема, без изображений, 2–6 nodes); instruction (показ действия, одна иллюстрация или 2–6 nodes).
Не соединяй перечень понятий причинными стрелками. Схему цепочки используй только для подтверждённого маршрута/процесса.
Для перечня используй hero_caption с иллюстрацией. Подписи nodes связывай с ТОЧНЫМИ цитатами текущей narration.
representation: literal, schematic, analogy. Метафора не превращается в устройство организма; аналогию явно обозначай.
Не добавляй фактов, чисел, патологий, людей и причинных связей. Упомянутый учёный не обязан быть виден.
cast — только ID действительно видимых героев реестра; roles — нужный безымянный демонстратор.
Органы, клетки, частицы и приборы — assets, не персонажи. Инструкция требует буквального показа позы/предметов/действия.
Переиспользуй уже известные assets по key, если предмет и ракурс не изменились. Для сравнения у двух subject одинаковые проекция, масштаб и оформление.
Без типографики и стрелок внутри assets: текст/схемы рисует код. Не описывай макет всего кадра в subject.
Длительность, художественный стиль и геометрию добавляет код. Не требуй генерации видео. Сохрани текущую мысль в контексте соседних.
'''

class Asset(StrictModel):
    key:str=Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    subject:str=Field(min_length=1,max_length=1800)
    people:bool=False
class Node(StrictModel):
    label:str=Field(min_length=1,max_length=90)
    cue_quote:str=Field(default='',max_length=160)
class Shot(StrictModel):
    n:int
    layout:str
    function:str
    representation:str
    headline:str=Field(min_length=1,max_length=180)
    caption:str=Field(default='',max_length=350)
    asset_keys:list[str]
    labels:list[str]=Field(default_factory=list)
    nodes:list[Node]=Field(default_factory=list)
    cast:list[str]=Field(default_factory=list)
    roles:list[str]=Field(default_factory=list)
class Plan(StrictModel):
    assets:list[Asset]
    shots:list[Shot]

def asset_name(key):return 'Атлас / '+key

def stage_legacy(analysis,story,*,api_key=None,client=None,cache=None,progress=None):
    frames=[{'n':i+1,'narration':s['source_quote']} for i,s in enumerate(analysis['scenes'])]
    registry={c.get('cast_id') or c['id']:c for c in analysis.get('characters',[])}
    signature=hashlib.sha256((VERSION+PROMPT+story+json.dumps([frames,[(k,c['name']) for k,c in registry.items()]],ensure_ascii=False)).encode()).hexdigest()
    old=analysis.get('diagnostics',{}).get('science_atlas',{})
    if old.get('complete') and old.get('source_key')==signature:return analysis
    result=copy.deepcopy(analysis);cache=cache or DiskCache();stats=Stats();owned=client is None
    catalog={};shots=[]
    try:
        for start in range(0,len(frames),30):
            batch=frames[start:start+30];key=hashlib.sha256((signature+str(start)).encode()).hexdigest()
            path=cache.directory/('science-atlas-'+key+'.json')
            payload={'story':story,'frames':batch,'characters':[{'id':k,'name':c['name']} for k,c in registry.items()],
                     'known_assets':[a.model_dump() for a in catalog.values()]}
            plan=Plan.model_validate_json(path.read_text(encoding='utf-8')) if path.is_file() else None
            for attempt in range(2):
                if plan is None:
                    if client is None:client=JsonClient(api_key=api_key)
                    if progress:progress(f'Научпоп: постановка сцен {start+1}–{start+len(batch)}',72)
                    plan=client.ask_json([{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],Plan,stats,phase='C',script_hash=key)
                merged={**catalog};asset_conflict=False
                for a in plan.assets:
                    if a.key in merged and merged[a.key]!=a:asset_conflict=True
                    merged[a.key]=a
                valid=not asset_conflict and len({a.key for a in plan.assets})==len(plan.assets) and ([s.n for s in plan.shots]==[f['n'] for f in batch])
                valid=valid and all(s.layout in LAYOUTS and s.representation in ('literal','schematic','analogy')
                    and set(s.asset_keys)<=merged.keys() and set(s.cast)<=registry.keys()
                    and len(s.nodes)<=6 and len(s.headline.split())<=9 and len(s.caption.split())<=22
                    and ((s.layout=='comparison' and len(s.asset_keys)==2 and len(s.labels)==2)
                         or (s.layout=='process' and not s.asset_keys and 2<=len(s.nodes)<=6)
                         or (s.layout=='instruction' and (len(s.asset_keys)==1 or (not s.asset_keys and 2<=len(s.nodes)<=6)))
                         or (s.layout in ('hero_caption','image_left','image_right') and len(s.asset_keys)==1))
                    for s in plan.shots)
                if valid:catalog=merged;break
                plan=None;payload['repair']='Исправь номера/макеты/ссылки и длину текста. Не теряй ни одной входной сцены.'
            if plan is None:raise AnalysisError('Не удалось восстановить научпоп-план для текущего пакета; предыдущие пакеты сохранены.')
            path.parent.mkdir(parents=True,exist_ok=True);atomic_json(path,plan.model_dump());shots.extend(plan.shots)
        for shot,scene in zip(shots,result['scenes']):
            quote=scene['source_quote'];nodes=[]
            for node in shot.nodes:
                data=node.model_dump();pos=quote.find(node.cue_quote) if node.cue_quote else -1
                data['source_char_start']=scene.get('source_start',0)+pos if pos>=0 else None
                nodes.append(data)
            visual=shot.model_dump(exclude={'n','cast','roles','nodes'});visual.update(version=VERSION,nodes=nodes)
            subject='; '.join(catalog[k].subject for k in shot.asset_keys) or 'Схема: '+' → '.join(n.label for n in shot.nodes)
            names=[registry[k]['name'] for k in shot.cast]
            scene.update(science_atlas=visual,beat=subject,composition=subject,audit_visual_direction=subject,audit_source_quote=quote,
                characters=names,character_ids=shot.cast,cast=[{'character_id':k,'name':registry[k]['name']} for k in shot.cast],
                episodic_people=shot.roles,visible_people=len(names)+len(shot.roles),raw_visible_people=len(names)+len(shot.roles),
                mentioned_characters=[],offscreen_characters=[],voice_only_characters=[],background_roles=[],depicted_people=[],
                wardrobe={name:scene.get('wardrobe',{}).get(name,'default') for name in names},
                props=[],location='',location_ref='',location_id=None,population_policy='source_reviewed_participants',
                compact_staging={'version':VERSION,'action':subject,'kind':'comment'},
                animation={'selected_engine':'ken_burns','status':'planned_ken_burns','duration_seconds':6,'source':'science_atlas'})
        result['locations']=[]
        result['props']=[{'name':asset_name(a.key),'description':a.subject,'importance':'story','first_appearance':1,'owner':'','default_state':'present'} for a in catalog.values()]
        diagnostics=result.setdefault('diagnostics',{})
        diagnostics['science_atlas']={'complete':True,'source_key':signature,'version':VERSION,'assets':[a.model_dump() for a in catalog.values()],'usage':asdict(stats)}
        usage=result.setdefault('deepseek_usage',{})
        for name in ('tokens_in','tokens_out','cost_usd','cache_hit_tokens','cache_miss_tokens'):
            usage[name]=usage.get(name,0)+getattr(stats,name)
        usage['science_atlas_cost_usd']=stats.cost_usd
        return result
    finally:
        if owned and client is not None:client.close()


def stage(analysis,story,**kwargs):
    from .science_planner import stage as scientific_stage
    return scientific_stage(analysis,story,**kwargs)

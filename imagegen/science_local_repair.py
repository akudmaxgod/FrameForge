"""Deterministic contract repairs and an optional local, unbilled source review."""
import hashlib,json,re,urllib.request

def overlay_request(subject):
    # A physical instrument pointer is part of an object, not a diagram overlay.
    clean=re.sub(r'стрелк[а-яё]*\s+(?:циферблат[а-яё]*|часов|часов[а-яё]*|компас[а-яё]*|манометр[а-яё]*|весов|прибора)', '',subject,flags=re.I)
    term=r'(?:текст[а-яё]*|подпис[а-яё]*|стрел[а-яё]*|надпис[а-яё]*|цифр[а-яё]*|график[а-яё]*)'
    clean=re.sub(r'без\s+'+term+r'(?:(?:\s*,\s*|\s+и\s+|\s+или\s+)'+term+r')*','',clean,flags=re.I)
    return bool(re.search(r'график|стрелк|строк[аиу].{0,35}(?:текст|слов)|призыв\s+(?:поставить|подпис)',clean,re.I))

def repair_contract(plan,known=None):
    known=known or {};assets={**known,**{a.key:a for a in plan.assets}}
    for shot in plan.shots:
        if shot.layout=='comparison' and len(shot.asset_keys)==1 and len(shot.labels)>=2:
            # Keep the supplied state and explanation; never fabricate a second image.
            shot.layout='hero_caption';shot.presentation_motion='none'
            shot.focus=shot.support_quote[:350]
        if shot.presentation_motion!='none' and (len(shot.asset_keys)!=1 or shot.layout=='comparison'):
            shot.presentation_motion='none'
        if shot.support_asset_key and (shot.support_asset_key not in assets or shot.support_asset_key in shot.asset_keys or len(shot.asset_keys)!=1 or shot.layout=='comparison'):
            shot.support_asset_key='';shot.support_cue_quote=''
        if shot.layout!='comparison' or len(shot.asset_keys)!=2:continue
        first,second=(assets.get(k) for k in shot.asset_keys)
        if not first or not second or first.identity.casefold()!=second.identity.casefold() or first.view.casefold()==second.view.casefold():continue
        # Already accepted illustrations are immutable. Derive a separate camera
        # specification for this comparison, preserving subject and state exactly.
        key=second.key[:49]+'_v_'+hashlib.sha256(first.view.encode()).hexdigest()[:10]
        aligned=assets.get(key) or second.model_copy(update={'key':key,'view':first.view},deep=True)
        if key not in assets:plan.assets.append(aligned);assets[key]=aligned
        shot.asset_keys[1]=key
    return plan

class LocalScienceReview:
    def __init__(self,service):self.service=service
    def authorize_extra_pass(self,messages,schema,script_hash):
        if getattr(self,'prefer_local',False):return None
        from .paid_pass_approval import gate
        from .script_analysis.client import request_context
        permit=gate.request(str(self.service.root.resolve()),request_context(messages,schema,'C',script_hash),'Научная постановка следующего фрагмента')
        if permit is None:self.prefer_local=True
        return permit
    def generate_plan(self,payload,schema):
        """Create and review uncached shots locally; cache each accepted shot."""
        from .science_planner import Asset,normalize_relations,errors,Review
        from .script_analysis.errors import AnalysisError
        from .script_analysis.cache import atomic_json
        from .media_analysis import LlamaServerManager
        manager=LlamaServerManager.get_manager(self.service)
        if not all(p.is_file() for p in (manager.binary_path,manager.base_model,manager.mmproj)):
            raise AnalysisError('Для научной постановки нужна локальная Qwen. Дополнительный запрос DeepSeek заблокирован.')
        manager.ensure_running();known={a['key']:a for a in payload.get('known_assets',[])};shots=[]
        cache=self.service.root/'science-local-plans';cache.mkdir(parents=True,exist_ok=True)
        example={'assets':[{'key':'object_main','subject':'Конкретный видимый объект и его части','identity':'Объект','view':'вид сбоку','state':'описанное состояние','people':False}], 'shots':[{'n':1,'layout':'hero_caption','function':'structure','representation':'literal','focus':'Что видно и что зритель поймёт','support_quote':'Дословная цитата narration','headline':'Короткая тема','asset_keys':['object_main'],'labels':['Видимая часть','Роль детали'],'frame_density':'filled','presentation_motion':'focus_explain'}]}
        system=('Составь ОДИН научный кадр точно по narration. Не добавляй факты, числа или причинные связи. Верни JSON assets/shots по образцу. '
                'n сохрани входной. Ключи assets: латиница a-z0-9_. Иллюстрация показывает конкретный объект, не график с текстом. '
                'Можно использовать неизменённый known asset. Заполни кадр по смыслу: 2–4 короткие labels до 5 слов, точные пояснения из narration. '
                'headline до 9 слов, focus до 350 символов. support_quote — точная непустая цитата narration. '
                'Для сравнения: layout=comparison, 2 разных assets/2 labels, motion=none, одинаковая камера. Не дублируй картинку. '
                'Для местной геометрии: layout=local_board, assets=[], motion=none, elements: kind=box/line/arrow/dot/cross/text,x,y,w,h в 0..1; геометрия обязана показывать опыт. '
                'Не используй process/factors/text_card. cast=[],roles=[]. Условия воображаемого опыта оставь голосу. '
                'Не показывай следующий шаг или результат до его упоминания. У измерительного прибора оставь показание нечитаемым, если в текущем фрагменте его нет. '
                'Не выдавай условную схему за измеренные данные. '+json.dumps(example,ensure_ascii=False))
        for frame in payload['frames']:
            words=set(re.findall(r'\w{4,}',frame['narration'].lower()))
            ranked=sorted(known.values(),key=lambda a:-len(words&set(re.findall(r'\w{4,}',json.dumps(a,ensure_ascii=False).lower()))))[:2]
            compact=[{k:(v[:220] if isinstance(v,str) else v) for k,v in a.items()} for a in ranked]
            content={'frame':frame,'known_assets':compact,
                     'previous_shot':next((s for s in payload.get('previous_plan',{}).get('shots',[]) if s['n']==frame['n']),None)}
            if content['previous_shot']:
                content['previous_shot']={k:v for k,v in content['previous_shot'].items() if k in ('focus','labels','asset_keys','layout')}
            content['fix_required']=[i.get('reason','')[:220] for i in payload.get('fix_required',[]) if i.get('n') in (0,frame['n'])][:3]
            key=hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest();path=cache/(key+'.json')
            accepted=None
            if path.is_file():
                try:
                    saved=json.loads(path.read_text(encoding='utf-8'));candidate=schema.model_validate(saved['plan'])
                    typed_known={k:Asset.model_validate(a) for k,a in known.items()}
                    candidate=normalize_relations(candidate,{},[frame],typed_known)
                    if saved.get('reviewed') and not errors(candidate,[frame],typed_known,{})[0]:accepted=candidate
                except (ValueError,KeyError):pass
            for attempt in range(3 if accepted is None else 0):
                request=urllib.request.Request(f'http://127.0.0.1:{manager.port}/v1/chat/completions',data=json.dumps({'model':'qwen3-vl','temperature':0,'max_tokens':1600,'response_format':{'type':'json_object'},'messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(content,ensure_ascii=False)}]}).encode(),headers={'Content-Type':'application/json'})
                try:
                    with manager.inference_lock,urllib.request.urlopen(request,timeout=150) as response:raw=json.load(response)
                    value=json.loads(raw['choices'][0]['message']['content'])
                    if isinstance(value.get('assets'),dict) and 'subject' not in value['assets'] and all(isinstance(a,dict) for a in value['assets'].values()):
                        value['assets']=[{**asset,'key':key} for key,asset in value['assets'].items()]
                    for field in ('assets','shots'):
                        if isinstance(value.get(field),dict):
                            value[field]=[value[field]]
                    if len(value.get('shots',[]))==1:
                        shot=value['shots'][0]
                        if shot.get('n')==frame['n'] and (not shot.get('support_quote') or shot['support_quote'] not in frame['narration']):
                            quote=frame['narration'][:1200]
                            shot['support_quote']=quote
                    candidate=schema.model_validate(value)
                    typed_known={k:Asset.model_validate(a) for k,a in known.items()}
                    rename={}
                    for asset in candidate.assets:
                        if asset.key in typed_known and asset!=typed_known[asset.key]:
                            key=asset.key[:40]+'_local_'+hashlib.sha256(asset.model_dump_json().encode()).hexdigest()[:10]
                            rename[asset.key]=key;asset.key=key
                    for shot in candidate.shots:
                        shot.asset_keys=[rename.get(k,k) for k in shot.asset_keys]
                        shot.support_asset_key=rename.get(shot.support_asset_key,shot.support_asset_key)
                    candidate=normalize_relations(candidate,{},[frame],typed_known);problems,merged=errors(candidate,[frame],typed_known,{})
                    if not problems:
                        review=self.ask_json([{'role':'user','content':json.dumps({'frames':[frame],'shots':[s.model_dump() for s in candidate.shots],'assets':[a.model_dump() for a in merged.values() if a.key in {k for s in candidate.shots for k in s.asset_keys+([s.support_asset_key] if s.support_asset_key else [])}]},ensure_ascii=False)}],Review)
                        problems=[i.model_dump() for i in review.issues]
                    if not problems:
                        accepted=candidate;atomic_json(path,{'reviewed':True,'plan':candidate.model_dump(),'engine':'local_qwen','paid_requests':0});break
                    content['fix_required']=[{k:str(v)[:260] for k,v in problem.items() if k in ('n','reason','correction')} for problem in problems[:3]]
                except Exception as error:
                    from pydantic import ValidationError
                    if isinstance(error,ValidationError):
                        detail=', '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in error.errors(include_input=False,include_url=False))
                        content['fix_required']=[{'reason':('Поля ответа: '+detail)[:350]}]
                    else:
                        content['fix_required']=[{'reason':f'Локальная проверка завершилась ошибкой ({type(error).__name__}).'}]
            if accepted is None:
                issues=[{'n':frame['n'],**issue} for issue in content.get('fix_required',[])]
                error=AnalysisError(f"Локальная Qwen не завершила кадр {frame['n']}. Готовые кадры сохранены.")
                error.science_review={'issues':issues,'saved_in_batch':len(shots)}
                raise error
            shots.extend(accepted.shots);known.update({a.key:a.model_dump() for a in accepted.assets})
        needed={k for s in shots for k in s.asset_keys+([s.support_asset_key] if s.support_asset_key else [])}
        result=schema.model_validate({'assets':[a for k,a in known.items() if k in needed],'shots':[s.model_dump() for s in shots]})
        result._locally_reviewed=True
        return result
    def ask_json(self,messages,schema,*args,**kwargs):
        from .media_analysis import LlamaServerManager
        from .script_analysis.errors import AnalysisError
        manager=LlamaServerManager.get_manager(self.service)
        if not all(p.is_file() for p in (manager.binary_path,manager.base_model,manager.mmproj)):
            raise AnalysisError('Для бесплатной повторной проверки нужна установленная локальная Qwen. DeepSeek повторно не вызывался; сохранённый план не потерян.')
        manager.ensure_running();payload=json.loads(messages[-1]['content']);issues=[]
        system=('Проверь научный кадр по точной narration: только конкретные противоречия, выдуманные факты или отсутствующий ключевой объект. '
                'Не требуй повторить озвучку текстом. elements рисует код; стрелка циферблата — часть прибора. '
                'Условие сравнения может звучать только голосом. Замечай конкретные показания приборов и результаты, если они раскрыты раньше времени. '
                'Рамка внимания допустима лишь для видимой детали; не требуй точных координат до готового изображения. '
                'Одна иллюстрация с короткими пояснениями допустима; не требуй две картинки, если layout не comparison. '
                'Верни JSON {"issues":[]}; при реальной ошибке объект {n,code,reason,correction}. '
                'code: meaning,wrong_subject,relation,comparison,instruction,unsupported_claim,unusable_asset,layout,cue. Не придумывай замечания ради замечаний.')
        frames={f['n']:f for f in payload['frames']}
        for shot in payload['shots']:
            keys=shot.get('asset_keys',[])+([shot['support_asset_key']] if shot.get('support_asset_key') else [])
            content={'frame':frames[shot['n']],'shot':shot,'assets':[a for a in payload['assets'] if a['key'] in keys]}
            request=urllib.request.Request(f'http://127.0.0.1:{manager.port}/v1/chat/completions',data=json.dumps({'model':'qwen3-vl','temperature':0,'max_tokens':700,'response_format':{'type':'json_object'},'messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(content,ensure_ascii=False)}]}).encode(),headers={'Content-Type':'application/json'})
            try:
                with manager.inference_lock,urllib.request.urlopen(request,timeout=120) as response:raw=json.load(response)
                result=schema.model_validate(json.loads(raw['choices'][0]['message']['content']))
                if any(issue.n!=shot['n'] for issue in result.issues):raise ValueError('Incorrect scene number')
                issues.extend(result.issues)
            except Exception as error:
                raise AnalysisError('Локальная повторная проверка не завершена. DeepSeek повторно не вызывался; план сохранён.') from error
        return schema(issues=issues)

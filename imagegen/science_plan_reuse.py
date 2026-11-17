"""Reuse an already saved, verified scientific plan for the identical source text."""
import copy,json,hashlib

def current_density(source):
    from .science_explanatory import clean_notes,valid_scheme
    scenes=source.get('scenes',[])
    if not scenes:return False
    for scene in scenes:
        p=scene.get('data',scene).get('science_atlas') or {};density=p.get('frame_density')
        if density=='single_subject':
            if len(str(p.get('single_subject_reason','')).strip())<20:return False
            continue
        if density!='filled':return False
        keys=p.get('asset_keys',[])
        if len(keys)==1:
            notes=clean_notes(p.get('explanatory_labels',p.get('labels') or []),limit=4)
            geometry=[e for e in p.get('elements',[]) if e.get('kind')!='text']
            if len(notes)<2 and not valid_scheme(p.get('explanatory_scheme'),p.get('support_quote','')) and not p.get('support_asset_key') and not p.get('detail_assets') and len(geometry)<2:return False
        elif not keys:
            elements=p.get('elements',[])
            if len(elements)<2 or not any(e.get('kind')!='text' for e in elements):return False
    return True

def plan_hash(source):
    data=[{'science_atlas':s.get('data',s).get('science_atlas'),'source_quote':s.get('data',s).get('source_quote'),'source_start':s.get('data',s).get('source_start'),'source_end':s.get('data',s).get('source_end')} for s in source.get('scenes',[])]
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def find(service,project):
    for row in service.db.all('SELECT id FROM projects WHERE id!=? AND story=? AND visual_format=? AND id NOT IN (SELECT project_id FROM project_trash) ORDER BY created_at DESC',(project['id'],project['story'],'science_atlas')):
        source=service.detail(row['id']);meta=source.get('analysis_metadata') or {};diagnostic=meta.get('diagnostics',{}).get('science_atlas',{})
        if diagnostic.get('version')!='science-staging/3' or not diagnostic.get('complete') or not source['preflight']['ok'] or not current_density(source):continue
        return build(source)
    return None

def build(source):
    if not current_density(source):
        from .domain import DomainError
        raise DomainError('Сохранённая постановка использует старые правила наполнения. Повторное использование без обновления недоступно.',409)
    data={'world':source.get('world',''),'characters':copy.deepcopy(source['characters']),'locations':[],
          'props':[{k:p.get(k) for k in ('name','description','importance','first_appearance','owner','default_state')} for p in source['props'] if p['importance']=='story'],
          'scenes':[copy.deepcopy(s['data']) for s in source['scenes']],'analyzer_version':'deepseek',
          'diagnostics':copy.deepcopy(source['analysis_metadata'].get('diagnostics',{})),
          'deepseek_usage':{'cost_usd':0,'science_atlas_cost_usd':0,'result_cache_hit':True},
          '_verified_science_reuse':{'source_project_id':source['id'],'source_version':source['version'],'source_plan_hash':plan_hash(source),'density_contract_version':1}}
    data['diagnostics']['result_cache_hit']=True
    data['diagnostics']['science_atlas']['reused_from_project']=source['id']
    for s in data['scenes']:s['animation']={'selected_engine':'ken_burns','status':'planned_ken_burns','duration_seconds':6,'source':'science_atlas'}
    from .science_sequence import arrange_analysis
    return arrange_analysis(data)

def verify(service,project,analysis):
    proof=analysis['_verified_science_reuse'];source=service.detail(proof['source_project_id'])
    if (source['story']!=project['story'] or source['version']!=proof['source_version'] or proof.get('source_plan_hash')!=plan_hash(source) or not source['preflight']['ok']):raise ValueError('Проверенный исходный план изменился; повторное использование отменено.')
    return build(source)

"""One source-bound packet shared by prompts, review and timing consumers."""
import json
from .domain import DomainError, fingerprint
from .catalog_evidence import anchor_evidence, properties

VERSION='source-contract/2-plan-precedence'


def source_span(story,data):
    quote=data.get('source_quote')
    if not isinstance(quote,str) or not quote.strip():
        raise DomainError('Для кадра нужна точная цитата сценария.',409)
    start=data.get('source_start')
    inferred=False
    if start is None:
        start=story.find(quote);inferred=True
        if start<0 or story.find(quote,start+1)>=0:
            raise DomainError('Цитата отсутствует или встречается несколько раз. Укажите её точные границы.',409)
    if type(start) is not int:
        raise DomainError('Некорректная начальная граница цитаты.',409)
    end=data.get('source_end',start+len(quote))
    if type(end) is not int or start<0 or end>len(story) or end-start!=len(quote) or story[start:end]!=quote:
        raise DomainError('Границы кадра не совпадают с точной цитатой сценария.',409)
    return {'start':start,'end':end,'quote':quote,'inferred_offsets':inferred}


def coverage(story,scenes):
    spans=[];errors=[];covered=set();previous=-1
    for scene in scenes:
        data=scene.get('data',scene)
        try:span=source_span(story,data)
        except DomainError as error:
            errors.append({'scene_id':scene.get('id'),'reason':str(error)});continue
        reuse=data.get('intentional_visual_reuse') is True and bool(str(data.get('reuse_reason','')).strip())
        if span['start']<previous and not reuse:
            errors.append({'scene_id':scene.get('id'),'reason':'Фрагменты повторяются, пересекаются или идут не по порядку.'})
        if not reuse:previous=span['end']
        covered.update(range(span['start'],span['end']))
        spans.append({**span,'scene_id':scene.get('id'),'intentional_reuse':reuse})
    missing=[i for i,c in enumerate(story) if c.isalnum() and i not in covered]
    gaps=[]
    for i in missing:
        if gaps and i==gaps[-1]['end']:gaps[-1]['end']=i+1
        else:gaps.append({'start':i,'end':i+1})
    for gap in gaps:gap['quote']=story[gap['start']:gap['end']]
    return {'ok':not errors and not gaps,'spans':spans,'errors':errors,'uncovered':gaps,
            'method':'literal_source_coverage','semantic_completeness_measured':False}


def require_complete_coverage(story, scenes):
    """Reject partial plans before they replace a saved project."""
    report = coverage(story, scenes)
    if not report['ok']:
        missing = sum(gap['end']-gap['start'] for gap in report['uncovered'])
        raise DomainError(
            f"План не покрывает весь сценарий: пропущено {missing} букв и цифр; "
            f"ошибок границ или порядка: {len(report['errors'])}. "
            "Неполный план не сохранён. Нужен подробный план по всему сюжету.", 409)
    return report


def passport(project,scenes,*,include_narrative=True):
    story=project['story'];metadata=project.get('analysis_metadata') or {}
    if isinstance(metadata,str):metadata=json.loads(metadata)
    anchors=metadata.get('forgotten_cities',{}).get('anchors',[])
    catalog=[]
    for anchor in anchors:
        evidence = anchor_evidence(story, anchor)
        catalog.append({'id':anchor.get('anchor_id'),'name':anchor.get('canonical_name'),
            'kind':anchor.get('anchor_type'),'time_layer':anchor.get('visual_time_layer') or anchor.get('time_layer'),'aliases':anchor.get('aliases',[]),
            'properties':properties(story, anchor, evidence),'evidence':evidence[0] if evidence else None,
            'evidence_spans':evidence, 'source_kind':anchor.get('source_kind'),
            'identity_scope':anchor.get('identity_scope', 'specific_object'),
            'temporal_context':anchor.get('temporal_context'),
            'description_unverified':anchor.get('description','')})
    report=coverage(story,scenes)
    result={'version':VERSION,'project_id':project['id'],'story_hash':fingerprint(story),
        'events':report['spans'],'catalog':catalog,'coverage':report,
        'claim_policy':'Source quotes are evidence; generated descriptions are unverified data.'}
    if include_narrative:
        from .causal_passport import build
        result['narrative']=build(story,report['spans'])
        result['passport_version']='source-passport/2'
    return {**result,'hash':fingerprint(result)}


def scene_contract(project,scene,references,review_plan=None,catalog_context=None):
    data=scene.get('data',scene);span=source_span(project['story'],data)
    packet=data.get('forgotten_cities_prompt_contract') or {}
    editorial=packet.get('editorial_decision') or {}
    manual=bool(data.get('audit_visual_direction') and data.get('audit_source_quote')==span['quote'])
    direction=data.get('audit_visual_direction') if manual else packet.get('visual_direction') or data.get('composition') or data.get('beat','')
    plan=review_plan or data.get('source_fidelity_plan') or {}
    # Grounding may have deliberately kept the exact source instead of this
    # cached interpretation. Its forbidden rules and subject must not re-enter
    # the outgoing prompt through the final contract.
    from .forgotten_cities.fidelity import plan_precedence, digest
    ignored = 'explicit_manual_direction' if manual and plan else plan_precedence(packet, plan)
    plan_resolution = {'status':'source_precedence', 'reason':ignored,
                       'ignored_plan_hash':digest(plan)} if ignored else None
    if ignored:
        plan = {}
    conflicts=[]
    from .story_reliability import claim_errors
    conflicts.extend(claim_errors({**data,'visual_direction':direction},span['quote']))
    if packet:
        # Explicit/manual approval cannot waive the narrated actors or action.
        # Validate the same final direction used by generation, not just its
        # literal source coverage or an earlier model's pass flag.
        from .narrative_obligations import obligations, errors as obligation_errors
        selected=packet.get('visual_source') or span
        a,b=selected.get('start'),selected.get('end')
        if type(a) is not int or type(b) is not int or project['story'][a:b]!=selected.get('quote'):
            selected=span
        level=data.get('evidence_level') or plan.get('evidence_level')
        if not level and 'SOURCE LEGEND ILLUSTRATION' in str(direction):level='illustrative_hypothesis'
        conflicts.extend(obligation_errors({'expected_people':data.get('visible_people',0),
            'roles':data.get('episodic_people') or [],'visual_direction':direction,
            'subject_state':data.get('subject_state') or plan.get('subject_state'),
            'time_layer':packet.get('visual_time_layer') or data.get('time_layer'),
            'evidence_level':level}, obligations(project['story'],selected)))
        from .narrative_continuity import state_errors
        conflicts.extend(state_errors({'visual_direction':direction},selected['quote']))
    from .scene_semantics import review as semantic_review
    semantic = editorial.get('original_semantic_review') or (semantic_review(project['story'], span, packet) if packet else None)
    resolved=set(editorial.get('resolved_codes') or [])
    if packet.get('editorial_error'):conflicts.append(packet['editorial_error'])
    for check in editorial.get('source_historical_checks', []):
        if check.get('claim') not in editorial.get('omit_claims', []):
            conflicts.append('Историческое утверждение требует редакторского решения: '+check['claim'])
    if semantic and not manual:
        conflicts.extend(item['reason'] for item in semantic['findings'] if item['code'] not in resolved)
    narrative = packet.get('narrative_context')
    if narrative and narrative.get('needs_review') and not manual:
        conflicts.extend(narrative.get('review_reasons') or ['Контекст связанного события требует проверки.'])
    if plan and not manual:
        if plan.get('status')!='pass':conflicts.append('Проверка не подтвердила смысл сцены.')
        layer=packet.get('visual_time_layer')
        if layer and plan.get('time_layer') and layer!=plan['time_layer']:conflicts.append('План проверки и итоговое задание задают разные временные слои.')
        required=set(packet.get('required_artifacts') or [])
        if required and required!=set(plan.get('required_artifacts') or []):conflicts.append('Обязательные предметы проверки и итогового задания различаются.')
    if catalog_context:
        if any(item.get('state')!='bound' for item in catalog_context):conflicts.append('Привязка общего каталога устарела или противоречит сценарию.')
        time_layer=packet.get('visual_time_layer') or data.get('time_layer') or plan.get('time_layer')
        if time_layer and any(item.get('document',{}).get('time_layer') and item['document']['time_layer']!=time_layer for item in catalog_context):conflicts.append('Каталог и сцена задают разные временные состояния.')
        if not manual:
            for item in catalog_context:
                for check in item.get('document', {}).get('historical_checks', []):
                    if check.get('claim') in span['quote'] and check.get('verdict') in ('contradicted', 'uncertain') and check.get('claim') not in editorial.get('omit_claims', []):
                        message = ('Внешняя проверка противоречит утверждению сцены: ' if check['verdict']=='contradicted'
                                   else 'Историческое утверждение требует редакторского решения: ')
                        conflicts.append(message + check['claim'])
    result={'version':VERSION,'project_id':project['id'],'scene_id':scene.get('id'),
        'revision':scene.get('current_revision'),'story_hash':fingerprint(project['story']),'source':span,
        'context':project['story'][max(0,span['start']-800):span['end']],
        'parent_source_context':data.get('semantic_parent_source'),
        'subject':packet.get('primary_subject') or plan.get('subject') or data.get('subject'),
        'action':packet.get('source_action') or data.get('visual_action') or data.get('action'),'direction':direction,
        'period':(data.get('visual_content') or {}).get('period') or data.get('period') or data.get('time'),
        'source_status':(data.get('visual_content') or {}).get('source_status') or data.get('source_status'),
        'spatial_place':(data.get('visual_content') or {}).get('place') or data.get('visual_place') or data.get('location'),
        'subject_state':data.get('subject_state') or plan.get('subject_state'),
        'location':data.get('location_ref') or data.get('location'),
        'time_layer':packet.get('visual_time_layer') or data.get('time_layer') or plan.get('time_layer'),
        'participants':data.get('character_ids') or data.get('characters') or data.get('episodic_people') or [],
        'animals':data.get('source_animals') or [],
        'event_context':packet.get('narrative_context'), 'semantic_review':semantic,
        'visual_source':packet.get('visual_source') or span, 'editorial_decision':editorial or None,
        'required_artifacts':packet.get('required_artifacts') or plan.get('required_artifacts') or [],
        'forbidden':plan.get('forbidden') or [],'references':references,'timing':data.get('voice_timing'),
        'review_plan':plan,'plan_resolution':plan_resolution,'catalog_context':catalog_context or [],'shared_catalog_policy':'Pinned claims retain their original evidence. Current source and explicit editorial decisions take precedence. Historical accuracy and target-image checks are separate.','manual_decision':{'applied':manual,'direction':data.get('audit_visual_direction')},
        'conflicts':list(dict.fromkeys(conflicts)),'status':'needs_resolution' if conflicts else 'bound',
        'precedence':['exact_source_and_explicit_editorial_decision','agreed_scene_contract','style_and_composition'],
        'free_design':['lighting','framing','minor decoration','background headcount unless source-critical']}
    return {**result,'hash':fingerprint(result)}


def render_contract(contract):
    """A render document is not the audit dossier.

    Keep complete explicit visual constraints and an immutable pointer to the
    full evidence. Never serialize the same narration as source, visual_source
    and context into the image prompt. Nothing is cut from the stored dossier.
    """
    import copy
    fields=('subject','action','direction','period','source_status','spatial_place',
            'subject_state','location','time_layer','participants','animals',
            'required_artifacts','forbidden','precedence','free_design',
            'manual_decision','editorial_decision')
    result={key:copy.deepcopy(contract[key]) for key in fields if contract.get(key) not in (None,'',[],{})}
    result.update(version='render-contract/1',audit_contract_hash=contract.get('hash'),
                  evidence_policy='Full exact source and audit history remain in the project. Render only this scene direction; do not enact unrelated narration.')
    plan=contract.get('review_plan') or {}
    required=('subject','action','visual_direction','subject_state','period','time_layer',
              'required_artifacts','required_elements','must_show','must_not_show','forbidden','constraints')
    constraints={key:copy.deepcopy(plan[key]) for key in required if plan.get(key) not in (None,'',[],{})}
    if constraints:result['review_constraints']=constraints
    source=contract.get('visual_source') or contract.get('source') or {}
    quote=source.get('quote','')
    result['source_evidence']={'start':source.get('start'),'end':source.get('end'),
                              'quote_sha256':fingerprint(quote),'full_quote_retained':True}
    if isinstance(quote,str) and len(quote)<=1600:
        result['source_evidence']['quote']=quote
    else:
        result['source_evidence']['quote_in_prompt']=False
    return result

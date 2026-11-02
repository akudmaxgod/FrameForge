"""Durable source review and canonical reference routing for voice-led videos."""
import copy
import json
import re
from .db import dump, now
from .domain import DomainError
from .forgotten_cities.fidelity import SourceFidelityReviewer, digest, source_context
from .documentary_plan_rules import source_constraints, plan_errors

VERSION='voice-editorial/2-source-inventory'
DOCUMENTARY_TIME_LAYERS=['museum_display','excavation','modern_ruins','historical_reconstruction']

def temporal_location_reference(ref,time_layer,locations):
    """Resolve an existing place's unique temporal view, never invent a place."""
    if ref is None:return ''  # JSON mode can spell an optional empty reference as null.
    if not isinstance(ref,str) or not ref:return ref
    def label(value):return ' '.join(value.casefold().replace('—','-').replace('–','-').split())
    normalized={label(name):name for name in locations}
    current=normalized.get(label(ref),ref.strip())
    expected='modern_ruins' if time_layer=='excavation' else time_layer
    if locations.get(current)==expected:return current
    base=re.split(r'\s+[—–-]\s+',current,maxsplit=1)[0].strip().casefold()
    matches=[name for name,layer in locations.items() if layer==expected and
             re.split(r'\s+[—–-]\s+',name,maxsplit=1)[0].strip().casefold()==base]
    return matches[0] if len(matches)==1 else current


def participant_roles(count, roles, *, distinct_slots=False):
    """Give each planned person a distinct slot without adding a new role type."""
    if count == 0:
        return []
    roles = [role.strip() for role in roles if isinstance(role, str) and role.strip()]
    if not roles or len(roles) > count:
        raise DomainError('Число участников не согласовано с описанными ролями.', 409)
    if len(roles) == count and len(set(roles)) == count and not distinct_slots:
        return roles
    bases=[re.sub(r'\s*·\s*участник\s*№\d+\s*$','',role).strip() for role in roles]
    return [f'{bases[index % len(bases)]} · участник №{index + 1}' for index in range(count)]


def critic_format(keys,locations,artifacts):
    schema=SourceFidelityReviewer._plan_schema({'scenes':[{'key':key} for key in keys],
        'anchors':[{'canonical_name':name} for name in artifacts]})
    entry=schema['properties']['scenes']['items']
    entry['properties']['status']['enum']=['pass']
    entry['properties']['time_layer']['enum']=list(DOCUMENTARY_TIME_LAYERS)
    entry['properties']['location_ref']={'type':'string','enum':['',*locations]}
    entry['required'].append('location_ref');entry['additionalProperties']=False
    schema['properties']['checked_keys']={'type':'array','items':{'type':'string','enum':keys},'enum':[keys]}
    schema['required'].append('checked_keys');schema['additionalProperties']=False
    return {'type':'json_schema','json_schema':{'name':'documentary_plan_review','strict':True,'schema':schema}}


def source_plan_format(keys, locations, artifacts):
    result = critic_format(keys, locations, artifacts)
    result['json_schema']['schema']['properties']['scenes']['items']['properties']['status']['enum'] = ['pass','uncertain']
    result['json_schema']['schema']['properties']['scenes']['items']['properties']['time_layer']['enum'].append('')
    return result


def visual_transition(quote):
    """Only commentary may reuse a preceding shot; factual actions cannot."""
    if '?' not in quote and re.search(r'\b(?:поднял\w*|разбил\w*|ударил\w*|вош[её]л\w*|спустил\w*|'
            r'измерил\w*|открыл\w*|собрал\w*|построил\w*|погрузил\w*|обнаружил\w*)\b',quote,re.I):
        return False
    return bool(re.search(r'^\s*(?:обстоятельств\w*|значени\w*|смысл\w*|информаци\w*|вывод\w*)\b|'
        r'^\s*(?:с\s+одной\s+стороны|с\s+другой\s+стороны|с\s+другой\s*[—–-]|'
        r'прич[её]м\s+проверка|она\s+не\s+превращает|нам\s+не\s+нужно\s+приписывать)\b|'
        r'\?\s*$|'
        r'^\s*сегодня\s+мы\s+(?:привыкли|считаем|знаем|понимаем)\b|'
        r'\bмы\s+(?:не\s+)?(?:теряем|потеряем|перестанем\s+различать|видим|знаем|понимаем|вспомним)\b|'
        r'^\s*речь\s+ид[её]т\s+о\b|продолжают\s+исследоваться|'
        r'вопрос\s+(?:посложнее|который)|(?:возник|возникает|вернуться\s+к)\s+.*вопрос|'
        r'^\s*(?:перед\s+нами\s+(?:любопытное|особенно)|для\s+нас\s+это|это\s+(?:уже\s+меняет|меняет|да[её]т)|'
        r'на\s+этом\s+этапе|начн[её]м\s+с\s+самого|название\s+немного|и\s+вот\s+теперь|'
        r'несмотря\s+на\s+это|но\s+дальше\s+возник)|потрясени\w*.*впереди|но это (?:ещ[её] )?не вс[её]',quote,re.I))


class VoiceEditorial:
    def _retry_wait(self,pid,seconds):
        self.workflow.check(pid)
        if self.workflow.stop_event.wait(seconds):raise InterruptedError()
        self.workflow.check(pid)

    def __init__(self, workflow, review_model=None):
        self.workflow=workflow
        self.service=workflow.service
        self.review_model=review_model

    def prepare_presence(self, pid):
        """Persist explicit participant slots before the ordinary project preflight."""
        detail = self.service.detail(pid)
        edits = []
        for scene in detail['scenes']:
            data = copy.deepcopy(scene['data'])
            if type(data.get('audit_people')) is not int:
                continue
            roles = participant_roles(data['audit_people'], data.get('audit_roles') or [])
            if roles == data.get('episodic_people') and roles == data.get('audit_roles'):
                continue
            if scene['active_generation_id'] or scene['video_asset_id']:
                raise DomainError('Состав участников нужно исправить до генерации кадра.', 409)
            data.update(audit_roles=roles, episodic_people=roles,
                        people_policy='anonymous_action' if roles else 'none')
            edits.append((scene, data))
        if not edits:
            return []
        self.workflow.check(pid)
        with self.service.db.transaction() as conn:
            for scene, data in edits:
                current = conn.execute('SELECT current_revision FROM scenes WHERE id=?', (scene['id'],)).fetchone()[0]
                if current != scene['current_revision']:
                    raise DomainError('Кадр изменён во время проверки участников.', 409)
                conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene['id'], current + 1, dump(data), now()))
                conn.execute('UPDATE scenes SET current_revision=? WHERE id=?', (current + 1, scene['id']))
            conn.execute('UPDATE projects SET version=version+1 WHERE id=?', (pid,))
        return [scene['position'] for scene, _ in edits]

    def review(self,pid):
        if self.service._project_profile_type(pid)!='forgotten_cities':return
        detail=self.service.detail(pid)
        from .narrative_contract import verify_applied
        if verify_applied(detail,detail['scenes']):return
        if detail['analysis_metadata'].get('voice_editorial',{}).get('version')==VERSION:return self.refine(pid)
        if any(s['active_generation_id'] or s['video_asset_id'] for s in detail['scenes']):
            raise DomainError('Проверку привязок нужно завершить до генерации кадров.',409)
        StoryAnalyzerV3Pipeline = None
        from .forgotten_cities.geography import ForgottenCitiesGeographyClassifier
        structural=StoryAnalyzerV3Pipeline().analyze_structural(detail['story'])
        metadata=ForgottenCitiesGeographyClassifier().enrich_metadata(structural,detail['story'])
        locations=[a for a in metadata['anchors'] if a['anchor_type']!='artifact' and not a.get('source_kind')]
        artifacts=[a for a in metadata['anchors'] if a['anchor_type']=='artifact']
        settings=self.service.settings()
        story_client = None
        client=story_client(settings,model=self.review_model,timeout=120,
            retry_wait=lambda seconds:self._retry_wait(pid,seconds))
        repair_context=None
        def request(prompt,images=(),**kwargs):
            self.workflow.check(pid)
            instructions,encoded=prompt.split('Data follows:\n',1)
            instructions=instructions.replace('documented_event|museum_display|excavation|modern_ruins|historical_reconstruction|map|abstract_explanation',
                'museum_display|excavation|modern_ruins|historical_reconstruction')
            body=json.loads(encoded)
            body['global_context']={'city':metadata['primary_city_name'],
                'materials':metadata['city_bible']['material_palette'],
                'opening':detail['story'][:400],'conclusion':detail['story'][-400:]}
            body['anchors']=[{'canonical_name':a['canonical_name'],'description':a['description'][:1000]} for a in artifacts]
            for source in body['scenes']:
                source.pop('context',None)
                source['source_constraints']=source_constraints(detail['story'],source)
            instruction=('Additionally include location_ref in EVERY scene, choosing ONLY an exact canonical name from '
                'location_choices below or the empty string. The empty string means no city image reference is useful '
                '(objects, publications, maps, comparison places and scientific details). Never treat an origin, comparison '
                'or mentioned region as the main setting. A Great Bath shot uses the matching Great Bath reference, with '
                'its temporal state matching time_layer. Period fieldwork depicts the narrated year, not modern machinery. '
                'subject_state describes the physical subject, not the image medium. An archival photograph or film '
                'still of researchers must use living/context, not artifact. Artifact means an excavated object itself '
                'and requires zero living people. Keep the actual team and its narrated research year. '
                'An unspecified appearance is not a reason to reject a narrated role: a source-backed anonymous '
                'worker or captain can be shown without inventing a documented likeness. For discussion of an '
                'author describing an object, prefer that actual object over an invented portrait. '
                'A priest-king bust or dancing figurine remains an object, never a living portrait. '
                'Calling a port the gates of a country is a functional metaphor, not evidence of stone gates or arches. '
                'Preserve the actual captain, workers, buyer or seller when the source narrates their actions; do not '
                'replace people with a nearby artifact. A hypothetical transaction must remain hypothetical. '
                'Do not add people to make an image lively. Evidence of destruction can show the specific '
                'collapsed structures described, without invented residents, casualties, fleeing crowds or fire. '
                'Underwater recovery in a bay is not a dry terrestrial excavation trench. '
                'An explicitly described ordinary material or object, such as grain, soil or a wooden board, is '
                'a defensible physical subject even without a named canonical reference. Show its described '
                'properties in one closeup, required_artifacts=[] when no canonical object applies. Absence of '
                'a unique name, exact appearance or explicit date does not by itself justify uncertain: use '
                'the physical subject and period established by the local narration, without invented ownership. '
                'Descriptions of carving, casting, jewelry and weights are not textile work. '
                'For a rhetorical question about absent palaces or temples, show the described excavated urban fabric '
                'without inventing those buildings. For transitions or academic conclusions choose an explicitly described '
                'surrounding site or physical object as a contextual cutaway. Abstract weight systems can be illustrated '
                'by the described physical stone weights, without invented diagrams. Use uncertain only when even a '
                'source-grounded contextual cutaway cannot be identified. Museum artifact details normally have an empty '
                'location_ref; an ancient city reference must never be attached to a museum or excavation time layer. '
                'No readable text, captions, invented labels or diagrams in the image. '
                'A narrated researcher examining a physical map or document is a valid living subject. '
                'Show that person studying the sheet in a period-appropriate research setting, with markings '
                'unreadable. The ban on invented infographic images does not prohibit source-narrated use '
                'of real maps or documents; do not mark such an action uncertain merely because a map appears. '
                'Return all required fields from the original instructions and location_ref. location_choices: '+
                json.dumps([{'name':a['canonical_name'],'time_layer':a['time_layer']} for a in locations],ensure_ascii=False))
            if repair_context:instruction+=' Correct this previous invalid plan using the source context: '+dump(repair_context)
            def submit(sources):
                self.workflow.check(pid)
                first=min(s['start'] for s in sources);last=max(s['end'] for s in sources)
                from .source_windows import excerpt
                payload={**body,'scenes':sources,'story':excerpt(detail['story'],first,last,before=2400,after=1500)}
                content=instructions.replace('FULL story','provided story excerpt and global context')+'Data follows:\n'+dump(payload)
                request_body={'model':client.model,'temperature':0,'max_completion_tokens':4000,
                    'response_format':source_plan_format([s['key'] for s in sources],
                        [a['canonical_name'] for a in locations],[a['canonical_name'] for a in artifacts]),
                    'messages':[{'role':'system','content':instruction},{'role':'user','content':content}]}
                if client.model in ('openai/gpt-oss-120b','openai/gpt-oss-20b'):request_body['reasoning_effort']='low'
                try:
                    raw=client._post_json(request_body)
                except RuntimeError as error:
                    if '413' in str(error) and len(sources)>1:
                        mid=len(sources)//2
                        return submit(sources[:mid])+submit(sources[mid:])
                    if 'json_validate_failed' in str(error):
                        raw=client._post_json({**request_body,'response_format':{'type':'json_object'}})
                    else:
                        raise
                if raw['choices'][0].get('finish_reason')!='stop':
                    raise DomainError('Смысловая проверка не завершила ответ. Готовые результаты сохранены.',503)
                return json.loads(raw['choices'][0]['message']['content'])['scenes']
            entries=[]
            for offset in range(0,len(body['scenes']),4):entries.extend(submit(body['scenes'][offset:offset+4]))
            return {'scenes':entries}
        reviewer=SourceFidelityReviewer(self.service.root,settings,request=request,project_id=pid)
        reviewer.model=('nvidia:' if settings.get('text_backend')=='nvidia' else 'groq:')+client.model+':'+VERSION
        reviewer.key_model=reviewer.model
        self.workflow.update(pid,state='analyzing',stage='Проверяю сюжет, эпохи и привязки всех кадров…',progress=0,error=None)
        plans=reviewer.review_plans([s['data'] for s in detail['scenes']],detail['story'],artifacts,
            lambda done,total:self.workflow.update(pid,stage=f'Проверяю сюжет и локации: {done} из {total}',progress=100*done/total),batch_size=4)
        names={a['canonical_name']:a for a in locations}
        sources={reviewer.key(s['data'],detail['story'],artifacts):source_context(s['data'],detail['story']) for s in detail['scenes']}
        facts={key:source_constraints(detail['story'],source) for key,source in sources.items()}
        def routing_error(plan):
            if plan['status']!='pass':return 'No defensible shot was selected; consider a contextual cutaway grounded in the excerpt.'
            if plan['time_layer'] not in DOCUMENTARY_TIME_LAYERS:return f'time_layer={plan["time_layer"]!r} does not date a physical documentary view. Select the actual period of the depicted source-backed subject.'
            ref=temporal_location_reference(plan.get('location_ref'),plan['time_layer'],{name:a['time_layer'] for name,a in names.items()})
            plan['location_ref']=ref
            if not isinstance(ref,str) or (ref and ref not in names):return f'Unknown location_ref={ref!r}; use an exact allowed name or empty string.'
            expected='modern_ruins' if plan['time_layer']=='excavation' else plan['time_layer']
            if ref and names[ref]['time_layer']!=expected:return f'location_ref={ref!r} has layer {names[ref]["time_layer"]!r}; the scene requires {expected!r}.'
            if plan.get('key') in sources:
                errors=plan_errors(plan,sources[plan['key']],facts[plan['key']],metadata['anchors'])
                if errors:return 'Source constraints violated: '+' '.join(errors)
            return ''
        for index,scene in enumerate(detail['scenes']):
            key=reviewer.key(scene['data'],detail['story'],artifacts)
            problem=routing_error(plans[key])
            for attempt in range(3):
                if not problem:break
                repair_context={'error':problem,'previous_plan':plans[key]}
                self.workflow.update(pid,stage=f'Уточняю смысл и эталон кадра {scene["position"]}')
                try:
                    reviewer._review_plan_batch([(key,source_context(scene['data'],detail['story']))],detail['story'],artifacts,plans,repair=True)
                except DomainError as error:
                    if getattr(error,'diagnostics',{}).get('source_fidelity_key')!=key:raise
                    problem=str(error)
                    repair_context={'error':problem,'previous_plan':reviewer._read('invalid-'+key)}
                    continue
                repair_context=None
                # A purely transitional sentence can hold a verified adjacent
                # image. Keep its source provenance instead of inventing a
                # literal depiction of surprise or an unspecified discovery.
                if plans[key]['status']=='uncertain' and visual_transition(scene['data']['source_quote']):
                    for adjacent in detail['scenes'][max(0,index-2):index][::-1]:
                        other=plans[reviewer.key(adjacent['data'],detail['story'],artifacts)]
                        if not routing_error(other):
                            plans[key]={**copy.deepcopy(other),'key':key,'original_status':'uncertain',
                                'contextual_source_quote':adjacent['data']['source_quote'],
                                'reason':'Contextual cutaway for a transitional sentence; image supported by adjacent quote: '+adjacent['data']['source_quote']}
                            reviewer._write(key,plans[key]);break
                problem=routing_error(plans[key])
            if problem:raise DomainError(f'Кадр {scene["position"]} требует уточнения: '+problem+' '+plans[key]['reason'],409)
            reviewer._write(key,plans[key])
        edits=[];report=[]
        for scene in detail['scenes']:
            self.workflow.check(pid)
            data=copy.deepcopy(scene['data'])
            plan=plans[reviewer.key(data,detail['story'],artifacts)]
            if plan['status']!='pass':raise DomainError(f'Кадр {scene["position"]} требует уточнения: '+plan['reason'],409)
            ref=plan.get('location_ref')
            if not isinstance(ref,str) or (ref and ref not in names):
                raise DomainError(f'Проверка кадра {scene["position"]} вернула неизвестную локацию.',409)
            if ref:
                layer=names[ref]['time_layer']
                expected='modern_ruins' if plan['time_layer']=='excavation' else plan['time_layer']
                if layer!=expected:raise DomainError(f'Эталон кадра {scene["position"]} не соответствует эпохе.',409)
            binding={'artifact':'artifact_detail','buried_remains':'buried_remains','architecture':'architecture'}.get(
                plan['subject_state'],'artifact_action' if plan['required_artifacts'] and plan['expected_people'] else 'context')
            data.update(audit_visual_direction=plan['visual_direction'],audit_source_quote=data['source_quote'],
                audit_required_artifacts=plan['required_artifacts'],audit_time_layer=plan['time_layer'],
                audit_source_binding=binding,audit_people=plan['expected_people'],audit_roles=plan['roles'],
                audit_location_ref=ref,audit_forbidden=plan['forbidden'],audit_subject=plan['subject'],
                audit_context_quote=plan.get('contextual_source_quote',''),
                location_ref=ref,location=plan['subject'],time_layer=plan['time_layer'],time=plan['time_layer'],
                composition=plan['visual_direction'],visible_people=plan['expected_people'],
                raw_visible_people=plan['expected_people'],episodic_people=plan['roles'],
                props=[{'name':name,'state':'placed','visible':True,'continuity':'continues'} for name in plan['required_artifacts']])
            data.pop('source_fidelity_plan',None)
            edits.append((scene,data))
            report.append({'frame':scene['position'],'source_quote':data['source_quote'],'subject':plan['subject'],
                'location_ref':ref,'time_layer':plan['time_layer'],'required_artifacts':plan['required_artifacts'],
                'reason':plan['reason'],'old_location_ref':scene['data'].get('location_ref','')})
            if plan.get('contextual_source_quote'):report[-1]['contextual_source_quote']=plan['contextual_source_quote']
        self.workflow.check(pid)
        required_names={a['canonical_name'] for a in locations}
        unused=[{'id':r['id'],'name':r['name']} for r in detail['locations'] if r['name'] not in required_names]
        audit={'version':VERSION,'checked_frames':len(edits),'model':reviewer.model,'required_location_names':sorted(required_names),
               'models_used':sorted({p.get('model',reviewer.model) for p in plans.values()}),
               'unused_locations':unused,'artifact_names':[a['canonical_name'] for a in artifacts],
               'scene_plan':report,'cloud_calls':client.total_usage['calls']}
        with self.service.db.transaction() as conn:
            if self.service.project(pid)['version']!=detail['version']:raise DomainError('Проект изменён во время проверки.',409)
            from .forgotten_cities.source_inventory import reconcile_reference_inventory
            reconcile_reference_inventory(conn,pid,metadata)
            for scene,data in edits:
                current=conn.execute('SELECT current_revision FROM scenes WHERE id=?',(scene['id'],)).fetchone()[0]
                if current!=scene['current_revision']:raise DomainError('Кадр изменён во время проверки.',409)
                revision=current+1
                conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(scene['id'],revision,dump(data),now()))
                conn.execute('UPDATE scenes SET current_revision=? WHERE id=?',(revision,scene['id']))
            full=detail['analysis_metadata'];full['forgotten_cities']=metadata;full['voice_editorial']=audit
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?',(dump(full),pid))
        directory=self.service.root/'voice'/pid;directory.mkdir(parents=True,exist_ok=True)
        (directory/'editorial.json').write_text(dump(audit),encoding='utf-8')
        self.refine(pid)

    def refine(self,pid):
        """Critique the completed plan, including semantic reference relevance."""
        detail=self.service.detail(pid)
        audit=detail['analysis_metadata']['voice_editorial']
        if audit.get('refinement_version')=='voice-plan-critic/1':return self.finalize(pid)
        if any(s['active_generation_id'] or s['video_asset_id'] for s in detail['scenes']):
            raise DomainError('Проверку плана нужно завершить до генерации кадров.',409)
        settings=self.service.settings()
        story_client = None
        client=story_client(settings,model=self.review_model,timeout=120,
            retry_wait=lambda seconds:self._retry_wait(pid,seconds))
        metadata=detail['analysis_metadata']['forgotten_cities']
        locations={a['canonical_name']:a['time_layer'] for a in metadata['anchors'] if a['anchor_type']!='artifact' and not a.get('source_kind')}
        artifacts={a['canonical_name'] for a in metadata['anchors'] if a['anchor_type']=='artifact'}
        directory=self.service.root/'voice'/pid/'plan_reviews';directory.mkdir(parents=True,exist_ok=True)
        plans={};changed=set()
        instruction=('Critique a historical documentary image plan against its actual quote and surrounding narration. '
            'Return JSON {"checked_keys":[every input key],"scenes":[ONLY corrected plans]}. Corrected plans must include '
            'key,status=pass,subject,subject_state=living|buried_remains|artifact|architecture|context,visual_direction,'
            'forbidden (string array),expected_people (integer 0..6),roles (string array),required_artifacts (exact allowed names),'
            'time_layer=museum_display|excavation|modern_ruins|historical_reconstruction,location_ref,reason. '
            'Inspect every plan. A reference must match the depicted physical SUBJECT and time, not merely a nearby mention. '
            'A port called the gates of a country does not establish physical gates or arches; use the actual port '
            'and maritime approach described in the source. Retain source-narrated participants and their actions '
            'instead of replacing them with neighboring artifacts. Distinguish hypothetical imagined transactions '
            'from attested events. Underwater archaeological recovery is not a dry terrestrial trench. '
            'Do not add residents, fleeing crowds, victims or fire to archaeological evidence of destruction '
            'unless the source actually states them. Show the described collapsed blocks or structures. '
            'Ordinary materials and generic physical objects explicitly described in the quote can be shown '
            'without a canonical artifact reference; required_artifacts=[] is valid for such details. '
            'subject_state describes the physical subject, not the image medium. An archival view of researchers '
            'has living/context subjects, not artifact. Artifact/buried_remains closeups have zero living people. '
            'A river or dried riverbed cannot use a swimming reservoir reference. Closeup objects, maps, unrelated regions and '
            'laboratory research normally need location_ref="". Do not substitute unrelated statues or beads for migration, '
            'urban society or environmental change. Use the locally narrated action/object, or a relevant nearby contextual '
            'cutaway for abstract sentences. Modern scholars are not ancient residents; use the discussed object or actual '
            'research era. If "today" describes a monument, show present archaeological state. Capacity to hold water does '
            'not establish that the modern reservoir is currently filled. Avoid invented rulers, palaces, battles and rituals '
            'when narration denies or merely hypothesizes them: show the described physical evidence. One coherent '
            'photoreal documentary still, no collage, written captions, diagrams, graphs, infographics or invented labels. '
            'For diagrams/maps in old plans choose the described physical landscape, strata or object instead. Preserve '
            'a source-narrated person examining an actual map or document as a physical research scene, '
            'with unreadable markings; do not replace this narrated action with an infographic or empty landscape. '
            'useful correct plans. A transition can hold an adjacent grounded view. Artifact/buried_remains means zero living '
            'people. Exact location choices with temporal states: '+dump(locations)+'. Artifact choices: '+dump(sorted(artifacts)))
        def request_critic(payload):
            if client.model in ('openai/gpt-oss-120b','openai/gpt-oss-20b'):payload={**payload,'reasoning_effort':'low'}
            try:raw=client._post_json(payload)
            except RuntimeError as error:
                # Some provider deployments reject their own constrained output.
                # One JSON-mode fallback still goes through our full validation.
                if 'json_validate_failed' not in str(error):raise
                raw=client._post_json({**payload,'response_format':{'type':'json_object'}})
            if raw['choices'][0].get('finish_reason')=='length':
                body=json.loads(payload['messages'][-1]['content']);sources=body['scenes']
                if len(sources)>1:
                    combined={'checked_keys':[],'scenes':[]};mid=len(sources)//2
                    for part in (sources[:mid],sources[mid:]):
                        small={**body,'scenes':part,'full_correction_shape':part[0]['plan']}
                        answer=request_critic({**payload,'response_format':critic_format([s['key'] for s in part],locations,artifacts),
                            'messages':[*payload['messages'][:-1],{'role':'user','content':dump(small)}]})
                        if answer['choices'][0].get('finish_reason')!='stop':return answer
                        result=json.loads(answer['choices'][0]['message']['content'])
                        for field in combined:combined[field].extend(result[field])
                    return {'choices':[{'finish_reason':'stop','message':{'content':dump(combined)}}]}
            return raw
        for offset in range(0,len(detail['scenes']),4):
            self.workflow.check(pid)
            batch=detail['scenes'][offset:offset+4];inputs=[]
            constraints={s['id']:source_constraints(detail['story'],source_context(s['data'],detail['story'])) for s in batch}
            for scene in batch:
                data=scene['data'];source=source_context(data,detail['story'])
                plan={'key':scene['id'],'status':'pass','subject':data['audit_subject'],
                    'subject_state':{'artifact_detail':'artifact','buried_remains':'buried_remains','architecture':'architecture'}.get(data['audit_source_binding'],'living' if data['audit_people'] else 'context'),
                    'visual_direction':data['audit_visual_direction'],'forbidden':data['audit_forbidden'],
                    'expected_people':data['audit_people'],'roles':data['audit_roles'],
                    'required_artifacts':data['audit_required_artifacts'],'time_layer':data['audit_time_layer'],
                    'location_ref':data['audit_location_ref'],'reason':audit['scene_plan'][scene['position']-1]['reason']}
                plans[scene['id']]=plan
                inputs.append({'key':scene['id'],'quote':source['quote'],
                    'context':detail['story'][max(0,source['start']-450):source['end']+450],'plan':plan})
            path=directory/(digest({'version':'voice-plan-critic/1','inputs':inputs,'instructions':instruction})+'.json')
            self.workflow.update(pid,state='analyzing',stage=f'Перепроверяю готовый план: {offset} из {len(detail["scenes"])}',progress=100*offset/len(detail['scenes']),error=None)
            if path.exists():result=json.loads(path.read_text(encoding='utf-8'))
            else:
                from .source_windows import excerpt
                first=min(s['data']['source_start'] for s in batch)
                last=max(s['data']['source_end'] for s in batch)
                # Consecutive windows overlap heavily. Their union preserves
                # all local evidence without sending the same sentences four times.
                payload={'context':excerpt(detail['story'],first,last,before=450,after=450),
                         'primary_city':metadata.get('primary_city_name',''),
                         'object_specs':[{'name':a['canonical_name'],'source_description':a['description']}
                            for a in metadata['anchors'] if a['anchor_type']=='artifact' and
                            any(a['canonical_name'] in item['plan']['required_artifacts'] for item in inputs)],
                         'scenes':[{**{k:v for k,v in item.items() if k!='context'},
                                    'source_constraints':constraints[item['key']]} for item in inputs]}
                raw=request_critic({'model':client.model,'temperature':0,'max_completion_tokens':3000,
                    'response_format':critic_format([s['id'] for s in batch],locations,artifacts),'messages':[{'role':'system','content':instruction},
                    {'role':'user','content':dump(payload)}]})
                if raw['choices'][0].get('finish_reason')!='stop':raise DomainError('Повторная проверка не завершила ответ.',503)
                result=json.loads(raw['choices'][0]['message']['content'])
            keys=[s['id'] for s in batch]
            if not isinstance(result,dict) or not isinstance(result.get('checked_keys'),list) or sorted(result['checked_keys'])!=sorted(keys) or not isinstance(result.get('scenes'),list):
                raw=request_critic({'model':client.model,'temperature':0,'max_completion_tokens':3000,
                    'response_format':critic_format(keys,locations,artifacts),'messages':[{'role':'system','content':instruction+
                    ' The previous result omitted checked keys. You must inspect all these scenes and return checked_keys '
                    'EXACTLY equal to '+dump(keys)+'. Include every key even for unchanged scenes.'},
                    {'role':'user','content':dump({'scenes':inputs,'full_correction_shape':inputs[0]['plan']})}]})
                if raw['choices'][0].get('finish_reason')!='stop':raise DomainError('Повторная проверка не завершила ответ.',503)
                result=json.loads(raw['choices'][0]['message']['content'])
                if not isinstance(result,dict) or not isinstance(result.get('checked_keys'),list) or sorted(result['checked_keys'])!=sorted(keys) or not isinstance(result.get('scenes'),list):
                    raise DomainError('Повторная проверка не покрыла все кадры. Готовые результаты сохранены.',503)
            replacements={}
            def validate(entry):
                # A critic may return only changed fields; untouched fields keep
                # the already checked original plan, never invented defaults.
                plan=SourceFidelityReviewer._normalize_plan({**plans.get(entry.get('key'),{}),**entry},artifacts)
                SourceFidelityReviewer._validate_plan(plan,artifacts)
                if plan['time_layer'] not in DOCUMENTARY_TIME_LAYERS:
                    raise DomainError('A physical documentary view requires the actual time of its subject, not a map or abstract explanation.',503)
                plan['location_ref']=temporal_location_reference(plan.get('location_ref'),plan['time_layer'],locations)
                ref=plan.get('location_ref');expected='modern_ruins' if plan['time_layer']=='excavation' else plan['time_layer']
                if ref and locations.get(ref)!=expected:
                    raise DomainError(f'location_ref={ref!r} has layer {locations.get(ref)!r}; the scene requires {expected!r}. Use a matching exact name or empty string.',503)
                if plan.get('key') not in keys or plan['key'] in replacements or plan['status']!='pass' or not isinstance(ref,str):
                    raise DomainError('Повторная проверка вернула неверную привязку кадра.',503)
                source=source_context(next(s['data'] for s in batch if s['id']==plan['key']),detail['story'])
                errors=plan_errors(plan,source,constraints[plan['key']],metadata['anchors'])
                if errors:raise DomainError('Source constraints violated: '+' '.join(errors),503)
                return plan
            returned={entry.get('key'):entry for entry in result['scenes']}
            if len(returned)!=len(result['scenes']) or set(returned)-set(keys):
                raise DomainError('Повторная проверка вернула неизвестный или повторный кадр.',503)
            # Validate unchanged plans too: checked_keys alone does not prove
            # that the critic respected source-backed factual constraints.
            validated=[]
            for key in keys:
                entry=returned.get(key,plans[key])
                try:plan=validate(entry)
                except DomainError as error:
                    source=next((item for item in inputs if item['key']==entry.get('key')),None)
                    if source is None:raise
                    raw=request_critic({'model':client.model,'temperature':0,'max_completion_tokens':1600,
                        'response_format':critic_format([source['key']],locations,artifacts),'messages':[{'role':'system','content':instruction+
                        ' Repair the invalid result. An artifact closeup or buried remains must have expected_people=0 '
                        'and roles=[]. If people perform a narrated action with an object, subject_state must be living '
                        'or context, with actual count and roles. Create a fresh replacement from the SELECTED '
                        'quote and source evidence. The validation error states a prohibition to satisfy; it is '
                        'not requested image content. Do not restate the prohibited imagery. Return exactly one corrected scene.'},
                        {'role':'user','content':dump({'scenes':[{k:v for k,v in source.items() if k!='plan'}],
                            'primary_city':metadata.get('primary_city_name',''),
                            'source_constraints':constraints[source['key']], 'validation_error':str(error)})}]})
                    if raw['choices'][0].get('finish_reason')!='stop':raise DomainError('Исправление плана не завершено.',503)
                    repair=json.loads(raw['choices'][0]['message']['content'])
                    if not isinstance(repair.get('scenes'),list) or len(repair['scenes'])!=1:raise DomainError('Не удалось исправить план кадра.',503)
                    try:plan=validate(repair['scenes'][0])
                    except DomainError as failure:
                        path.with_suffix('.invalid').write_text(dump({'scene':source,'invalid_result':repair['scenes'][0],
                            'validation_error':str(failure),'model':client.model}),encoding='utf-8')
                        raise DomainError(f'Кадр {next(s["position"] for s in batch if s["id"]==key)}: '+str(failure),503) from failure
                    plan['repaired_by_model']=client.model
                    # Keep a successful targeted correction even if another
                    # scene of the same batch later needs repair.
                    result['scenes']=[e for e in result['scenes'] if e.get('key')!=key]+[plan]
                    path.write_text(dump(result),encoding='utf-8')
                validated.append(plan)
                replacements[plan['key']]=plan
            result['scenes']=validated
            result.setdefault('review_model',client.model)
            path.write_text(dump(result),encoding='utf-8')
            changed.update(key for key,plan in replacements.items() if any(
                plan.get(field)!=plans[key].get(field) for field in ('subject','subject_state','visual_direction',
                    'forbidden','expected_people','roles','required_artifacts','time_layer','location_ref','reason')))
            plans.update(replacements)
        self.workflow.check(pid)
        report=[]
        with self.service.db.transaction() as conn:
            if self.service.project(pid)['version']!=detail['version']:raise DomainError('Проект изменён во время проверки.',409)
            for scene in detail['scenes']:
                plan=plans[scene['id']];data=copy.deepcopy(scene['data'])
                binding={'artifact':'artifact_detail','buried_remains':'buried_remains','architecture':'architecture'}.get(plan['subject_state'],'artifact_action' if plan['required_artifacts'] and plan['expected_people'] else 'context')
                if scene['id'] in changed:
                    data.update(audit_visual_direction=plan['visual_direction'],audit_required_artifacts=plan['required_artifacts'],
                        audit_time_layer=plan['time_layer'],audit_source_binding=binding,audit_people=plan['expected_people'],
                        audit_roles=plan['roles'],audit_location_ref=plan['location_ref'],audit_forbidden=plan['forbidden'],audit_subject=plan['subject'],
                        location_ref=plan['location_ref'],location=plan['subject'],time_layer=plan['time_layer'],time=plan['time_layer'],
                        composition=plan['visual_direction'],subject_state=plan['subject_state'],visible_people=plan['expected_people'],
                        raw_visible_people=plan['expected_people'],episodic_people=plan['roles'],
                        props=[{'name':name,'state':'placed','visible':True,'continuity':'continues'} for name in plan['required_artifacts']])
                    current=conn.execute('SELECT current_revision FROM scenes WHERE id=?',(scene['id'],)).fetchone()[0]
                    if current!=scene['current_revision']:raise DomainError('Кадр изменён во время проверки.',409)
                    conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(scene['id'],current+1,dump(data),now()))
                    conn.execute('UPDATE scenes SET current_revision=? WHERE id=?',(current+1,scene['id']))
                previous=audit['scene_plan'][scene['position']-1]
                report.append({**previous,'subject':plan['subject'],'location_ref':plan['location_ref'],'time_layer':plan['time_layer'],
                    'required_artifacts':plan['required_artifacts'],'reason':plan['reason']})
            used_locations={p['location_ref'] for p in plans.values() if p['location_ref']}
            used_artifacts={name for p in plans.values() for name in p['required_artifacts']}
            for anchor in metadata['anchors']:
                anchor['reference_required']=anchor['canonical_name'] in (used_artifacts if anchor['anchor_type']=='artifact' else used_locations)
            audit.update(refinement_version='voice-plan-critic/1',refined_frames=len(detail['scenes']),corrected_frames=len(changed),
                required_location_names=sorted(used_locations),artifact_names=sorted(used_artifacts),scene_plan=report,
                unused_locations=[{'id':r['id'],'name':r['name']} for r in detail['locations'] if r['name'] not in used_locations])
            full=detail['analysis_metadata'];full['voice_editorial']=audit;full['forgotten_cities']=metadata
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?',(dump(full),pid))
        (directory.parent/'editorial.json').write_text(dump(audit),encoding='utf-8')
        self.finalize(pid)

    def finalize(self,pid):
        from .voice_reference_rules import guard_plan
        detail=self.service.detail(pid);full=detail['analysis_metadata'];audit=full['voice_editorial']
        if audit.get('guard_version')=='voice-reference-rules/1':return
        metadata=full['forgotten_cities'];edits=[];report=copy.deepcopy(audit['scene_plan'])
        for index,scene in enumerate(detail['scenes']):
            data=copy.deepcopy(scene['data']);row=report[index]
            original={'subject':data['audit_subject'],'visual_direction':data['audit_visual_direction'],
                'subject_state':{'artifact_detail':'artifact','buried_remains':'buried_remains','architecture':'architecture'}.get(data['audit_source_binding'],'living' if data['audit_people'] else 'context'),
                'required_artifacts':data['audit_required_artifacts'],'time_layer':data['audit_time_layer'],
                'location_ref':data['audit_location_ref'],'expected_people':data['audit_people'],
                'roles':data['audit_roles'],'forbidden':data['audit_forbidden'],'reason':row['reason']}
            previous=detail['scenes'][index-1]['data']['source_quote'] if index else ''
            plan=guard_plan(original,data['source_quote'],previous,metadata['anchors'])
            if plan==original:continue
            binding={'artifact':'artifact_detail','buried_remains':'buried_remains','architecture':'architecture'}.get(plan['subject_state'],'context')
            data.update(audit_subject=plan['subject'],audit_visual_direction=plan['visual_direction'],
                audit_required_artifacts=plan['required_artifacts'],audit_time_layer=plan['time_layer'],
                audit_location_ref=plan['location_ref'],audit_people=plan['expected_people'],audit_roles=plan['roles'],
                audit_forbidden=plan['forbidden'],audit_source_binding=binding,
                location_ref=plan['location_ref'],location=plan['subject'],time_layer=plan['time_layer'],time=plan['time_layer'],
                composition=plan['visual_direction'],subject_state=plan['subject_state'],visible_people=plan['expected_people'],
                raw_visible_people=plan['expected_people'],episodic_people=plan['roles'],
                props=[{'name':name,'state':'placed','visible':True,'continuity':'continues'} for name in plan['required_artifacts']])
            if plan.get('contextual_source_quote'):data['audit_context_quote']=plan['contextual_source_quote']
            for field in ('subject','required_artifacts','time_layer','location_ref','reason'):row[field]=plan[field]
            if plan.get('contextual_source_quote'):row['contextual_source_quote']=plan['contextual_source_quote']
            edits.append((scene,data))
        self.workflow.check(pid)
        used_locations={r['location_ref'] for r in report if r['location_ref']}
        used_artifacts={name for r in report for name in r['required_artifacts']}
        for anchor in metadata['anchors']:
            anchor['reference_required']=anchor['canonical_name'] in (used_artifacts if anchor['anchor_type']=='artifact' else used_locations)
        audit.update(guard_version='voice-reference-rules/1',guard_corrected_frames=[s['position'] for s,_ in edits],scene_plan=report,
            required_location_names=sorted(used_locations),artifact_names=sorted(used_artifacts),
            unused_locations=[{'id':r['id'],'name':r['name']} for r in detail['locations'] if r['name'] not in used_locations])
        with self.service.db.transaction() as conn:
            if self.service.project(pid)['version']!=detail['version']:raise DomainError('Проект изменён во время проверки.',409)
            for scene,data in edits:
                current=conn.execute('SELECT current_revision FROM scenes WHERE id=?',(scene['id'],)).fetchone()[0]
                if current!=scene['current_revision']:raise DomainError('Кадр изменён во время проверки.',409)
                conn.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(scene['id'],current+1,dump(data),now()))
                conn.execute('UPDATE scenes SET current_revision=? WHERE id=?',(current+1,scene['id']))
            conn.execute('UPDATE projects SET analysis_metadata=?,version=version+1 WHERE id=?',(dump(full),pid))
        (self.service.root/'voice'/pid/'editorial.json').write_text(dump(audit),encoding='utf-8')

    def reference_verdict(self,pid,anchor,asset_id,target=None):
        from .review_policy import visual_reviews_enabled, manual_review
        if not visual_reviews_enabled():
            return manual_review()
        asset=self.service.asset(asset_id)
        key=digest({'version':VERSION,'asset_sha256':asset['sha256'],'anchor':anchor})
        directory=self.service.root/'voice'/pid/'reference_reviews';directory.mkdir(parents=True,exist_ok=True)
        path=directory/(key+'.json')
        durable=self.service.settings().get('google_queue_enabled') is True or any(directory.glob('*.job.json'))
        if not durable and path.exists():return json.loads(path.read_text(encoding='utf-8'))
        prompt=('Review this candidate reference image for a historical documentary. Inspect actual visible pixels. '
            'Does it depict the specified place or artifact with the stated materials, scale and temporal state? '
            'A city master must be a broad outdoor urban view, never a furnished living room. Modern ruins must '
            'be an outdoor archaeological site. A carved statue is an object, not a living human. A Great Bath '
            'must show the specified brick reservoir, geometry, steps and appropriate historical/ruin state. '
            'Reject unsupported modern furnishings, anachronisms, wrong subjects or different materials. '
            'Return JSON {"status":"pass" or "fail","score":integer 0..100,"reason":"brief explanation",'
            '"correction":"concrete correction if needed"}. Source specification: '+dump(anchor))
        if durable:
            from .google_reference_reviews import ReferenceReviews
            self.workflow.check(pid)
            return ReferenceReviews(self.service).check(pid,anchor,asset_id,prompt,target)
        reviewer=SourceFidelityReviewer(self.service.root,self.service.settings(),project_id=pid)
        pixels=(self.service.root/asset['path']).read_bytes()
        for attempt in range(3):
            self.workflow.check(pid)
            try:
                raw=reviewer.request(prompt,[pixels]);break
            except DomainError as error:
                if attempt==2 or not re.search(r'HTTP (502|503|504)',str(error)):raise
                self.workflow.update(pid,stage='Проверка эталона временно недоступна. Повторяю проверку сохранённого изображения…')
                if self.workflow.stop_event.wait(5*(attempt+1)):raise InterruptedError()
        if (not isinstance(raw,dict) or raw.get('status') not in ('pass','fail') or
            type(raw.get('score')) is not int or not 0<=raw['score']<=100 or not isinstance(raw.get('reason'),str)):
            raise DomainError('Сервис не смог проверить содержимое эталона. Выбор остановлен.',503)
        path.write_text(dump(raw),encoding='utf-8')
        return raw

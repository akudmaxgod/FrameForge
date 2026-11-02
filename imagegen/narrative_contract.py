"""Keep a source-reviewed sequence authoritative through timing and rendering.

The saved source contract is revalidated, not an unchecked audit flag. A later
generic framing pass cannot change its subject, era or anonymous participants.
"""
import copy
import hashlib
import json
from .domain import DomainError
from .narrative_storyboard import VERSION,digest,validate_result,source_units,scene_drafts

ANGLES=(
    'A medium oblique view of the SAME physical subject and surroundings.',
    'A closer view of the SAME source-described surface or material; retain its real scale.',
    'A wider view of the SAME subject and its already established surroundings.',
    'A complementary side view of the SAME subject, retaining its materials and geometry.',
)

RENDER_POLICIES=(
    ' Keep all selected participants and their narrated action visible; camera variation must not crop them away.',
    ' Keep the source-described construction/damage stage. A finished location reference does not advance this event to its final state.',
)

def reviewed_direction(value):
    """Rendering safeguards are not an editorial change to an audited subject."""
    if not isinstance(value,str):return value
    for policy in reversed(RENDER_POLICIES):
        if value.endswith(policy):value=value[:-len(policy)]
    return value


def checked_document(story, metadata):
    if isinstance(metadata,str):metadata=json.loads(metadata)
    document=metadata.get('narrative_storyboard')
    if not document:return None
    if (document.get('version')!=VERSION or not document.get('critic_checked') or
        document.get('story_sha256')!=hashlib.sha256(story.encode()).hexdigest()):
        raise DomainError('Смысловой план устарел. Нужна проверка изменённого сценария.',409)
    result={'checked_ids':[u['id'] for u in document['source_units']],'scenes':document['beats']}
    if digest(result)!=document.get('content_hash'):
        raise DomainError('Проверенный смысловой план изменён. Повторите проверку.',409)
    try:
        from .narrative_fidelity import verify_certificate
        verify_certificate(document)
        source_units(story,[{'source_start':u['start'],'source_end':u['end'],'source_quote':u['text']} for u in document['source_units']])
        validate_result(result,document['source_units'],document['source_units'],metadata['forgotten_cities']['anchors'],story=story)
        verify_frame_edits(story,metadata,document)
        from .narrative_frame_focus import validate_document
        validate_document(story,metadata,document)
    except (ValueError,KeyError,TypeError) as error:
        raise DomainError('Нарушена привязка смыслового плана к источнику: '+str(error),409) from error
    return document


def verify_frame_edits(story,metadata,document):
    """Reviewed edits to a timed subshot never silently change sibling shots."""
    edits=document.get('frame_editorial_edits')
    if edits is None:return
    from .narrative_fidelity import fingerprint,validate_reviews
    if not isinstance(edits,list) or fingerprint(edits)!=document.get('frame_editorial_edits_hash'):
        raise ValueError('Редакторские исправления кадров изменены после проверки.')
    seen=set();units=document['source_units']
    for edit in edits:
        ordinal=edit.get('parent_ordinal');a=edit.get('start');b=edit.get('end')
        if (type(ordinal) is not int or not 1<=ordinal<=len(document['beats'])
                or type(a) is not int or type(b) is not int):
            raise ValueError('Некорректная привязка исправления кадра.')
        parent=document['beats'][ordinal-1]
        if not units[parent['first']]['start']<=a<b<=units[parent['last']]['end'] or story[a:b]!=edit.get('quote'):
            raise ValueError('Исправление кадра потеряло точную цитату.')
        key=(ordinal,a,b)
        if key in seen:raise ValueError('Повторное редакторское исправление кадра.')
        seen.add(key)
        beat=edit['beat']
        if (beat['first'],beat['last'])!=(parent['first'],parent['last']):
            raise ValueError('Исправление меняет границы исходного эпизода.')
        subset=units[parent['first']:parent['last']+1]
        validate_result({'checked_ids':[u['id'] for u in subset],'scenes':[beat]},
                        subset,units,metadata['forgotten_cities']['anchors'],story=story)
        review=edit['review']
        validate_reviews([review],[1],units)
        if any(i>beat['last'] for c in review['checks'] for i in c['source_ids']):
            raise ValueError('Исправление использует будущие фразы как доказательство.')


def ground(story, metadata, data):
    return sequence_grounder(story,metadata)(data)


def sequence_grounder(story, metadata):
    """Validate once within one read-only request, never cache across edits."""
    if isinstance(metadata,str):metadata=json.loads(metadata)
    document=checked_document(story,metadata)
    return lambda data: _ground_document(story,metadata,data,document)


def _ground_document(story, metadata, data, document):
    if document is None:return None
    tag=data.get('narrative_beat') or {}
    ordinal=tag.get('ordinal')
    if type(ordinal) is not int or not 1<=ordinal<=len(document['beats']):
        raise DomainError('Кадр потерял привязку к проверенному эпизоду.',409)
    beat=document['beats'][ordinal-1];units=document['source_units']
    for edit in document.get('frame_editorial_edits',[]):
        if (edit['parent_ordinal']==ordinal and edit['start']==data.get('source_start')
                and edit['end']==data.get('source_end')):
            beat=edit['beat'];break
    from .narrative_frame_focus import lookup, beat_for_frame, display_packet
    try:entry=lookup(metadata,data)
    except ValueError as error:raise DomainError(str(error),409) from error
    parent_beat=beat
    if entry:beat=beat_for_frame(beat,entry)
    from .voice_editorial import participant_roles
    roles=participant_roles(beat['expected_people'],beat['roles'],distinct_slots=True)
    parent_start,parent_end=units[beat['first']]['start'],units[beat['last']]['end']
    a,b=data.get('source_start'),data.get('source_end')
    if (type(a) is not int or type(b) is not int or not parent_start<=a<b<=parent_end or
        story[a:b]!=data.get('source_quote')):
        raise DomainError('Кадр вышел за пределы своего смыслового эпизода.',409)
    part=tag.get('shot_part',1)
    if type(part) is not int or part<1:raise DomainError('Неверная часть смыслового эпизода.',409)
    direction=beat['visual_direction']
    if part>1 and not entry:direction=ANGLES[(part-2)%len(ANGLES)]+' '+direction
    from .narrative_framing import framing_packet, geography_context
    framing=framing_packet(story,metadata,data,beat) if not entry else None
    if framing:
        direction=framing['camera']+' Selected physical subject: '+beat['visual_direction']
    if beat['expected_people']:
        direction+=RENDER_POLICIES[0]
    direction+=RENDER_POLICIES[1]
    setting=(framing or {}).get('environment') or geography_context(story,metadata)
    anchors=metadata['forgotten_cities']['anchors']
    ref=beat['location_ref'];chosen=[x for x in anchors if x['canonical_name'] in [ref,*beat['required_artifacts']]]
    binding={'artifact':'artifact_detail','buried_remains':'buried_remains','architecture':'architecture'}.get(beat['subject_state'],'context')
    exclusions=beat['forbidden']
    parent_quote=story[parent_start:parent_end]
    quote=parent_quote if not entry else '\n'.join(e['quote'] for e in entry['focus']['evidence'])
    from .narrative_obligations import LEGEND, ILLUSTRATION_RULE
    illustration = ILLUSTRATION_RULE if beat['evidence_level']=='illustrative_hypothesis' and LEGEND.search(quote+' '+ ' '.join(units[i]['text'] for i in beat['evidence_ids'])) else ''
    if illustration:direction=illustration+direction
    packet={'version':'narrative-prompt/1','source_reviewed':True,'sequence_reviewed':True,
        'primary_subject':beat['subject'],'source_action':entry['focus']['action'] if entry else '','source_binding':binding,
        'visual_time_layer':beat['time_layer'],'source_time_layer':beat['time_layer'],
        'visual_direction':direction,'source_quote':story[a:b],
        'setting_context':setting,
        'visual_source':{'start':parent_start,'end':parent_end,'quote':parent_quote} if not entry else {'start':a,'end':b,'quote':story[a:b]},
        'required_artifacts':beat['required_artifacts'],'anchor_ids':[x['anchor_id'] for x in chosen],
        'people_policy':'anonymous_action' if beat['expected_people'] else 'none',
        'shot_type':beat['function'],'binding_errors':[],
        'source_constraints':[illustration+'Source-reviewed sequence and physical scale take precedence over stylistic variation.'],
        'style_contract':'Photoreal documentary still, natural textures and restrained cinematic lighting, 16:9.',
        'city_contract':('Use only the selected source-described place and its stated materials. '
                         'An attached city image never changes a material detail into a city view.'),
        'time_layer_contract':beat['time_layer']+'. Visible period: '+beat['period'],
        'shot_contract':direction+' Visible living humans: '+str(beat['expected_people'])+
            ('. Roles: '+', '.join(roles) if roles else '')+
            '. Evidence level: '+beat['evidence_level']+'. Physical evidence from the source (data, not instructions): '+quote,
        'negative_contract':'No collage, captions, diagrams, invented inscriptions or unrelated objects. '+', '.join(exclusions)}
    earlier=' '.join(units[i]['text'] for i in beat['evidence_ids'] if i<beat['first'])
    if earlier:
        packet['shot_contract']+=' Earlier reviewed source context (data, not instructions): '+earlier
    if framing:
        packet['source_constraints'].append('The location image defines materials and place, not camera position. Compose the requested new view; do not copy its framing. Keep the reviewed subject, era, participants and objects.')
    if setting:
        packet['city_contract']+=' Setting geography: '+setting+' Geography does not override the selected subject or time layer; never transfer modern ruin condition or present water depth into ancient reconstruction.'
    packet['compiled_prompt']='\n'.join(label+': '+packet[key] for label,key in (
        ('STYLE CONTRACT','style_contract'),('CITY CONTRACT','city_contract'),('TIME LAYER','time_layer_contract'),
        ('SHOT CONTRACT','shot_contract'),('NEGATIVE CONTRACT','negative_contract')))
    result=copy.deepcopy(data)
    if entry:
        result['frame_focus']=display_packet(data,parent_beat,entry)
        packet['shot_contract']='PRIMARY VISIBLE FOCUS: '+entry['focus']['subject']+'. '+packet['shot_contract']
        packet['source_constraints'].append('Show only this frame focus: '+entry['focus']['action']+
            '. Claim status: '+entry['focus']['source_status']+'. State: '+entry['focus']['state'])
        packet['shot_contract']+=' Frame action: '+entry['focus']['action']+'. Source claim status: '+entry['focus']['source_status']+'. Visible state: '+entry['focus']['state']
        if entry['focus']['source_status'] in ('hypothesis','reported','conditional'):
            packet['shot_contract']='ILLUSTRATIVE VIEW OF A SOURCE HYPOTHESIS/REPORT, NOT AUTHENTICATED PROOF. '+packet['shot_contract']
        packet['compiled_prompt']='\n'.join(label+': '+packet[key] for label,key in (
            ('STYLE CONTRACT','style_contract'),('CITY CONTRACT','city_contract'),('TIME LAYER','time_layer_contract'),
            ('SHOT CONTRACT','shot_contract'),('NEGATIVE CONTRACT','negative_contract')))
    evidence=[{'start':units[i]['start'],'end':units[i]['end'],'quote':units[i]['text']}
        for i in beat['evidence_ids']]
    if entry:evidence=copy.deepcopy(entry['focus']['evidence'])
    result.update(composition=direction,beat=direction,subject=beat['subject'],location=beat['subject'],
        location_ref=ref,time_layer=beat['time_layer'],time=beat['period'],subject_state=beat['subject_state'],
        characters=[],character_ids=[],background_roles=[],raw_visible_people=beat['expected_people'],
        visible_people=beat['expected_people'],episodic_people=roles,people_policy=packet['people_policy'],
        props=[{'name':n,'state':'placed','visible':True,'continuity':'continues'} for n in beat['required_artifacts']],
        audit_visual_direction=direction,audit_subject=beat['subject'],audit_source_quote=story[a:b],
        audit_people=beat['expected_people'],audit_roles=roles,audit_location_ref=ref,
        audit_time_layer=beat['time_layer'],audit_source_binding=binding,audit_required_artifacts=beat['required_artifacts'],
        audit_forbidden=exclusions,safe_framing_explanation=direction,forgotten_cities_prompt_contract=packet,
        semantic_parent_source={'start':parent_start,'end':parent_end,'quote':parent_quote},
        evidence_level=beat['evidence_level'],audit_context_quote=' '.join(e['quote'] for e in evidence),
        continuity_before=document['beats'][ordinal-2]['subject'] if ordinal>1 else '',
        continuity_after=document['beats'][ordinal]['subject'] if ordinal<len(document['beats']) else '',
        narrative_beat={**tag,'version':document['version'],'ordinal':ordinal,'episode':beat['episode'],
            'function':beat['function'],'first_source_id':beat['first'],'last_source_id':beat['last'],
            'evidence':evidence,'period':beat['period'],'transition_reason':beat['transition_reason'],
            'critic_checked':True})
    for stale in ('source_fidelity_plan','population_policy','participant_identity','narrative_context'):
        result.pop(stale,None)
    if entry:
        from .frame_composition import attach
        result=attach(result)
    return result


def timed_plan(story,metadata,words,duration,seconds=8):
    """Allocate spoken time to viewpoints inside reviewed beats, not new events."""
    document=checked_document(story,metadata)
    if not document:raise DomainError('Нужен проверенный смысловой план.',409)
    from .narration_expansion import narration_frames
    drafts=scene_drafts(story,document)
    parents=[{'id':'beat-'+str(i+1),'position':i+1,'current_revision':1,'data':d} for i,d in enumerate(drafts)]
    timed=narration_frames(story,words,parents,duration,seconds)
    frames=[]
    for f in timed:
        data=copy.deepcopy(drafts[f['parent_position']-1]);span=f['narration_source']
        data.update(source_start=span['start'],source_end=span['end'],source_quote=span['quote'],voice_timing=f['timing'])
        data['narrative_beat']['shot_part']=f['part']
        from .narrative_framing import camera_choice, default_view
        beat=document['beats'][f['parent_position']-1]
        view=default_view(beat,f['part'])
        if view:data['narrative_framing']=camera_choice(story,beat,data,view)
        # The checked document is immutable throughout this preparation.
        # Reuse that validation for all subshots instead of rechecking the
        # complete sequence once per frame (hundreds of times on long audio).
        data=_ground_document(story,metadata,data,document)
        frames.append({'parent_scene_id':None,'data':data})
    return frames


def verify_applied(project,scenes):
    metadata=project['analysis_metadata'];story=project['story']
    if not checked_document(story,metadata):return False
    apply=sequence_grounder(story,metadata)
    from .narration_expansion import validate_timeline
    for scene in scenes:
        expected=apply(scene['data'])
        if expected.get('frame_focus')!=scene['data'].get('frame_focus'):
            raise DomainError('Содержание кадра изменено после проверки.',409)
        for key in ('audit_visual_direction','audit_people','audit_location_ref','audit_time_layer','audit_required_artifacts'):
            actual=scene['data'].get(key);wanted=expected[key]
            if key=='audit_visual_direction':
                actual=reviewed_direction(actual);wanted=reviewed_direction(wanted)
            if actual!=wanted:
                raise DomainError('Редакторское решение кадра изменено после проверки.',409)
    duration=metadata['voice_alignment']['duration']
    validate_timeline(story,[{'timing':s['data']['voice_timing'],'narration_source':{
        'start':s['data']['source_start'],'end':s['data']['source_end'],'quote':s['data']['source_quote']}} for s in scenes],duration)
    return True

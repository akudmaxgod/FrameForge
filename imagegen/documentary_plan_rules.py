"""Source-derived constraints that an editorial model cannot waive."""
import re
from .forgotten_cities.event_context import for_source


def source_constraints(story, source):
    context = for_source(story, source['quote'], source['start']) or {}
    local = story[max(0, source['start'] - 600):source['end'] + 600]
    figurative = bool((context.get('place') or {}).get('figurative') or
                      re.search(r'ворот\w*\s+(?:Египт\w*|стран\w*)', local, re.I))
    literal = bool(re.search(r'(?:каменн\w*|деревянн\w*|городск\w*)\s+ворот\w*|\bарк\w*\b', source['quote'], re.I))
    actors=[p['name'] for p in context.get('participants', [])]
    # A narrated person's viewpoint is not the next sentence's observer.
    # Keep the selected role when the source explicitly establishes its view.
    viewpoint=re.search(r'^\s*для\s+([А-Яа-яЁё-]+)\b.*(?:впереди|начина\w*|вид\w*|открыва\w*)',source['quote'],re.I)
    if viewpoint:
        from .nlp import RussianNLPAdapter
        morph=RussianNLPAdapter.get_instance().analyze_token_morph(viewpoint.group(1))
        if morph.pos=='NOUN' and morph.animacy=='anim':actors.append(viewpoint.group(1))
    from .forgotten_cities.source_inventory import inventory
    from .forgotten_cities.physical_evidence import artifact_evidence
    _, objects = inventory(story)
    descriptions = {item['name']: item['description'] for item in
                    artifact_evidence([item['name'] for item in objects], story)}
    from .narrative_obligations import obligations
    return {'narrative_obligations': obligations(story, source),
            'physical_participants': list(dict.fromkeys(actors)),
            'physical_viewpoint': bool(viewpoint and actors),
            'implicit_actor': bool(context.get('implicit_actor')),
            'past_participant_action': bool(context.get('past_participant_action')),
            'time': context.get('time'), 'figurative_port_gate': figurative and not literal,
            'artifact_descriptions': descriptions}


def plan_errors(plan, source, constraints, anchors=()):
    errors = []
    if constraints.get('narrative_obligations'):
        from .narrative_obligations import errors as obligation_errors
        errors.extend(obligation_errors(plan, constraints['narrative_obligations']))
    count, roles = plan.get('expected_people'), plan.get('roles') or []
    if count == 0 and roles or count and not roles:
        errors.append(f'expected_people={count!r}, roles={roles!r} contradict each other. '
                      'If the chosen image includes people, supply nonempty anonymous source-grounded role labels; '
                      'if it includes no people, both expected_people=0 and roles=[] are required.')
    if (constraints['implicit_actor'] or constraints['past_participant_action'] or constraints['physical_viewpoint']) and constraints['physical_participants'] and count == 0:
        errors.append('The source explicitly narrates physical participants performing an action: ' +
                      ', '.join(constraints['physical_participants']) + '. Retain these participants and their action.')
    time=constraints.get('time') or {}
    evidence=time.get('evidence') or {}
    if (count and time.get('layer')=='historical_reconstruction' and
            source['start']<=evidence.get('start',-1)<source['end'] and
            plan.get('time_layer')!='historical_reconstruction'):
        errors.append('The selected physical participant is explicitly ancient: '+evidence.get('quote','')+
                      '. Keep that participant in historical_reconstruction, not the neighboring modern observer.')
    if constraints['figurative_port_gate']:
        text = str(plan.get('subject', '')) + '. ' + str(plan.get('visual_direction', ''))
        for clause in re.split(r'[.;,\n]', text):
            if re.search(r'\b(?:no|without|avoid|forbid|never|do not)\b|не\s+(?:изображ|добав|созда|выдум)|\b(?:без|никаких)\b|figurative|metaphor', clause, re.I):
                continue
            if re.search(r'\bgates?\b|\barches?\b|\bворот\w*\b|\bарк\w*\b', clause, re.I):
                errors.append('The port is figuratively the gates of a country; physical gates, gate remains and arches are not established. Show the actual maritime port or its submerged urban remains.')
                break
    graphics=(r'\boverlays?\b|\boverlaid\b|\bannotations?\b|\binfographics?\b|\bdiagrams?\b|\bcollage\b|'
              r'\barrows?\b|\bicons?\b|\bcolored markers\b|\bschematic (?:plan|illustration)\b|'
              r'\bmap[-‑ ](?:like|style)\b|\b(?:cross[-‑ ]section(?:al)?|cut[-‑ ]?away) (?:illustration|drawing)\b|'
              r'наложени\w*|инфографик\w*|диаграмм\w*|коллаж\w*|стрелк\w*|схематическ\w*\s+(?:план|иллюстрац)\w*')
    physical_document=bool(count and constraints['physical_participants'] and
        re.search(r'\b(?:table|desk|sheet|paper|screen)\b|\b(?:стол\w*|лист\w*|бумаг\w*|экран\w*)\b',str(plan.get('visual_direction','')),re.I))
    for sentence in re.split(r'[.;\n]', str(plan.get('visual_direction', ''))):
        for mention in re.finditer(graphics,sentence,re.I):
            if physical_document and re.fullmatch(r'diagrams?|диаграмм\w*',mention.group(),re.I):continue
            # Negation applies to the whole coordinated list: "no labels,
            # overlays, or diagrams". A later "no captions" must not exempt
            # a previously requested overlay in that same sentence.
            prefix=re.split(r'\b(?:but|however|но|однако)\b',sentence[:mention.start()],flags=re.I)[-1]
            if re.search(r'\b(?:no|not|without|avoid|forbid|never|без|никаких|не)\b[\w\s,/-]*$',prefix,re.I):continue
            errors.append('Use one physical documentary scene, without invented graphic overlays, annotations, diagrams or collages. Offending sentence: '+sentence.strip())
            break
        if errors and errors[-1].startswith('Use one physical'):break
    if count:
        for forbidden in plan.get('forbidden', []):
            if any(role.strip() and re.search(r'\b'+re.escape(role.strip())+r'\b',forbidden,re.I) for role in roles):
                errors.append('The forbidden list bans a selected physical participant. Retain the source-narrated roles and remove that contradictory prohibition.')
                break
    # A specific named building must not silently become the whole city when
    # the model itself has selected an architectural view of that building.
    if plan.get('subject_state') == 'architecture' and plan.get('location_ref'):
        layer = 'modern_ruins' if plan.get('time_layer') == 'excavation' else plan.get('time_layer')
        matched = [a for a in anchors if a.get('anchor_type') == 'district' and a.get('time_layer') == layer and
                   any(source['start'] <= span['start'] < source['end'] for span in a.get('match_spans', []))]
        if len(matched) == 1 and plan['location_ref'] != matched[0]['canonical_name']:
            errors.append('The architectural subject has its own matching reference: ' + matched[0]['canonical_name'] +
                          '. Use this exact reference rather than a generic city view.')
    for anchor in anchors:
        if anchor['canonical_name'] in plan.get('required_artifacts', []):
            from .forgotten_cities.physical_evidence import motif_errors
            description = (constraints.get('artifact_descriptions') or {}).get(anchor['canonical_name'], anchor.get('description', ''))
            errors.extend(motif_errors(description, str(plan.get('visual_direction', ''))))
        if (anchor['canonical_name'] in plan.get('required_artifacts',[]) and
                anchor.get('source_properties',{}).get('evidence_kind')=='textual_manufacture' and
                plan.get('time_layer')!='historical_reconstruction'):
            errors.append('This object is described by ritual/manufacturing texts, not an excavated surviving specimen. '
                          'Show a historical reconstruction of its described manufacture or use, without claiming a modern find.')
    return errors

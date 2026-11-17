"""Independent scientific detail artwork, planned locally without an extra API pass."""
import copy,hashlib
from .science_explanatory import clean_notes,infer_scheme,valid_scheme
from .science_atlas import asset_name
VERSION='science-details/1'

def prepare(analysis):
    result=copy.deepcopy(analysis)
    props={p['name']:p for p in result.get('props',[])}
    catalog={a['key']:a for a in result.get('diagnostics',{}).get('science_atlas',{}).get('assets',[])}
    added={};bindings=0
    for index,scene in enumerate(result.get('scenes',[])):
        plan=scene.get('science_atlas') or {}
        if plan.get('presentation')!='visual_first':continue
        # Model coordinates have not been checked against the generated pixels.
        plan['annotation_policy']='verified_positions_only'
        retained_notes=copy.deepcopy(plan.get('explanatory_labels')) if plan.get('density_upgrade_version') or plan.get('frame_density') in ('filled','single_subject') else None
        if retained_notes is None:plan.pop('explanatory_labels',None)
        # Concise explanatory terminology/state, never the narrated paragraph.
        if len(plan.get('asset_keys',[]))==1 and (plan.get('function') in ('structure','mechanism') or plan.get('labels') or plan.get('explanatory_scheme') or plan.get('support_asset_key')) and not plan.get('elements'):
            material=catalog.get(plan['asset_keys'][0],{})
            notes=list(retained_notes if retained_notes is not None else plan.get('labels') or [])
            if not notes:
                identity=material.get('identity','').strip();state=material.get('state','').strip()
                if identity and len(identity.split())<=7:notes.append(identity)
                if state and len(state.split())<=8 and len(state)<=100 and state.casefold()!=identity.casefold():
                    # Long literal source excerpts are narration, not explanatory labels.
                    if len(state.split())<=4 or state.casefold() not in plan.get('support_quote','').casefold():notes.append(state)
            notes=clean_notes(notes,limit=4 if plan.get('presentation_motion')=='slide_explain' else 2)
            if notes:plan['explanatory_labels']=notes
            context=' '.join(s.get('science_atlas',{}).get('support_quote','') for s in result['scenes'][max(0,index-3):index])
            previous=plan.get('explanatory_scheme')
            scheme=(infer_scheme(plan,material,context) or previous) if previous and previous.get('kind')=='layers' else previous or infer_scheme(plan,material,context)
            if valid_scheme(scheme,plan.get('support_quote','')):plan['explanatory_scheme']=scheme
        support=plan.get('support_asset_key')
        if support and support not in plan.get('asset_keys',[]) and asset_name(support) in props and len(plan.get('asset_keys',[]))==1 and plan.get('layout')!='comparison':
            plan['detail_assets']=[{'key':support,'source_key':plan['asset_keys'][0],'purpose':plan.get('focus',''),'role':'explanation'}]
        cue=plan.get('support_cue_quote','');source=plan.get('support_quote','')
        if support and cue and cue in source:plan['support_reveal_ms']=round(6000*source.index(cue)/max(1,len(source)))
        if plan.get('detail_assets'):continue
        keys=plan.get('asset_keys',[])
        labels=[a.get('label','').strip() for a in plan.get('annotations',[]) if a.get('label','').strip()]
        if (len(keys)!=1 or plan.get('layout')=='comparison' or plan.get('elements')
                or plan.get('function') not in ('structure','mechanism') or not labels):continue
        base=props.get(asset_name(keys[0]))
        if not base:continue
        # The same source material and explanation reuse exactly the same detail.
        focus=plan.get('focus','').strip()
        key='detail_'+hashlib.sha256((keys[0]+'|'+focus).encode()).hexdigest()[:20]
        name=asset_name(key)
        description=(
            'Create ONE independent, substantially more detailed scientific illustration. '
            'This is a close anatomical/technical view explaining the mechanism, NOT a crop, '
            'duplicate overview, collage or two copies of the same image. '
            'Keep the exact object identity, state and scientific relationships from the source material. '
            'Show the relevant real structures and their connections clearly at a larger scale. '
            'Do not invent structures, causal claims or experimental results. '
            'No text, letters, labels, leader lines or arrows. '
            'SOURCE OBJECT AND STATE: '+base['description']+'\n'
            'EXPLANATION TO VISUALIZE: '+focus+'\n'
            'NARRATION CONTEXT (not text to draw): '+plan.get('support_quote','')+'\n'
            'Same engraved scientific atlas style and palette as the overview; different, informative detailed view.')
        if name not in props and name not in added:
            added[name]={'name':name,'description':description,'importance':'story',
                'first_appearance':scene.get('n',base.get('first_appearance',1)),
                'owner':'','default_state':'present'}
        plan['detail_assets']=[{'key':key,'source_key':keys[0],'purpose':focus}]
        plan['detail_version']=VERSION;bindings+=1
    result.setdefault('props',[]).extend(added.values())
    result.setdefault('diagnostics',{}).setdefault('science_atlas',{})['detail_planning']={
        'version':VERSION,'new_materials':len(added),'new_bindings':bindings,'extra_model_requests':0}
    from .science_sequence import arrange_analysis
    return arrange_analysis(result)

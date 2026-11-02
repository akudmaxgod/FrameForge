"""Pack our own repeated transport prose; retain source/editorial constraints.

Flow SDK rejected >4000 JS characters synchronously in controlled requests.
The full audit contract remains in snapshots. Nothing user-authored is sliced.
"""
import json,re
from .visual_style import marker_contract,MARKER_WIRE,MEDICAL_STORY
from .domain import SOURCE_PEOPLE_UNSPECIFIED

PROVIDER_LIMIT=4000
BASE_BUDGET=3600  # reserve composite-reference names and one retry instruction
MEDICAL_COMPACT=('Preserve source-stated illness, pain, swelling, asymmetry, pallor, wasting, skin changes and restricted movement in the specified body region, pose or expression, even in wide shots. '
 'Use only signs/actions stated in this CURRENT source; infer no sign from diagnosis alone, add no injury/blood/procedure, carry no illness into recovery. '
 'For internal illness use stated behavior/expression; invent no cutaway, diagram or anatomical finding.')
PEOPLE_COMPACT=('Depict people/body parts only when the current source requires them; unnamed source actors need no fixed portrait. Do not substitute an empty room for a bodily state. No invented census, bystanders, identities or illness. Authors/statues/portraits are not living actors.')

def units(text):return len(text.encode('utf-16-le'))//2

def pack(prompt):
    if units(prompt)<=BASE_BUDGET:return prompt
    marker='\nFINAL RENDER CONTRACT (scene constraints; never image lettering):\n'
    body,sep,encoded=prompt.partition(marker)
    body=body.replace(marker_contract(True),MARKER_WIRE).replace(MEDICAL_STORY,MEDICAL_COMPACT)
    body=body.replace(SOURCE_PEOPLE_UNSPECIFIED,PEOPLE_COMPACT)
    # These are application-owned boilerplate, not arbitrary narration spans.
    replacements=[
      (r'Full-body composition: show every visible character as a complete, anatomically coherent figure with head, torso, two arms, two complete legs and both feet visibly supported by the floor, ground, stairs, deck or other surface\. Never crop, fade, merge or omit the lower body\.',
       'Full body: head/torso, two arms, two hands, two complete legs and both feet supported by the source surface. Never crop, fade, merge or omit the lower body.'),
      (r'Compose a readable hand-drawn scene with a clear subject, simple stick-figure silhouettes and layered illustrated setting\. Match the selected reference identities and setting in the same flat marker drawing language\. Preserve white round heads, simple facial marks and stick limbs; no realistic skin or painted shading\.',
       'Readable layered marker setting. Match attached identities; white round heads, simple facial marks and stick-figure bodies; no realistic skin/shading.'),
      (r'In-world text is allowed only when the source explicitly calls for it, such as period-appropriate writing on a sign, letter, map, document, label or date\.[\s\S]*?Do not invent extra written details\.',
       'In-world text is allowed only when the source explicitly calls for it: period-appropriate lettering physically attached to its named sign/document/map/object, in perspective and material ink/paint. No invented writing, overlays, subtitles, captions, speech/dialogue balloons, UI, watermarks or logos.'),
      (r'Represent voices, music and other sounds only through visible character reactions or physical effects in the environment\. Never draw sound waves, voice trails, audio symbols or decorative graphic echoes\.',
       'Show sound only through visible reactions/physical effects. Never draw sound waves, voice trails, audio symbols or graphic echoes.'),
      (r'One single full-frame image only: one continuous camera view and physically coherent space\. Never make a split screen, diptych, triptych, comic panels, collage, contact sheet or building cross-section\. Every subject and animal must be one intact continuous physically plausible form without broken geometry\.',
       'One continuous physically coherent view; intact plausible subjects/animals. No split screen, diptych/triptych, panels, collage, contact sheet or building cross-section.'),
      (r'Full-bleed 16:9 image\. The scene must fill the entire canvas edge to edge\. No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox\.',
       'Full-bleed 16:9 image, edge to edge. No borders/margins/matting/frames/rounded corners/insets/letterbox/pillarbox.'),
      (r'Vary framing across adjacent images\. Camera choice changes composition only\. Preserve the source event, period, participants and uncertainty\. Keep the required action and all required participants readable; widen the shot if necessary\. For object-only scenes focus on the object, not a face\. Do not invent a patient, doctor, symptom, injury, blood, procedure, instrument or anatomical detail to make the image dramatic\.',
       'Vary framing only; preserve event, era, actors and uncertainty. Widen for required action/actors. Object-only: focus on object. Invent no patient, doctor, symptom, injury, blood, procedure, instrument or anatomy.'),
    ]
    for pattern,replacement in replacements:body=re.sub(pattern,lambda _:replacement,body)
    if sep:
        contract=json.loads(encoded)
        # Hashes, proof excerpts and audit-process prose do not direct pixels.
        # Keep every visual field, exclusion, editorial/manual decision and its
        # exact values; omit a repeated exact direction only if already present.
        evidence=contract.get('source_evidence') or {}
        compact={k:v for k,v in contract.items() if k not in ('version','audit_contract_hash','source_evidence','evidence_policy')}
        if compact.get('manual_decision')=={'applied':False,'direction':None}:compact.pop('manual_decision')
        normalized=lambda text:' '.join(str(text).replace('@','').split())
        if isinstance(compact.get('direction'),str) and normalized(compact['direction']) in normalized(body):compact.pop('direction')
        quote=evidence.get('quote')
        if isinstance(quote,str) and normalized(quote) not in normalized(body) and not any(normalized(quote) in normalized(v) for v in compact.values()):compact['source_quote']=quote
        for key in ('location','spatial_place'):
            if isinstance(compact.get(key),str) and normalized(compact[key]) in normalized(body):compact.pop(key)
        if compact.get('free_design')==['lighting','framing','minor decoration','background headcount unless source-critical']:
            compact.pop('free_design')  # application default design freedom, no source fact
        if compact.get('precedence')==['exact_source_and_explicit_editorial_decision','agreed_scene_contract','style_and_composition']:
            compact.pop('precedence');body+=' Source/editorial constraints override style.'
        encoded=json.dumps(compact,ensure_ascii=False,separators=(',',':'),sort_keys=True)
        body+='\nRENDER CONSTRAINTS (never image lettering):'+encoded
    return body

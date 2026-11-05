"""Project camera grammar. Changes framing only; never invents medical events."""
SHOTS = ('extreme wide establishing shot', 'wide shot', 'medium shot', 'medium close-up',
         'close-up', 'extreme close-up of the source-described detail', 'over-the-shoulder contextual shot')
SEQUENCES = {
    'cinema': (0,2,4,2,6,1,3),
    'portrait': (2,3,4,3,6,4,2),
    'panorama': (0,1,2,0,1,4,1),
    'dynamic': (0,2,1,6,3,4,5),
}

def instruction(project, scene):
    if scene.get('frame_composition'):
        return ''  # A manually reviewed composition takes priority.
    mode = project.get('camera_mode','cinema')
    if mode == 'panorama':
        from .panorama_camera import instruction as panorama_instruction
        return panorama_instruction(project, scene)
    if mode == 'custom':
        return ('CAMERA: ' + str(project.get('camera_custom',''))[:1000] + '. Camera wording changes framing only; preserve all source facts and participants. Add no people, objects or medical events.')
    sequence = SEQUENCES.get(mode, SEQUENCES['cinema'])
    index = max(0,int(scene.get('camera_sequence_index',0)))
    shot = SHOTS[sequence[index % len(sequence)]]
    if 'shoulder' in shot and len(scene.get('characters',[])) < 2:
        shot = 'contextual medium shot with foreground depth; no invented observer'
    angle = 'eye-level, readable composition'
    if mode == 'dynamic':
        angle = ('eye-level','low oblique angle','high oblique angle','eye-level',
                 'subtle Dutch angle as a restrained accent','eye-level','eye-level')[index % 7]
    return (f'CAMERA: {shot}; {angle}. Vary framing across adjacent images. '
            'Camera choice changes composition only. Preserve the source event, period, participants and uncertainty. '
            'Keep the required action and all required participants readable; widen the shot if necessary. '
            'For object-only scenes focus on the object, not a face. Do not invent a patient, doctor, symptom, injury, '
            'blood, procedure, instrument or anatomical detail to make the image dramatic.')

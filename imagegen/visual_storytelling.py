"""Source-first subject direction and finite camera pacing; no rendering calls."""
import copy
import re
from . import frame_composition as camera
from .source_review_status import signature

VERSION = 'visual-storytelling/1'
RULES = '''VISUAL STORYTELLING: Choose the clearest physical illustration of the
CURRENT explanation, not a decorative panorama for every sentence. For an
abstract explanation, prefer one simple, source-established object, material,
trace or visible action that directly carries its meaning. A source-described
analysis of tooth enamel may show the tooth; a described soil deposit may show
that deposit; described transport in baskets may show the actual carrying action.
These are conditional examples, NEVER permission to introduce those objects.
The object must be in this frame's subject_candidates and source_options/current
or prior context, in the correct period and state. Retain all essential actors,
spatial relationships and the current claim's uncertainty. Do not turn a material
trace into proof of a disputed cause. Do not replace required human action with
an isolated prop, or illustrate a negated cause as an event. If no object directly
explains the clause, hold its established physical subject rather than inventing
a symbol, apparatus, writing, chart, costume or scientific procedure.
Use economical visual_direction: one subject/action, necessary setting and state.
Use wide framing for spatial scale and establishing a place, medium framing for
an action or relationship, close framing for a source-backed object/detail.
Prefer a useful change of scale to repeating the same panorama. Framing never
authorizes extra objects or cropping away required participants. The application
checks camera pacing separately after source review. Start visual_direction with
exactly Wide shot:, Medium shot: or Close shot:, choosing a scale that serves
this explanation. Avoid prescribing a lens or fixed camera distance. These are
editorial camera choices, not claims that the source described a camera.
'''


def options(value):
    """Conservative scales: unknown/context subjects retain their source framing."""
    if value['participants']:
        return ('medium', 'wide'), 'action'
    suggested = re.match(r'^(Wide|Medium|Close) shot:', value['visual_direction'], re.I)
    suggested = suggested.group(1).lower() if suggested else None
    state = value['subject_state']
    if state == 'architecture':
        return ('wide', 'medium'), 'space'
    if state in ('artifact', 'buried_remains'):
        return ('close', 'medium'), 'detail'
    if state == 'living':
        return ('medium', 'wide'), 'subject'
    # "context" includes landscapes, water and tiny physical traces. An explicit
    # reviewed scale can resolve this; old ambiguous content keeps its framing.
    if suggested:
        return (suggested, 'medium' if suggested != 'medium' else 'wide'), 'source_framing'
    return ('preserve',), 'source_framing'


def plan(scenes):
    """Change only camera packets, never signed facts, reviews or source spans.

    Explicit/custom camera choices are retained. On new automatic plans, two
    equal adjacent scales are enough before a safe alternative is considered.
    New spatial settings still get an establishing view; there is no blind cycle.
    """
    result = [copy.deepcopy(scene) for scene in scenes]
    history = []
    previous_setting = None
    for scene in result:
        composition = camera.validate(scene)
        value = scene.get('visual_content')
        if not composition or not value:
            history.append(None)
            previous_setting = None
            continue
        marker = scene.get('visual_storytelling') or {}
        automatic = (marker.get('version') == VERSION
                     and marker.get('composition_hash') == signature(composition))
        explicit = any(composition[k] != 'preserve' for k in camera.CHOICES)
        setting = (value['place'], value['period'], value['time_layer'])
        manual = scene.get('frame_composition_manual') == scene['frame_facts']['content_hash']
        if manual or scene.get('manual_prompt') or (explicit and not automatic):
            history.append(composition['shot_size'])
            previous_setting = setting
            continue
        eligible, purpose = options(value)
        size = eligible[0]
        establishing = purpose == 'space' and setting != previous_setting
        if not establishing and len(history) >= 2 and history[-2:] == [size, size] and len(eligible) > 1:
            size = eligible[1]
        selected = camera.choice(scene['frame_facts'], {
            'shot_size': size, 'camera_angle': 'preserve', 'placement': 'preserve'})
        scene['frame_composition'] = selected
        scene['visual_storytelling'] = {
            'version': VERSION, 'purpose': purpose,
            'composition_hash': signature(selected)}
        history.append(size)
        previous_setting = setting
    return result

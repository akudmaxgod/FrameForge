"""Import user-written prompts without claiming story alignment or AI review."""
import copy
import json
import math
import re

from .db import uid, now, dump
from .domain import DomainError, MAX_SCENES, fingerprint, required_text
from .generation_scene import BASE_BUDGET, VERSION as WIRE_VERSION

VERSION = 'user-prompt-txt/1'
MAX_TEXT = 4_000_000


def legacy_bridge_prompt(prompt):
    """Exact additive envelope used by installed bridge 0.1.73; no source removal."""
    if not re.search(r'In-world text is allowed only when the source explicitly calls for it', prompt, re.I):
        prompt += '\nIn-world text is allowed only when the source explicitly calls for it (for example, a period-appropriate sign, document, letter, map, label or date). Every permitted word must be physically printed, handwritten or engraved on that named object, follow its surface perspective, and use material ink or paint. Never render words as floating, glowing or superimposed typography. Never add subtitles, captions, narration overlays, speech bubbles, dialogue balloons or UI windows.'
    if not re.search(r'Never draw sound waves, voice trails', prompt, re.I):
        prompt += '\nRepresent voices, music and other sounds only through visible character reactions or physical effects in the environment. Never draw sound waves, voice trails, audio symbols or decorative graphic echoes.'
    if not re.search(r'Full-bleed 16:9 image', prompt, re.I):
        prompt += '\nFull-bleed 16:9 image. The scene must fill the entire canvas edge to edge. No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox.'
    return prompt


def parse(text, separator='paragraphs'):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise DomainError('Выберите непустой TXT с промптами до 4 миллионов символов.')
    if '\x00' in text or '\ufffd' in text:
        raise DomainError('Не удалось прочитать TXT. Сохраните файл в UTF-8.')
    if re.match(r'^\s*\[\s*(?:\{|\])',text):
        return [item['prompt'] for item in parse_bundle(text,separator)]
    source = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if separator not in ('paragraphs', 'lines'):
        raise DomainError('Выберите разделитель промптов.')
    parts = re.split(r'\n[\t ]*\n(?:[\t ]*\n)*', source) if separator == 'paragraphs' else source.split('\n')
    prompts = [p.strip() for p in parts if p.strip()]
    if not 1 <= len(prompts) <= MAX_SCENES:
        raise DomainError(f'В файле должно быть от 1 до {MAX_SCENES} промптов.')
    for index, prompt in enumerate(prompts, 1):
        if len(prompt) > BASE_BUDGET:
            raise DomainError(f'Промпт {index}: {len(prompt)} символов; максимум {BASE_BUDGET}. '
                              'Если в файле несколько сцен, проверьте разделитель. Текст не обрезан.')
    return prompts


def parse_bundle(text,separator='paragraphs'):
    if isinstance(text,str) and re.match(r'^\s*\[\s*(?:\{|\])',text):
        if len(text)>MAX_TEXT:raise DomainError('Файл промптов слишком большой.')
        try:items=json.loads(text)
        except ValueError:raise DomainError('JSON должен содержать массив объектов prompt и narration.') from None
        if not isinstance(items,list) or not 1<=len(items)<=MAX_SCENES:raise DomainError('Недопустимое количество сцен в JSON.')
        result=[]
        for i,item in enumerate(items,1):
            if not isinstance(item,dict):raise DomainError('Сцена JSON должна быть объектом: '+str(i))
            prompt=required_text(item.get('prompt'),'Промпт '+str(i),BASE_BUDGET)
            narration=required_text(item.get('narration'),'Фрагмент озвучки '+str(i),4000)
            if any(x in prompt+narration for x in ('\x00','\ufffd')):raise DomainError('Повреждённый текст сцены '+str(i))
            result.append({'prompt':prompt,'narration':narration})
        return result
    return [{'prompt':prompt,'narration':''} for prompt in parse(text,separator)]


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 60:
        raise DomainError('Длительность кадра должна быть числом от 1 до 60 секунд.')
    return round(value, 3)


def metadata(project):
    value = project.get('analysis_metadata') or {}
    if isinstance(value, str):
        value = json.loads(value)
    packet = value.get('prompt_import') or {}
    return packet if packet.get('version') == VERSION else None


def validate_scene(data):
    required_text(data.get('manual_prompt'), 'Промпт кадра', BASE_BUDGET)
    if '\x00' in data['manual_prompt'] or '\ufffd' in data['manual_prompt']:
        raise DomainError('Промпт содержит повреждённые символы.')
    duration((data.get('prompt_timing') or {}).get('duration_seconds'))
    if (data.get('prompt_import') or {}).get('version') != VERSION:
        raise DomainError('Не сохранён источник импортированного промпта.')


def create(service, data):
    story = required_text(data.get('story'), 'Сценарий', 160000)
    title = required_text(data.get('title'), 'Название', 200)
    bundle = parse_bundle(data.get('prompts_text'), data.get('separator', 'paragraphs'))
    prompts = [item['prompt'] for item in bundle]
    seconds = duration(data.get('seconds_per_scene', 5))
    filename = str(data.get('prompts_filename') or 'prompts.txt').replace('\\', '/').split('/')[-1][:200]
    story_filename = str(data.get('story_filename') or '').replace('\\', '/').split('/')[-1][:200]
    original_hash = fingerprint(data['prompts_text'])
    packet = {'version': VERSION, 'filename': filename, 'file_hash': original_hash,
        'story_filename': story_filename, 'story_hash': fingerprint(story),
        'count': len(prompts), 'seconds_per_scene': seconds,
        'duration_seconds': round(seconds*len(prompts), 3),
        'alignment': 'not_aligned_to_narration', 'automatic_analysis': False,
        'narration_hints':sum(bool(item['narration']) for item in bundle),
        'source_format':'json-prompt-narration/1' if any(item['narration'] for item in bundle) else 'txt-prompts/1'}
    project_id = uid()
    # One transaction: an invalid file never leaves an empty project behind.
    with service.db.transaction() as connection:
        connection.execute('''INSERT INTO projects(id,title,idea,story,style,mode,camera_mode,
            camera_pacing,aspect_ratio,analysis_metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (project_id, title, '', story, 'Cinematic realism, natural lighting', 'illustration',
             'dynamic', 'steady', '16:9', dump({'prompt_import': packet}), now()))
        for position, prompt in enumerate(prompts, 1):
            scene_id = uid()
            scene = {'text': '', 'beat': prompt, 'characters': [], 'props': [], 'location': '',
                'location_ref': '', 'time': '', 'composition': prompt,
                'manual_prompt': prompt, 'prompt_import': {'version': VERSION,
                    'file_hash': original_hash, 'ordinal': position, 'original_prompt_hash': fingerprint(prompt)},
                'prompt_timing': {'duration_seconds': seconds,
                    'start_seconds': round((position-1)*seconds, 3), 'end_seconds': round(position*seconds, 3),
                    'alignment': 'planned_only'},
                'animation': {'duration_seconds': seconds}}
            hint=bundle[position-1]['narration']
            if hint:
                scene['prompt_narration_hint']=hint
                # Only unique verbatim source text can become an exact quote.
                # Semantic guesses are stored elsewhere, never as provenance.
                a=story.find(hint)
                if a>=0 and story.find(hint,a+1)<0:
                    scene.update(source_start=a,source_end=a+len(hint),source_quote=hint)
            connection.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (scene_id, project_id, position))
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, 1, dump(scene), now()))
        service.db.event(connection, project_id, 'prompts_imported', packet)
    return service.detail(project_id)


def preflight(project):
    errors = []
    packet = metadata(project)
    if not packet or packet['story_hash'] != fingerprint(project['story']):
        errors.append({'code': 'changed_prompt_source', 'message': 'Сценарий импортированного проекта изменён.'})
    for scene in project.get('scenes', []):
        try:
            validate_scene(scene['data'])
            if scene['data']['prompt_import']['file_hash'] != packet['file_hash']:
                raise DomainError('Кадр принадлежит другому файлу промптов.')
        except (DomainError, KeyError, TypeError) as error:
            errors.append({'code': 'invalid_imported_prompt', 'scene_index': scene['position'], 'message': str(error)})
    return {'ok': not errors, 'valid': not errors, 'errors': errors, 'warnings': [],
        'semantic_verified': False, 'source_type': VERSION, 'narration_alignment': 'not_aligned'}


def snapshot(service, project, scene):
    data = scene['data']
    validate_scene(data)
    packet = metadata(project)
    if (not packet or packet['file_hash'] != data['prompt_import']['file_hash']
            or packet['story_hash'] != fingerprint(project['story'])):
        raise DomainError('Источник импортированного кадра изменился.', 409)
    prompt = data['manual_prompt']
    contract = {'version': VERSION, 'status': 'bound', 'source_type': 'user_authored_prompt',
        'project_id': project['id'], 'scene_id': scene['id'], 'revision': scene['current_revision'],
        'direction': prompt, 'prompt_hash': fingerprint(prompt), 'file_hash': packet['file_hash'],
        'story_hash': packet['story_hash'], 'story_alignment': 'not_aligned',
        'semantic_review': 'not_run', 'conflicts': []}
    contract['hash'] = fingerprint(contract)
    return {'profile': service.profile(), 'project_id': project['id'], 'scene_id': scene['id'],
        'scene_revision': scene['current_revision'], 'scene': copy.deepcopy(data), 'prompt': prompt,
        'references': [], 'uninhabited': False, 'expected_people': 0, 'detail_faces': False,
        'style': project['style'], 'world': '', 'story_hash': packet['story_hash'],
        'template_version': VERSION, 'source_contract': contract,
        'prompt_import': {'version': VERSION, 'file_hash': packet['file_hash']},
        'flow_wire': {'version': WIRE_VERSION, 'packet': contract, 'prompt': prompt,
            'legacy_bridge_prompt': legacy_bridge_prompt(prompt),
            'prompt_hash': fingerprint(prompt), 'error': None}}


def is_user_snapshot(value):
    contract = value.get('source_contract') or {}
    return ((value.get('prompt_import') or {}).get('version') == VERSION
        and contract.get('version') == VERSION and contract.get('status') == 'bound'
        and contract.get('prompt_hash') == fingerprint(value.get('prompt', '')))


def project_prompts(service, project, scene_ids=None):
    selected = set(scene_ids) if scene_ids is not None else None
    rows = []
    hashes = [r['sha256'] for r in service.db.all('SELECT sha256 FROM assets WHERE project_id=?', (project['id'],)) if r['sha256']]
    for scene in project['scenes']:
        if selected is not None and scene['id'] not in selected:
            continue
        error = None
        try:
            snap = snapshot(service, project, scene)
        except DomainError as exc:
            error = str(exc)
            snap = {'prompt': '', 'source_contract': None, 'flow_wire': {}}
        pending = service.db.one("SELECT id FROM generations WHERE scene_id=? AND scene_revision=? AND state='queued' AND cancel_requested=0 ORDER BY rowid DESC LIMIT 1",
            (scene['id'], scene['current_revision']))
        active = service.db.one('SELECT asset_id FROM generations WHERE id=?', (scene.get('active_generation_id'),)) or {}
        asset = active.get('asset_id')
        rows.append({'scene_id': scene['id'], 'revision': scene['current_revision'], 'position': scene['position'],
            'beat': scene['data']['manual_prompt'], 'prompt': snap['prompt'], 'flow_prompt': snap['prompt'],
            'flow_prompt_version': WIRE_VERSION, 'flow_prompt_hash': fingerprint(snap['prompt']),
            'comfy_prompt': snap['prompt'], 'source_contract': snap['source_contract'],
            'reference_omissions': [], 'source_fidelity_retry': {},
            'google_queue_enabled': service.settings().get('google_queue_enabled') is True,
            'generation_id': pending['id'] if pending else scene.get('active_generation_id'),
            'active_generation_id': scene.get('active_generation_id'),
            'has_image': bool(asset) and not scene.get('stale') and not pending, 'image_asset_id': asset,
            'image_url': '/api/assets/'+asset if asset else None,
            'has_video': bool(scene.get('video_asset_id')), 'video_asset_id': scene.get('video_asset_id'),
            'references': [], 'ready_for_flow': error is None, 'semantic_valid': error is None,
            'semantic_review': 'not_run_user_prompts', 'render_ready': error is None, 'flow_error': error,
            'animation': scene['data'].get('animation', {}), 'veo_prompt': scene['data']['manual_prompt']})
    return {'project_id': project['id'], 'profile_type': None, 'title': project['title'], 'scenes': rows,
        'characters': [], 'locations': [], 'props': [], 'locked_characters': [], 'locked_locations': [],
        'locked_props': [], 'reference_jobs': [], 'known_hashes': list(set(hashes)),
        'animation_settings': project.get('animation_settings', {})}

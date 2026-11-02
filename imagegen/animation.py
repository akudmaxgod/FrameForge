import io
import hashlib
import re
from functools import lru_cache
from .domain import DomainError
from .ken_burns import KEN_BURNS_PRESETS

DEFAULT_ANIMATION_SETTINGS = {
    'plan_mode': 'selective',
    'credit_limit': 50,
    'veo_lite_credit_cost': 10,
    'max_veo_frames': 5,
    'ken_burns_enabled': True,
    'default_duration_seconds': 5,
    'fps': 24,
    'transition': 'crossfade'
}


def smart_ken_burns_motion(scene, position=0):
    """Conservative editorial rules, not subject/face recognition."""
    shot = str(scene.get('shot') or '').casefold()
    text = ' '.join(str(scene.get(key) or '') for key in ('beat', 'source_quote')).casefold()
    if any(word in shot for word in ('close', 'portrait', 'face', 'detail', 'insert', 'крупн', 'портрет', 'лицо', 'деталь')):
        if position % 3 == 2:
            return {'preset':'gentle_pull_back', 'reason':'Мягкое отдаление от детали: спокойная смена движения между соседними кадрами.'}
        return {'preset':'gentle_push_in', 'reason':'Мягкий наезд: акцент на лице или детали, без сильного кадрирования.'}
    if any(word in shot for word in ('wide', 'panoramic', 'establishing', 'landscape', 'panorama', 'общий', 'широк', 'панорам', 'пейзаж')):
        if position % 3 != 1:
            return {'preset':'gentle_pan_left' if position % 3 == 2 else 'gentle_pan_right', 'reason':'Спокойный проход по общему плану: небольшой запас по краям сохраняет картинку внутри кадра.'}
        return {'preset':'gentle_pull_back', 'reason':'Мягкое отдаление: раскрыть место и сохранить общий план.'}
    if any(word in shot for word in ('atmosphere','атмосфер')):
        return {'preset':'gentle_pan_left' if position % 2 else 'gentle_pan_right', 'reason':'Заметный плавный проход камеры для атмосферного плана, без изменения масштаба.'}
    if any(word in text for word in ('reveals the', 'opens the view', 'уходит', 'покидает', 'открывается вид', 'затихает')):
        return {'preset':'gentle_pull_back', 'reason':'Мягкое отдаление: раскрытие пространства или завершение эпизода.'}
    if any(word in text for word in ('notices', 'discovers', 'reaction', 'замечает', 'обнаруживает', 'осознаёт', 'осознает', 'реакция')):
        return {'preset':'gentle_push_in', 'reason':'Мягкий наезд: внимание к находке или реакции.'}
    if isinstance(scene.get('visible_people'), int) and scene['visible_people'] >= 2:
        return {'preset':'gentle_pull_back', 'reason':'Мягкое отдаление: сохранить группу в кадре.'}
    return {'preset':'gentle_push_in' if position % 2 else 'gentle_pull_back', 'reason':'Спокойное движение: чередование наезда и отдаления для нейтральных планов.'}


def choose_ken_burns_preset(scene, position=0, previous_preset=None):
    """Choose a gentle, varied camera path for consecutive local clips."""
    shot = str(scene.get('shot') or '').casefold()
    text = ' '.join(str(scene.get(key) or '') for key in ('beat', 'source_quote')).casefold()
    people = scene.get('visible_people', 0)
    if any(word in shot for word in ('wide', 'panoramic', 'establishing', 'landscape', 'panorama')):
        preferred = 'slow_panorama'
    elif isinstance(people, int) and people >= 2:
        preferred = 'balanced_two_shot'
    elif any(word in shot for word in ('close', 'portrait', 'face')):
        preferred = 'portrait_push_in'
    elif any(word in shot for word in ('detail', 'insert')) or any(word in text for word in ('меч', 'свиток', 'письмо', 'ключ', 'предмет', 'sword', 'letter', 'key')):
        preferred = 'object_reveal'
    else:
        rotation = ('subject_parallax', 'slow_pull_back', 'slow_zoom_in')
        preferred = rotation[position % len(rotation)]

    if preferred != previous_preset:
        return preferred
    alternatives = ('subject_parallax', 'slow_pull_back', 'slow_panorama', 'object_reveal', 'balanced_two_shot', 'portrait_push_in')
    return next((preset for preset in alternatives if preset != previous_preset), 'slow_zoom_in')

_ACTION_VERBS = {
    'run', 'runs', 'running', 'ran', 'walk', 'walks', 'walking', 'walked',
    'grab', 'grabs', 'grabbing', 'grabbed', 'leap', 'leaps', 'leaping',
    'jump', 'jumps', 'jumping', 'turn', 'turns', 'turning', 'turned',
    'enter', 'enters', 'entering', 'entered', 'fight', 'fights', 'fighting',
    'strike', 'strikes', 'striking', 'struck', 'climb', 'climbs', 'climbing',
    'rush', 'rushes', 'rushing', 'chase', 'chases', 'chasing', 'push', 'pushes',
    'pull', 'pulls', 'reach', 'reaches', 'dodge', 'dodges', 'fall', 'falls', 'falling',
    'бежит', 'идёт', 'хватает', 'прыгает', 'поворачивается', 'входит',
    'бьёт', 'сражается', 'лезет', 'мчится', 'гонится', 'толкает', 'тянет', 'падает'
}

_EMOTION_KEYWORDS = {
    'anger', 'angry', 'shock', 'shocked', 'fear', 'scared', 'terror', 'terrified',
    'joy', 'joyful', 'cry', 'crying', 'cries', 'scream', 'screaming', 'screams',
    'shout', 'shouting', 'shouts', 'tension', 'tense', 'grief', 'grieving',
    'panic', 'panicking', 'ярость', 'гнев', 'шок', 'страх', 'ужас', 'радость',
    'плач', 'плачет', 'крик', 'кричит', 'напряжение', 'паника', 'urgent', 'alarm'
}

_PLOT_KEYWORDS = {
    'find', 'finds', 'found', 'discover', 'discovers', 'discovered',
    'reveal', 'reveals', 'revealed', 'weapon', 'gun', 'knife', 'bomb',
    'explosion', 'danger', 'dangerous', 'secret', 'truth', 'clue',
    'находит', 'обнаруживает', 'оружие', 'нож', 'бомба', 'взрыв',
    'опасность', 'тревога', 'тайна', 'правда', 'улика', 'console', 'emergency'
}

_ENVIRONMENT_KEYWORDS = {
    'rain', 'raining', 'storm', 'stormy', 'fire', 'burning', 'water',
    'waves', 'sea', 'ocean', 'car', 'vehicle', 'boat', 'ship',
    'lightning', 'smoke', 'fog', 'mist', 'дождь', 'буря', 'шторм',
    'огонь', 'пламя', 'горит', 'вода', 'волны', 'море', 'океан',
    'машина', 'автомобиль', 'лодка', 'корабль', 'молния', 'дым', 'туман'
}


def determine_camera_motion(scene):
    """Determine camera motion string based on shot composition and scene action."""
    shot = str(scene.get('shot') or '').casefold()
    beat = str(scene.get('beat') or scene.get('source_quote') or '').casefold()

    if any(k in shot for k in ('wide', 'panoramic', 'establishing', 'landscape', 'panorama')):
        return 'slow panoramic pan'
    if any(k in shot for k in ('tracking', 'dynamic')) or any(k in beat for k in ('enters', 'walks', 'runs', 'rushes', 'arrived')):
        return 'slow tracking dolly-in'
    if any(k in shot for k in ('close', 'portrait', 'detail', 'face')) or any(k in beat for k in ('reaction', 'shock', 'tears', 'cries')):
        return 'slow subtle push-in'
    if any(k in beat for k in ('finds', 'discovers', 'reveals', 'unlocked', 'notices')):
        return 'controlled focus push-in'
    return 'slow cinematic push-in'


def compile_veo_prompt(scene, camera_motion=None):
    """Generate exact Veo 3.1 Lite prompt according to the specification."""
    if camera_motion is None:
        camera_motion = determine_camera_motion(scene)
    animation = scene.get('animation') or {}
    if animation.get('motion_scope') == 'camera_only':
        return compile_story_aligned_veo_prompt(
            scene, camera_motion=camera_motion, duration_seconds=animation.get('duration_seconds', 5),
        )
    beat = str(scene.get('beat') or scene.get('source_quote') or '').strip()
    return (
        f"Use supplied references exactly. Preserve identity, hair, wardrobe, age, "
        f"location and active props. Action: {beat}. Camera: {camera_motion}. "
        f"No new people, no identity or wardrobe changes, no extra props, no subtitles, "
        f"captions, UI, logos, watermarks or text overlays."
    )


def score_scene_for_animation(scene, position, total_scenes):
    """Calculate animation_score (0-100), reasons, and camera_motion for a scene."""
    score = 0
    reasons = []

    # 1. Hook factor
    if position == 0:
        score += 35
        reasons.append('hook')
    elif position == 1:
        score += 28
        reasons.append('hook')
    elif position == 2:
        score += 20
        reasons.append('hook')
    elif position in (3, 4):
        score += 10
        reasons.append('hook_early')

    # Analyze beat and quote text
    beat_text = ' '.join(filter(None, [
        str(scene.get('beat') or ''),
        str(scene.get('source_quote') or ''),
        str(scene.get('emotion') or '')
    ])).casefold()

    tokens = set(re.findall(r'\b[a-zA-Zа-яА-ЯёЁ]{3,}\b', beat_text))

    # 2. Physical action
    if any(v in tokens for v in _ACTION_VERBS):
        score += 25
        reasons.append('character_action')

    # 3. Emotion / reaction / conflict
    if any(e in tokens for e in _EMOTION_KEYWORDS):
        score += 20
        reasons.append('emotion_reaction')

    # 4. Plot twist / discovery / threat
    if any(p in tokens for p in _PLOT_KEYWORDS):
        score += 15
        reasons.append('plot_twist')

    # 5. Expressive environment
    if any(w in tokens for w in _ENVIRONMENT_KEYWORDS):
        score += 10
        reasons.append('expressive_environment')

    # 6. Risk penalty: complex crowd or high people count
    visible_people = scene.get('visible_people', 1)
    if isinstance(visible_people, int) and visible_people >= 3:
        score -= 20
        reasons.append('high_risk_group')

    score = max(0, min(100, score))
    camera_motion = determine_camera_motion(scene)
    return score, reasons, camera_motion


def validate_animation_settings(settings):
    """Normalize and validate project animation settings."""
    merged = dict(DEFAULT_ANIMATION_SETTINGS)
    if isinstance(settings, dict):
        merged.update(settings)
    mode = merged.get('plan_mode', 'economy')
    if mode not in ('economy', 'manual', 'selective'):
        raise DomainError(f'Недопустимый режим планирования анимации: {mode}')
    if mode == 'economy':
        merged['credit_limit'] = 50
        merged['max_veo_frames'] = 5
        merged['allow_over_budget'] = False
    else:
        merged['credit_limit'] = None
        merged['max_veo_frames'] = None
        merged['allow_over_budget'] = True
    return merged


def build_animation_plan(project, scenes):
    """Build a durable per-frame animation plan.

    Selective planning defaults to local 2.5D; Omni is explicitly selected.
    Legacy economy/manual planning remains compatible. It never silently
    substitutes Veo: Veo is a paid, explicit manual choice. A scene without a
    confirmed static image remains waiting for that image, so it cannot create a
    fake local video or a bridge job that Flow cannot execute.
    """
    settings = validate_animation_settings(project.get('animation_settings'))
    scene_items = []
    for idx, sc in enumerate(scenes):
        data = sc.get('data') or sc
        pos = sc.get('position', idx)
        score, reasons, camera_motion = score_scene_for_animation(data, pos, len(scenes))
        existing = data.get('animation') or {}
        manual_engine = existing.get('selected_engine') if existing.get('mode') == 'manual' else None
        static_asset = sc.get('asset_id') or sc.get('source_image_asset_id')
        scene_items.append({'scene': sc, 'data': data, 'position': pos, 'score': score,
            'reasons': reasons, 'camera_motion': camera_motion, 'existing_anim': existing,
            'manual_engine': manual_engine, 'has_static_image': bool(static_asset)})

    updated_scenes, previous_kb_preset = [], None
    omni_count = veo_count = kb_count = waiting_count = 0
    for item in scene_items:
        existing = item['existing_anim']
        manual = item['manual_engine']
        # Replanning must never discard a completed legacy video.  Old
        # snapshots can predate engine namespaces, so retain their source and
        # only supply safe display defaults.
        if item['scene'].get('video_asset_id') or existing.get('status') in ('completed','queued_omni','generating_omni','queued_veo','generating_veo'):
            preserved = dict(existing)
            engine = preserved.get('selected_engine') or ('ken_burns' if preserved.get('source') == 'ken_burns' else 'veo_lite')
            preserved.setdefault('selected_engine', engine)
            preserved.setdefault('status', 'completed')
            preserved.setdefault('omni', {'resolution': '720p', 'duration_seconds': 6})
            preserved.setdefault('veo', {'camera_motion': preserved.get('camera_motion') or item['camera_motion']})
            if engine == 'omni_flash': omni_count += 1
            elif engine == 'veo_lite': veo_count += 1
            else: kb_count += 1
            updated_scenes.append({'scene_id': item['scene'].get('id'), 'position': item['position'], 'animation': preserved})
            continue
        engine = manual if manual in ('veo_lite', 'omni_flash', 'ken_burns') else ('ken_burns' if settings['plan_mode']=='selective' else 'omni_flash')
        omni = dict(existing.get('omni') or {})
        omni.setdefault('resolution', '720p')
        omni.setdefault('duration_seconds', 6)
        veo = dict(existing.get('veo') or {})
        veo.setdefault('camera_motion', existing.get('camera_motion') or item['camera_motion'])
        kb_preset = existing.get('ken_burns_preset')
        if engine == 'ken_burns':
            if settings['plan_mode']=='selective' and existing.get('ken_burns_mode')!='manual':
                kb_preset = smart_ken_burns_motion(item['data'],item['position'])['preset']
            else:
                kb_preset = kb_preset if manual == 'ken_burns' and kb_preset in KEN_BURNS_PRESETS else choose_ken_burns_preset(item['data'], item['position'], previous_kb_preset)
            previous_kb_preset = kb_preset
        if not item['has_static_image']:
            status = 'waiting_source_image'
            waiting_count += 1
        else:
            status = f'planned_{engine}'
            if engine == 'omni_flash': omni_count += 1
            elif engine == 'veo_lite': veo_count += 1
            else: kb_count += 1
        animation = {
            'mode': 'manual' if manual else 'auto', 'selected_engine': engine,
            'recommendation': engine if settings['plan_mode']=='selective' or manual else 'omni_flash',
            'animation_score': item['score'], 'reasons': item['reasons'],
            'camera_motion': veo['camera_motion'], 'veo': veo, 'omni': omni,
            'ken_burns_preset': kb_preset,
            'duration_seconds': omni['duration_seconds'] if engine == 'omni_flash' else settings['default_duration_seconds'],
            'credit_cost': settings['veo_lite_credit_cost'] if engine == 'veo_lite' else 0,
            'status': status, 'video_asset_id': existing.get('video_asset_id'), 'error': None,
            'safe_crop': {'overscan': .15, 'max_pan_x': .08, 'max_pan_y': .05, 'edge_safe': True, 'fallback': 'slow_zoom_in'},
        }
        if kb_preset: animation['safe_crop']['actual_preset'] = kb_preset
        if settings['plan_mode']=='selective':
            motion=smart_ken_burns_motion(item['data'],item['position'])
            animation['ken_burns_mode']=existing.get('ken_burns_mode','auto')
            animation['ken_burns_reason']=motion['reason'] if animation['ken_burns_mode']=='auto' else 'Движение выбрано вручную.'
            if kb_preset and kb_preset.startswith('gentle_'):animation['safe_crop']['overscan']=0
        updated_scenes.append({'scene_id': item['scene'].get('id'), 'position': item['position'], 'animation': animation})

    summary = {'plan_mode': settings['plan_mode'], 'credit_limit': settings['credit_limit'],
        'total_credits': veo_count * settings['veo_lite_credit_cost'],
        'omni_frames_count': omni_count, 'veo_frames_count': veo_count,
        'ken_burns_frames_count': kb_count, 'waiting_source_image_count': waiting_count,
        'total_frames': len(scene_items)}
    return {'scenes': updated_scenes, 'summary': summary, 'settings': settings}


class ContinuityResult(dict):
    @property
    def is_valid(self):
        return not self.get('has_violation', False)

    def __iter__(self):
        err = self['violations'][0] if self.get('violations') else None
        return iter((not self.get('has_violation', False), err))


def validate_story_continuity(scene_data_or_scenes, scenes=None, cast_bible=None, props=None):
    """Validate story continuity rules (pot, packing boxes, off-screen character presence).

    Accepts either:
    - a list of scenes, e.g. validate_story_continuity(scenes_list)
    - a single scene_data dict and optional scenes list, e.g. validate_story_continuity(scene_data, scenes=scenes)

    Returns ContinuityResult dict that can also be unpacked as (is_valid, err).
    """
    if isinstance(scene_data_or_scenes, list):
        target_scenes = scene_data_or_scenes
        all_scenes = scene_data_or_scenes
    else:
        target_scenes = [scene_data_or_scenes]
        all_scenes = scenes or [scene_data_or_scenes]

    violations = []

    normalized_all = []
    for idx, s in enumerate(all_scenes):
        s_data = s.get('data') or s if isinstance(s, dict) else {}
        pos = s.get('position') if isinstance(s, dict) and s.get('position') is not None else s_data.get('position', idx)
        text = ' '.join(filter(None, [
            str(s_data.get('beat') or ''),
            str(s_data.get('source_quote') or ''),
            str(s_data.get('continuity_before') or ''),
            str(s_data.get('continuity_after') or '')
        ])).casefold()
        normalized_all.append((pos, text, s_data))

    normalized_all.sort(key=lambda x: x[0])

    for target in target_scenes:
        t_data = target.get('data') or target if isinstance(target, dict) else {}
        t_pos = target.get('position') if isinstance(target, dict) and target.get('position') is not None else t_data.get('position', 0)
        t_text = ' '.join(filter(None, [
            str(t_data.get('beat') or ''),
            str(t_data.get('source_quote') or ''),
            str(t_data.get('continuity_before') or ''),
            str(t_data.get('continuity_after') or '')
        ])).casefold()

        # 1. Pot continuity rule (кастрюля / горшок / pot)
        pot_scenes = [(pos, txt, d) for pos, txt, d in normalized_all if any(w in txt for w in ('pot', 'кастрюл', 'горшок'))]
        if pot_scenes:
            find_positions = [pos for pos, txt, _ in pot_scenes if any(w in txt for w in ('find', 'наход', 'нашел', 'обнаруж', 'discover', 'searches the cabinet and discovers'))]
            wear_positions = [pos for pos, txt, _ in pot_scenes if any(w in txt for w in ('puts', 'надева', 'надел', 'wear', 'wore', 'on head', 'на голове', 'шлем', 'helmet'))]

            if find_positions and wear_positions:
                first_find = min(find_positions)
                first_wear = min(wear_positions)
                if first_wear < first_find and t_pos <= first_wear:
                    msg = "Кастрюля (горшок / pot) не может быть надета на голову до её обнаружения героем. Перегенерируйте статичный кадр."
                    if msg not in violations:
                        violations.append(msg)

            if wear_positions:
                first_wear = min(wear_positions)
                if t_pos > first_wear:
                    if any(w in t_text for w in ('searches for pot', 'looks for pot', 'ищет кастрюл', 'без кастрюли', 'pot vanished')) and not any(w in t_text for w in ('takes off', 'removes', 'снимает', 'снял')):
                        msg = "Кастрюля (горшок / pot) отсутствует после надевания без сюжетного объяснения. Перегенерируйте статичный кадр."
                        if msg not in violations:
                            violations.append(msg)

        # 2. Packing boxes continuity rule (коробки / boxes)
        box_scenes = [(pos, txt, d) for pos, txt, d in normalized_all if any(w in txt for w in ('box', 'boxes', 'коробк', 'упаков', 'сборы'))]
        if box_scenes:
            pack_start = [pos for pos, txt, _ in box_scenes if any(w in txt for w in ('starts packing', 'begins packing', 'начинает упаков', 'начинает собирать', 'упаковывает', 'started packing'))]
            sealed = [pos for pos, txt, _ in box_scenes if any(w in txt for w in ('sealed', 'stacked', 'loaded', 'запечатаны', 'заклеены', 'упакованы'))]
            if pack_start and sealed:
                first_pack = min(pack_start)
                first_seal = min(sealed)
                if first_seal < first_pack and t_pos <= first_seal:
                    msg = "Коробки (boxes) появились упакованными и запечатанными до начала сборов. Перегенерируйте статичный кадр."
                    if msg not in violations:
                        violations.append(msg)

        # 3. Off-screen voice / telephone / radio constraint
        offscreen_tokens = ('радио', 'рации', 'рация', 'телефон', 'звонок', 'голос по', 'на фото', 'на фотографии', 'radio', 'phone', 'call', 'photograph', 'photo')
        if any(tok in t_text for tok in offscreen_tokens):
            for char_name in t_data.get('characters', []):
                cn = str(char_name).casefold()
                if any(f"{cn} {tok}" in t_text or f"{tok} {cn}" in t_text or f"голос {cn}" in t_text or f"{cn}'s voice" in t_text or f"{cn} speaks over" in t_text for tok in offscreen_tokens):
                    raw_people = t_data.get('visible_people')
                    if raw_people is not None and raw_people < len(t_data.get('characters', [])):
                        msg = f"Персонаж {char_name} упомянут только по связи или на фото, но добавлен как физически присутствующий в кадре. Перегенерируйте статичный кадр."
                        if msg not in violations:
                            violations.append(msg)

    has_violation = len(violations) > 0
    return ContinuityResult({
        'has_violation': has_violation,
        'violations': violations,
        'is_valid': not has_violation
    })



def compile_story_aligned_veo_prompt(scene_data, characters=None, location=None, props=None,
                                     camera_motion=None, duration_seconds=5):
    """Generate exact story-aligned Veo 3.1 Lite prompt matching TASK_STORY_ALIGNED_VEO_IMPORT.md."""
    if camera_motion is None:
        camera_motion = determine_camera_motion(scene_data)

    if (scene_data.get('animation') or {}).get('motion_scope') == 'camera_only':
        return (
            "Animate the supplied image as the exact first frame. Preserve identity, hair, wardrobe, age, "
            "every visible person, pose, object, location, lighting and composition. "
            "Treat the image as a frozen moment: keep all people and objects stationary throughout the clip. "
            "Do not continue the depicted action or introduce new events.\n"
            f"0–{duration_seconds} s: one slow, steady camera movement across the unchanged scene.\n"
            f"Camera: {camera_motion}.\n"
            "No body movement, falls, impacts, injuries, new people, new props, morphing or text overlays."
        )

    beat = str(scene_data.get('beat') or scene_data.get('source_quote') or '').strip()

    # Micro actions based on duration
    # 0-2s: initial state and small movement
    initial_action = str(scene_data.get('continuity_before') or '').strip()
    if not initial_action:
        initial_action = f"Subject starts in scene composition, establishing focus on {beat[:60]}."

    # 2-5s: main story action
    main_action = beat if beat else "Subject executes primary physical motion."

    # 5-8s (or end of 5s): completion in state compatible with next scene
    final_action = str(scene_data.get('continuity_after') or '').strip()
    if not final_action:
        final_action = "Subject completes movement, settling into final posture compatible with next scene."

    # Continuity details
    char_names = [c['name'] if isinstance(c, dict) else str(c) for c in (characters or [])]
    chars_str = (f"Characters: {', '.join(char_names)}" if char_names
                 else "Preserve every person visible in the supplied image")

    prop_items = []
    for p in (props or []):
        if isinstance(p, dict):
            p_name = p.get('name')
            p_state = p.get('state') or 'present'
            prop_items.append(f"{p_name} ({p_state})")
        else:
            prop_items.append(str(p))
    props_str = f"Props: {', '.join(prop_items)}" if prop_items else "no extra props"
    continuity_str = f"{chars_str}; {props_str}"

    if duration_seconds and duration_seconds > 5:
        time_breakdown = (
            f"0–2 s: {initial_action}\n"
            f"2–5 s: {main_action}\n"
            f"5–{duration_seconds} s: {final_action}"
        )
    else:
        time_breakdown = (
            f"0–2 s: {initial_action}\n"
            f"2–{duration_seconds} s: {main_action}; {final_action}"
        )

    return (
        f"Animate the supplied image as the exact first frame of this scene. "
        f"Preserve identity, hair, wardrobe, age. Keep the same cast identities, faces, hair, clothes, "
        f"location, lighting, composition and visible props.\n"
        f"Story beat from source: {beat}.\n"
        f"{time_breakdown}\n"
        f"Camera: {camera_motion}.\n"
        f"Continuity: {continuity_str}.\n"
        f"Do not add people or props, change identities or costume, skip ahead in the story, "
        f"add subtitles, captions or graphic text overlays. In-world writing on existing story props is allowed."
    )


def build_animation_passport(project, scene, scenes=None, cast_map=None,
                             locations_map=None, props_map=None, assets_map=None):
    """Build structured Animation Passport from source of truth."""
    scene_data = dict(scene.get('data') or scene if isinstance(scene, dict) else {})
    scene_id = scene.get('id')
    position = scene.get('position', 1)
    cur_rev = scene.get('current_revision', 1)

    source_image_asset_id = scene.get('source_image_asset_id') or scene.get('asset_id') or scene.get('active_generation_id')
    source_image_sha256 = None
    if assets_map and source_image_asset_id and source_image_asset_id in assets_map:
        source_image_sha256 = assets_map[source_image_asset_id].get('sha256')
        source_image_asset_id = assets_map[source_image_asset_id].get('id')

    source_quote = scene_data.get('source_quote', '')
    beat = scene_data.get('beat', '')
    continuity_before = scene_data.get('continuity_before', '')
    continuity_after = scene_data.get('continuity_after', '')

    scene_text = ' '.join(filter(None, [
        str(scene_data.get('beat') or ''),
        str(scene_data.get('source_quote') or '')
    ])).casefold()
    offscreen_tokens = ('радио', 'рации', 'рация', 'телефон', 'звонок', 'голос', 'на фото', 'radio', 'phone', 'call', 'voice', 'transmitter')

    negative_constraints = [
        "No morphing faces or bodies",
        "No sudden costume changes",
        "No disappearing or teleporting props",
        "No unnatural jitter or distorted hands"
    ]

    char_names = scene_data.get('characters', [])
    char_list = []
    missing_portraits = []
    stale_reasons = []

    for cname in char_names:
        c_lower = str(cname).casefold()
        is_offscreen = any(tok in scene_text for tok in offscreen_tokens) and any(
            pat in scene_text for pat in (
                f"{c_lower} speaks", f"{c_lower} calls", f"voice of {c_lower}",
                f"голос {c_lower}", f"{c_lower} over the", f"{c_lower} по радио",
                f"{c_lower} по рации", f"{c_lower} по телефону"
            )
        )
        if is_offscreen:
            negative_constraints.append(f"Do not show {cname} physically in room (voice/radio only).")
            continue

        c_info = (cast_map or {}).get(cname) or {'name': cname}
        from .subject_policy import requires_portrait
        if not requires_portrait(c_info):
            continue
        has_portrait = bool(c_info.get('locked') and (c_info.get('references', {}).get('portrait') or c_info.get('portrait_asset_id')))
        if not has_portrait and not c_info.get('locked'):
            missing_portraits.append(cname)
        char_list.append({
            'name': cname,
            'canonical_name': c_info.get('canonical_name', cname),
            'id': c_info.get('id'),
            'role': c_info.get('role', ''),
            'locked': bool(c_info.get('locked')),
            'portrait_asset_id': c_info.get('references', {}).get('portrait') or c_info.get('portrait_asset_id'),
            'emotion': scene_data.get('emotion', '')
        })

    loc_ref = scene_data.get('location_ref') or scene_data.get('location')
    loc_info = (locations_map or {}).get(loc_ref) or {'name': loc_ref or 'Unknown'}
    location = {
        'name': loc_info.get('name', loc_ref or ''),
        'description': loc_info.get('description', ''),
        'time': scene_data.get('time', 'Day')
    }

    active_props = []
    for sp in scene_data.get('props', []):
        p_name = sp.get('name') if isinstance(sp, dict) else str(sp)
        p_info = (props_map or {}).get(p_name) or {}
        active_props.append({
            'name': p_name,
            'state': sp.get('state', 'present') if isinstance(sp, dict) else 'present',
            'owner': sp.get('owner', '') if isinstance(sp, dict) else ''
        })

    camera_motion = scene_data.get('animation', {}).get('camera_motion') or determine_camera_motion(scene_data)
    duration = scene_data.get('animation', {}).get('duration_seconds', 5)

    initial_0_2 = continuity_before if continuity_before else f"Subject starts in scene composition, establishing focus on {beat[:50]}."
    main_2_5 = beat if beat else "Subject executes primary physical action."
    end_5_8 = continuity_after if continuity_after else "Subject completes movement, settling into final posture."
    micro_actions = {
        '0_2s': initial_0_2,
        '2_5s': main_2_5,
        '5_8s': end_5_8
    }
    motion_scope = (scene_data.get('animation') or {}).get('motion_scope', 'story_action')
    if motion_scope == 'camera_only':
        micro_actions = dict.fromkeys(micro_actions, "Keep the depicted moment unchanged; move only the camera.")

    prompt = compile_story_aligned_veo_prompt(
        scene_data, characters=char_list, location=location, props=active_props,
        camera_motion=camera_motion, duration_seconds=duration
    )

    cont_res = validate_story_continuity(scene, scenes=scenes or [scene])
    continuity_error = "; ".join(cont_res['violations']) if cont_res.get('has_violation') else None
    has_image = bool(source_image_asset_id)

    if continuity_error:
        stale_reasons.append(continuity_error)
    if not has_image:
        stale_reasons.append("У кадра отсутствует сгенерированное статичное изображение.")
    if scene.get('stale'):
        stale_reasons.append("Статичный кадр устарел после изменения сценария или персонажей.")
    if missing_portraits:
        stale_reasons.append(f"Отсутствуют закреплённые портреты персонажей: {', '.join(missing_portraits)}.")

    is_stale = len(stale_reasons) > 0
    stale_reason = "; ".join(stale_reasons) if stale_reasons else None

    return {
        'project_id': project.get('id'),
        'scene_id': scene_id,
        'position': position,
        'scene_revision': cur_rev,
        'source_image_asset_id': source_image_asset_id,
        'source_image_sha256': source_image_sha256,
        'has_source_image': has_image,
        'continuity_error': continuity_error,
        'source_quote': source_quote,
        'beat': beat,
        'continuity_before': continuity_before,
        'continuity_after': continuity_after,
        'characters': char_list,
        'location': location,
        'active_props': active_props,
        'camera_motion': camera_motion,
        'motion_scope': motion_scope,
        'negative_constraints': negative_constraints,
        'duration_seconds': duration,
        'micro_actions': micro_actions,
        'prompt': prompt,
        'stale': is_stale,
        'stale_reason': stale_reason,
        'flow_instructions': [
            "1. Проверить статичное изображение кадра и паспорт анимации.",
            "2. Открыть Google Flow; выбрать Veo 3.1 Lite, режим Frames to Video → First, 16:9.",
            "3. Загрузить исходное изображение этого кадра как первый frame.",
            "4. Вставить подготовленный сюжетный промпт кадра. Проверить цену Flow.",
            "5. Сгенерировать клип и убедиться в сохранении героев, реквизита и действия.",
            "6. Скачать MP4 и загрузить в карточку FrameForge."
        ],
        'flow_params': {
            'model': 'veo-3.1-lite',
            'mode': 'Frames to Video: First',
            'aspect_ratio': '16:9',
            'duration_seconds': duration,
            'credit_cost': 10
        }
    }


def scene_video_metadata(source):
    """Read measured media properties without altering the requested duration."""
    import av
    try:
        with av.open(source if hasattr(source, 'read') else str(source)) as media:
            if not media.streams.video:
                return {}
            stream = media.streams.video[0]
            duration = float(stream.duration * stream.time_base) if stream.duration and stream.time_base else (media.duration or 0) / av.time_base
            if duration <= 0:
                return {}
            return {'duration_seconds': round(duration, 3), 'width': stream.width, 'height': stream.height}
    except Exception:
        return {}


@lru_cache(maxsize=512)
def cached_scene_video_metadata(path, size, modified_ns):
    # The file stat is part of the key so replaced media cannot reuse old values.
    return scene_video_metadata(path)


def validate_scene_video_bytes(video_bytes, filename=None, max_size_bytes=150*1024*1024):
    """Deeply validate video bytes using PyAV to verify container, codec, resolution and duration."""
    if not video_bytes or len(video_bytes) == 0:
        raise DomainError('Файл видео пустой.')
    if len(video_bytes) > max_size_bytes:
        raise DomainError('Превышен максимальный размер видео (максимум 150 МБ).')

    import av
    try:
        container = av.open(io.BytesIO(video_bytes))
    except Exception as e:
        raise DomainError(f'Не удалось открыть видеофайл: неверный формат или повреждённый контейнер ({e}).')

    try:
        video_streams = [s for s in container.streams if s.type == 'video']
        if not video_streams:
            raise DomainError('Файл не содержит видеопотока.')
        stream = video_streams[0]
        width = stream.width
        height = stream.height
        if not width or not height or width <= 0 or height <= 0:
            raise DomainError('Недопустимое разрешение видео.')

        aspect = width / height
        if aspect < 1.60 or aspect > 1.95:
            raise DomainError(f'Неверное соотношение сторон: {width}x{height} ({aspect:.2f}:1). Требуется формат 16:9.')

        duration = None
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = float(container.duration) / 1000000.0

        if duration is not None and duration <= 0:
            raise DomainError('Длительность видео должна быть больше 0 секунд.')
        if duration is not None and duration > 60:
            raise DomainError(f'Слишком большая длительность клипа: {duration:.1f} с (максимум 60 с).')

        try:
            frame_iter = container.decode(video=0)
            first_frame = next(frame_iter)
            if first_frame is None:
                raise DomainError('Видеопоток пустой или повреждён.')
        except StopIteration:
            raise DomainError('Видеопоток не содержит декодируемых кадров.')
        except Exception as e:
            raise DomainError(f'Ошибка декодирования кадров видео: {e}')

        return {
            'width': width,
            'height': height,
            'duration': duration or 5.0,
            'format': container.format.name,
            'sha256': hashlib.sha256(video_bytes).hexdigest()
        }
    finally:
        container.close()


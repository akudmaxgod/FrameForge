import os
import io
import re
import copy
import hashlib
import base64
import json
import secrets
import math
import zipfile
from pathlib import Path

try:
    import sys
    from dotenv import load_dotenv
    app_root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    load_dotenv(app_root / '.env')
except Exception:
    pass
from .db import Database, uid, now, dump
from .domain import (DomainError, required_text, validate_analysis, compile_prompt,
                     compile_flow_prompt, fingerprint, scene_mentions_people, scene_has_explicit_unbound_person, scene_anonymous_people_count, scene_over_shoulder_anonymous_people_count,
                     extract_scene_episodic_roles,
                     character_requirements, prop_is_story, is_nonhuman_character,
                     differentiate_character_visual_features,
                     resolve_scene_presence_contract,
                     validate_project_preflight, build_global_story_bible,
                     GenerationReferenceBundle, compile_channel_style_contract,
                     VALID_PROFILE_TYPES, VALID_CHARACTER_ROLES, VALID_CHANNEL_ASSET_TYPES,
                     STICKMAN_DEFAULT_VERSION, STICKMAN_DEFAULT_CHARACTER, STICKMAN_DEFAULT_CHARACTER_VERSION,
                     FORGOTTEN_CITIES_DEFAULT_VERSION)
from .cast import apply_cast_diff_resolution, slugify_id
from .animation import (DEFAULT_ANIMATION_SETTINGS, validate_animation_settings,
                        score_scene_for_animation, build_animation_plan,
                        compile_veo_prompt, determine_camera_motion,
                        build_animation_passport, validate_scene_video_bytes,
                        smart_ken_burns_motion, scene_video_metadata, cached_scene_video_metadata,
                        validate_story_continuity, compile_story_aligned_veo_prompt)
from .ken_burns import KEN_BURNS_PRESETS, render_ken_burns_video
from .assembler import assemble_storyboard_video
from .review_policy import visual_reviews_enabled, manual_review, MANDATORY_STILL_APPROVAL

TERMINAL = {'succeeded', 'failed', 'cancelled', 'resource_degraded'}
ACTIVE = {'preparing', 'submitted', 'running', 'saving', 'reconciling'}
PROJECT_MODES = {'characters', 'dynamic', 'illustration', 'objects'}
CAMERA_MODES = {'cinema', 'portrait', 'panorama', 'dynamic', 'custom'}
CAMERA_PACINGS = {'slow', 'steady', 'fast'}
ASPECT_RATIOS = {'16:9', '21:9', '9:16'}


def _get_composite_font(size=18):
    try:
        from PIL import ImageFont
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()


def build_composite_reference_sheet(images_with_labels, target_width=1536, target_height=864):
    from PIL import Image, ImageDraw
    n = len(images_with_labels)
    if n == 0:
        raise ValueError("No images provided for composite sheet")
    if n == 1:
        return images_with_labels[0][0]

    canvas = Image.new("RGB", (target_width, target_height), color=(18, 22, 30))
    draw = ImageDraw.Draw(canvas)
    font = _get_composite_font(18)
    pad = 12
    banner_h = 32

    if n == 2:
        w = (target_width - 3 * pad) // 2
        h = target_height - 2 * pad
        boxes = [(pad, pad, w, h), (2 * pad + w, pad, w, h)]
    elif n == 3:
        h_top = (target_height - 3 * pad) // 2
        h_bot = target_height - 3 * pad - h_top
        w_top = target_width - 2 * pad
        w_bot = (target_width - 3 * pad) // 2
        boxes = [
            (pad, pad, w_top, h_top),
            (pad, 2 * pad + h_top, w_bot, h_bot),
            (2 * pad + w_bot, 2 * pad + h_top, w_bot, h_bot)
        ]
    elif n == 4:
        w = (target_width - 3 * pad) // 2
        h = (target_height - 3 * pad) // 2
        boxes = [
            (pad, pad, w, h),
            (2 * pad + w, pad, w, h),
            (pad, 2 * pad + h, w, h),
            (2 * pad + w, 2 * pad + h, w, h)
        ]
    else:  # n >= 5 (up to 5)
        h = (target_height - 3 * pad) // 2
        w_top = (target_width - 4 * pad) // 3
        w_bot = (target_width - 3 * pad) // 2
        bot_offset = (target_width - (2 * w_bot + pad)) // 2
        boxes = [
            (pad + 0 * (w_top + pad), pad, w_top, h),
            (pad + 1 * (w_top + pad), pad, w_top, h),
            (pad + 2 * (w_top + pad), pad, w_top, h),
            (bot_offset + 0 * (w_bot + pad), 2 * pad + h, w_bot, h),
            (bot_offset + 1 * (w_bot + pad), 2 * pad + h, w_bot, h)
        ]

    for (img, label), (x, y, w, h) in zip(images_with_labels, boxes[:n]):
        draw.rectangle([x, y, x + w, y + h], fill=(28, 34, 48), outline=(55, 68, 95), width=2)
        draw.rectangle([x, y, x + w, y + banner_h], fill=(42, 52, 75))
        draw.text((x + 10, y + 6), label[:50], font=font, fill=(230, 240, 255))

        inner_w = w - 8
        inner_h = h - banner_h - 8
        img_copy = img.copy()
        img_copy.thumbnail((inner_w, inner_h), Image.Resampling.LANCZOS)
        im_x = x + 4 + (inner_w - img_copy.width) // 2
        im_y = y + banner_h + 4 + (inner_h - img_copy.height) // 2
        canvas.paste(img_copy, (im_x, im_y))

    return canvas




def public_settings(settings, root=None):
    deepseek_key_configured = bool(settings.get('deepseek_api_key') or os.environ.get('DEEPSEEK_API_KEY'))
    result = dict(settings)
    result['deepseek_api_key'] = ''
    result['deepseek_key_configured'] = deepseek_key_configured
    if root:
        from .stock_secrets import is_pixabay_key_configured
        result['pixabay_key_configured'] = is_pixabay_key_configured(root)
    else:
        result['pixabay_key_configured'] = False
    result['pixabay_api_key'] = ''
    return result

class Service:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.root / 'app.sqlite')
        self.config_path = self.root / 'settings.json'
        from .editing import Editing
        self.editing = Editing(self)
        from .media_library import MediaLibrary
        self.media_library = MediaLibrary(self)
        from .media_analysis import MediaAnalysis
        self.media_analysis = MediaAnalysis(self)
        from .media_matching import MediaMatching
        self.media_matching = MediaMatching(self)
        from .montage_inserts import MontageInserts
        self.montage_inserts = MontageInserts(self)
        from .stock_providers.pixabay import PixabayProvider
        self.pixabay = PixabayProvider(self)
        from .media_folder_workflow import MediaFolderWorkflow
        self.media_folder = MediaFolderWorkflow(self)

    def _project_profile_type(self, project_id):
        row = self.db.one('''SELECT cp.profile_type
            FROM projects p LEFT JOIN channel_profiles cp ON cp.id=p.channel_profile_id
            WHERE p.id=?''', (project_id,))
        return (row or {}).get('profile_type')

    def _requires_still_approval(self, project_id):
        return MANDATORY_STILL_APPROVAL and self._project_profile_type(project_id) == 'forgotten_cities'

    @staticmethod
    def _decode_review(row):
        if not row:
            return {'status': 'pending_review', 'reason_code': '', 'note': '',
                    'continuity': None, 'passport': None, 'updated_at': None}
        result = dict(row)
        result['status'] = result.get('status') or ('approved' if result.get('accepted') else 'rejected')
        for source, target in (('continuity_json', 'continuity'), ('passport_json', 'passport')):
            raw = result.pop(source, None)
            try:
                result[target] = json.loads(raw) if isinstance(raw, str) and raw else (raw or None)
            except ValueError:
                result[target] = None
        return result

    def generation_review(self, generation_id):
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        if not generation:
            raise DomainError('Генерация не найдена.', 404)
        return self._decode_review(self.db.one('SELECT * FROM reviews WHERE generation_id=?', (generation_id,)))

    def _generation_matches_current_scene(self, generation, scene):
        if generation.get('scene_revision') == scene.get('current_revision'):
            return True
        old_row = self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?',
                              (scene['id'], generation.get('scene_revision')))
        if not old_row:
            return False
        old_data = json.loads(old_row['data'])
        current_data = dict(scene.get('data') or {})
        old_data.pop('animation', None)
        current_data.pop('animation', None)
        return old_data == current_data

    def _current_still_approval(self, scene):
        generation_id = scene.get('active_generation_id')
        if not generation_id:
            return {'status': 'pending_review', 'reason': 'no_active_generation'}
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        if not generation or not generation.get('asset_id'):
            return {'status': 'pending_review', 'reason': 'no_active_asset'}
        if not self._generation_matches_current_scene(generation, scene):
            return {'status': 'pending_review', 'reason': 'stale_generation'}
        review = self.generation_review(generation_id)
        snapshot=json.loads(generation['snapshot'])
        source_review=snapshot.get('source_fidelity_review') or {}
        enforce_source = visual_reviews_enabled() and (self.settings().get('source_contract_enabled') is True or self.settings().get('google_queue_enabled') is True) and (
            bool((snapshot.get('scene') or {}).get('forgotten_cities_prompt_contract')) or bool(source_review))
        from .google_image_reviews import current_approval
        source_pending = visual_reviews_enabled() and bool(source_review.get('job_id') and source_review.get('status')!='pass')
        if ((source_pending or (enforce_source and not current_approval(self, generation, snapshot))) and
                review.get('status') not in ('approved','rejected')):
            return {**review,'status':'pending_review','reason':'source_review_pending',
                    'review_state':source_review.get('review_state'),'generation_id':generation_id,
                    'asset_id':generation['asset_id']}
        if visual_reviews_enabled() and review.get('status') != 'rejected' and generation.get('state') == 'succeeded' and generation.get('purpose') == 'scene' and not scene.get('stale'):
            candidates = self.db.all('''SELECT g.* FROM generations g JOIN assets a ON a.id=g.asset_id
                WHERE g.scene_id=? AND g.purpose='scene' AND g.state='succeeded' ''', (scene['id'],))
            current_assets = {g['asset_id'] for g in candidates if self._generation_matches_current_scene(g, scene)}
            if current_assets == {generation['asset_id']}:
                asset = self.db.one('SELECT path FROM assets WHERE id=?', (generation['asset_id'],))
                if asset and (self.root / asset['path']).is_file():
                    return {**review, 'status': 'approved', 'reason': None, 'automatic': True,
                            'approval_source': 'single_variant', 'generation_id': generation_id,
                            'asset_id': generation['asset_id']}
        if review.get('status') != 'approved':
            return {**review, 'reason': 'not_approved', 'generation_id': generation_id,
                    'asset_id': generation.get('asset_id')}
        passport = review.get('passport') or {}
        asset = self.db.one('SELECT * FROM assets WHERE id=?', (generation['asset_id'],))
        if not asset or passport.get('asset_id') != asset.get('id') or passport.get('asset_sha256') != asset.get('sha256'):
            return {**review, 'status': 'pending_review', 'reason': 'approval_passport_mismatch',
                    'generation_id': generation_id, 'asset_id': generation.get('asset_id')}
        project = self.project(scene['project_id'])
        if passport.get('profile_version_id') != project.get('channel_profile_version_id'):
            return {**review, 'status': 'pending_review', 'reason': 'profile_version_changed',
                    'generation_id': generation_id, 'asset_id': generation.get('asset_id')}
        current_reference_hashes = []
        if project.get('channel_profile_id'):
            current_reference_hashes = sorted(
                row.get('content_hash') for row in self.db.all(
                    'SELECT content_hash FROM channel_reference_assets WHERE channel_id=? ORDER BY content_hash',
                    (project['channel_profile_id'],)
                ) if row.get('content_hash')
            )
        if sorted(passport.get('channel_reference_hashes') or []) != current_reference_hashes:
            return {**review, 'status': 'pending_review', 'reason': 'reference_hashes_changed',
                    'generation_id': generation_id, 'asset_id': generation.get('asset_id')}
        return {**review, 'reason': None, 'generation_id': generation_id,
                'asset_id': generation.get('asset_id')}

    def _assert_scene_still_approved(self, scene_or_id):
        scene = self.scene(scene_or_id) if isinstance(scene_or_id, str) else scene_or_id
        # Review history is optional. A ready, current source file is still
        # required even when the old review gate is disabled.
        if self._project_profile_type(scene['project_id']) == 'forgotten_cities':
            generation = self.db.one('SELECT * FROM generations WHERE id=?', (scene.get('active_generation_id'),))
            if not generation or generation.get('state') != 'succeeded' or not generation.get('asset_id'):
                raise DomainError('Для анимации требуется готовое выбранное изображение кадра.', 409)
            if not self._generation_matches_current_scene(generation, scene):
                raise DomainError('Изображение кадра устарело: сначала обновите его после изменения сцены.', 409)
            asset = self.db.one('SELECT * FROM assets WHERE id=?', (generation['asset_id'],))
            path = self.root / asset['path'] if asset else None
            if not path or not path.is_file():
                raise DomainError('Файл выбранного изображения кадра не найден.', 409)
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset.get('sha256'):
                raise DomainError('Файл выбранного изображения изменён: заново сохраните изображение кадра.', 409)
        if not self._requires_still_approval(scene['project_id']):
            return None
        approval = self._current_still_approval(scene)
        if approval.get('status') != 'approved':
            raise DomainError(
                'Анимация заблокирована: сначала утвердите текущий вариант изображения этого кадра.',
                409
            )
        return approval

    def review_generation(self, generation_id, decision, reason_code='', note=''):
        if decision not in ('approved', 'rejected'):
            raise DomainError('Решение должно быть approved или rejected.')
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        if not generation or generation.get('state') != 'succeeded' or not generation.get('scene_id') or not generation.get('asset_id'):
            raise DomainError('Для проверки нужен готовый вариант изображения сцены.', 409)
        if not self._requires_still_approval(generation['project_id']) and self._project_profile_type(generation['project_id']) != 'forgotten_cities':
            raise DomainError('Отдельное утверждение изображений доступно только профилю «Забытые города».', 409)
        scene = self.scene(generation['scene_id'])
        if not self._generation_matches_current_scene(generation, scene):
            raise DomainError('Этот вариант относится к устаревшей версии кадра.', 409)
        asset = self.db.one('SELECT * FROM assets WHERE id=?', (generation['asset_id'],))
        from .forgotten_cities.continuity import evaluate_generation_continuity
        approved_rows = self.db.all('''SELECT r.passport_json FROM reviews r
            JOIN generations g ON g.id=r.generation_id
            WHERE g.project_id=? AND r.status='approved' AND r.generation_id!=?''',
            (generation['project_id'], generation_id))
        approved_passports = []
        for row in approved_rows:
            try:
                approved_passports.append(json.loads(row.get('passport_json') or '{}'))
            except ValueError:
                pass
        project = self.detail(generation['project_id'])
        continuity = evaluate_generation_continuity(
            generation, asset, project=project, approved_passports=approved_passports
        )
        if decision == 'approved':
            checked_snapshot = self.scene_snapshot(scene, allow_unlocked=True)
            continuity['source_fidelity_review'] = self.validate_scene_source_image(
                checked_snapshot, (self.root / asset['path']).read_bytes())
        if decision == 'approved' and continuity.get('deterministic_status') != 'pass':
            messages = [item.get('message', '') for item in continuity.get('blocking_errors', [])]
            raise DomainError('Утверждение заблокировано: ' + '; '.join(filter(None, messages)), 409)
        passport = continuity.get('passport') or {}
        accepted = 1 if decision == 'approved' else 0
        timestamp = now()
        with self.db.transaction() as connection:
            live = connection.execute('SELECT current_revision,active_generation_id FROM scenes WHERE id=?', (scene['id'],)).fetchone()
            if live['current_revision'] != scene['current_revision'] or live['active_generation_id'] != scene.get('active_generation_id'):
                raise DomainError('Кадр изменился во время утверждения. Обновите страницу и повторите.', 409)
            connection.execute('''INSERT INTO reviews(
                generation_id,accepted,note,created_at,status,reason_code,continuity_json,passport_json,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(generation_id) DO UPDATE SET
                accepted=excluded.accepted,note=excluded.note,status=excluded.status,
                reason_code=excluded.reason_code,continuity_json=excluded.continuity_json,
                passport_json=excluded.passport_json,updated_at=excluded.updated_at''',
                (generation_id, accepted, str(note)[:4000], timestamp, decision,
                 str(reason_code)[:100], dump(continuity), dump(passport), timestamp))
            if decision == 'approved':
                if scene.get('active_generation_id') != generation_id:
                    connection.execute("""UPDATE veo_jobs SET status='cancelled',completed_at=?
                        WHERE scene_id=? AND status IN ('queued_omni','generating_omni','queued_veo','generating_veo')""",
                        (timestamp, scene['id']))
                connection.execute('UPDATE scenes SET active_generation_id=? WHERE id=?',
                                   (generation_id, scene['id']))
            elif scene.get('active_generation_id') == generation_id:
                connection.execute("""UPDATE veo_jobs SET status='cancelled',completed_at=?
                    WHERE scene_id=? AND status IN ('queued_omni','generating_omni','queued_veo','generating_veo')""",
                    (timestamp, scene['id']))
                connection.execute('UPDATE scenes SET video_asset_id=NULL WHERE id=?', (scene['id'],))
            self.db.event(connection, generation['project_id'], 'generation_reviewed', {
                'generation_id': generation_id, 'scene_id': scene['id'], 'decision': decision,
                'reason_code': str(reason_code)[:100],
            })
        return {'generation_id': generation_id, 'decision': decision,
                'review': self.generation_review(generation_id)}

    def approve_selected_stills(self, project_id, data):
        """Approve the viewed, selected image of each supplied scene; no generation."""
        if not self._requires_still_approval(project_id) and self._project_profile_type(project_id) != 'forgotten_cities':
            raise DomainError('Утверждение кадров доступно профилю «Забытые города».', 409)
        items = data.get('scenes')
        if not isinstance(items, list) or not items or len(items) > 8 or not all(isinstance(item, dict) for item in items):
            raise DomainError('Передайте от 1 до 8 выбранных изображений за один шаг.')
        ids = [item.get('scene_id') for item in items]
        if not all(isinstance(sid, str) for sid in ids) or len(set(ids)) != len(ids):
            raise DomainError('Список кадров некорректен.')
        validated = []
        for item in items:
            scene = self.scene(item['scene_id'])
            if scene['project_id'] != project_id:
                raise DomainError('Кадр принадлежит другому проекту.')
            if scene['current_revision'] != item.get('revision') or scene.get('active_generation_id') != item.get('generation_id'):
                raise DomainError('Выбранное изображение изменилось. Обновите страницу и повторите утверждение.', 409)
            validated.append(scene)
        approved = []; already = []; failed = []
        for scene in validated:
            try:
                if self._current_still_approval(scene).get('status') == 'approved':
                    already.append(scene['id']); continue
                self.review_generation(scene.get('active_generation_id'), 'approved', reason_code='bulk_selected_approval')
                approved.append(scene['id'])
            except DomainError as error:
                failed.append({'scene_id': scene['id'], 'position': scene['position'], 'error': str(error)})
        return {'approved': approved, 'already_approved': already, 'failed': failed}

    def settings(self):
        defaults = {'comfy_url': 'http://127.0.0.1:8188',
                    'text_backend': 'deepseek',
                    'deepseek_api_key': os.environ.get('DEEPSEEK_API_KEY', ''),
                    'deepseek_model': 'deepseek-flash',
                    'unet': 'flux-2-klein-4b-Q4_K_M.gguf', 'clip': 'Qwen3-4B-Q4_K_M.gguf',
                    'vae': 'flux2-klein-vae.safetensors', 'width': 1024, 'height': 576,
                    'job_timeout_seconds': 1800,
                    'image_backend': 'flow',
                    'local_image_generation_enabled': False,
                    'flow_project_url': '',
                    'flow_image_model': '🍌 Nano Banana 2',
                    'pollinations_model': 'flux',
                    'google_api_key': '',
                    'google_extra_api_keys': '',
                    'google_model': 'gemini-3.1-flash-image',
                    'google_text_model': 'gemini-3.1-flash-lite',
                    'google_queue_enabled': False,
                    'source_contract_enabled': False,
                    'google_quota_registry': {'version': 1, 'budget_fraction': 0.8, 'groups': []}}
        if self.config_path.exists():
            saved = json.loads(self.config_path.read_text(encoding='utf-8'))
            if 'ollama_url' in saved and 'text_url' not in saved:
                saved['text_url'] = defaults['text_url']
            settings = {**defaults, **{key: value for key, value in saved.items() if key in defaults}}
            # Migrate old text-backend values to deepseek
            if settings.get('text_backend') in ('local', 'hybrid', 'groq', 'nvidia', 'flow', None):
                settings['text_backend'] = 'deepseek'
            if not settings.get('deepseek_api_key'):
                settings['deepseek_api_key'] = os.environ.get('DEEPSEEK_API_KEY', '')
            # The RTX 3070 bundle is validated at 1024×576. Do not resume an
            # old 1536×864 setting after a restart: it can exceed the safety
            # margin even when ComfyUI runs in low-VRAM mode.
            if (settings['width'], settings['height']) not in ((1024, 576), (768, 432)):
                settings['width'], settings['height'] = 1024, 576
            return settings
        return defaults

    def save_settings(self, values):
        from .providers import JsonHTTP
        if self.db.one("SELECT id FROM generations WHERE state IN ('preparing','submitted','running','saving','reconciling')"):
            raise DomainError('Нельзя менять backend во время активной генерации.', 409)
        settings = self.settings()
        if 'analysis_backend' in values and 'text_backend' not in values:
            values['text_backend'] = values['analysis_backend']
        values = dict(values)
        if 'deepseek_api_key' in values and not values['deepseek_api_key']:
            values.pop('deepseek_api_key')
        if 'pixabay_api_key' in values:
            pix_val = values.pop('pixabay_api_key')
            if pix_val and isinstance(pix_val, str) and pix_val.strip():
                from .stock_secrets import set_pixabay_key
                set_pixabay_key(self.root, pix_val.strip())
        for key in settings:
            if key in values:
                settings[key] = values[key]
        JsonHTTP(settings['comfy_url'])
        for key in ('unet', 'clip', 'vae', 'pollinations_model', 'google_api_key', 'google_extra_api_keys', 'google_model', 'google_text_model', 'flow_project_url', 'flow_image_model', 'deepseek_api_key', 'deepseek_model'):
            if key in settings and (not isinstance(settings[key], str) or len(settings[key]) > 2000):
                raise DomainError(f'Неверное значение для {key}.')
        settings['text_backend'] = 'deepseek'
        if type(settings.get('google_queue_enabled')) is not bool:
            raise DomainError('Параметр google_queue_enabled должен быть логическим значением.')
        if type(settings.get('source_contract_enabled')) is not bool:
            raise DomainError('Параметр единого задания сцены должен быть логическим значением.')
        if not isinstance(settings.get('google_quota_registry'), dict):
            raise DomainError('Неверный реестр квот Google.')
        if settings.get('google_queue_enabled'):
            from .google_queue import configured_queue
            try:
                configured_queue(self.root,settings)
            except ValueError as error:
                raise DomainError('Неверный реестр квот Google: ' + str(error)) from None
        if settings['image_backend'] not in ('comfy', 'flow', 'pollinations', 'google_nanobanana'):
            raise DomainError('Неподдерживаемый бэкенд изображений.')
        if type(settings['local_image_generation_enabled']) is not bool:
            raise DomainError('Параметр локальной генерации должен быть логическим значением.')
        if settings['image_backend'] == 'comfy' and not settings['local_image_generation_enabled']:
            raise DomainError('Локальный генератор удалён. Выберите Google Flow.', 409)
        if settings['flow_image_model'] not in ('🍌 Nano Banana 2 Lite', '🍌 Nano Banana 2', '🍌 Nano Banana Pro'):
            raise DomainError('Выберите поддержанную модель Google Flow.')
        if (settings['width'], settings['height']) not in ((1024, 576), (768, 432)):
            raise DomainError('Выберите поддержанный размер 16:9.')
        if type(settings['job_timeout_seconds']) is not int or not 60 <= settings['job_timeout_seconds'] <= 7200:
            raise DomainError('Тайм-аут должен быть от 60 до 7200 секунд.')
        obsolete_keys = {'groq_api_key', 'groq_extra_api_keys', 'groq_model', 'nvidia_api_key', 'nvidia_model',
                         'nvidia_groq_assist', 'story_analyzer_version', 'v3_semantic_first', 'v3_two_layer_audit',
                         'v3_two_layer_audit_mode', 'text_url', 'text_model'}
        clean_settings = {k: v for k, v in settings.items() if k not in obsolete_keys}
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(dump(clean_settings), encoding='utf-8')
        temporary.replace(self.config_path)
        return settings

    # ----------------------------------------------------------------------
    # Channel Profiles & Versions Service Layer
    # ----------------------------------------------------------------------

    def list_channels(self, include_archived=False):
        """List all channel profiles with summary of active version, primary character, and references."""
        sql = "SELECT * FROM channel_profiles"
        if not include_archived:
            sql += " WHERE is_archived=0"
        sql += " ORDER BY created_at DESC"
        channels = self.db.all(sql)
        for ch in channels:
            ch['is_archived'] = bool(ch.get('is_archived'))
            active_version = None
            if ch.get('active_version_id'):
                active_version = self.db.one("SELECT * FROM channel_profile_versions WHERE id=?", (ch['active_version_id'],))
            if not active_version:
                active_version = self.db.one("SELECT * FROM channel_profile_versions WHERE channel_id=? ORDER BY version_number DESC LIMIT 1", (ch['id'],))
            ch['active_version'] = active_version
            
            # Main character preview
            main_char = self.db.one("SELECT * FROM channel_characters WHERE channel_id=? AND role='main' AND is_active=1 LIMIT 1", (ch['id'],))
            if not main_char:
                main_char = self.db.one("SELECT * FROM channel_characters WHERE channel_id=? AND is_active=1 ORDER BY rowid ASC LIMIT 1", (ch['id'],))
            ch['main_character'] = main_char
            
            # Primary reference assets
            refs = self.db.all("SELECT * FROM channel_reference_assets WHERE channel_id=? ORDER BY is_primary DESC, sort_order ASC", (ch['id'],))
            ch['reference_assets_count'] = len(refs)
            primary_ref = next((r for r in refs if r.get('is_primary')), refs[0] if refs else None)
            ch['primary_reference_asset'] = primary_ref
        return channels

    def get_channel(self, channel_id):
        """Get complete channel details including all versions, characters, and reference assets."""
        ch = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (channel_id,))
        if not ch:
            raise DomainError("Профиль канала не найден.", 404)
        ch = dict(ch)
        ch['is_archived'] = bool(ch.get('is_archived'))
        
        versions = self.db.all("SELECT * FROM channel_profile_versions WHERE channel_id=? ORDER BY version_number ASC", (channel_id,))
        ch['versions'] = versions
        active_version = next((v for v in versions if v['id'] == ch.get('active_version_id')), versions[-1] if versions else None)
        ch['active_version'] = active_version
        
        characters = self.db.all("SELECT * FROM channel_characters WHERE channel_id=? ORDER BY rowid ASC", (channel_id,))
        for c in characters:
            c_versions = self.db.all("SELECT * FROM channel_character_versions WHERE character_id=? ORDER BY version_number ASC", (c['id'],))
            c['versions'] = c_versions
            c['active_version'] = c_versions[-1] if c_versions else None
            c['is_locked'] = bool(c.get('is_locked'))
            c['is_active'] = bool(c.get('is_active'))
        ch['characters'] = characters
        
        assets = self.db.all("SELECT * FROM channel_reference_assets WHERE channel_id=? ORDER BY sort_order ASC, created_at ASC", (channel_id,))
        for a in assets:
            a['is_primary'] = bool(a.get('is_primary'))
        ch['reference_assets'] = assets
        
        if active_version:
            char_summaries = []
            for c in characters:
                c_ver = c.get('active_version') or {}
                char_summaries.append({**c, **c_ver})
            ch['compiled_style_contract'] = compile_channel_style_contract(ch, active_version, char_summaries)
        else:
            ch['compiled_style_contract'] = ""
            
        return ch

    def create_channel(self, data):
        """Create a new channel profile with immutable version 1, optional default main character, and version snapshot."""
        name = required_text(data.get('name'), 'Название канала', 200)
        slug = str(data.get('slug') or slugify_id(name)).strip()[:100]
        description = str(data.get('description') or '')[:2000]
        profile_type = data.get('profile_type', 'stickman')
        if profile_type not in VALID_PROFILE_TYPES:
            profile_type = 'stickman'
            
        channel_id = uid()
        version_id = uid()
        ts = now()
        
        is_stickman_type = profile_type in ('stickman', 'stickman_story')
        if is_stickman_type:
            version_data = dict(STICKMAN_DEFAULT_VERSION)
        elif profile_type == 'forgotten_cities':
            version_data = dict(FORGOTTEN_CITIES_DEFAULT_VERSION)
        else:
            version_data = {
            'style_prompt': 'Cinematic digital art, clean consistent style.',
            'negative_prompt': 'noisy, text, blurry, distorted.',
            'composition_rules': 'Standard wide composition.',
            'color_palette': '',
            'line_style': '',
            'background_style': '',
            'aspect_ratio': '16:9',
            'animation_rules': '',
            }
        if isinstance(data.get('version'), dict):
            version_data.update(data['version'])
        elif isinstance(data.get('style_prompt'), str):
            for k in ('style_prompt', 'negative_prompt', 'composition_rules', 'color_palette', 'line_style', 'background_style', 'aspect_ratio', 'animation_rules'):
                if k in data:
                    version_data[k] = data[k]
                    
        should_init_char = (
            profile_type != 'forgotten_cities'
            and data.get('init_stickman') is not False
            and data.get('characters') != []
        )
        char_data = dict(STICKMAN_DEFAULT_CHARACTER) if is_stickman_type else {
            'canonical_name': 'Главный Герой',
            'role': 'main',
            'description': 'Основной постоянный персонаж канала.',
            'appearance_prompt': 'Main recurring protagonist with distinct recognizable appearance.',
            'negative_prompt': 'inconsistent face, distorted anatomy.',
            'is_locked': 1,
            'is_active': 1,
        }
        if isinstance(data.get('character'), dict):
            char_data.update(data['character'])
            should_init_char = True
            
        char_version_data = dict(STICKMAN_DEFAULT_CHARACTER_VERSION) if is_stickman_type else {
            'appearance_prompt': char_data.get('appearance_prompt', ''),
            'wardrobe_rules': '',
            'proportion_rules': '',
            'face_rules': '',
            'color_rules': '',
        }
        if isinstance(data.get('character_version'), dict):
            char_version_data.update(data['character_version'])
            should_init_char = True
            
        char_id = uid()
        char_ver_id = uid()
        
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO channel_profiles(id, name, slug, description, profile_type, is_archived, created_at, updated_at, active_version_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (channel_id, name, slug, description, profile_type, 0, ts, ts, version_id)
            )
            conn.execute(
                """INSERT INTO channel_profile_versions(id, channel_id, version_number, style_prompt, negative_prompt, composition_rules, color_palette, line_style, background_style, aspect_ratio, animation_rules, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, channel_id, 1,
                 version_data.get('style_prompt', ''),
                 version_data.get('negative_prompt', ''),
                 version_data.get('composition_rules', ''),
                 version_data.get('color_palette', ''),
                 version_data.get('line_style', ''),
                 version_data.get('background_style', ''),
                 version_data.get('aspect_ratio', '16:9'),
                 version_data.get('animation_rules', ''),
                 ts)
            )
            if should_init_char:
                conn.execute(
                    """INSERT INTO channel_characters(id, channel_id, canonical_name, role, description, appearance_prompt, negative_prompt, is_locked, is_active)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (char_id, channel_id,
                     char_data.get('canonical_name', 'Стикмен'),
                     char_data.get('role', 'main'),
                     char_data.get('description', ''),
                     char_data.get('appearance_prompt', ''),
                     char_data.get('negative_prompt', ''),
                     1 if char_data.get('is_locked', 1) else 0,
                     1 if char_data.get('is_active', 1) else 0)
                )
                conn.execute(
                    """INSERT INTO channel_character_versions(id, character_id, version_number, appearance_prompt, wardrobe_rules, proportion_rules, face_rules, color_rules, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (char_ver_id, char_id, 1,
                     char_version_data.get('appearance_prompt', ''),
                     char_version_data.get('wardrobe_rules', ''),
                     char_version_data.get('proportion_rules', ''),
                     char_version_data.get('face_rules', ''),
                     char_version_data.get('color_rules', ''),
                     ts)
                )
            
            if isinstance(data.get('references'), list):
                for idx, r in enumerate(data['references']):
                    asset_id = uid()
                    conn.execute(
                        """INSERT INTO channel_reference_assets(id, channel_id, character_id, asset_type, file_path, content_hash, sort_order, is_primary, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (asset_id, channel_id,
                         r.get('character_id', char_id if r.get('asset_type') in ('character_front', 'character_pose', 'expression_sheet') else None),
                         r.get('asset_type', 'character_front'),
                         r.get('file_path', ''),
                         r.get('content_hash', ''),
                         idx,
                         1 if r.get('is_primary', idx == 0) else 0,
                         ts)
                    )
                    
        return self.get_channel(channel_id)

    def update_channel(self, channel_id, data):
        """Update channel metadata or save a new version if style fields changed."""
        ch = self.get_channel(channel_id)
        name = required_text(data.get('name', ch['name']), 'Название канала', 200)
        slug = str(data.get('slug', ch.get('slug', slugify_id(name)))).strip()[:100]
        description = str(data.get('description', ch['description']))[:2000]
        ts = now()
        
        active_ver = ch.get('active_version') or {}
        version_fields = ['style_prompt', 'negative_prompt', 'composition_rules', 'color_palette', 'line_style', 'background_style', 'aspect_ratio', 'animation_rules']
        has_version_change = any(k in data and str(data[k]) != str(active_ver.get(k, '')) for k in version_fields)
        
        new_version_id = ch.get('active_version_id')
        with self.db.transaction() as conn:
            if has_version_change or data.get('force_new_version'):
                new_version_id = uid()
                last_ver_num = max([v['version_number'] for v in ch.get('versions', [])] or [0])
                new_ver_num = last_ver_num + 1
                v_data = {k: data.get(k, active_ver.get(k, '')) for k in version_fields}
                conn.execute(
                    """INSERT INTO channel_profile_versions(id, channel_id, version_number, style_prompt, negative_prompt, composition_rules, color_palette, line_style, background_style, aspect_ratio, animation_rules, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_version_id, channel_id, new_ver_num,
                     v_data['style_prompt'], v_data['negative_prompt'], v_data['composition_rules'],
                     v_data['color_palette'], v_data['line_style'], v_data['background_style'],
                     v_data.get('aspect_ratio', '16:9'), v_data['animation_rules'], ts)
                )
            conn.execute(
                "UPDATE channel_profiles SET name=?, slug=?, description=?, active_version_id=?, updated_at=? WHERE id=?",
                (name, slug, description, new_version_id, ts, channel_id)
            )
        return self.get_channel(channel_id)

    def duplicate_channel(self, channel_id, new_name=None):
        """Clone an existing channel profile with fresh UUIDs and isolated assets."""
        orig = self.get_channel(channel_id)
        name = (new_name or f"{orig['name']} (Копия)").strip()[:200]
        orig_slug = orig.get('slug') or slugify_id(orig['name'])
        slug = f"{orig_slug}-copy"[:100]
        new_channel_id = uid()
        new_version_id = uid()
        ts = now()
        
        active_ver = orig.get('active_version') or STICKMAN_DEFAULT_VERSION
        
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO channel_profiles(id, name, slug, description, profile_type, is_archived, created_at, updated_at, active_version_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_channel_id, name, slug, orig.get('description', ''), orig.get('profile_type', 'stickman_story'), 0, ts, ts, new_version_id)
            )
            conn.execute(
                """INSERT INTO channel_profile_versions(id, channel_id, version_number, style_prompt, negative_prompt, composition_rules, color_palette, line_style, background_style, aspect_ratio, animation_rules, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_version_id, new_channel_id, 1,
                 active_ver.get('style_prompt', ''),
                 active_ver.get('negative_prompt', ''),
                 active_ver.get('composition_rules', ''),
                 active_ver.get('color_palette', ''),
                 active_ver.get('line_style', ''),
                 active_ver.get('background_style', ''),
                 active_ver.get('aspect_ratio', '16:9'),
                 active_ver.get('animation_rules', ''),
                 ts)
            )
            
            char_id_map = {}
            for c in orig.get('characters', []):
                new_char_id = uid()
                char_id_map[c['id']] = new_char_id
                conn.execute(
                    """INSERT INTO channel_characters(id, channel_id, canonical_name, role, description, appearance_prompt, negative_prompt, is_locked, is_active)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_char_id, new_channel_id, c['canonical_name'], c.get('role', 'main'),
                     c.get('description', ''), c.get('appearance_prompt', ''), c.get('negative_prompt', ''),
                     1 if c.get('is_locked') else 0, 1 if c.get('is_active') else 0)
                )
                c_ver = c.get('active_version') or {}
                new_c_ver_id = uid()
                conn.execute(
                    """INSERT INTO channel_character_versions(id, character_id, version_number, appearance_prompt, wardrobe_rules, proportion_rules, face_rules, color_rules, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_c_ver_id, new_char_id, 1,
                     c_ver.get('appearance_prompt', c.get('appearance_prompt', '')),
                     c_ver.get('wardrobe_rules', ''),
                     c_ver.get('proportion_rules', ''),
                     c_ver.get('face_rules', ''),
                     c_ver.get('color_rules', ''),
                     ts)
                )
                
            for a in orig.get('reference_assets', []):
                new_asset_id = uid()
                new_char_link = char_id_map.get(a.get('character_id')) if a.get('character_id') else None
                conn.execute(
                    """INSERT INTO channel_reference_assets(id, channel_id, character_id, asset_type, file_path, content_hash, sort_order, is_primary, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_asset_id, new_channel_id, new_char_link, a['asset_type'], a['file_path'], a['content_hash'],
                     a.get('sort_order', 0), 1 if a.get('is_primary') else 0, ts)
                )
                
        return self.get_channel(new_channel_id)

    def archive_channel(self, channel_id):
        """Archive a channel profile rather than hard deleting it."""
        self.get_channel(channel_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE channel_profiles SET is_archived=1, updated_at=? WHERE id=?", (now(), channel_id))
        return self.get_channel(channel_id)

    def unarchive_channel(self, channel_id):
        """Restore an archived channel profile."""
        self.get_channel(channel_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE channel_profiles SET is_archived=0, updated_at=? WHERE id=?", (now(), channel_id))
        return self.get_channel(channel_id)

    def save_channel_version(self, channel_id, data):
        """Create a new immutable version of a channel profile and set as active."""
        ch = self.get_channel(channel_id)
        last_ver_num = max([v['version_number'] for v in ch.get('versions', [])] or [0])
        new_ver_num = last_ver_num + 1
        new_version_id = uid()
        ts = now()
        active_ver = ch.get('active_version') or {}
        
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO channel_profile_versions(id, channel_id, version_number, style_prompt, negative_prompt, composition_rules, color_palette, line_style, background_style, aspect_ratio, animation_rules, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_version_id, channel_id, new_ver_num,
                 data.get('style_prompt', active_ver.get('style_prompt', '')),
                 data.get('negative_prompt', active_ver.get('negative_prompt', '')),
                 data.get('composition_rules', active_ver.get('composition_rules', '')),
                 data.get('color_palette', active_ver.get('color_palette', '')),
                 data.get('line_style', active_ver.get('line_style', '')),
                 data.get('background_style', active_ver.get('background_style', '')),
                 data.get('aspect_ratio', active_ver.get('aspect_ratio', '16:9')),
                 data.get('animation_rules', active_ver.get('animation_rules', '')),
                 ts)
            )
            conn.execute(
                "UPDATE channel_profiles SET active_version_id=?, updated_at=? WHERE id=?",
                (new_version_id, ts, channel_id)
            )
        return self.get_channel(channel_id)

    def save_channel_character(self, channel_id, data):
        """Create or update a channel character and create a new immutable character version."""
        ch = self.get_channel(channel_id)
        char_id = data.get('id')
        canonical_name = required_text(data.get('canonical_name') or data.get('name'), 'Имя персонажа', 100)
        role = data.get('role', 'main')
        if role not in VALID_CHARACTER_ROLES:
            role = 'main'
        description = str(data.get('description') or '')[:2000]
        appearance_prompt = str(data.get('appearance_prompt') or '')[:2000]
        negative_prompt = str(data.get('negative_prompt') or '')[:2000]
        is_locked = 1 if data.get('is_locked', 1) else 0
        is_active = 1 if data.get('is_active', 1) else 0
        ts = now()
        
        with self.db.transaction() as conn:
            if not char_id:
                char_id = uid()
                conn.execute(
                    """INSERT INTO channel_characters(id, channel_id, canonical_name, role, description, appearance_prompt, negative_prompt, is_locked, is_active)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (char_id, channel_id, canonical_name, role, description, appearance_prompt, negative_prompt, is_locked, is_active)
                )
                ver_num = 1
            else:
                conn.execute(
                    """UPDATE channel_characters SET canonical_name=?, role=?, description=?, appearance_prompt=?, negative_prompt=?, is_locked=?, is_active=?
                       WHERE id=? AND channel_id=?""",
                    (canonical_name, role, description, appearance_prompt, negative_prompt, is_locked, is_active, char_id, channel_id)
                )
                existing_vers = conn.execute("SELECT version_number FROM channel_character_versions WHERE character_id=?", (char_id,)).fetchall()
                ver_num = max([r[0] for r in existing_vers] or [0]) + 1
                
            char_ver_id = uid()
            conn.execute(
                """INSERT INTO channel_character_versions(id, character_id, version_number, appearance_prompt, wardrobe_rules, proportion_rules, face_rules, color_rules, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (char_ver_id, char_id, ver_num,
                 appearance_prompt,
                 str(data.get('wardrobe_rules') or '')[:2000],
                 str(data.get('proportion_rules') or '')[:2000],
                 str(data.get('face_rules') or '')[:2000],
                 str(data.get('color_rules') or '')[:2000],
                 ts)
            )
            conn.execute("UPDATE channel_profiles SET updated_at=? WHERE id=?", (ts, channel_id))
            
        return self.get_channel(channel_id)

    def upload_channel_reference(self, channel_id, character_id, asset_type, file_bytes, filename=None, is_primary=0):
        """Save a reference image asset for a channel and record in database."""
        ch = self.get_channel(channel_id)
        if not file_bytes or not isinstance(file_bytes, bytes):
            raise DomainError("Пустые данные файла изображения.", 400)
        if asset_type not in VALID_CHANNEL_ASSET_TYPES:
            asset_type = 'character_front'
            
        sha = hashlib.sha256(file_bytes).hexdigest()
        ext = '.png'
        if filename and '.' in filename:
            ext = Path(filename).suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
            ext = '.png'
            
        rel_dir = Path("channel_references") / channel_id
        target_dir = self.root / "data" / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        rel_path = f"data/channel_references/{channel_id}/{sha[:16]}{ext}"
        abs_path = self.root / rel_path
        abs_path.write_bytes(file_bytes)
        
        asset_id = uid()
        ts = now()
        with self.db.transaction() as conn:
            if is_primary:
                conn.execute(
                    "UPDATE channel_reference_assets SET is_primary=0 WHERE channel_id=? AND asset_type=?",
                    (channel_id, asset_type)
                )
            existing_count = conn.execute("SELECT count(*) FROM channel_reference_assets WHERE channel_id=?", (channel_id,)).fetchone()[0]
            conn.execute(
                """INSERT INTO channel_reference_assets(id, channel_id, character_id, asset_type, file_path, content_hash, sort_order, is_primary, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (asset_id, channel_id, character_id, asset_type, rel_path, sha, existing_count, 1 if is_primary else 0, ts)
            )
            conn.execute("UPDATE channel_profiles SET updated_at=? WHERE id=?", (ts, channel_id))
            
        return self.get_channel(channel_id)

    def delete_channel_reference(self, channel_id, asset_id):
        """Delete a channel reference asset."""
        self.get_channel(channel_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM channel_reference_assets WHERE id=? AND channel_id=?", (asset_id, channel_id))
            conn.execute("UPDATE channel_profiles SET updated_at=? WHERE id=?", (now(), channel_id))
        return self.get_channel(channel_id)

    def preview_project_channel_update(self, project_id, target_version_id=None):
        """Calculate diff and list affected scenes before updating project to a new channel version."""
        proj = self.project(project_id)
        channel_id = proj.get('channel_profile_id')
        if not channel_id:
            raise DomainError("Проект не привязан к каналу.", 400)
            
        ch = self.get_channel(channel_id)
        current_version_id = proj.get('channel_profile_version_id')
        target_version = None
        if target_version_id:
            target_version = next((v for v in ch['versions'] if v['id'] == target_version_id), None)
        if not target_version:
            target_version = ch.get('active_version')
        if not target_version:
            raise DomainError("Целевая версия профиля канала не найдена.", 404)
            
        current_version = next((v for v in ch['versions'] if v['id'] == current_version_id), None)
        
        scenes = self.db.all("SELECT id, position FROM scenes WHERE project_id=? ORDER BY position", (project_id,))
        affected_scenes = []
        for s in scenes:
            rev_row = self.db.one("SELECT data FROM scene_revisions WHERE scene_id=?", (s['id'],))
            s_data = json.loads(rev_row['data']) if rev_row else {}
            affected_scenes.append({
                'scene_id': s['id'],
                'position': s['position'],
                'beat': s_data.get('beat', ''),
                'visible_people': s_data.get('visible_people', 0),
                'characters': s_data.get('characters', []),
            })
            
        style_diff = {
            'old_version_number': current_version['version_number'] if current_version else None,
            'new_version_number': target_version['version_number'],
            'old_style_prompt': current_version.get('style_prompt', '') if current_version else '',
            'new_style_prompt': target_version.get('style_prompt', ''),
            'old_line_style': current_version.get('line_style', '') if current_version else '',
            'new_line_style': target_version.get('line_style', ''),
            'old_background_style': current_version.get('background_style', '') if current_version else '',
            'new_background_style': target_version.get('background_style', ''),
        }
        
        return {
            'project_id': project_id,
            'channel_id': channel_id,
            'channel_name': ch['name'],
            'current_version_id': current_version_id,
            'target_version_id': target_version['id'],
            'style_diff': style_diff,
            'affected_scenes_count': len(affected_scenes),
            'affected_scenes': affected_scenes,
            'can_apply': current_version_id != target_version['id'],
        }

    def apply_project_channel_update(self, project_id, target_version_id=None):
        """Apply new channel version snapshot to project."""
        proj = self.project(project_id)
        channel_id = proj.get('channel_profile_id')
        if not channel_id:
            raise DomainError("Проект не привязан к каналу.", 400)
            
        ch = self.get_channel(channel_id)
        target_version = None
        if target_version_id:
            target_version = next((v for v in ch['versions'] if v['id'] == target_version_id), None)
        if not target_version:
            target_version = ch.get('active_version')
        if not target_version:
            raise DomainError("Целевая версия профиля канала не найдена.", 404)
            
        char_map = {}
        for c in ch.get('characters', []):
            c_ver = c.get('active_version') or {}
            char_map[c['id']] = {
                'canonical_name': c['canonical_name'],
                'version_id': c_ver.get('id'),
                'version_number': c_ver.get('version_number', 1)
            }
            
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE projects SET channel_profile_version_id=?, channel_character_version_map=?, style=?, version=version+1 WHERE id=?",
                (target_version['id'], dump(char_map), target_version.get('style_prompt', proj['style']), project_id)
            )
            self.db.event(conn, project_id, 'channel_profile_updated', {
                'channel_id': channel_id,
                'target_version_id': target_version['id'],
                'version_number': target_version['version_number']
            })
            
        return self.detail(project_id)

    # ----------------------------------------------------------------------
    # Projects Service Methods
    # ----------------------------------------------------------------------

    def project(self, project_id):
        project = self.db.one('SELECT * FROM projects WHERE id=? AND NOT EXISTS (SELECT 1 FROM project_trash WHERE project_id=projects.id)', (project_id,))
        if not project:
            raise DomainError('Проект не найден.', 404)
        return project

    def create_project(self, data):
        title = required_text(data.get('title'), 'Название', 200)
        story = required_text(data.get('story'), 'История', 160000)
        idea = str(data.get('idea', ''))[:4000]
        mode = data.get('mode', 'characters')
        if mode not in PROJECT_MODES:
            raise DomainError('Неизвестный режим проекта.')
        camera_mode = data.get('camera_mode', 'cinema')
        if camera_mode not in CAMERA_MODES:
            camera_mode = 'cinema'
        camera_pacing = data.get('camera_pacing', 'steady')
        if camera_pacing not in CAMERA_PACINGS:
            camera_pacing = 'steady'
        aspect_ratio = data.get('aspect_ratio', '16:9')
        if aspect_ratio not in ASPECT_RATIOS:
            aspect_ratio = '16:9'

        channel_profile_id = data.get('channel_profile_id')
        channel_version_id = None
        char_version_map = {}
        style = str(data.get('style') or '').strip()

        if channel_profile_id:
            ch = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (channel_profile_id,))
            if not ch:
                raise DomainError('Выбранный профиль канала не найден.', 404)
            if ch.get('is_archived'):
                raise DomainError('Выбранный профиль канала заархивирован.', 400)
            active_ver = None
            if ch.get('active_version_id'):
                active_ver = self.db.one("SELECT * FROM channel_profile_versions WHERE id=?", (ch['active_version_id'],))
            if not active_ver:
                active_ver = self.db.one("SELECT * FROM channel_profile_versions WHERE channel_id=? ORDER BY version_number DESC LIMIT 1", (channel_profile_id,))
            if active_ver:
                channel_version_id = active_ver['id']
                if not style:
                    style = active_ver['style_prompt']
            chars = self.db.all("SELECT * FROM channel_characters WHERE channel_id=? AND is_active=1", (channel_profile_id,))
            for c in chars:
                c_ver = self.db.one("SELECT * FROM channel_character_versions WHERE character_id=? ORDER BY version_number DESC LIMIT 1", (c['id'],))
                char_version_map[c['id']] = {
                    'canonical_name': c['canonical_name'],
                    'version_id': c_ver['id'] if c_ver else None,
                    'version_number': c_ver['version_number'] if c_ver else 1
                }

        if not style:
            style = 'Cinematic realism, natural lighting'

        project_id = uid()
        with self.db.transaction() as connection:
            connection.execute(
                'INSERT INTO projects(id,title,idea,story,style,mode,camera_mode,camera_pacing,aspect_ratio,channel_profile_id,channel_profile_version_id,channel_character_version_map,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (project_id, title, idea, story, style, mode, camera_mode, camera_pacing, aspect_ratio, channel_profile_id, channel_version_id, dump(char_version_map), now())
            )
        self.update_camera_settings(project_id, {'camera_custom': data.get('camera_custom', '')})
        return self.project(project_id)

    def update_camera_settings(self, project_id, data):
        self.project(project_id)
        updates, params = [], []
        if 'camera_custom' in data:
            if not isinstance(data['camera_custom'], str) or len(data['camera_custom']) > 1000:
                raise DomainError('Описание камеры должно быть текстом до 1000 символов.')
            updates.append('camera_custom=?')
            params.append(data['camera_custom'])
        if 'camera_mode' in data and data['camera_mode'] in CAMERA_MODES:
            updates.append('camera_mode=?')
            params.append(data['camera_mode'])
        if 'camera_pacing' in data and data['camera_pacing'] in CAMERA_PACINGS:
            updates.append('camera_pacing=?')
            params.append(data['camera_pacing'])
        if 'aspect_ratio' in data and data['aspect_ratio'] in ASPECT_RATIOS:
            updates.append('aspect_ratio=?')
            params.append(data['aspect_ratio'])
        if updates:
            params.append(project_id)
            with self.db.transaction() as connection:
                connection.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id=?", params)
        return self.project(project_id)

    def detail(self, project_id):
        result = self.project(project_id)
        result['cast_bible'] = json.loads(result.get('cast_bible') or '{}')
        result['analysis_metadata'] = json.loads(result.get('analysis_metadata') or '{}')
        from .voice_workflow import public as voice_public
        result['voice_workflow'] = voice_public(self.db.one('SELECT * FROM voice_workflows WHERE project_id=?', (project_id,)))
        result['animation_settings'] = json.loads(result.get('animation_settings') or '{}') or dict(DEFAULT_ANIMATION_SETTINGS)
        result['characters'] = self.db.all('SELECT * FROM characters WHERE project_id=? ORDER BY rowid', (project_id,))
        from .subject_policy import enrich
        result['characters'] = enrich(result['characters'], result['cast_bible'])
        for character in result['characters']:
            character['wardrobe'] = json.loads(character['wardrobe'])
            character['aliases'] = json.loads(character.get('aliases') or '[]')
            character['evidence'] = json.loads(character.get('evidence') or '[]')
            character['references'] = json.loads(character.pop('references_json'))
            character.setdefault('cast_id', character['id'])
            from .visual_style import is_marker_style
            if is_marker_style(result.get('style')):
                from .portrait_contract import identity_description, subject_gender
                character['visual_gender'] = subject_gender(character['description'],character['name'])
        char_by_cast = {c.get('cast_id') or c['id']: c for c in result['characters']}
        char_by_id = {c['id']: c for c in result['characters']}
        result['locations'] = self.db.all('SELECT * FROM locations WHERE project_id=? ORDER BY rowid', (project_id,))
        for location in result['locations']:
            location['references'] = json.loads(location.pop('references_json'))
            location['casting'] = self.reference_cast('location', location)
        result['props'] = self.db.all('SELECT * FROM props WHERE project_id=? ORDER BY rowid', (project_id,))
        for prop in result['props']:
            prop['references'] = json.loads(prop.pop('references_json'))
            prop['casting'] = self.reference_cast('prop', prop)
        result['scenes'] = self.db.all('SELECT * FROM scenes WHERE project_id=? ORDER BY position', (project_id,))
        for scene in result['scenes']:
            row = self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?',
                              (scene['id'], scene['current_revision']))
            # Keep the stored revision separate from the display copy.  The
            # display copy is enriched with the current Cast Bible names
            # below; using it for stale detection made a scene look stale
            # whenever a character label was synced, even when its image and
            # its actual scene content had not changed.
            persisted_scene_data = json.loads(row['data'])
            scene['data'] = copy.deepcopy(persisted_scene_data)
            if scene['data'].get('character_ids'):
                synced = [
                    (char_by_cast.get(cid) or char_by_id.get(cid, {})).get('name')
                    for cid in scene['data']['character_ids']
                    if (char_by_cast.get(cid) or char_by_id.get(cid))
                ]
                if synced:
                    scene['data']['characters'] = synced
            scene['stale'] = False
            scene['video_stale'] = False
            active_image_asset_id = None
            if scene['active_generation_id']:
                generation = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (scene['active_generation_id'],))
                if generation:
                    active_image_asset_id = generation.get('asset_id')
                    gen_rev = generation['scene_revision']
                    cur_rev = scene['current_revision']
                    if gen_rev != cur_rev:
                        gen_data_row = self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?', (scene['id'], gen_rev))
                        if gen_data_row:
                            g_data = json.loads(gen_data_row['data'])
                            g_content = {k: v for k, v in g_data.items() if k != 'animation'}
                            c_content = {k: v for k, v in persisted_scene_data.items() if k != 'animation'}
                            scene['stale'] = (g_content != c_content)
                        else:
                            scene['stale'] = True
            if scene.get('video_asset_id'):
                video_asset = self.db.one('SELECT path FROM assets WHERE id=?', (scene['video_asset_id'],))
                if video_asset:
                    video_path = self.root / video_asset['path']
                    try:
                        stat = video_path.stat()
                        scene['video_metadata'] = dict(cached_scene_video_metadata(str(video_path), stat.st_size, stat.st_mtime_ns))
                    except OSError:
                        scene['video_metadata'] = {}
            if scene.get('video_asset_id') and active_image_asset_id:
                vid_source_asset = scene['data'].get('animation', {}).get('source_image_asset_id')
                if not vid_source_asset:
                    v_gen = self.db.one("SELECT snapshot FROM generations WHERE asset_id=? AND purpose='scene_video'", (scene['video_asset_id'],))
                    if v_gen:
                        try:
                            s_data = json.loads(v_gen['snapshot']) if isinstance(v_gen['snapshot'], str) else v_gen['snapshot']
                            vid_source_asset = s_data.get('source_image_asset_id')
                        except Exception:
                            pass
                if vid_source_asset and vid_source_asset != active_image_asset_id:
                    scene['video_stale'] = True
                    if not scene.get('stale_reason'):
                        scene['stale_reason'] = 'Видео создано для другого варианта кадра'
        result['generations'] = self.db.all('SELECT * FROM generations WHERE project_id=? ORDER BY created_at', (project_id,))
        scene_by_id = {scene['id']: scene for scene in result['scenes']}
        for generation in result['generations']:
            generation['snapshot'] = json.loads(generation['snapshot'])
            generation['progress'] = json.loads(generation['progress'])
            if generation.get('purpose') == 'scene':
                current_scene = scene_by_id.get(generation.get('scene_id'))
                generation['current_scene_compatible'] = bool(
                    current_scene and self._generation_matches_current_scene(generation, current_scene)
                )
                generation['review'] = self._decode_review(
                    self.db.one('SELECT * FROM reviews WHERE generation_id=?', (generation['id'],))
                )
        result['batches'] = self.db.all('SELECT * FROM batches WHERE project_id=? ORDER BY created_at DESC', (project_id,))
        result['story_bible'] = build_global_story_bible(result['story'], result['characters'], result['scenes'])
        
        if result.get('channel_profile_id'):
            ch = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (result['channel_profile_id'],))
            if ch:
                active_ver = None
                if ch.get('active_version_id'):
                    active_ver = self.db.one("SELECT * FROM channel_profile_versions WHERE id=?", (ch['active_version_id'],))
                curr_ver = None
                if result.get('channel_profile_version_id'):
                    curr_ver = self.db.one("SELECT * FROM channel_profile_versions WHERE id=?", (result['channel_profile_version_id'],))
                main_char = self.db.one("SELECT * FROM channel_characters WHERE channel_id=? AND role='main' LIMIT 1", (ch['id'],))
                primary_asset = self.db.one("SELECT * FROM channel_reference_assets WHERE channel_id=? AND is_primary=1 LIMIT 1", (ch['id'],))
                if not primary_asset:
                    primary_asset = self.db.one("SELECT * FROM channel_reference_assets WHERE channel_id=? LIMIT 1", (ch['id'],))
                result['channel_profile'] = {
                    'id': ch['id'],
                    'name': ch['name'],
                    'profile_type': ch.get('profile_type', 'stickman_story'),
                    'is_archived': bool(ch.get('is_archived')),
                    'active_version_id': result.get('channel_profile_version_id'),
                    'active_version_number': curr_ver['version_number'] if curr_ver else None,
                    'current_version_id': result.get('channel_profile_version_id'),
                    'current_version_number': curr_ver['version_number'] if curr_ver else None,
                    'latest_version_id': ch.get('active_version_id'),
                    'latest_version_number': active_ver['version_number'] if active_ver else None,
                    'has_update': bool(active_ver and curr_ver and active_ver['id'] != curr_ver['id']),
                    'main_character_name': main_char['canonical_name'] if main_char else None,
                    'primary_reference_path': primary_asset['file_path'] if primary_asset else None,
                }
            else:
                result['channel_profile'] = None
        else:
            result['channel_profile'] = None
        result['still_approval_required'] = self._requires_still_approval(project_id)
        if (result.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities':
            from .story_audit import audit_results
            result['story_audit'] = audit_results(self, project_id)
            for scene in result['scenes']:
                scene['still_approval'] = self._current_still_approval(scene)
                if scene['still_approval'].get('automatic'):
                    for generation in result['generations']:
                        if generation['id'] == scene.get('active_generation_id'):
                            generation['review'] = {**generation.get('review', {}), **scene['still_approval']}
            
        from .subject_policy import normalize_scene
        for item in result['scenes']:
            item['data'] = normalize_scene(item['data'], result['characters'])
        from .character_selection import project_selection
        result.update(project_selection(result))
        result['preflight'] = self._current_project_preflight(result)
        return result

    def _current_project_preflight(self, project):
        from .prompt_import import metadata as prompt_import_metadata, preflight as prompt_import_preflight
        if prompt_import_metadata(project):
            return prompt_import_preflight(project)
        effective = project
        failures = []
        if (self.settings().get('source_contract_enabled') is True
                and (project.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities'):
            metadata=project.get('analysis_metadata') or {}
            if isinstance(metadata,str):metadata=json.loads(metadata)
            sequence_invalid=False
            narrative_grounder=None
            if metadata.get('narrative_storyboard'):
                from .narrative_contract import sequence_grounder
                try:narrative_grounder=sequence_grounder(project['story'],metadata)
                except (DomainError, ValueError, KeyError, TypeError) as error:
                    sequence_invalid=True
                    failures.append({'code':'invalid_narrative_sequence','message':str(error),
                                     'details':{'scope':'sequence'}})
            scenes = []
            for scene in project['scenes']:
                if sequence_invalid:
                    scenes.append(scene)
                    continue
                try:
                    if narrative_grounder:
                        scenes.append({**scene,'data':narrative_grounder(scene['data'])})
                    else:
                        scenes.append(self._ground_forgotten_scene(scene, project))
                except (DomainError, ValueError, KeyError, TypeError) as error:
                    scenes.append(scene)
                    failures.append({'code':'unresolved_source_scene', 'scene_index':scene['position'],
                                     'message':str(error), 'details':{'scene_id':scene['id']}})
            effective = {**project, 'scenes':scenes}
        result=validate_project_preflight(effective, project.get('story'))
        result['errors'].extend(failures)
        sequence_messages=set()
        unique_errors=[]
        for item in result['errors']:
            if item.get('code')=='invalid_narrative_sequence':
                if item.get('message') in sequence_messages:continue
                sequence_messages.add(item.get('message'))
            unique_errors.append(item)
        result['errors']=unique_errors
        if failures:result['ok']=result['valid']=False
        if self.settings().get('source_contract_enabled') is True:
            from .source_contract import coverage
            report=coverage(project['story'],project['scenes'])
            result['source_coverage']=report
            if not report['ok']:
                result['ok']=False
                result.setdefault('errors',[]).append({'message':'Сцены не покрывают сценарий без пропусков и непреднамеренных повторов.'})
            metadata=project.get('analysis_metadata') or {}
            if isinstance(metadata,str):metadata=json.loads(metadata)
            review=metadata.get('source_fidelity_review') or {}
            if metadata.get('coverage_recovery') and review.get('local_conflict_count'):
                result['ok']=result['valid']=False
                result.setdefault('errors',[]).append({
                    'code':'recovered_plan_semantics_pending',
                    'message':f"Текст покрыт полностью. Требуют смысловой проверки {review['local_conflict_count']} кадров.",
                    'details':{'scene_positions':review.get('local_conflict_positions',[])}})
        return result

    def project_preflight(self, project_id):
        return self.detail(project_id)['preflight']

    def _clear_analysis_for_replacement(self, connection, project_id):
        """Only the final save transaction can replace an unused analysis."""
        if connection.execute("SELECT id FROM generations WHERE project_id=? AND purpose NOT IN ('analysis','edit_audio') LIMIT 1", (project_id,)).fetchone():
            raise DomainError('После начала создания изображений или видео повторный разбор запрещён. Создайте новый проект.', 409)
        connection.execute('DELETE FROM scene_revisions WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)', (project_id,))
        connection.execute('DELETE FROM scenes WHERE project_id=?', (project_id,))
        connection.execute('DELETE FROM characters WHERE project_id=?', (project_id,))
        connection.execute('DELETE FROM locations WHERE project_id=?', (project_id,))
        connection.execute('DELETE FROM props WHERE project_id=?', (project_id,))

    def import_analysis(self, project_id, analysis, version):
        project = self.project(project_id)
        validate_analysis(analysis, project['story'])
        from .story_reliability import require_consistent_analysis
        require_consistent_analysis(analysis,project['story'])
        if self.settings().get('source_contract_enabled') is True:
            from .source_contract import require_complete_coverage
            require_complete_coverage(project['story'], analysis['scenes'])
        cast_bible = analysis.get('cast_bible') or {}
        with self.db.transaction() as connection:
            current = connection.execute('SELECT version FROM projects WHERE id=?', (project_id,)).fetchone()[0]
            if current != version:
                raise DomainError('Проект изменён. Обновите страницу.', 409)
            self._clear_analysis_for_replacement(connection, project_id)
            for character in analysis['characters']:
                char_id = uid()
                cast_id = character.get('cast_id') or character.get('id') or slugify_id(character.get('canonical_name') or character['name'])
                connection.execute('''INSERT INTO characters(id,project_id,name,description,wardrobe,cast_id,canonical_name,aliases,role,status,evidence)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                                   (char_id, project_id, character['name'], character['description'],
                                    dump(character.get('wardrobe', {})),
                                    cast_id,
                                    character.get('canonical_name', character['name']),
                                    dump(character.get('aliases', [character['name']])),
                                    character.get('role', ''),
                                    character.get('status', 'active'),
                                    dump(character.get('evidence', []))))
            for location in analysis.get('locations', []):
                connection.execute('INSERT INTO locations(id,project_id,name,description) VALUES(?,?,?,?)',
                                   (uid(), project_id, location['name'], location['description']))
            for prop in analysis.get('props', []):
                connection.execute('INSERT INTO props(id,project_id,name,description,importance,first_appearance,owner,default_state) VALUES(?,?,?,?,?,?,?,?)',
                                   (uid(), project_id, prop['name'], prop['description'],
                                    prop.get('importance', 'standard'), prop.get('first_appearance', 1),
                                    prop.get('owner', ''), prop.get('default_state', 'absent')))
                                    
            channel_profile_id = project.get('channel_profile_id')
            channel_version_id = project.get('channel_profile_version_id')
            ch_profile = connection.execute("SELECT * FROM channel_profiles WHERE id=?", (channel_profile_id,)).fetchone() if channel_profile_id else None
            ch_ver = connection.execute("SELECT * FROM channel_profile_versions WHERE id=?", (channel_version_id,)).fetchone() if channel_version_id else None
            ch_chars = connection.execute("SELECT * FROM channel_characters WHERE channel_id=?", (channel_profile_id,)).fetchall() if channel_profile_id else []
            ch_assets = connection.execute("SELECT * FROM channel_reference_assets WHERE channel_id=?", (channel_profile_id,)).fetchall() if channel_profile_id else []
            contract_str = compile_channel_style_contract(dict(ch_profile), dict(ch_ver), [dict(c) for c in ch_chars]) if ch_profile and ch_ver else ""

            for position, scene in enumerate(analysis['scenes'], 1):
                scene_id = uid()
                if channel_profile_id:
                    scene['channel_profile_id'] = channel_profile_id
                    scene['channel_profile_version_id'] = channel_version_id
                    scene['canonical_character_ids'] = [c['id'] for c in ch_chars if c['canonical_name'] in scene.get('characters', [])]
                    scene['reference_asset_ids'] = [a['id'] for a in ch_assets]
                    scene['compiled_style_contract'] = contract_str
                connection.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)', (scene_id, project_id, position))
                connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, 1, dump(scene), now()))
            connection.execute('UPDATE projects SET world=?,cast_bible=?,analysis_metadata=?,version=version+1 WHERE id=?',
                               (str(analysis.get('world', '')), dump(cast_bible),
                                dump(analysis.get('v3_metadata') or analysis.get('v2_metadata') or {}), project_id))
            self.db.event(connection, project_id, 'analysis_saved', {})
        return self.detail(project_id)

    def character(self, character_id):
        character = self.db.one('SELECT * FROM characters WHERE id=?', (character_id,))
        if not character:
            raise DomainError('Персонаж не найден.', 404)
        return character

    def edit_character(self, character_id, description, revision, name=None):
        with self.db.transaction() as connection:
            character = connection.execute('SELECT * FROM characters WHERE id=?', (character_id,)).fetchone()
            if not character:
                raise DomainError('Персонаж не найден.', 404)
            if character['locked'] or character['revision'] != revision:
                raise DomainError('Персонаж LOCK или изменён. Правка недоступна.', 409)
            description = required_text(description, 'Описание', 4000)
            if name:
                new_name = required_text(name, 'Имя', 80)
                connection.execute('UPDATE characters SET name=?,canonical_name=?,description=?,revision=revision+1 WHERE id=?',
                                   (new_name, new_name, description, character_id))
            else:
                connection.execute('UPDATE characters SET description=?,revision=revision+1 WHERE id=?', (description, character_id))
        return self.character(character_id)

    def profile(self):
        settings = self.settings()
        if not all(settings.get(key) for key in ('unet', 'clip', 'vae')):
            raise DomainError('Сначала выберите установленные модели в настройках.', 409)
        return settings

    def insert_generation(self, connection, project_id, purpose, snapshot, *, batch_id=None,
                          scene_id=None, scene_revision=None, character_id=None, character_revision=None,
                          location_id=None, location_revision=None, prop_id=None, prop_revision=None, seed=None):
        generation_id = uid()
        seed = secrets.randbelow(2**53) if seed is None else seed
        connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
            batch_id,scene_id,scene_revision,character_id,character_revision,location_id,location_revision,prop_id,prop_revision,created_at)
            VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)''',
            (generation_id, project_id, purpose, dump(snapshot), seed, batch_id, scene_id, scene_revision,
             character_id, character_revision, location_id, location_revision, prop_id, prop_revision, now()))
        self.db.event(connection, project_id, 'queued', {'id': generation_id})
        retry = snapshot.get('source_fidelity_retry') or {}
        if visual_reviews_enabled() and purpose == 'scene' and retry.get('exhausted'):
            connection.execute("UPDATE generations SET state='failed',finished_at=?,error=?,progress=? WHERE id=?",
                               (now(), retry['reason'], dump({'stage': 'Пропущен после смысловой проверки',
                               'source_fidelity_retry': retry}), generation_id))
        return generation_id

    def casting_snapshot(self, project, character, profile=None):
        from .mode_policy import allows
        if not allows(project, 'character'):
            raise DomainError('Выбранный режим не использует этот тип постоянных эталонов.', 409)
        """Build the same immutable identity request for manual and automatic casting."""
        profile = {**(profile or self.profile()), 'width': 768, 'height': 432}
        subject = 'reference subject' if project['mode'] == 'objects' else 'character'
        requirements = ({'gender': None, 'age': None, 'one_person': False}
                        if project['mode'] == 'objects' else
                        character_requirements(character['description'], character['name']))
        age, gender = requirements['age'], requirements['gender']
        from .visual_style import is_marker_style, marker_contract
        if is_marker_style(project.get('style')):
            from .portrait_contract import identity_description, subject_gender
            identity = identity_description(character['description'])
            gender = subject_gender(character['description'],character['name'])
            requirements['gender'] = gender
            # Portraits have their own compact contract. Never prepend the
            # project/scene style: use the dedicated bounded portrait contract.
            prompt = ('SOLO IDENTITY REFERENCE ONLY. Exactly one person, exactly one head. '
                      'One full-body character, head to shoes visible, facing camera, pure white background. '
                      'Identity data: ' + identity + '. '
                      'Style: Minimalist Marker Explainer. 2D stick-figure character, oversized white round head, '
                      'slightly wobbly bold black ink contour with rounded ends and varied pressure. '
                      'Large head and simplified body with balanced expressive proportions. '
                      'Distinctive simple hair and clothing; flat muted beige, brown and charcoal colors. '
                      'Eyewear and facial hair only if explicitly stated in Identity data. Otherwise no glasses and no moustache or beard. '
                      'Clothing, sleeves and trousers have simple flat volume; bare limbs may be thin strokes. Hands are simple outlined shapes with a few finger lines when needed. '
                      'Simple oval eyes, expressive eyebrows and mouth; small nose, ears, age lines or illness markings only where identity requires them. '
                      'Flat white head and flat muted clothing; no gradients, skin modeling, 3D, glossy vector finish or detailed cartoon anatomy. '
                      'One figure in one view only; no alternate poses or character sheet. '
                      'Plain white empty background. No environment or scene action, furniture, classroom, clinic, '
                      'patients, students, companions, anatomical diagrams, posters, second head or extra body. '
                      'No additional people, collage, lettering, captions or logos.')
            from .appearance_design import appearance_design
            design = appearance_design(character, gender)
            prompt += ' ' + design['prompt']
            prompt += ' Retain explicitly described visible illness or physical differences in Identity data, but do not invent visible signs from a diagnosis alone or add clinical scene action.'
            if age: prompt += ' Depict age category: ' + age['label'] + '.'
            if gender: prompt += ' Depict stated gender: ' + gender['label'] + '.'
            if len(prompt) > 2600:
                raise DomainError('Описание портрета слишком длинное. Сократите описание внешности; запрос не обрезан и не отправлен.', 422)
            return {'profile': profile, 'prompt': prompt, 'references': [], 'wardrobe_key': None,
                    'character_description': character['description'], 'age_constraint': age, 'gender_constraint': gender,
                    'character_requirements': requirements, 'style': project['style'], 'appearance_design': design}
        if requirements.get('entity_type') == 'nonhuman':
            style = project['style']
            for old_style, new_style in (
                ('realistic human faces and anatomy', 'realistic animal anatomy'),
                ('natural skin texture', 'natural fur, skin and hide texture'),
                ('consistent character design', 'coherent wildlife subject design'),
            ):
                style = style.replace(old_style, new_style)
            prompt = (f"Style: {style}. One non-human animal or creature reference: {character['description']}. "
                      "Depict exactly one complete animal, with species-appropriate anatomy, natural proportions, "
                      "limbs, hooves/paws/fins/wings as applicable, coat or skin, markings and horns if described. "
                      "Full-body three-quarter wildlife reference in a simple natural setting, with the entire body visible. "
                      "No people, no human face, no human body, no clothes, no human portrait, no hands, no human silhouette. "
                      "One continuous image only; never make a collage, contact sheet, split screen, diptych, triptych or panel layout. "
                      "No text, captions, letters, numbers, logos, watermarks, signs or interface elements.")
            return {'profile': profile, 'prompt': prompt, 'references': [], 'wardrobe_key': None,
                    'character_description': character['description'], 'age_constraint': None, 'gender_constraint': None,
                    'character_requirements': requirements, 'style': style}
        # A project world can mention another character's age (for example, an
        # elderly mother). Including it in every portrait caused the compact
        # diffusion model to copy that age onto the daughter.  Casting needs a
        # clean identity request, not the complete story context.
        prompt = f"Style: {project['style']}. One {subject}: {character['description']}. "
        prompt += ('Turn every stated trait into one specific, memorable and internally coherent visual identity. '
                   'Clearly resolve face shape, eyes, nose, mouth, hairstyle and texture, build, posture, '
                   'distinguishing features and natural skin detail while staying compatible with the description. ')
        if gender:
            gender_subject = gender['label'] if age and age['maximum'] <= 17 else f"adult {gender['label']}"
            prompt += (f"STRICT GENDER REQUIREMENT: depict an unmistakable {gender_subject}; "
                       f"do not depict another gender. ")
        if age:
            prompt += (f"STRICT AGE REQUIREMENT: the person must visibly look {age['label']}; this age is non-negotiable. "
                       f"Do not depict anyone younger than {age['minimum']} or older than {age['maximum']}. ")
            # A compact diffusion model often treats a numeric age range as
            # weak metadata.  Restate it as visible traits for younger adults;
            # this is particularly important for roles such as "daughter",
            # which previously produced elderly portraits despite a 30s range.
            if 30 <= age['minimum'] and age['maximum'] <= 39:
                prompt += ('AGE APPEARANCE GUARD: clearly a person in their thirties, a mature but youthful adult face, '
                           'age-appropriate smooth skin and naturally pigmented hair. Do not show grey, silver, white or '
                           'grey-streaked hair unless the character description explicitly requires it. Do not show an elderly, '
                           'senior, grandmotherly, heavily wrinkled or white-haired appearance. ')
            elif age['maximum'] <= 17:
                prompt += ('AGE APPEARANCE GUARD: clearly a child or teenager matching the stated age range, with child-appropriate '
                           'facial and body proportions. Do not depict an adult, middle-aged or elderly person. ')
            elif age['maximum'] <= 49:
                prompt += ('AGE APPEARANCE GUARD: clearly a mid-adult face matching the stated range; do not show an elderly, '
                           'senior, grandmotherly, heavily wrinkled or white-haired appearance. ')
        visual_traits = differentiate_character_visual_features(character.get('name', ''), character.get('description', ''), gender, age)
        if visual_traits:
            prompt += f"Distinct visual traits: {visual_traits}. "
        prompt += ('SOLO IDENTITY REFERENCE ONLY. Exactly one person, centered head-and-shoulders portrait, '
                   'facing the camera. Clean plain neutral studio background. No other people, crowds, companions, '
                   'bystanders, background faces, reflections, mirrors, silhouettes, body doubles or partial bodies. ')
        is_illustration = any(w in project.get('style', '').casefold() for w in ('illustration', 'comic', 'cartoon', 'animated', 'painting', 'drawing'))
        medium_word = 'image' if is_illustration else 'photograph'
        prompt += (f'One continuous {medium_word} only; never create a collage, contact sheet, split screen, diptych, '
                   'triptych, panel layout or multiple views. ')
        prompt += ('Create a new original appearance. Completely fictional original character; strictly do not depict any '
                   'celebrity, famous actor, public figure, or celebrity lookalike. The face occupies 35 to 45 percent of the frame, both eyes '
                   'are clear, neutral natural expression, realistic facial anatomy, no face obstruction. ')
        if is_illustration:
            prompt += ('Artistic character portrait, digital painting illustration, expressive brushwork, not a real photograph. ')
        prompt += ('No text, captions, letters, numbers, logos, watermarks, signs or interface elements. ')
        prompt += 'Simple casual top styling matching the specified visual traits.'
        return {'profile': profile, 'prompt': prompt, 'references': [], 'wardrobe_key': None,
                'character_description': character['description'], 'age_constraint': age, 'gender_constraint': gender,
                'character_requirements': requirements,
                'style': project['style']}

    def cast(self, character_id, count=1, reference_asset=None, wardrobe_key=None):
        if type(count) is not int or not 1 <= count <= 4:
            raise DomainError('Можно заказать один новый портрет.')
        if reference_asset or wardrobe_key:
            raise DomainError('Отдельные референсы одежды не используются.')
        character = self.character(character_id)
        project = self.project(character['project_id'])
        snapshot = self.casting_snapshot(project, character)
        with self.db.transaction() as connection:
            latest = connection.execute('SELECT revision FROM characters WHERE id=?',(character_id,)).fetchone()
            if latest['revision'] != character['revision']:
                raise DomainError('Персонаж изменён; обновите страницу.',409)
            pending = connection.execute("""SELECT * FROM generations WHERE character_id=? AND character_revision=?
                AND purpose='casting' AND state IN ('queued','preparing','submitted','running','saving','reconciling')
                AND cancel_requested=0 ORDER BY created_at DESC,rowid DESC""",(character_id,character['revision'])).fetchall()
            if pending:
                active = [g for g in pending if g['state'] != 'queued']
                chosen = active[0] if active else pending[0]
                for duplicate in pending:
                    if duplicate['id'] != chosen['id'] and duplicate['state'] == 'queued':
                        connection.execute("UPDATE generations SET state='cancelled',cancel_requested=1,finished_at=?,error=?,progress=? WHERE id=? AND state='queued'",
                            (now(),'Повторное ожидание портрета заменено одним заданием.',dump({'stage':'Дубль ожидания отменён','percent':0}),duplicate['id']))
                if chosen['state'] == 'queued':
                    connection.execute("UPDATE generations SET snapshot=? WHERE id=? AND state='queued'",(dump(snapshot),chosen['id']))
                return {'generation_ids':[chosen['id']], 'reused':True, 'state':chosen['state']}
            generation_id = self.insert_generation(connection, project['id'], 'casting', snapshot,
                character_id=character_id,character_revision=character['revision'])
        return {'generation_ids':[generation_id], 'reused':False, 'state':'queued'}

    def claim_character_cast(self, character_id, generation_id, request_id):
        required_text(request_id,'ID запроса',200)
        character = self.character(character_id)
        fresh = self.casting_snapshot(self.project(character['project_id']), character)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT g.*,c.revision AS current_revision FROM generations g JOIN characters c ON c.id=g.character_id WHERE g.id=? AND g.character_id=? AND g.purpose='casting'",(generation_id,character_id)).fetchone()
            if not row or row['cancel_requested'] or row['character_revision'] != row['current_revision']:
                raise DomainError('Портрет отменён или описание персонажа изменилось.',409)
            snapshot = json.loads(row['snapshot'])
            if row['state'] == 'running' and snapshot.get('flow_request_id') == request_id:
                return {'ok':True, 'prompt':snapshot['prompt'], 'generation_id':generation_id}
            if row['state'] != 'queued':
                raise DomainError('Этот портрет уже отправлен или завершён. Повторный запрос не создан.',409)
            snapshot = fresh
            snapshot['portrait_contract_version'] = 'solo-portrait/2'
            snapshot['flow_request_id'] = request_id
            connection.execute("UPDATE generations SET state='running',snapshot=?,started_at=?,progress=? WHERE id=? AND state='queued'",
                (dump(snapshot),now(),dump({'stage':'Портрет отправлен в Flow','percent':10}),generation_id))
        return {'ok':True, 'prompt':snapshot['prompt'], 'generation_id':generation_id}

    def fail_character_cast(self, character_id, generation_id, request_id, reason):
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM generations WHERE id=? AND character_id=? AND purpose='casting'",(generation_id,character_id)).fetchone()
            if not row or json.loads(row['snapshot']).get('flow_request_id') != request_id:
                raise DomainError('Запрос портрета не найден.',409)
            if row['state'] == 'running':
                connection.execute("UPDATE generations SET state='failed',finished_at=?,error=?,progress=? WHERE id=?",
                    (now(),str(reason or 'Flow отклонил запрос')[:600],dump({'stage':'Ошибка портрета — можно перегенерировать','percent':100}),generation_id))
        return {'ok':True}

    def lock(self, character_id, references, revision, review=None):
        """Lock the selected, detector-validated portrait without extra confirmation."""
        character = self.character(character_id)
        if not isinstance(references, dict) or set(references) != {'portrait'}:
            raise DomainError('Выберите один основной портрет.')
        asset = self.asset(references['portrait'])
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (asset['generation_id'],))
        if generation['character_id'] != character_id or generation['character_revision'] != revision:
            raise DomainError('Референс другого персонажа/версии.')
        from .portrait_contract import validate_portrait
        from PIL import Image
        snapshot = json.loads(generation['snapshot'])
        from .visual_style import is_marker_style
        portrait_review = None
        if is_marker_style(snapshot.get('style')):
            with Image.open(self.root / asset['path']) as portrait_image:
                portrait_review = validate_portrait(portrait_image, snapshot, confirmed=bool(review and review.get('one_person_confirmed')))
        if portrait_review and portrait_review['status'] == 'unverified':
            raise DomainError(portrait_review['reason'],409)
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM characters WHERE id=?', (character_id,)).fetchone()
            if row['revision'] != revision:
                raise DomainError('Персонаж уже LOCK или изменён.', 409)
            connection.execute('UPDATE characters SET locked=1,references_json=? WHERE id=?', (dump(references), character_id))
            self.db.event(connection, character['project_id'], 'character_locked', {'id': character_id})
        return self.character(character_id)

    def location(self, location_id):
        location = self.db.one('SELECT * FROM locations WHERE id=?', (location_id,))
        if not location:
            raise DomainError('Локация не найдена.', 404)
        return location

    @staticmethod
    def location_anchor(name, description):
        """Concrete, positive visual evidence that makes a place recognisable."""
        name_clean = name.casefold()
        text = f'{name} {description}'.casefold()

        def match(terms, target=text):
            for t in terms:
                if re.search(r'\b' + re.escape(t) + r'\b', target):
                    return True
                if any(ord(c) > 127 for c in t) and t in target:
                    return True
            return False

        # 1. Primary priority: check location name specifically
        if match(('cockpit', 'flight deck', 'flight-deck', 'кабин', 'штурвал', 'рубк'), name_clean):
            return ('Aircraft cockpit flight-deck interior with dual pilot seats, flight control yokes, illuminated avionics dials, glowing multi-function navigation displays, overhead switch console, and wide cockpit windshield framing the night sky outside. Authentic aviation flight deck, uninhabited, empty seats, strictly no people.')
        if match(('outside sky', 'night sky', 'sky', 'clouds', 'небо', 'облак', 'туч'), name_clean):
            return ('Atmospheric sky environment with dramatic cloud formations, deep horizon, volumetric mist and moody ambient lighting. Wide panoramic perspective, no buildings, no people.')
        if match(('airplane cabin', 'aircraft cabin', 'passenger cabin', 'салон'), name_clean):
            return ('Passenger airplane cabin interior with rows of seats, central aisle, overhead luggage bins, small oval airplane windows, and soft cabin lighting. Empty passenger cabin with no people.')
        if match(('bank', 'vault', 'safe deposit', 'ячейк', 'банк'), name_clean):
            return ('Bank-vault interior with a closed circular vault door, rows of safe-deposit drawers, security glass, a fixed service counter, tiled floor and ceiling lights. Closed storage and fixed architecture only; keep the vault sealed and all counters clear.')
        if match(('living room', 'lounge', 'гостин', 'зал'), name_clean):
            return ('Residential living-room interior with an empty sofa, armchair, coffee table, floor lamp, curtains, shelves, clear carpet and a stable window layout. Furniture is orderly and surfaces are clear.')
        if match(('hospital', 'clinic', 'medical ward', 'hospital ward', 'больниц', 'палат', 'клиник'), name_clean):
            return ('Hospital-room interior with one made bed, bedside cabinet, privacy curtain, medical wall panel and clinical window. The bed and floor are clear.')

        # 2. Secondary priority: check combined name and description
        if match(('cockpit', 'flight deck', 'flight-deck', 'штурвал')):
            return ('Aircraft cockpit flight-deck interior with dual pilot seats, flight control yokes, illuminated avionics dials, glowing multi-function navigation displays, overhead switch console, and wide cockpit windshield framing the night sky outside. Authentic aviation flight deck, uninhabited, empty seats, strictly no people.')
        if match(('outside sky', 'night sky', 'sky', 'clouds', 'небо', 'облак')):
            return ('Atmospheric sky environment with dramatic cloud formations, deep horizon, volumetric mist and moody ambient lighting. Wide panoramic perspective, no buildings, no people.')
        if match(('airplane cabin', 'aircraft cabin', 'passenger cabin', 'салон')):
            return ('Passenger airplane cabin interior with rows of seats, central aisle, overhead luggage bins, small oval airplane windows, and soft cabin lighting. Empty passenger cabin with no people.')
        if match(('bank', 'vault', 'safe deposit', 'ячейк', 'банк')):
            return ('Bank-vault interior with a closed circular vault door, rows of safe-deposit drawers, security glass, a fixed service counter, tiled floor and ceiling lights. Closed storage and fixed architecture only; keep the vault sealed and all counters clear.')
        if match(('living room', 'lounge', 'гостин', 'зал')):
            return ('Residential living-room interior with an empty sofa, armchair, coffee table, floor lamp, curtains, shelves, clear carpet and a stable window layout. Furniture is orderly and surfaces are clear.')
        if match(('hospital', 'clinic', 'medical ward', 'hospital ward', 'больниц', 'палат', 'клиник')):
            return ('Hospital-room interior with one made bed, bedside cabinet, privacy curtain, medical wall panel and clinical window. The bed and floor are clear.')
        if match(('notary', 'нотари', 'law office', 'юрист')):
            return ('Formal notary-office interior with desk, visitor chairs, filing cabinet, desk lamp, document trays and an orderly professional layout. Desk and seating are clear.')
        if match(('house', 'home', 'дом', 'квартир')):
            return ('Private-home architecture with stable door and window positions, lived-in furniture and a coherent room layout. Architecture and inanimate furnishings only.')

        return ('Recognisable physical architecture with fixtures and furnishing that prove the stated place. Architecture and inanimate furnishings only.')

    def location_snapshot(self, project, location, profile=None):
        from .mode_policy import allows
        if not allows(project, 'location'):
            raise DomainError('Выбранный режим не использует этот тип постоянных эталонов.', 409)
        profile = {**(profile or self.profile()), 'width': 768, 'height': 432}
        description = location['description']
        channel_profile = None
        if project.get('channel_profile_id'):
            channel_profile = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (project['channel_profile_id'],))
        is_forgotten_cities = bool(channel_profile and channel_profile.get('profile_type') == 'forgotten_cities')
        narrative_cues = (' where ', ' meets ', ' meet ', ' finds ', ' find ', ' learns ', ' learn ', ' arrives ',
                          ' reads ', ' retrieves ', ' discovers ', ' woman ', ' man ', ' person ', ' people ',
                          ' приходит', ' встреча', ' находит', ' узнаёт', ' узнает', ' приезжает', ' женщина', ' мужчина')
        # A narrative description would make the model illustrate the scene instead
        # of establishing the reusable physical place.
        if any(cue in description.casefold() for cue in narrative_cues):
            description = (f"A believable, permanently consistent {location['name']} with fixed architectural "
                           "features appropriate to its function.")
        location_name = location['name'].casefold()
        style = project['style']
        # Text-conditioned models treat a long negative list of faces/limbs as
        # concepts to draw. Remove person-oriented style terms before making a
        # deliberately empty location reference.
        for old, new in (
            ('realistic human faces and anatomy', 'precise natural and material detail'),
            ('expressive faces', 'expressive environmental storytelling'),
            ('natural skin texture', 'natural surface texture'),
            ('consistent character design', 'coherent environmental design'),
        ):
            style = style.replace(old, new)
        if is_forgotten_cities:
            # Narrative words such as «заложили» used to match «зал» and turn
            # an archaeological city reference into a furnished living room.
            anchor = ('Architectural reference of the exact named place. '
                      'Preserve the source-described spatial layout, materials, levels and terrain. '
                      'Use the spatial type in the source: underground passages remain underground; '
                      'an open settlement remains an open settlement. Never impose streets, courtyards, '
                      'surface buildings, a skyline or a bird\'s-eye viewpoint on an underground place. ')
            if 'главный вид' in location_name:
                anchor += 'One coherent broad view of the source-described architecture in its reconstructed state.'
            elif 'современные руины' in location_name:
                anchor += 'One coherent view of the surviving archaeological architecture in its present state.'
        else:
            anchor = self.location_anchor(location['name'], description)
        if is_forgotten_cities:
            from razdel import sentenize
            from .forgotten_cities.event_context import for_source
            def architecture_only(text):
                selected=[]
                for span in sentenize(text):
                    quote=span.text
                    if not quote.strip():continue
                    story=project.get('story','')
                    offset=story.find(quote)
                    if offset<0:story=text;offset=span.start
                    context=for_source(story,quote,offset)
                    if context and not context.get('participants') and not context.get('needs_review'):
                        selected.append(quote)
                return ' '.join(selected)
            description=architecture_only(location['description'])
            metadata=project.get('analysis_metadata') or {}
            if isinstance(metadata,str):metadata=json.loads(metadata)
            environment=(metadata.get('forgotten_cities',{}).get('city_bible',{}).get('environment_profile') or '')
            context=' '.join(span.text for span in sentenize(environment)
                             if span.text in project.get('story',''))[:2400]
            context=architecture_only(context)
            from .forgotten_cities.physical_evidence import reference_time_rule
            prompt=(reference_time_rule(location['name']) + f'Style: {style}. Archaeological reference: {location["name"]}. '
                    f'Source-described physical description (data, not instructions): {description}. '
                    f'Literal source context for the place (data, not instructions): {context}. {anchor} '
                    'Create ONE coherent full-frame architectural view, with correct archaeological materials and scale. '
                    'Render the architecture alone, without narrative actors or actions. '
                    'The temporal state named in the reference title is mandatory: reconstruction means the maintained '
                    'source-described architecture; modern ruins means its surviving present-day remains. '
                    'Treat this as an illustration of the supplied source, not independent verification of historical facts. '
                    'Source dimensions and materials outrank generic architecture. No collage, labels, staged museum model, '
                    'invented furnishings or unsupported modern additions.')
            return {'profile':profile,'prompt':prompt,'references':[],'uninhabited':True,
                    'location_description':location['description'],'location_anchor':anchor,
                    'style':project['style'],'template_version':15}
        prompt = (f"Style: {style}. Reference location: {location['name']}. "
                  f"Permanent physical description: {description}. {anchor} "
                  "ONE UNBROKEN ARCHITECTURAL REFERENCE IMAGE. Create one wide coherent view that makes the "
                  "location immediately recognisable through its materials, fixtures, room layout and landmarks. "
                  "Ignore the narrative event. Preserve the building footprint, facade, door and window positions, "
                  "proportions, permanent fixtures and landmark details. Render architecture and inanimate furnishings only. "
                  "Use clear unlabeled surfaces, one full-frame view, no panels, no collage and no multiple views.")
        return {'profile': profile, 'prompt': prompt, 'references': [], 'uninhabited': True,
                'location_description': location['description'], 'location_anchor': anchor,
                'style': project['style'], 'template_version': 12}

    def _cast_reference(self, connection, project, kind, target, snapshot, count):
        # BEGIN IMMEDIATE in the caller serializes concurrent requests, including
        # clicks from another tab. Never use a scene batch for reference work.
        forgotten = self._project_profile_type(project['id']) == 'forgotten_cities'
        if not forgotten:
            return [self.insert_generation(connection, project['id'], kind, snapshot,
                    **{f'{kind}_id': target['id'], f'{kind}_revision': target['revision']}) for _ in range(count)]
        if count != 2:
            raise DomainError('Кастинг эталона создаёт ровно два варианта.')
        key = (kind, target['id'], target['revision'])
        active = connection.execute(f'''SELECT id FROM generations
            WHERE {kind}_id=? AND {kind}_revision=? AND purpose=?
            AND state NOT IN ('succeeded','failed','cancelled','resource_degraded')
            ORDER BY rowid''', (target['id'], target['revision'], kind)).fetchall()
        session = connection.execute('''SELECT * FROM reference_casts
            WHERE target_kind=? AND target_id=? AND revision=?''', key).fetchone()
        if active:
            return json.loads(session['generation_ids']) if session else [r['id'] for r in active]
        cast_id = uid()
        snapshot = {**snapshot, 'generation_role': 'reference', 'reference_cast_id': cast_id}
        ids = [self.insert_generation(connection, project['id'], kind, snapshot,
               **{f'{kind}_id': target['id'], f'{kind}_revision': target['revision']}) for _ in range(count)]
        connection.execute('''INSERT INTO reference_casts
            (target_kind,target_id,revision,project_id,generation_ids,selected_asset_id,created_at)
            VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(target_kind,target_id,revision)
            DO UPDATE SET generation_ids=excluded.generation_ids,selected_asset_id=NULL,created_at=excluded.created_at''',
            (*key, project['id'], dump(ids), now()))
        return ids

    def reference_cast(self, kind, target):
        row = self.db.one('''SELECT * FROM reference_casts
            WHERE target_kind=? AND target_id=? AND revision=?''', (kind, target['id'], target['revision']))
        if row:
            row['generation_ids'] = json.loads(row['generation_ids'])
        return row

    def select_reference(self, kind, target_id, asset_id, revision):
        if kind not in ('location', 'prop'):
            raise DomainError('Неизвестный тип эталона.')
        with self.db.transaction() as connection:
            target = connection.execute(f'SELECT * FROM {kind}s WHERE id=?', (target_id,)).fetchone()
            if not target:
                raise DomainError('Эталон не найден.', 404)
            if target['locked'] or target['revision'] != revision:
                raise DomainError('Эталон закреплён или изменён.', 409)
            session = connection.execute('''SELECT * FROM reference_casts
                WHERE target_kind=? AND target_id=? AND revision=?''', (kind, target_id, revision)).fetchone()
            if not session:
                raise DomainError('Сначала создайте варианты эталона.', 409)
            ids = json.loads(session['generation_ids'])
            jobs = [connection.execute('SELECT * FROM generations WHERE id=?', (gid,)).fetchone() for gid in ids]
            if not all(job and job['state'] == 'succeeded' and job['asset_id'] for job in jobs):
                raise DomainError('Дождитесь готовности обоих вариантов.', 409)
            if asset_id not in [job['asset_id'] for job in jobs]:
                raise DomainError('Вариант не принадлежит текущему кастингу.')
            connection.execute('''UPDATE reference_casts SET selected_asset_id=?
                WHERE target_kind=? AND target_id=? AND revision=?''', (asset_id, kind, target_id, revision))
        return {'selected_asset_id': asset_id}

    def _validate_reference_lock(self, connection, kind, target_id, revision, asset_id):
        job = connection.execute('''SELECT g.* FROM generations g
            JOIN assets a ON a.generation_id=g.id WHERE a.id=?''', (asset_id,)).fetchone()
        if (not job or job['purpose'] != kind or job['state'] != 'succeeded'
                or job[f'{kind}_id'] != target_id or job[f'{kind}_revision'] != revision):
            raise DomainError('Выберите готовый вариант этого эталона и версии.')
        session = connection.execute('''SELECT * FROM reference_casts
            WHERE target_kind=? AND target_id=? AND revision=?''', (kind, target_id, revision)).fetchone()
        if session and json.loads(job['snapshot']).get('reference_cast_id') and job['id'] not in json.loads(session['generation_ids']):
            raise DomainError('Выберите вариант текущего кастинга.', 409)
        if session and job['id'] in json.loads(session['generation_ids']):
            for gid in json.loads(session['generation_ids']):
                candidate = connection.execute('SELECT state FROM generations WHERE id=?', (gid,)).fetchone()
                if not candidate or candidate['state'] != 'succeeded':
                    raise DomainError('Дождитесь готовности обоих вариантов.', 409)
            if session['selected_asset_id'] != asset_id:
                raise DomainError('Сначала выберите этот вариант.', 409)

    def _cancel_superseded_reference(self, connection, kind, target_id, revision):
        connection.execute(f'''UPDATE generations SET cancel_requested=1,
            state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
            finished_at=CASE WHEN state='queued' THEN ? ELSE finished_at END
            WHERE {kind}_id=? AND {kind}_revision=? AND purpose=?
              AND state NOT IN ('succeeded','failed','cancelled','resource_degraded')''',
            (now(), target_id, revision, kind))

    def cast_location(self, location_id, count=2):
        if type(count) is not int or not 1 <= count <= 4:
            raise DomainError('Можно заказать 1–4 варианта.')
        location = self.location(location_id)
        if location['locked']:
            raise DomainError('Локация уже зафиксирована.', 409)
        project = self.project(location['project_id'])
        snapshot = self.location_snapshot(project, location)
        with self.db.transaction() as connection:
            latest = connection.execute('SELECT revision,locked FROM locations WHERE id=?', (location_id,)).fetchone()
            if latest['locked'] or latest['revision'] != location['revision']:
                raise DomainError('Локация изменена; обновите страницу.', 409)
            ids = self._cast_reference(connection, project, 'location', location, snapshot, count)
        return {'generation_ids': ids}

    def lock_location(self, location_id, references, revision):
        location = self.location(location_id)
        if not isinstance(references, dict) or set(references) != {'environment'}:
            raise DomainError('Выберите один референс локации.')
        asset = self.asset(references['environment'])
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (asset['generation_id'],))
        if generation['location_id'] != location_id or generation['location_revision'] != revision:
            raise DomainError('Референс другой локации/версии.')
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM locations WHERE id=?', (location_id,)).fetchone()
            if row['locked'] or row['revision'] != revision:
                raise DomainError('Локация уже зафиксирована или изменена.', 409)
            self._validate_reference_lock(connection, 'location', location_id, revision, asset['id'])
            connection.execute('UPDATE locations SET locked=1,references_json=? WHERE id=?', (dump(references), location_id))
            self._cancel_superseded_reference(connection, 'location', location_id, revision)
            self.db.event(connection, location['project_id'], 'location_locked', {'id': location_id})
        return self.location(location_id)

    def edit_location(self, location_id, description, revision):
        with self.db.transaction() as connection:
            location = connection.execute('SELECT * FROM locations WHERE id=?', (location_id,)).fetchone()
            if not location:
                raise DomainError('Локация не найдена.', 404)
            if location['locked'] or location['revision'] != revision:
                raise DomainError('Локация уже зафиксирована или изменена.', 409)
            description = required_text(description, 'Описание локации', 4000)
            connection.execute('UPDATE locations SET description=?,revision=revision+1 WHERE id=?', (description, location_id))
        return self.location(location_id)

    def prop(self, prop_id):
        prop = self.db.one('SELECT * FROM props WHERE id=?', (prop_id,))
        if not prop:
            raise DomainError('Реквизит не найден.', 404)
        return prop

    def prop_snapshot(self, project, prop, profile=None):
        from .mode_policy import allows
        if not allows(project, 'prop'):
            raise DomainError('Выбранный режим не использует этот тип постоянных эталонов.', 409)
        profile = {**(profile or self.profile()), 'width': 768, 'height': 432}
        style = project['style']
        prompt = (f"Style: {style}. Prop reference object: {prop['name']}. "
                  f"Object description: {prop['description']}. "
                  "SOLO OBJECT REFERENCE ONLY. Isolated persistent prop object on a clean, neutral plain studio background. "
                  "Centred composition, clear view of the entire object, no persons, no faces, no hands, no background clutter, "
                  "no text, no watermark, no logo. Maintain its exact defining shape, material, proportions, colors and unique details.")
        channel = self.db.one('SELECT profile_type FROM channel_profiles WHERE id=?', (project.get('channel_profile_id'),))
        if channel and channel['profile_type'] == 'forgotten_cities':
            from .forgotten_cities.grounding import artifact_source_constraints
            prompt += ' ' + ' '.join(artifact_source_constraints([prop['name']], project.get('story', '')))
        return {'profile': profile, 'prompt': prompt, 'references': [], 'uninhabited': True,
                'prop_description': prop['description'], 'style': project['style'], 'template_version': 1}

    def cast_prop(self, prop_id, count=2):
        if type(count) is not int or not 1 <= count <= 4:
            raise DomainError('Можно заказать 1–4 варианта.')
        prop = self.prop(prop_id)
        if prop['locked']:
            raise DomainError('Реквизит уже зафиксирован.', 409)
        project = self.project(prop['project_id'])
        snapshot = self.prop_snapshot(project, prop)
        with self.db.transaction() as connection:
            latest = connection.execute('SELECT revision,locked FROM props WHERE id=?', (prop_id,)).fetchone()
            if latest['locked'] or latest['revision'] != prop['revision']:
                raise DomainError('Реквизит изменен; обновите страницу.', 409)
            ids = self._cast_reference(connection, project, 'prop', prop, snapshot, count)
        return {'generation_ids': ids}

    def lock_prop(self, prop_id, references, revision):
        prop = self.prop(prop_id)
        if not isinstance(references, dict):
            raise DomainError('Выберите один референс реквизита.')
        asset_id = references.get('prop') or references.get('item')
        if not asset_id:
            raise DomainError('Выберите один референс реквизита.')
        asset = self.asset(asset_id)
        generation = self.db.one('SELECT * FROM generations WHERE id=?', (asset['generation_id'],))
        if generation['prop_id'] != prop_id or generation['prop_revision'] != revision:
            raise DomainError('Референс другого реквизита/версии.')
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM props WHERE id=?', (prop_id,)).fetchone()
            if row['locked'] or row['revision'] != revision:
                raise DomainError('Реквизит уже зафиксирован или изменен.', 409)
            self._validate_reference_lock(connection, 'prop', prop_id, revision, asset_id)
            norm_refs = {'prop': asset_id, 'item': asset_id}
            connection.execute('UPDATE props SET locked=1,references_json=? WHERE id=?', (dump(norm_refs), prop_id))
            self._cancel_superseded_reference(connection, 'prop', prop_id, revision)
            self.db.event(connection, prop['project_id'], 'prop_locked', {'id': prop_id})
        return self.prop(prop_id)

    def unlock_prop(self, prop_id):
        prop = self.prop(prop_id)
        project_id = prop['project_id']
        with self.db.transaction() as connection:
            connection.execute('UPDATE props SET locked=0 WHERE id=?', (prop_id,))
            self.db.event(connection, project_id, 'prop_unlocked', {'id': prop_id})
        return self.prop(prop_id)

    def edit_prop(self, prop_id, description, revision, importance=None):
        with self.db.transaction() as connection:
            prop = connection.execute('SELECT * FROM props WHERE id=?', (prop_id,)).fetchone()
            if not prop:
                raise DomainError('Реквизит не найден.', 404)
            if prop['locked'] or prop['revision'] != revision:
                raise DomainError('Реквизит уже зафиксирован или изменен.', 409)
            description = required_text(description, 'Описание реквизита', 4000)
            old_importance = prop['importance'] if 'importance' in prop.keys() else 'story'
            new_importance = old_importance
            if importance is not None:
                if importance not in ('story', 'incidental', 'wardrobe', 'set_dressing'):
                    raise DomainError('Неверная важность реквизита.')
                new_importance = importance
                connection.execute('UPDATE props SET description=?,importance=?,revision=revision+1 WHERE id=?', (description, new_importance, prop_id))
            else:
                connection.execute('UPDATE props SET description=?,revision=revision+1 WHERE id=?', (description, prop_id))

            if new_importance != old_importance:
                p_name = prop['name']
                scenes = connection.execute('SELECT s.id, s.current_revision, sr.data FROM scenes s JOIN scene_revisions sr ON s.id=sr.scene_id AND s.current_revision=sr.revision WHERE s.project_id=?', (prop['project_id'],)).fetchall()
                for s in scenes:
                    s_id, s_rev, s_data_json = s['id'], s['current_revision'], s['data']
                    s_data = json.loads(s_data_json)
                    uses_prop = any(sp.get('name') == p_name for sp in s_data.get('props', []))
                    if uses_prop:
                        connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (s_id, s_rev + 1, s_data_json, now()))
                        connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (s_rev + 1, s_id))
        return self.prop(prop_id)

    def upload_prop_image(self, prop_id, image_bytes, lock=True):
        prop = self.prop(prop_id)
        project_id = prop['project_id']
        project = self.project(project_id)
        processed_data = self._normalize_reference_image(image_bytes)

        generation_id = uid()
        asset_id = uid()
        snapshot = self.prop_snapshot(project, prop)

        relative = f"projects/{project_id}/{generation_id}.png"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(processed_data)

        metadata = {
            'id': generation_id,
            'project_id': project_id,
            'purpose': 'prop',
            'snapshot': snapshot,
            'sha256': hashlib.sha256(processed_data).hexdigest(),
            'output_size': (768, 432),
            'saved_at': now(),
            'state': 'succeeded',
            'source': 'upload_google_flow',
            'error': None
        }
        metadata_dir = self.root / 'metadata'
        metadata_dir.mkdir(exist_ok=True)
        (metadata_dir / f"{generation_id}.json").write_text(dump(metadata), encoding='utf-8')

        references = {'prop': asset_id}
        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                prop_id,prop_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                (generation_id, project_id, 'prop', dump(snapshot), 0,
                 prop_id, prop['revision'], asset_id, now(), now(), now(),
                 dump({'stage': 'Реквизит загружен', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 hashlib.sha256(processed_data).hexdigest(), 768, 432, len(processed_data)))

            if lock:
                connection.execute('UPDATE props SET locked=1,references_json=? WHERE id=?', (dump(references), prop_id))
                self._cancel_superseded_reference(connection, 'prop', prop_id, prop['revision'])
                self.db.event(connection, project_id, 'prop_locked', {'id': prop_id})
            else:
                self.db.event(connection, project_id, 'generation_completed', {'id': generation_id, 'state': 'succeeded'})

        return self.prop(prop_id)

    def scene(self, scene_id):
        scene = self.db.one('SELECT * FROM scenes WHERE id=?', (scene_id,))
        if not scene:
            raise DomainError('Сцена не найдена.', 404)
        row = self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?', (scene_id, scene['current_revision']))
        scene['data'] = json.loads(row['data'])
        return scene

    def edit_scene(self, scene_id, data, revision):
        scene = self.scene(scene_id)
        project = self.project(scene['project_id'])
        updated = {**scene['data'], **data}
        characters = self.db.all('SELECT * FROM characters WHERE project_id=?', (project['id'],))
        for character in characters:
            character['wardrobe'] = json.loads(character['wardrobe'])
            character['aliases'] = json.loads(character.get('aliases') or '[]')
        from .character_selection import resolve_links
        names_changed = 'characters' in data and data['characters'] != scene['data'].get('characters', [])
        updated, link_errors = resolve_links(updated, characters, names_only=names_changed)
        if link_errors:
            raise DomainError('; '.join(e['message'] for e in link_errors))
        locations = self.db.all('SELECT name,description FROM locations WHERE project_id=?', (project['id'],))
        props = self.db.all('SELECT name,description,first_appearance,owner,default_state FROM props WHERE project_id=?', (project['id'],))
        # Voice-aligned frames may contain a fraction of a sentence. Generic
        # analysis validation expands such quotes, breaking timing and invalidating
        # audit_source_quote. Preserve an exact existing character span on edits.
        voice_source = None
        if updated.get('voice_timing'):
            start, end = updated.get('source_start'), updated.get('source_end')
            quote = updated.get('source_quote')
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(project['story'])
                    and project['story'][start:end] == quote):
                voice_source = {key: copy.deepcopy(updated[key]) for key in
                                ('source_quote', 'source_start', 'source_end', 'voice_timing')}
        from .prompt_import import metadata as prompt_import_metadata, validate_scene as validate_imported_scene
        if prompt_import_metadata(project):
            updated['prompt_import'] = copy.deepcopy(scene['data']['prompt_import'])
            updated['prompt_timing'] = copy.deepcopy(scene['data']['prompt_timing'])
            validate_imported_scene(updated)
            updated['beat'] = updated['composition'] = updated['manual_prompt']
        else:
            validate_analysis({'characters': characters, 'locations': locations, 'props': props, 'scenes': [updated]}, project['story'])
        if voice_source:
            updated.update(voice_source)
        if updated.get('narrative_framing'):
            # A camera edit keeps the checked event authoritative and saves the
            # exact same framing that will reach the renderer / source preflight.
            updated = self._ground_forgotten_scene({**scene, 'data': updated}, project)['data']
        if not isinstance(updated.get('manual_prompt', ''), str) or len(updated.get('manual_prompt', '')) > 15000:
            raise DomainError('Prompt должен быть строкой до 15 000 символов.')
        with self.db.transaction() as connection:
            current = connection.execute('SELECT current_revision FROM scenes WHERE id=?', (scene_id,)).fetchone()[0]
            if current != revision:
                raise DomainError('Сцена изменена. Обновите страницу.', 409)
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, current + 1, dump(updated), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?', (current + 1, scene_id))
        return self.scene(scene_id)

    def _ground_forgotten_scene(self, scene, project):
        from .narrative_contract import ground as ground_narrative
        reviewed=ground_narrative(project['story'],project.get('analysis_metadata') or {},scene['data'])
        if reviewed is not None:return {**scene,'data':reviewed}
        if scene['data'].get('narration_parent'):
            from .narration_expansion import ground_additional
            return ground_additional(self, scene, project)
        from .forgotten_cities.prompts import build_prompt_packet
        from .forgotten_cities.grounding import apply_packet, enforce_source_constraints
        metadata = project.get('analysis_metadata') or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        forgotten = metadata.get('forgotten_cities') or {}
        # Imported legacy scenes can have no episode IR. Their literal source
        # still applies; project preflight separately blocks unanalysed batches.
        if not forgotten:
            forgotten = {'anchors': [], 'event_contexts': [], 'sequences': []}
        data = scene['data']
        packet = build_prompt_packet(forgotten, data.get('forgotten_event_id') or '',
            data.get('beat', ''), data.get('location', ''),
            ordinal=scene.get('position') or 1, story_text=project.get('story', ''),
            source_quote=data.get('source_quote', ''), source_start=data.get('source_start'))
        from .forgotten_cities.fidelity import SourceFidelityReviewer, apply_review, source_context
        reviewer = SourceFidelityReviewer(self.root, self.settings(), project_id=project['id'])
        anchors = [a for a in forgotten.get('anchors', []) if a.get('anchor_type') == 'artifact']
        audited = bool(data.get('audit_visual_direction') and data.get('audit_source_quote') == data.get('source_quote'))
        from .editorial_plans import EditorialPlans, apply_decision
        editorial, editorial_error = EditorialPlans(self).bound(scene, project)
        # An explicit image-audit correction supersedes the earlier text plan,
        # including its inferred artifact identity. Keep literal source guards.
        plan = reviewer.cached_plan(data, project.get('story', ''), anchors) if project.get('story') and not audited and not editorial else None
        if plan and plan.get('status') == 'pass':
            packet = apply_review(packet, plan, anchors)
        elif project.get('story') and data.get('source_quote'):
            context = source_context(data, project['story'])
            if packet.get('narrative_context'):
                source_start = (packet.get('narrative_context') or {}).get('source', {}).get('start')
                if type(source_start) is int:
                    context['context'] = project['story'][max(0, source_start-800):source_start+len(packet['source_quote'])]
            source_instruction = (' SOURCE CONTEXT (resolve pronouns and object parts; not a request to depict every sentence): ' +
                context['context'] + ' Depict the selected source quote, preserving negations, uncertainty and the actual subject. '
                'Never visualize a denied/unknown event as fact or replace the stated action/object with a generic city view.')
            packet['shot_contract'] += source_instruction
            packet['compiled_prompt'] += '\n' + source_instruction
        if packet['binding_errors']:
            raise DomainError('Кадр не соответствует определённому предмету сюжета: ' + '; '.join(packet['binding_errors']), 409)
        from .forgotten_cities.lifecycle import apply_city_lifecycle
        treatment = apply_city_lifecycle({'source_binding': packet['source_binding'],
            'shot_type': packet['shot_type'], 'direction': packet['visual_direction'],
            'visual_time_layer': packet['visual_time_layer']}, packet['source_quote'],
            project.get('story', ''), data.get('source_start'))
        if treatment.get('city_population'):
            packet.update(city_population=treatment['city_population'], visual_direction=treatment['direction'],
                          visual_time_layer=treatment['visual_time_layer'], shot_type=treatment['shot_type'],
                          time_layer_contract='Time layer: ' + treatment['visual_time_layer'],
                          shot_contract='Authoritative source: ' + packet['source_quote'] + '. Depict: ' + treatment['direction'])
            packet.pop('source_fidelity_plan', None)
        if audited:
            direction = data['audit_visual_direction']
            packet.pop('city_population',None)
            packet['source_reviewed']=True
            allowed = {a['canonical_name'] for a in anchors}
            required = [name for name in data.get('audit_required_artifacts', []) if name in allowed]
            if 'audit_required_artifacts' in data:
                packet['required_artifacts'] = required
            if required:
                binding = packet['source_binding'] if packet['source_binding'] in ('buried_remains', 'artifact_action') else 'artifact_detail'
                packet.update(required_artifacts=required, source_binding=binding)
                if binding == 'artifact_detail':
                    packet.update(shot_type='prop_detail', visual_time_layer='museum_display',
                                  time_layer_contract='Time layer: museum_display')
                packet.pop('city_population', None)
            # Persisted editorial corrections may specify an explicit time and
            # subject state. This keeps a burial or a modern analogy from being
            # silently rewritten as a museum macro when binding its reference.
            if data.get('audit_source_binding') in ('artifact_detail', 'artifact_action', 'buried_remains', 'architecture', 'context', 'narrated_event'):
                packet['source_binding'] = data['audit_source_binding']
            if data.get('audit_time_layer') in ('historical_reconstruction', 'modern_ruins', 'excavation', 'museum_display', 'documented_event'):
                packet['visual_time_layer'] = data['audit_time_layer']
                packet['time_layer_contract'] = 'Time layer: ' + data['audit_time_layer']
            # A revision must not turn an explicitly inhabited reconstruction
            # into empty present-day ruins just to vary its composition.
            if packet.get('city_population') == 'inhabited' and re.search(r'\b(?:ruins|excavat\w*|abandoned|eroded|no\s+(?:living\s+)?people)\b', direction, re.I):
                direction = packet['visual_direction']
            packet.update(visual_direction=direction,
                          shot_contract='Authoritative source: ' + packet['source_quote'] + '. Depict: ' + direction)
            packet.pop('source_fidelity_plan', None)
        if editorial:
            packet = apply_decision(packet, editorial)
            from .forgotten_cities.structures import scene_structure_anchors
            selected = scene_structure_anchors(forgotten.get('anchors', []), project['story'],
                editorial['focus']['quote'], editorial['focus']['start'], editorial['time_layer'])
            packet['anchor_ids'] = [a['anchor_id'] for a in selected]
        if editorial_error:
            packet['editorial_error'] = editorial_error
        packet = enforce_source_constraints(packet, project.get('story', ''))
        if audited:
            direction = packet['visual_direction']
        grounded = apply_packet(data, packet)
        if editorial:
            grounded.update(subject=editorial['subject']['quote'], action=editorial['focus']['quote'],
                required_anchor_ids=[], location_anchor_id=None, background_roles=[],
                source_context={'quote': packet['source_quote'], 'context':project['story'][max(0, editorial['source']['start']-800):editorial['source']['end']]})
        if audited:
            # The final source contract reads this field too. Keep every
            # outgoing representation on the source-corrected direction.
            grounded.update(composition=direction, safe_framing_explanation=direction,
                            audit_visual_direction=direction)
        if audited and type(data.get('audit_people')) is int and 0 <= data['audit_people'] <= 6 and packet['source_binding'] not in ('artifact_detail', 'buried_remains'):
            count = data['audit_people']
            roles = data.get('audit_roles')
            if not isinstance(roles, list) or len(roles) != count or not all(isinstance(role, str) for role in roles) or len(set(roles)) != count:
                roles = [f'анонимный участник действия {index + 1}' for index in range(count)]
            grounded.update(visible_people=count, raw_visible_people=count,
                            episodic_people=roles,
                            people_policy='anonymous_action' if count else 'none')
        if audited and not packet.get('required_artifacts'):
            grounded['props'] = []
        if project.get('story') and data.get('source_quote') and not editorial:
            context = source_context(data, project['story'])
            grounded['source_context'] = context
        if audited and isinstance(data.get('audit_location_ref'),str):
            grounded['location_ref']=data['audit_location_ref']
        elif packet['source_binding'] == 'artifact_detail':
            # The city's reference image otherwise strongly steers a macro
            # object shot back toward a landscape, even with a correct quote.
            grounded['location_ref'] = ''
        elif packet.get('city_population'):
            # A ruins reference must not steer an inhabited reconstruction (or
            # vice versa). Use the matching existing locked city anchor.
            layer = packet['visual_time_layer']
            names = {row['name'] for row in self.db.all('SELECT name FROM locations WHERE project_id=?', (project['id'],))}
            matching = [a for a in forgotten.get('anchors', [])
                        if a.get('anchor_type') != 'artifact' and a.get('time_layer') == layer
                        and a.get('canonical_name') in names]
            if matching:
                grounded['location_ref'] = matching[0]['canonical_name']
        return {**scene, 'data': grounded}

    def review_source_plans(self, project_id, scenes, progress=None):
        project = self.project(project_id)
        channel = self.db.one('SELECT profile_type FROM channel_profiles WHERE id=?', (project.get('channel_profile_id'),))
        if not channel or channel.get('profile_type') != 'forgotten_cities':
            return {}
        if not self.settings().get('google_api_key') and self.settings().get('google_queue_enabled') is not True:
            # Offline analysis remains usable; visual review is advisory.
            return {}
        from .forgotten_cities.fidelity import SourceFidelityReviewer
        metadata = project.get('analysis_metadata') or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        anchors = [a for a in (metadata.get('forgotten_cities') or {}).get('anchors', []) if a.get('anchor_type') == 'artifact']
        reviewer = SourceFidelityReviewer(self.root, self.settings(), project_id=project_id)
        try:
            unresolved = [s.get('data', s) for s in scenes
                          if not (s.get('data', s).get('audit_visual_direction') and
                                  s.get('data', s).get('audit_source_quote') == s.get('data', s).get('source_quote'))]
            return reviewer.review_plans(unresolved, project['story'], anchors, progress) if unresolved else {}
        except Exception as error:
            # Only the optional remote audit is caught; grounding and DB validation stay outside.
            return {'status': 'unverified', 'reason': 'Сервис проверки плана недоступен.',
                    **self._google_review_pending(error)}

    def _google_review_pending(self,error):
        if not self.settings().get('google_queue_enabled'):
            return {}
        return {'review_state':getattr(error,'review_state',None) or 'error_response',
                'job_id':getattr(error,'job_id',None),'next_retry_at':getattr(error,'next_at',None)}

    def resume_google_image_reviews(self,queue=None,limit=32):
        if not visual_reviews_enabled():
            return {'updated': 0, 'enabled': False}
        if self.settings().get('google_queue_enabled') is not True:
            return {'updated':0}
        from .google_queue import configured_queue
        from .google_image_reviews import ImageReviewResumer
        queue=queue or configured_queue(self.root,self.settings())
        return ImageReviewResumer(self,queue).tick(limit) if queue is not None else {'updated':0}

    def resume_google_reference_reviews(self,queue=None,limit=32):
        if not visual_reviews_enabled():
            return {'updated': 0, 'enabled': False}
        if self.settings().get('google_queue_enabled') is not True:
            return {'updated':0}
        from .google_queue import configured_queue
        from .google_reference_reviews import ReferenceReviews
        queue=queue or configured_queue(self.root,self.settings())
        return ReferenceReviews(self).tick(queue,limit) if queue is not None else {'updated':0}

    def google_review_summary(self,project_id):
        self.project(project_id)
        if not self.settings().get('google_queue_enabled'):
            return {'enabled':False,'counts':{},'exceptions':[]}
        rows=self.db.all('''SELECT s.id AS scene_id,s.position,s.active_generation_id,
          g.snapshot,g.asset_id,r.status AS manual_status,r.note AS manual_note
          FROM scenes s LEFT JOIN generations g ON g.id=s.active_generation_id
          LEFT JOIN reviews r ON r.generation_id=g.id WHERE s.project_id=? ORDER BY s.position''',(project_id,))
        counts,exceptions={},[]
        for row in rows:
            snapshot=json.loads(row['snapshot'] or '{}')
            review=snapshot.get('source_fidelity_review') or {}
            state=review.get('review_state') or ('checked' if review.get('status')=='pass' else 'unverified_legacy')
            if not visual_reviews_enabled():
                state = 'manual_approved' if row['manual_status']=='approved' else 'needs_manual_review'
            if visual_reviews_enabled() and review.get('status')=='pass' and row['asset_id']:
                from .google_image_reviews import current_approval
                if not current_approval(self,{'asset_id':row['asset_id'],'scene_id':row['scene_id'],'project_id':project_id},snapshot):
                    state='stale_review'
            if not row['asset_id']:
                state='waiting_generation'
            elif row['manual_status']=='rejected':
                state='rejected_by_user'
            elif visual_reviews_enabled() and row['manual_status']=='approved' and review and state!='checked':
                state='user_exception'
            counts[state]=counts.get(state,0)+1
            if state not in ('checked', 'manual_approved'):
                exceptions.append({'scene_id':row['scene_id'],'position':row['position'],
                    'generation_id':row['active_generation_id'],'asset_id':row['asset_id'],'state':state,
                    'source_state':review.get('review_state'),'source_quote':(snapshot.get('scene') or {}).get('source_quote',''),
                    'reason':review.get('reason',''),'manual_note':row['manual_note'] or '',
                    'job_id':review.get('job_id'),'next_retry_at':review.get('next_retry_at')})
        analyses=[]
        for generation in self.db.all("SELECT id,snapshot,error FROM generations WHERE project_id=? AND purpose='analysis' AND state='waiting_review' ORDER BY created_at", (project_id,)):
            wait=json.loads(generation['snapshot']).get('plan_review_wait') or {}
            analyses.append({'generation_id':generation['id'], 'state':wait.get('state','error_response'),
                'job_id':wait.get('job_id'), 'next_retry_at':wait.get('next_at'), 'reason':generation['error'] or ''})
        from .google_reference_reviews import ReferenceReviews
        batch_waits=self.db.all("SELECT id,state FROM batches WHERE project_id=? AND state IN ('waiting_review','paused') ORDER BY created_at",(project_id,))
        prompt_waits=[]
        for generation in self.db.all("SELECT g.id,g.scene_id,g.state,g.snapshot,g.error,s.position FROM generations g LEFT JOIN scenes s ON s.id=g.scene_id WHERE g.project_id=? AND g.state IN ('waiting_prompt','blocked_prompt')",(project_id,)):
            review=json.loads(generation['snapshot']).get('prompt_review') or {}
            prompt_waits.append({'generation_id':generation['id'],'scene_id':generation['scene_id'],'position':generation['position'],
                'state':review.get('status') or generation['state'],'generation_state':generation['state'],
                'reason':generation['error'] or review.get('reason',''),'job_id':review.get('job_id'),'next_retry_at':review.get('next_retry_at')})
        return {'enabled':True,'scenes':len(rows),'counts':counts,'exceptions':exceptions,'analysis_waits':analyses,
                'reference_checks':ReferenceReviews(self).summary(project_id) if visual_reviews_enabled() else [],
                'visual_review_mode':'automatic' if visual_reviews_enabled() else 'manual',
                'batch_waits':batch_waits,'prompt_waits':prompt_waits if visual_reviews_enabled() else []}

    def google_generation_capacity(self, project_id):
        """Bound pending frame reviews in the built-in generator only."""
        if not visual_reviews_enabled():
            self.project(project_id)
            return {'enabled': False, 'allowed': True, 'pending': 0, 'limit': 3}
        if self.settings().get('google_queue_enabled') is not True:
            return {'enabled':False, 'allowed':True, 'pending':0, 'limit':3}
        rows=self.db.all('''SELECT g.id,g.scene_id,g.snapshot,r.status AS manual_status FROM generations g
          JOIN scenes s ON s.id=g.scene_id AND s.current_revision=g.scene_revision
          LEFT JOIN reviews r ON r.generation_id=g.id
          WHERE g.project_id=? AND g.purpose='scene' AND g.state='succeeded' AND g.asset_id IS NOT NULL''', (project_id,))
        pending=set()
        for row in rows:
            review=json.loads(row['snapshot']).get('source_fidelity_review') or {}
            requires_check = bool(review.get('job_id') or review.get('version') or (self.settings().get('source_contract_enabled') is True and review))
            if requires_check and review.get('status') not in ('pass','fail') and row['manual_status'] not in ('approved','rejected'):
                pending.add(row['id'])
        return {'enabled':True, 'allowed':len(pending)<3, 'pending':len(pending), 'limit':3,
                'scope':'built_in_generator'}

    def validate_scene_source_image(self, snapshot, data):
        if not visual_reviews_enabled():
            snapshot['visual_review_policy'] = 'manual'
            return manual_review()
        if not (snapshot.get('scene') or {}).get('forgotten_cities_prompt_contract'):
            return None
        from .forgotten_cities.fidelity import SourceFidelityReviewer
        project = self.project(snapshot['project_id'])
        from .google_image_reviews import review_references
        references = review_references(self, snapshot)
        reviewer = SourceFidelityReviewer(self.root, self.settings(), project_id=snapshot['project_id'])
        try:
            verdict = reviewer.review_image(snapshot, data, project['story'], references)
        except Exception as error:
            verdict = {'status': 'unverified', 'reason': 'Сервис смысловой проверки недоступен. Изображение сохранено.',
                       **self._google_review_pending(error)}
        if verdict.get('status') == 'uncertain' and self.settings().get('google_queue_enabled'):
            verdict = {**verdict,'review_state':'semantic_uncertainty'}
        if verdict.get('status') not in ('pass', 'fail'):
            verdict = {**verdict, 'status': 'unverified'}
        if verdict.get('job_id'):
            from .google_image_reviews import capture_binding
            verdict['binding']=capture_binding(self,snapshot,data,project['story'])
        if verdict['status'] == 'fail':
            from .domain import SourceImageRejected
            from .forgotten_cities.retries import RetryStore
            # Archive only the bounded candidates recorded by the retry budget.
            retry = RetryStore(self.db).reject(snapshot, verdict)
            digest = hashlib.sha256(data).hexdigest()
            directory = self.root / 'fidelity-rejected' / hashlib.sha256(str(snapshot.get('scene_id')).encode()).hexdigest()
            candidate_id = snapshot.get('fidelity_candidate_id') or verdict['image_sha256']
            if any(v.get('candidate_id', v['image_sha256']) == candidate_id for v in retry.get('rejected', [])):
                directory.mkdir(parents=True, exist_ok=True)
                (directory / (digest + '.png')).write_bytes(data)
                (directory / (digest + '.json')).write_text(dump({'scene_id': snapshot.get('scene_id'),
                    'project_id': snapshot['project_id'], 'scene_revision': snapshot.get('scene_revision'),
                    'verdict': verdict, 'retry': retry}), encoding='utf-8')
            if retry['exhausted'] and snapshot.get('scene_id'):
                with self.db.transaction() as conn:
                    conn.execute("""UPDATE generations SET state='failed', error=?, finished_at=?, progress=?
                        WHERE scene_id=? AND scene_revision=? AND purpose='scene' AND state='queued'
                        AND cancel_requested=0""", (verdict['reason'], now(), dump({'stage': 'Пропущен после смысловой проверки',
                        'percent': 0, 'source_fidelity_retry': retry}), snapshot['scene_id'], snapshot['scene_revision']))
                self.refresh_batches()
            raise SourceImageRejected('Смысловая проверка отклонила изображение: ' + verdict['reason'] +
                                      ' Исправление: ' + retry['correction'], retry)
        snapshot['source_fidelity_review'] = verdict
        return verdict

    def scene_snapshot(self, scene, allow_unlocked=False, *, _project=None, _narrative_grounder=None):
        project = _project if _project is not None else self.project(scene['project_id'])
        scene = {**scene, 'data': {**scene['data'], 'camera_sequence_index': max(0, scene.get('position', 1)-1)}}
        from .prompt_import import metadata as prompt_import_metadata, snapshot as imported_snapshot
        if prompt_import_metadata(project):
            return imported_snapshot(self, project, scene)
        channel_profile = None
        if project.get('channel_profile_id'):
            channel_profile = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (project['channel_profile_id'],))
        is_forgotten_cities = bool(channel_profile and channel_profile.get('profile_type') == 'forgotten_cities')
        if is_forgotten_cities:
            scene = ({**scene,'data':_narrative_grounder(scene['data'])} if _narrative_grounder
                     else self._ground_forgotten_scene(scene, project))
        characters = self.db.all('SELECT * FROM characters WHERE project_id=?', (project['id'],))
        from .subject_policy import enrich, normalize_scene
        characters = enrich(characters, project.get('cast_bible'))
        project = {**project, 'characters': characters}
        from .character_selection import resolve_links
        linked, link_errors = resolve_links(scene['data'], characters)
        if link_errors:
            raise DomainError('; '.join(e['message'] for e in link_errors))
        linked = normalize_scene(linked, characters)
        scene = {**scene, 'data': linked}
        by_name = {c['name']: c for c in characters}
        selected = [by_name[name] for name in linked['characters'] if name in by_name]
        selected_humans = [character for character in selected if not is_nonhuman_character(character)]
        locations = self.db.all('SELECT * FROM locations WHERE project_id=?', (project['id'],))
        from .generation_scene import normalize as normalize_generation_scene
        scene = {**scene, 'data': normalize_generation_scene(project, scene['data'], locations)}
        location_by_name = {item['name']: item for item in locations}
        selected_locations = ([location_by_name[scene['data']['location_ref']]]
                              if scene['data'].get('location_ref') and scene['data']['location_ref'] in location_by_name else [])
        if is_forgotten_cities:
            from .forgotten_cities.references import scene_locations
            selected_locations = scene_locations(project, scene['data'], locations)
        from .generation_scene import reference_locations
        selected_locations, reference_omissions = reference_locations(project, scene['data'], selected_locations)
        props = self.db.all('SELECT * FROM props WHERE project_id=?', (project['id'],))
        mode = project.get('mode')
        from .mode_policy import allows, filter_references
        if not allows(project, 'location'): selected_locations = []
        if not allows(project, 'prop'): props = []
        # Do not reject a real human scene only because the analysis omitted the
        # character link.  This occurred after a restart for several story beats.
        # A no-people check is enabled only when neither the selected cast nor the
        # source text says that a person belongs in the image.
        source_has_people = scene_mentions_people(scene['data'], by_name)
        episodic_roles = scene['data'].get('episodic_people') or scene['data'].get('background_roles') or extract_scene_episodic_roles(scene['data'], by_name)
        anonymous_people = max(len(episodic_roles), scene_anonymous_people_count(scene['data'], by_name))
        from .domain import verified_source_participants
        source_selection = verified_source_participants(scene['data'], project['story'])
        if (scene['data'].get('forgotten_cities_prompt_contract') or {}).get('sequence_reviewed'):
            # A quoted account of residents does not put a resident into this
            # reviewed architectural shot. Preserve explicit participant slots.
            source_selection=[{'name':role} for role in scene['data']['audit_roles']]
        editorial = (scene['data'].get('forgotten_cities_prompt_contract') or {}).get('editorial_decision')
        if source_selection is not None:
            bound_names = {c['name'].casefold() for c in selected_humans}
            episodic_roles = list(dict.fromkeys(p['name'] for p in source_selection if p['name'].casefold() not in bound_names))
            anonymous_people = len(episodic_roles)
        elif editorial:
            episodic_roles = [p['name'] for p in editorial['participants']]
            anonymous_people = len(episodic_roles)
        visible_shot_observers = scene_over_shoulder_anonymous_people_count(scene['data'], by_name)
        if editorial or source_selection is not None:
            visible_shot_observers = 0
        source_has_unbound_person = anonymous_people > 0 or len(episodic_roles) > 0
        declared_people = scene['data'].get('visible_people')
        if source_selection is not None:
            declared_people = len(selected_humans) + len(source_selection)
        if type(declared_people) is not int:
            declared_people = len(selected_humans) + len(episodic_roles) if (selected_humans or episodic_roles) else (1 if source_has_people else 0)
        if visible_shot_observers:
            declared_people = max(declared_people, len(selected_humans) + visible_shot_observers)
        if episodic_roles:
            declared_people = max(declared_people, len(selected_humans) + len(episodic_roles))
        # An old scene edited before the composition-contract release may have
        # a stored zero even after a human source quote was changed.
        if mode in {'characters', 'dynamic'} and declared_people == 0 and (selected_humans or source_has_unbound_person):
            declared_people = max(1, min(3, len(selected_humans) + anonymous_people))
        # A stale analysis may still carry visible_people: 1 for a named animal.
        # Only an explicit source role may add a human alongside a non-human cast.
        if selected and not selected_humans and not source_has_unbound_person:
            declared_people = 0
        if not selected and not source_has_unbound_person:
            # A pronoun or emotional description without a cast link cannot
            # authorize a random face in an otherwise object-only frame.
            declared_people = 0
        # Forgotten Cities is configured in Objects mode because it anchors
        # places and artifacts.  Its evidence-backed people policy can still
        # require an anonymous historical person in a documentary shot.
        expected_people = declared_people if mode in {'characters', 'dynamic'} or selected_humans or is_forgotten_cities or source_selection is not None else 0
        binding = (scene['data'].get('forgotten_cities_prompt_contract') or {}).get('source_binding')
        if binding in ('buried_remains', 'artifact_detail'):
            expected_people = 0
            episodic_roles = []
            selected = []
            selected_humans = []
        if scene['data'].get('population_policy') == 'object_only':
            expected_people = 0
            episodic_roles = []
            selected = []
            selected_humans = []
        from .domain import people_count_unresolved
        unresolved_people=people_count_unresolved(scene['data']) and not selected_humans
        if unresolved_people:expected_people=0  # unknown census, never a verified zero
        uninhabited = expected_people == 0 and not unresolved_people and scene['data'].get('subject_state') != 'buried_remains'
        if expected_people < len(selected_humans):
            raise DomainError('В сцене указано меньше видимых людей, чем выбранных персонажей.')
        prompt_scene = {**scene['data'], 'visible_people': expected_people, 'episodic_people': episodic_roles}
        from .participant_identity import bind_identity, identity_reference
        participant_identity = bind_identity(self, project, prompt_scene, expected_people)
        if participant_identity:
            prompt_scene['participant_identity'] = participant_identity
        omitted_context_locations = []
        if is_forgotten_cities:
            from .forgotten_cities.references import isolate_material_context
            selected_locations, omitted_context_locations = isolate_material_context(
                prompt_scene, selected_locations, participant_identity)
        channel_context = None
        if project.get('channel_profile_id'):
            ch_profile = self.db.one("SELECT * FROM channel_profiles WHERE id=?", (project['channel_profile_id'],))
            if ch_profile:
                ver_id = project.get('channel_profile_version_id') or ch_profile.get('active_version_id')
                ch_version = self.db.one("SELECT * FROM channel_profile_versions WHERE id=?", (ver_id,)) if ver_id else None
                ch_characters = self.db.all("SELECT * FROM channel_characters WHERE channel_id=?", (project['channel_profile_id'],))
                ch_assets = self.db.all("SELECT * FROM channel_reference_assets WHERE channel_id=?", (project['channel_profile_id'],))
                
                # Check reference asset files integrity (Scenario G)
                for asset in ch_assets:
                    f_path_str = asset.get('file_path') or ''
                    if f_path_str:
                        asset_path = self.root / f_path_str
                        if not asset_path.exists() or asset_path.stat().st_size == 0:
                            raise DomainError(
                                f"Файл референса '{asset.get('asset_type')}' ({f_path_str}) отсутствует или поврежден. Пожалуйста, восстановите файл или замените его в настройках канала.",
                                400
                            )
                
                channel_context = {
                    'profile': dict(ch_profile),
                    'version': dict(ch_version) if ch_version else None,
                    'characters': [dict(c) for c in ch_characters],
                    'reference_assets': [dict(a) for a in ch_assets],
                }

        # Illustration scenes describe people independently; no portrait casting.
        if mode == 'illustration':
            selected = [{**character, 'locked': False} for character in selected]
            allow_unlocked = True

        prompt, references, compiled_style_contract, bundle = compile_prompt(
            project, prompt_scene, selected, selected_locations, props=props, allow_unlocked=allow_unlocked,
            channel_context=channel_context, return_contract=True
        )
        if participant_identity:
            references.insert(0, identity_reference(participant_identity))
        references = filter_references(project, references)
        reference_binding = None
        if is_forgotten_cities:
            expected = {json.loads(l['references_json'])['environment']
                        if isinstance(l.get('references_json'), str) else l['references']['environment']
                        for l in selected_locations}
            attached = {r['asset_id'] for r in references if r.get('location_id')}
            if expected != attached:
                raise DomainError('Эталон локации выбран, но не прикреплён к заданию кадра.', 409)
            reference_binding = {'version': 'scene-reference-binding/1',
                                 'environment_asset_ids': sorted(attached),
                                 'participant_asset_ids': [participant_identity['asset_id']] if participant_identity else [],
                                 'reason': 'source_compatible_environment' if attached else 'no_compatible_environment_reference'}
            if omitted_context_locations:
                reference_binding['omitted_context_location_ids'] = [l['id'] for l in omitted_context_locations]
                reference_binding['reason'] = 'unbound_participants_use_source_text_setting'

        snapshot = {
            'profile': self.profile(),
            'project_id': project['id'],
            'scene_id': scene['id'],
            'scene_revision': scene['current_revision'],
            'prompt': prompt,
            'references': references,
            'reference_binding': reference_binding,
            'reference_omissions': reference_omissions,
            'uninhabited': uninhabited,
            'expected_people': expected_people,
            'people_count_unresolved': unresolved_people,
            'detail_faces': False,
            'scene': prompt_scene,
            'story_hash': fingerprint(project['story']),
            'style': project['style'],
            'world': project['world'],
            'template_version': 9,
            'channel_profile_id': project.get('channel_profile_id'),
            'channel_profile_version_id': project.get('channel_profile_version_id'),
            'channel_character_version_map': project.get('channel_character_version_map'),
            'channel_reference_hashes': [a['content_hash'] for a in (channel_context.get('reference_assets') or [])] if channel_context else [],
            'compiled_style_contract': compiled_style_contract,
            'reference_bundle': bundle.to_dict() if bundle else None,
            'generation_reference_bundle': bundle.to_dict() if bundle else None,
        }

        if visual_reviews_enabled() and prompt_scene.get('forgotten_cities_prompt_contract'):
            from .forgotten_cities.retries import RetryStore, retry_prompt
            retry = RetryStore(self.db).get(snapshot)
            snapshot['source_fidelity_retry'] = retry
            snapshot['prompt'] = retry_prompt(prompt, retry)
        if self.settings().get('source_contract_enabled') is True:
            from .source_contract import scene_contract, render_contract
            from .forgotten_cities.fidelity import SourceFidelityReviewer
            metadata=project.get('analysis_metadata') or {}
            if isinstance(metadata,str):metadata=json.loads(metadata)
            anchors=[a for a in metadata.get('forgotten_cities',{}).get('anchors',[]) if a.get('anchor_type')=='artifact']
            plan=SourceFidelityReviewer(self.root,self.settings(),project_id=project['id']).cached_plan(prompt_scene,project['story'],anchors)
            from .source_catalog import SourceCatalog
            required=set((prompt_scene.get('forgotten_cities_prompt_contract') or {}).get('required_artifacts') or [])
            required.update(prompt_scene.get('required_anchor_ids') or [])
            location_id=prompt_scene.get('location_anchor_id')
            if location_id:required.add(location_id)
            required.update(r['anchor_id'] for r in references if r.get('anchor_id'))
            all_anchors=metadata.get('forgotten_cities',{}).get('anchors',[])
            packet_ids = (prompt_scene.get('forgotten_cities_prompt_contract') or {}).get('anchor_ids', [])
            required.update(a['anchor_id'] for a in all_anchors if a.get('source_kind') and a.get('anchor_id') in packet_ids)
            ids=[a['anchor_id'] for a in all_anchors if a.get('anchor_id') and (a['anchor_id'] in required or a.get('canonical_name') in required)]
            current_source = ((prompt_scene.get('forgotten_cities_prompt_contract') or {}).get('narrative_context') or {}).get('source', {})
            catalog_context=SourceCatalog(self).context(project['id'],ids, current_source.get('end', prompt_scene.get('source_end')))
            snapshot['source_contract']=scene_contract(project,{**scene,'data':prompt_scene},references,plan,catalog_context)
            snapshot['prompt']+='\nFINAL RENDER CONTRACT (scene constraints; never image lettering):\n'+dump(render_contract(snapshot['source_contract']))
        from .generation_scene import compile_wire
        wire = compile_wire(project, prompt_scene, references)
        if wire:
            snapshot['flow_wire'] = wire
            if wire.get('error') and snapshot.get('source_contract'):
                snapshot['source_contract']['conflicts'].append(wire['error'])
                snapshot['source_contract']['status'] = 'needs_resolution'
                snapshot['source_contract']['hash'] = fingerprint({k:v for k,v in snapshot['source_contract'].items() if k!='hash'})
        return snapshot

    def scene_composite_reference(self, scene_id):
        from PIL import Image
        scene = self.scene(scene_id)
        snap = self.scene_snapshot(scene, allow_unlocked=True)
        refs = snap.get('references', [])
        if len(refs) < 2:
            return {'scene_id': scene_id, 'has_composite': False, 'count': len(refs), 'data_url': None}

        items = []
        for r in refs:
            try:
                asset = self.asset(r['asset_id'])
                img_path = self.root / asset['path']
                if not img_path.exists():
                    continue
                img = Image.open(img_path).convert('RGB')
            except Exception:
                continue

            role = r.get('role', '')
            if 'character_id' in r:
                char_rows = self.db.all('SELECT name FROM characters WHERE id=?', (r['character_id'],))
                name = char_rows[0]['name'] if char_rows else r.get('name', 'Unknown')
                label = f"Character: @{name}"
            elif 'location_id' in r:
                loc_rows = self.db.all('SELECT name FROM locations WHERE id=?', (r['location_id'],))
                name = loc_rows[0]['name'] if loc_rows else r.get('name', 'Unknown')
                label = f"Location: {name}"
            elif 'prop_id' in r:
                prop_rows = self.db.all('SELECT name FROM props WHERE id=?', (r['prop_id'],))
                name = prop_rows[0]['name'] if prop_rows else r.get('name', 'Unknown')
                label = f"Prop: {name}"
            else:
                label = f"Reference: {role}"
            items.append((img, label))

        if len(items) < 2:
            return {'scene_id': scene_id, 'has_composite': False, 'count': len(items), 'data_url': None}

        sheet = build_composite_reference_sheet(items)
        buf = io.BytesIO()
        sheet.save(buf, format='PNG')
        png_bytes = buf.getvalue()
        sha = hashlib.sha256(png_bytes).hexdigest()
        b64 = base64.b64encode(png_bytes).decode('ascii')
        return {
            'scene_id': scene_id,
            'has_composite': True,
            'count': len(items),
            'sha256': sha,
            'data_url': f"data:image/png;base64,{b64}",
            'names': [it[1] for it in items],
            'width': 1536,
            'height': 864
        }

    def batch(self, project_id, idempotency_key, scene_ids=None):
        required_text(idempotency_key, 'Ключ запроса', 100)
        self.project(project_id)
        self.accept_ready_references(project_id)
        preflight = self.project_preflight(project_id)
        if not preflight.get('ok', False):
            errors = preflight.get('errors', [])
            msg = '; '.join([e.get('message', str(e)) if isinstance(e, dict) else str(e) for e in errors[:3]])
            raise DomainError(f'Генерация заблокирована (Preflight Error): {msg or "Проект не прошёл проверку целостности."}', 400)
        all_scenes = self.db.all('SELECT id FROM scenes WHERE project_id=? ORDER BY position', (project_id,))
        known = [scene['id'] for scene in all_scenes]
        if scene_ids is None:
            scene_ids = known
        if not isinstance(scene_ids, list) or not scene_ids or len(scene_ids) != len(set(scene_ids)) or not set(scene_ids) <= set(known):
            raise DomainError('Выберите существующие сцены проекта.')
        scenes = [self.scene(scene_id) for scene_id in known if scene_id in scene_ids]
        if self.settings().get('image_backend') == 'flow' and len(scenes) == 1:
            # Repeated clicks must not create several browser jobs for one frame.
            existing_proxy = self.db.one('''SELECT b.* FROM generations g
                JOIN batches b ON b.id=g.batch_id
                WHERE g.scene_id=? AND g.scene_revision=? AND g.state='queued'
                  AND g.cancel_requested=0 AND b.state IN ('running','waiting_review')
                ORDER BY g.created_at DESC, g.rowid DESC LIMIT 1''',
                                         (scenes[0]['id'], scenes[0]['current_revision']))
            if existing_proxy:
                return existing_proxy
        plan_review=self.review_source_plans(project_id, scenes)
        snapshot_project=self.project(project_id)
        from .narrative_contract import sequence_grounder
        snapshot_metadata=snapshot_project.get('analysis_metadata') or {}
        if isinstance(snapshot_metadata,str):snapshot_metadata=json.loads(snapshot_metadata)
        snapshot_grounder=(sequence_grounder(snapshot_project['story'],snapshot_metadata)
            if snapshot_metadata.get('narrative_storyboard') else None)
        snapshots = [self.scene_snapshot(scene,_project=snapshot_project,_narrative_grounder=snapshot_grounder) for scene in scenes]
        queued_review=self.settings().get('google_queue_enabled') is True
        if queued_review:
            request_hash=self._source_batch_signature(project_id,scenes)
            if plan_review.get('status')=='unverified':
                for snapshot in snapshots:
                    snapshot['plan_review_wait']={**plan_review,'signature':request_hash}
        # Retry counters/corrected prompts evolve during one batch and must not
        # invalidate an idempotent replay of its original scene request.
        legacy_request_hash = fingerprint([(s['id'], s['current_revision'],
            {k: v for k, v in snap.items() if k not in ('prompt', 'source_fidelity_retry', 'fidelity_candidate_id')})
            for s, snap in zip(scenes, snapshots)])
        if not queued_review:
            request_hash=legacy_request_hash
        replay = self.db.one('SELECT * FROM batches WHERE project_id=? AND idempotency_key=?', (project_id, idempotency_key))
        if replay:
            if replay['request_hash'] != request_hash:
                raise DomainError('Этот ключ запроса использован с другими параметрами.', 409)
            return replay
        if len(scenes) == 1 and snapshots[0].get('source_fidelity_retry', {}).get('exhausted'):
            # An explicit request for one problem frame starts a new bounded cycle.
            # Reopening/resuming a full queue never clears its budget.
            from .forgotten_cities.retries import RetryStore
            RetryStore(self.db).reset(scenes[0]['id'])
            snapshots = [self.scene_snapshot(scenes[0])]
        with self.db.transaction() as connection:
            existing = connection.execute('SELECT * FROM batches WHERE project_id=? AND idempotency_key=?', (project_id, idempotency_key)).fetchone()
            if existing:
                if existing['request_hash'] != request_hash:
                    raise DomainError('Этот ключ запроса использован с другими параметрами.', 409)
                return dict(existing)
            if self.settings().get('image_backend') == 'flow':
                # Browser clicks use new keys. Reuse an identical active queue
                # atomically, including bulk requests and concurrent callers.
                active = connection.execute('''SELECT * FROM batches
                    WHERE project_id=? AND request_hash=? AND state IN ('running','waiting_review')
                    ORDER BY created_at DESC LIMIT 1''', (project_id, request_hash)).fetchone()
                if active:
                    return dict(active)
            batch_id = uid()
            batch_state='waiting_review' if any(snapshot.get('plan_review_wait') for snapshot in snapshots) else 'running'
            connection.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)', (batch_id, project_id, idempotency_key, request_hash, batch_state, now()))
            for scene, snapshot in zip(scenes, snapshots):
                actual = connection.execute('SELECT current_revision FROM scenes WHERE id=?', (scene['id'],)).fetchone()[0]
                if actual != scene['current_revision']:
                    raise DomainError('Сцены изменились при создании очереди.', 409)
                self.insert_generation(connection, project_id, 'scene', snapshot, batch_id=batch_id,
                                       scene_id=scene['id'], scene_revision=scene['current_revision'])
        self.refresh_batches()
        return self.db.one('SELECT * FROM batches WHERE id=?', (batch_id,))

    def _source_batch_signature(self,project_id,scenes):
        project=self.project(project_id);settings=self.settings()
        references={kind:self.db.all(f'SELECT id,revision,locked,references_json FROM {kind} WHERE project_id=? ORDER BY id',(project_id,)) for kind in ('characters','locations','props')}
        profile={k:settings.get(k) for k in ('image_backend','google_image_model','google_text_model','width','height','checkpoint','vae','sampler','scheduler','steps','cfg')}
        catalog_bindings=[]
        if settings.get('source_contract_enabled') is True:
            from .source_catalog import SourceCatalog
            catalog=SourceCatalog(self)
            if catalog.path.exists():
                with catalog.db() as db:
                    catalog_bindings=[dict(row) for row in db.execute('SELECT anchor_id,entry_id,revision,story_hash,mode FROM links WHERE project_id=? ORDER BY anchor_id',(project_id,))]
        return fingerprint({'story':project['story'],'channel':project.get('channel_profile_id'),
            'channel_version':project.get('channel_profile_version_id'),'profile':profile,'references':references,
            'catalog_bindings':catalog_bindings,
            'scenes':[(s['id'],s['current_revision'],s['data']) for s in scenes]})

    def resume_google_source_batches(self,queue=None,limit=8):
        if self.settings().get('google_queue_enabled') is not True:
            return {'updated':0}
        from .google_queue import configured_queue
        queue=queue or configured_queue(self.root,self.settings());updated=0
        for batch in self.db.all("SELECT * FROM batches WHERE state='waiting_review' ORDER BY created_at"):
            if updated>=limit:break
            if queue.project_paused(batch['project_id']):continue
            rows=self.db.all("SELECT * FROM generations WHERE batch_id=? AND state='queued' AND cancel_requested=0 ORDER BY rowid",(batch['id'],))
            if not rows:continue
            snapshots=[json.loads(row['snapshot']) for row in rows]
            wait=snapshots[0].get('plan_review_wait') or {}
            try:
                scenes=[self.scene(row['scene_id']) for row in rows]
                if self._source_batch_signature(batch['project_id'],scenes)!=wait.get('signature'):
                    raise DomainError('Исходные данные пакета изменились. Проверка не применена.',409)
                job=queue.inspect(wait['job_id'])
                if job['project']!=batch['project_id'] or job['category']!='source_plan':
                    raise DomainError('Проверка не принадлежит этому пакету.',409)
                if job['state']!='succeeded':
                    if job['state'] not in ('queued','running','waiting_registry','waiting_model','waiting_quota','retry_wait'):
                        raise DomainError('Проверка пакета требует внимания.',409)
                    continue
                result=self.review_source_plans(batch['project_id'],scenes)
                if result.get('status')=='unverified':
                    for snapshot in snapshots:snapshot['plan_review_wait']={**result,'signature':wait['signature']}
                else:
                    snapshots=[self.scene_snapshot(scene) for scene in scenes]
                with self.db.transaction() as connection:
                    current=connection.execute('SELECT state FROM batches WHERE id=?',(batch['id'],)).fetchone()
                    if current['state']!='waiting_review':continue
                    if self._source_batch_signature(batch['project_id'],[self.scene(row['scene_id']) for row in rows])!=wait['signature']:
                        raise DomainError('Пакет изменился при применении проверки.',409)
                    for row,snapshot in zip(rows,snapshots):
                        connection.execute("UPDATE generations SET snapshot=? WHERE id=? AND state='queued' AND cancel_requested=0",(dump(snapshot),row['id']))
                    if result.get('status')!='unverified':
                        connection.execute("UPDATE batches SET state='running' WHERE id=?",(batch['id'],))
                updated+=1
            except (DomainError,KeyError,ValueError,OSError) as error:
                with self.db.transaction() as connection:
                    connection.execute("UPDATE batches SET state='paused' WHERE id=? AND state='waiting_review'",(batch['id'],))
                    connection.execute("UPDATE generations SET error=?,progress=? WHERE batch_id=? AND state='queued'",('Проверка пакета требует внимания. Изображения не запущены.',dump({'stage':'Проверка пакета требует внимания','percent':0}),batch['id']))
                updated+=1
        return {'updated':updated}

    def control_batch(self, batch_id, action):
        if action not in ('pause', 'resume', 'cancel'):
            raise DomainError('Неизвестное действие.')
        refreshed = []
        if action == 'resume':
            # Queues paused during the earlier over-strict face check retain an
            # old snapshot.  Refresh only queued, unchanged scenes so Resume is
            # safe after updating the application, without altering a frame that
            # is already running, complete, or manually edited.
            queued = self.db.all("SELECT * FROM generations WHERE batch_id=? AND state='queued'", (batch_id,))
            for generation in queued:
                snapshot = json.loads(generation['snapshot'])
                if snapshot.get('plan_review_wait'):
                    continue
                missing_reference_binding = (self._project_profile_type(generation['project_id']) == 'forgotten_cities'
                                             and not snapshot.get('reference_binding'))
                if snapshot.get('template_version', 0) >= 9 and not missing_reference_binding:
                    continue
                if missing_reference_binding:
                    from .flow_admission import FlowAdmission
                    with FlowAdmission(self).db() as admission_db:
                        pending = admission_db.execute("SELECT request_id FROM admissions WHERE scene=? AND revision=? AND state='reserved' LIMIT 1",
                                                       (generation['scene_id'], generation['scene_revision'])).fetchone()
                    if pending:
                        raise DomainError('У кадра есть незавершённая отправка Flow. Сначала сохраните её результат; повторная отправка остановлена.', 409)
                scene = self.scene(generation['scene_id']) if generation.get('scene_id') else None
                if scene and scene['current_revision'] == generation['scene_revision']:
                    refreshed.append((dump(self.scene_snapshot(scene)), generation['id']))
        with self.db.transaction() as connection:
            batch = connection.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise DomainError('Пакет не найден.', 404)
            if batch['state'] in ('cancelled', 'completed', 'completed_with_errors'):
                raise DomainError('Пакет уже завершён.', 409)
            state = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[action]
            if action=='resume':
                pending=connection.execute("SELECT id FROM generations WHERE batch_id=? AND state='queued' AND json_extract(snapshot,'$.plan_review_wait') IS NOT NULL LIMIT 1",(batch_id,)).fetchone()
                if pending:state='waiting_review'
            connection.execute('UPDATE batches SET state=? WHERE id=?', (state, batch_id))
            for snapshot, generation_id in refreshed:
                connection.execute('UPDATE generations SET snapshot=? WHERE id=? AND state=\'queued\'',
                                   (snapshot, generation_id))
            if action == 'cancel':
                connection.execute("UPDATE generations SET cancel_requested=1 WHERE batch_id=? AND state NOT IN ('succeeded','failed','cancelled','resource_degraded')", (batch_id,))
                connection.execute("UPDATE generations SET state='cancelled',finished_at=? WHERE batch_id=? AND state='queued'", (now(), batch_id))
        return {'state': state}

    def cancel_generation(self, generation_id):
        with self.db.transaction() as connection:
            generation = connection.execute('SELECT * FROM generations WHERE id=?', (generation_id,)).fetchone()
            if not generation:
                raise DomainError('Генерация не найдена.', 404)
            if generation['state'] in TERMINAL:
                raise DomainError('Генерация завершена.', 409)
            connection.execute('UPDATE generations SET cancel_requested=1 WHERE id=?', (generation_id,))
            if generation['state'] in ('queued', 'waiting_review','waiting_prompt','blocked_prompt'):
                connection.execute("UPDATE generations SET state='cancelled',finished_at=? WHERE id=?", (now(), generation_id))
        return {'cancel_requested': True}

    def retry(self, generation_id):
        old = self.db.one('SELECT * FROM generations WHERE id=?', (generation_id,))
        if not old or old['state'] not in ('failed', 'cancelled', 'resource_degraded'):
            raise DomainError('Повтор доступен только для завершённых неудачных попыток.', 409)
        snapshot = json.loads(old['snapshot'])
        if old['purpose'] == 'scene' and old['scene_id']:
            from .forgotten_cities.retries import RetryStore
            RetryStore(self.db).reset(old['scene_id'])
            scene = self.scene(old['scene_id'])
            snapshot = self.scene_snapshot(scene)
            old['scene_revision'] = scene['current_revision']
        with self.db.transaction() as connection:
            generation_id = self.insert_generation(connection, old['project_id'], old['purpose'], snapshot,
                scene_id=old['scene_id'], scene_revision=old['scene_revision'], character_id=old['character_id'],
                character_revision=old['character_revision'], location_id=old['location_id'],
                location_revision=old['location_revision'], prop_id=old.get('prop_id'),
                prop_revision=old.get('prop_revision'))
        return {'generation_id': generation_id}

    def select_generation(self, generation_id, note=''):
        with self.db.transaction() as connection:
            generation = connection.execute('SELECT * FROM generations WHERE id=?', (generation_id,)).fetchone()
            if not generation or generation['state'] != 'succeeded' or not generation['scene_id']:
                raise DomainError('Нужен успешный результат сцены.')
            scene = connection.execute('SELECT * FROM scenes WHERE id=?', (generation['scene_id'],)).fetchone()
            if scene['current_revision'] != generation['scene_revision']:
                raise DomainError('Результат устаревшей версии. Сгенерируйте текущую сцену.', 409)
            if scene['active_generation_id'] != generation_id:
                connection.execute("""
                    UPDATE veo_jobs SET status='cancelled', completed_at=?
                    WHERE scene_id=? AND status IN ('queued_omni', 'generating_omni', 'queued_veo', 'generating_veo')
                """, (now(), generation['scene_id']))
            connection.execute('UPDATE scenes SET active_generation_id=? WHERE id=?', (generation_id, generation['scene_id']))
            profile = connection.execute('''SELECT cp.profile_type FROM projects p
                LEFT JOIN channel_profiles cp ON cp.id=p.channel_profile_id WHERE p.id=?''',
                (scene['project_id'],)).fetchone()
            if not profile or profile['profile_type'] != 'forgotten_cities':
                timestamp = now()
                connection.execute('''INSERT INTO reviews(
                    generation_id,accepted,note,created_at,status,reason_code,continuity_json,passport_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(generation_id) DO UPDATE SET accepted=1,note=excluded.note,
                    status='approved',updated_at=excluded.updated_at''',
                    (generation_id, 1, str(note)[:4000], timestamp, 'approved', '', '{}', '{}', timestamp))
            self.db.event(connection, scene['project_id'], 'generation_selected', {'id': generation_id, 'scene_id': generation['scene_id']})
        review = self.generation_review(generation_id)
        return {'selected': generation_id, 'review_status': review.get('status')}

    def asset(self, asset_id):
        asset = self.db.one('SELECT * FROM assets WHERE id=?', (asset_id,))
        if not asset:
            raise DomainError('Изображение не найдено.', 404)
        path = (self.root / asset['path']).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise DomainError('Файл изображения недоступен.', 404)
        return asset

    def export(self, project_id):
        from PIL import Image, ImageDraw
        project = self.detail(project_id)
        if not project['scenes']:
            raise DomainError('В проекте нет сцен.')
        manifest = {'schema_version': 2, 'project': {k: project[k] for k in ('id', 'title', 'story', 'style', 'world')},
                    'characters': project['characters'], 'locations': project.get('locations', []),
                    'props': [p for p in project.get('props', []) if prop_is_story(p)], 'scenes': [], 'created_at': now()}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            reference_ids = set()
            contact_items = []
            for prop in project.get('props', []):
                for ref_key in ('prop', 'item'):
                    ref_asset = prop.get('references', {}).get(ref_key)
                    if ref_asset:
                        reference_ids.add(ref_asset)
            for loc in project.get('locations', []):
                ref_asset = loc.get('references', {}).get('environment')
                if ref_asset:
                    reference_ids.add(ref_asset)
            for scene in project['scenes']:
                if not scene['active_generation_id'] or scene['stale']:
                    raise DomainError(f"Сцена {scene['position']}: нет выбранного актуального результата.", 409)
                generation = next(g for g in project['generations'] if g['id'] == scene['active_generation_id'])
                asset = self.asset(generation['asset_id'])
                image_bytes = (self.root / asset['path']).read_bytes()
                if hashlib.sha256(image_bytes).hexdigest() != asset['sha256']:
                    raise DomainError(f"Сцена {scene['position']}: файл повреждён, hash не совпадает.", 409)
                filename = f"{scene['position']:04d}_{scene['id']}.png"
                archive.writestr(filename, image_bytes)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    thumbnail = image.convert('RGB').resize((320, 180), Image.Resampling.LANCZOS)
                thumb_buffer = io.BytesIO()
                thumbnail.save(thumb_buffer, format='PNG', optimize=True)
                thumb_name = f"thumbnails/{scene['position']:04d}_{scene['id']}.png"
                archive.writestr(thumb_name, thumb_buffer.getvalue())
                contact_items.append((scene['position'], thumbnail))
                manifest['scenes'].append({'scene': scene, 'generation': generation, 'asset': asset,
                                           'file': filename, 'thumbnail': thumb_name})
                for reference in generation['snapshot'].get('references', []):
                    reference_ids.add(reference['asset_id'])
                metadata = self.root / 'metadata' / (generation['id'] + '.json')
                if not metadata.is_file():
                    raise DomainError('Отсутствует metadata генерации; экспорт остановлен.', 409)
                archive.write(metadata, 'metadata/' + metadata.name)
            for asset_id in sorted(reference_ids):
                asset = self.asset(asset_id)
                archive.write(self.root / asset['path'], f'references/{asset_id}.png')
            columns, cell_width, cell_height = 4, 320, 210
            rows = (len(contact_items) + columns - 1) // columns
            sheet = Image.new('RGB', (columns * cell_width, rows * cell_height), 'white')
            draw = ImageDraw.Draw(sheet)
            for index, (position, thumbnail) in enumerate(contact_items):
                x, y = (index % columns) * cell_width, (index // columns) * cell_height
                draw.text((x + 8, y + 7), f'Scene {position}', fill='black')
                sheet.paste(thumbnail, (x, y + 30))
            sheet_buffer = io.BytesIO()
            sheet.save(sheet_buffer, format='PNG', optimize=True)
            archive.writestr('contact_sheet.png', sheet_buffer.getvalue())
            manifest['contact_sheet'] = 'contact_sheet.png'
            archive.writestr('manifest.json', dump(manifest))
        return buffer.getvalue()

    def project_prompts(self, project_id, *, include_all_scenes=False, scene_ids=None, _detail=None):
        detail = _detail if _detail is not None else self.detail(project_id)
        from .prompt_import import metadata as prompt_import_metadata, project_prompts as imported_prompts
        if prompt_import_metadata(detail):
            return imported_prompts(self, detail, scene_ids)
        requested_ids = set(scene_ids) if scene_ids is not None else None
        forgotten = (detail.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities'
        from .narrative_contract import sequence_grounder
        prompt_grounder=None;prompt_ground_error=None
        try:
            if forgotten and (detail.get('analysis_metadata') or {}).get('narrative_storyboard'):
                prompt_grounder=sequence_grounder(detail['story'],detail['analysis_metadata'])
        except DomainError as error:
            prompt_ground_error=error
        if forgotten and not include_all_scenes:
            queued_ids = {row['scene_id'] for row in self.db.all(
                "SELECT DISTINCT scene_id FROM generations WHERE project_id=? AND state='queued' AND cancel_requested=0 AND purpose='scene'",
                (project_id,))}
            requested_ids = queued_ids if requested_ids is None else requested_ids & queued_ids
        char_map = {c['id']: c for c in detail.get('characters', [])}
        cast_map = {c.get('cast_id') or c['id']: c for c in detail.get('characters', [])}
        bible_chars = {c['id']: c for c in detail.get('cast_bible', {}).get('characters', []) if isinstance(c, dict) and 'id' in c}
        # A named story person must be linked to a locked reference.  A concrete
        # one-off role (for example, "a passerby") is allowed without one; it
        # must not be confused with an omitted recurring character.  Animal
        # names are intentionally excluded here: a passerby may talk about a
        # raven without making the raven a human identity in the shot.
        known_human_names = [
            c.get('name', '') for c in detail.get('characters', [])
            if not is_nonhuman_character(c)
        ]
        loc_map = {l['id']: l for l in detail.get('locations', [])}
        prop_map = {p['id']: p for p in detail.get('props', [])}
        scenes = []
        prev_contract = None
        for s in detail['scenes']:
            if (detail.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities':
                try:
                    if prompt_ground_error:raise prompt_ground_error
                    s = ({**s,'data':prompt_grounder(s['data'])} if prompt_grounder
                         else self._ground_forgotten_scene(s, detail))
                except DomainError as error:
                    # A bad scene cannot prevent casting its references. It
                    # remains visibly blocked and must never enter Flow.
                    if requested_ids is None or s['id'] in requested_ids:
                        scenes.append({'scene_id': s['id'], 'position': s['position'],
                            'prompt': '', 'flow_prompt': '', 'references': [], 'has_image': False,
                            'ready_for_flow': False, 'semantic_valid': False, 'render_ready': False,
                            'flow_error': str(error)})
                    continue
            # Signed source content wins over stale legacy fields in every mode.
            from .generation_scene import normalize as normalize_generation_scene
            s = {**s, 'data': normalize_generation_scene(detail, s['data'], detail.get('locations', []))}
            # 1. Compute Presence Contract FIRST
            from .mode_policy import allows
            contract = resolve_scene_presence_contract(
                {**s['data'], 'portrait_references_enabled': allows(detail, 'character')}, detail.get('characters', []), detail.get('locations', []),
                story=detail.get('story', ''),
                previous_contract=prev_contract,
                require_locked_portraits=True,
                scene_index=s['position']
            )
            prev_contract = contract
            if requested_ids is not None and s['id'] not in requested_ids:
                continue

            # 2. Build normalized scene strictly from canonical contract outputs
            normalized_scene_data = {
                **s['data'],
                'character_ids': list(contract['visible_character_ids']),
                'characters': list(contract['visible_characters']),
                'visible_people': contract['visible_people'],
                'episodic_people': list(contract['episodic_people']),
                'voice_only_characters': list(contract.get('voice_only_characters', [])),
                'offscreen_characters': list(contract.get('offscreen_characters', [])),
                'depicted_people': list(contract.get('depicted_people', [])),
                'location_ref': contract['location_ref'] or '',
                'location': contract['location_ref'] or s['data'].get('location', ''),
            }
            normalized_scene = {**s, 'data': normalized_scene_data}

            # 3. Form snapshot, Flow prompt, and references from the normalized scene
            snapshot = self.scene_snapshot(normalized_scene, allow_unlocked=True,_project=detail,_narrative_grounder=prompt_grounder)
            normalized_scene_data = snapshot['scene']

            scene_chars = [cast_map[cid] for cid in contract['visible_character_ids'] if cid in cast_map]
            if not scene_chars and contract['visible_characters']:
                name_to_cast = {c['name'].casefold(): c for c in detail.get('characters', [])}
                scene_chars = [name_to_cast[cn.casefold()] for cn in contract['visible_characters'] if cn.casefold() in name_to_cast]

            scene_locs = [l for l in detail.get('locations', []) if l['name'] == normalized_scene_data.get('location_ref') or l['name'] == normalized_scene_data.get('location')]
            if (detail.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities':
                scene_locs = [{**loc_map[r['location_id']], 'reference_scope': r.get('scope'),
                               'anchor_id': r.get('anchor_id')}
                              for r in snapshot.get('references', [])
                              if r.get('location_id') in loc_map]
            scene_props = [p for p in detail.get('props', []) if prop_is_story(p) and any(sp.get('name') == p['name'] and sp.get('visible', True) and sp.get('state') != 'absent' for sp in normalized_scene_data.get('props', []))]

            normalized_scene_data = {**normalized_scene_data, 'camera_sequence_index': max(0, s.get('position',1)-1)}
            flow_prompt = compile_flow_prompt(detail, normalized_scene_data, characters=scene_chars, locations=scene_locs, props=scene_props)

            from .forgotten_cities.retries import retry_prompt
            flow_prompt = retry_prompt(flow_prompt, snapshot.get('source_fidelity_retry'))
            wire = snapshot.get('flow_wire')
            if wire:
                flow_prompt = wire['prompt']
            elif snapshot.get('source_contract'):
                from .source_contract import render_contract
                flow_prompt+='\nFINAL RENDER CONTRACT (scene constraints; never image lettering):\n'+dump(render_contract(snapshot['source_contract']))
            from .flow_prompt_budget import pack, units, BASE_BUDGET
            flow_prompt=pack(flow_prompt)
            scene_refs = []
            for r in snapshot.get('references', []):
                asset_id = r.get('asset_id')
                if not asset_id:
                    continue
                is_loc = 'location_id' in r
                is_prop = 'prop_id' in r
                if is_loc:
                    item = loc_map.get(r.get('location_id'))
                    r_type = 'location'
                    r_role = r.get('role', 'environment')
                    r_id = r.get('location_id')
                elif is_prop:
                    item = prop_map.get(r.get('prop_id'))
                    if not item or not prop_is_story(item):
                        continue
                    r_type = 'prop'
                    r_role = r.get('role', 'prop')
                    r_id = r.get('prop_id')
                elif r.get('participant_id'):
                    item = {'name': r['name']}
                    r_type = 'character'
                    r_role = r.get('role', 'portrait')
                    r_id = r['participant_id']
                else:
                    item = char_map.get(r.get('character_id'))
                    r_type = 'character'
                    r_role = r.get('role', 'portrait')
                    r_id = r.get('character_id')
                name = item['name'] if item else ''
                scene_refs.append({
                    'asset_id': asset_id,
                    'url': f'/api/assets/{asset_id}',
                    'role': r_role,
                    'type': r_type,
                    'name': name,
                    'id': r_id,
                    'scope': r.get('scope', 'exact_location' if is_loc else 'identity'),
                })

            semantic_valid = contract.get('semantic_valid', True)
            render_ready = contract.get('render_ready', True)
            ready_for_flow = bool(semantic_valid and render_ready)
            flow_error = contract['errors'][0]['message'] if contract['errors'] else None
            if units(flow_prompt)>BASE_BUDGET:
                ready_for_flow=False
                flow_error='Условия кадра не помещаются в предел Flow после упаковки без обрезки. Разделите кадр; факты и ограничения сохранены.'
            if wire and wire.get('error'):
                ready_for_flow = False
                flow_error = wire['error']
            if (self.settings().get('source_contract_enabled') is True
                    and (detail.get('channel_profile') or {}).get('profile_type')=='forgotten_cities'
                    and not detail['preflight'].get('ok')):
                ready_for_flow=False
                flow_error='; '.join(e['message'] for e in detail['preflight'].get('errors',[])[:3])
            if self.settings().get('source_contract_enabled') is True and ((visual_reviews_enabled() and self.settings().get('google_queue_enabled') is not True) or snapshot.get('source_contract',{}).get('status')!='bound'):
                ready_for_flow=False
                flow_error=flow_error or '; '.join(snapshot.get('source_contract',{}).get('conflicts',[])) or 'Итоговое задание или бесплатная очередь требуют внимания.'
            if self.db.one("SELECT id FROM generations WHERE scene_id=? AND scene_revision=? AND state='queued' AND cancel_requested=0 AND json_extract(snapshot,'$.plan_review_wait') IS NOT NULL LIMIT 1",(s['id'],s['current_revision'])):
                ready_for_flow=False
                flow_error='Пакет ожидает проверки смысла сцен. Изображения пока не запускаются.'

            active_image_asset_id = None
            if s.get('active_generation_id'):
                active_generation = self.db.one(
                    'SELECT asset_id FROM generations WHERE id=?', (s['active_generation_id'],)
                )
                active_image_asset_id = active_generation.get('asset_id') if active_generation else None
            # A user may regenerate an already approved Flow scene.  Keep the
            # current image visible in FrameForge while exposing the newer
            # queued proxy to the bridge as work still to be generated.
            pending_flow_generation = self.db.one('''SELECT g.id FROM generations g
                LEFT JOIN batches b ON b.id=g.batch_id
                WHERE g.scene_id=? AND g.scene_revision=? AND g.state='queued'
                  AND g.cancel_requested=0 AND (g.batch_id IS NULL OR b.state='running')
                ORDER BY g.created_at DESC, g.rowid DESC LIMIT 1''',
                                                  (s['id'], s['current_revision']))

            scenes.append({
                'scene_id': s['id'],
                'revision':s['current_revision'],
                'google_queue_enabled':self.settings().get('google_queue_enabled') is True,
                'source_contract':snapshot.get('source_contract'),
                'position': s['position'],
                'beat': normalized_scene_data.get('beat', ''),
                'prompt': flow_prompt,
                'flow_prompt': flow_prompt,
                'flow_prompt_version': wire['version'] if wire else None,
                'flow_prompt_hash': wire.get('prompt_hash') if wire else None,
                'reference_omissions': snapshot.get('reference_omissions', []),
                'comfy_prompt': snapshot['prompt'],
                'source_fidelity_retry': snapshot.get('source_fidelity_retry', {}),
                'has_image': bool(active_image_asset_id and not s.get('stale')
                                  and not pending_flow_generation),
                'active_generation_id': s.get('active_generation_id'),
                # active_generation_id identifies a generation, while /api/assets
                # needs the resulting asset id.  The Flow bridge imports this exact
                # image as Veo's first frame.
                'image_asset_id': active_image_asset_id,
                'image_url': f"/api/assets/{active_image_asset_id}" if active_image_asset_id else None,
                'has_video': bool(s.get('video_asset_id')),
                'video_asset_id': s.get('video_asset_id'),
                'video_url': f"/api/assets/{s.get('video_asset_id')}" if s.get('video_asset_id') else None,
                'references': scene_refs,
                'ready_for_flow': ready_for_flow,
                'semantic_valid': semantic_valid,
                'render_ready': render_ready,
                'flow_error': flow_error,
                'animation': normalized_scene_data.get('animation', {}),
                'veo_prompt': compile_veo_prompt(normalized_scene_data, normalized_scene_data.get('animation', {}).get('camera_motion')),
            })
        characters = []
        from .character_selection import project_selection
        selected_ids = set(detail.get('character_reference_ids', []) if 'character_reference_ids' in detail else project_selection(detail)['character_reference_ids'])
        manual_ids = {g.get('character_id') for g in detail.get('generations', [])
                      if g.get('purpose') == 'casting' and g.get('state') == 'queued' and not g.get('cancel_requested')}
        from .mode_policy import allows
        if not allows(detail, 'character'): manual_ids = set()
        manual_characters = []
        for c in (c for c in detail['characters'] if c['id'] in selected_ids or c['id'] in manual_ids):
            variants_count = len([
                g for g in detail.get('generations', [])
                if g.get('character_id') == c['id'] and g.get('state') == 'succeeded' and g.get('asset_id') and not g.get('snapshot', {}).get('wardrobe_key')
            ])
            target_list = characters if c['id'] in selected_ids else manual_characters
            target_list.append({
                'id': c['id'],
                'name': c['name'],
                'prompt': self.casting_snapshot(detail, c)['prompt'],
                'locked': bool(c.get('locked')) and not any(g.get('character_id') == c['id'] and g.get('purpose') == 'casting' and g.get('state') == 'queued' and not g.get('cancel_requested') for g in detail.get('generations',[])),
                'casting_generation_id': next((g['id'] for g in reversed(detail.get('generations',[])) if g.get('character_id') == c['id'] and g.get('purpose') == 'casting' and g.get('state') == 'queued' and not g.get('cancel_requested')),None),
                'portrait': c.get('references', {}).get('portrait'),
                'portrait_url': f"/api/assets/{c.get('references', {}).get('portrait')}" if c.get('references', {}).get('portrait') else None,
                'variants_count': variants_count,
            })
        locations = []
        for l in detail.get('locations', []):
            from .mode_policy import allows
            if not allows(detail, 'location'): continue
            requested = any(g.get('location_id') == l['id'] and g.get('state') == 'queued' and not g.get('cancel_requested') for g in detail.get('generations', []))
            if not requested and (not l.get('locked') or not l.get('references', {}).get('environment')):
                continue
            variants_count = len([
                g for g in detail.get('generations', [])
                if g.get('location_id') == l['id'] and g.get('state') == 'succeeded' and g.get('asset_id')
            ])
            locations.append({
                'id': l['id'],
                'name': l['name'],
                'prompt': self.location_snapshot(detail, l)['prompt'],
                'locked': bool(l.get('locked')),
                'environment': l.get('references', {}).get('environment'),
                'environment_url': f"/api/assets/{l.get('references', {}).get('environment')}" if l.get('references', {}).get('environment') else None,
                'variants_count': variants_count,
            })
        props = []
        for p in detail.get('props', []):
            from .mode_policy import allows
            if not allows(detail, 'prop'): continue
            if not prop_is_story(p):
                continue
            variants_count = len([
                g for g in detail.get('generations', [])
                if g.get('prop_id') == p['id'] and g.get('state') == 'succeeded' and g.get('asset_id')
            ])
            props.append({
                'id': p['id'],
                'name': p['name'],
                'description': p['description'],
                'importance': 'story',
                'first_appearance': p.get('first_appearance', 1),
                'owner': p.get('owner', ''),
                'default_state': p.get('default_state', 'absent'),
                'prompt': self.prop_snapshot(detail, p)['prompt'],
                'locked': bool(p.get('locked')),
                'prop': p.get('references', {}).get('prop'),
                'prop_url': f"/api/assets/{p.get('references', {}).get('prop')}" if p.get('references', {}).get('prop') else None,
                'variants_count': variants_count,
            })
        from .mode_policy import allows
        if not allows(detail, 'character'): characters = []
        locked_characters = [c for c in characters if c['locked'] and c.get('portrait')]
        locked_locations = [l for l in locations if l['locked'] and l.get('environment')]
        locked_props = [p for p in props if p['locked'] and p.get('prop')]

        known_hashes = set()
        for s in detail.get('scenes', []):
            active_gen = s.get('active_generation_id')
            if active_gen:
                a = self.db.one('SELECT sha256 FROM assets WHERE generation_id=?', (active_gen,))
                if a and a.get('sha256'):
                    known_hashes.add(a['sha256'])
        for c in detail.get('characters', []):
            refs = c.get('references', {})
            if isinstance(refs, dict) and refs.get('portrait'):
                a = self.db.one('SELECT sha256 FROM assets WHERE id=?', (refs['portrait'],))
                if a and a.get('sha256'):
                    known_hashes.add(a['sha256'])
        for l in detail.get('locations', []):
            refs = l.get('references', {})
            if isinstance(refs, dict) and refs.get('environment'):
                a = self.db.one('SELECT sha256 FROM assets WHERE id=?', (refs['environment'],))
                if a and a.get('sha256'):
                    known_hashes.add(a['sha256'])
        for p in detail.get('props', []):
            refs = p.get('references', {})
            if isinstance(refs, dict) and refs.get('prop'):
                a = self.db.one('SELECT sha256 FROM assets WHERE id=?', (refs['prop'],))
                if a and a.get('sha256'):
                    known_hashes.add(a['sha256'])

        forgotten = (detail.get('channel_profile') or {}).get('profile_type') == 'forgotten_cities'
        reference_jobs = []
        if forgotten and detail.get('mode') not in ('dynamic','illustration'):
            for kind, targets in (('location', detail['locations']), ('prop', detail['props'])):
                for target in targets:
                    session = target.get('casting')
                    if target['locked'] or not session:
                        continue
                    for job in detail['generations']:
                        if job['id'] in session['generation_ids'] and job['state'] == 'queued' and not job['cancel_requested']:
                            reference_jobs.append({'id': job['id'], 'target_id': target['id'],
                                'name': target['name'], 'purpose': kind, 'generation_role': 'reference',
                                'prompt': job['snapshot']['prompt'], 'locked': False,
                                'aspect_ratio': '1:1' if kind == 'prop' else '16:9'})
            # The bridge consumes explicit queued jobs, never all story resources.
            if not include_all_scenes:
                scenes = [s for s in scenes if self.db.one("SELECT id FROM generations WHERE scene_id=? AND state='queued' AND cancel_requested=0", (s['scene_id'],))]
        return {
            'project_id': project_id,
            'profile_type': 'forgotten_cities' if forgotten else self._project_profile_type(project_id),
            'reference_jobs': reference_jobs,
            'title': detail['title'],
            'scenes': scenes,
            'characters': [] if forgotten else characters,
            'manual_characters': [] if forgotten else manual_characters,
            'locations': [] if forgotten else locations,
            'props': [] if forgotten else props,
            'locked_characters': locked_characters,
            'locked_locations': locked_locations,
            'locked_props': locked_props,
            'known_hashes': list(known_hashes),
            'animation_settings': detail.get('animation_settings', DEFAULT_ANIMATION_SETTINGS),
        }


    def _normalize_reference_image(self, image_bytes):
        # Only this known resize/padding path inherits inspected provenance.
        # Uploaded or generated images without a checked original remain unverified.
        from .checked_visual_evidence import CheckedVisualEvidence
        store = CheckedVisualEvidence(self.root)
        parent = store.match(image_bytes)
        processed = self._process_image_bytes(image_bytes, (768, 432), preserve_aspect=True)
        if parent and not store.match(processed):
            store.record(processed, identity=parent['identity'], aliases=parent['aliases'], scope=parent['scope'],
                source_url=parent['source_url'], image_url=parent['image_url'], title=parent['title'],
                note=parent['inspection_note'] + ' Нормализация эталона без обрезки и растяжения; исходный SHA256: ' + parent['image_sha256'],
                features=parent['supported_features'], spatial_features=parent.get('spatial_features'), parent_binding=store.binding(parent))
        return processed

    def _process_image_bytes(self, image_bytes, target_size=(1024, 576), *, preserve_aspect=False):
        from PIL import Image
        if not image_bytes:
            raise DomainError('Файл изображения пустой.')
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                img = img.convert('RGB')
                if img.size != target_size:
                    if preserve_aspect:
                        # Reference geometry must survive portrait museum photos.
                        # Keep the whole object, adding margins rather than stretching it.
                        from PIL import ImageOps
                        img = ImageOps.pad(img, target_size, method=Image.Resampling.LANCZOS,
                                           color=img.getpixel((0, 0)))
                    else:
                        img = img.resize(target_size, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='PNG', optimize=True)
                return buf.getvalue()
        except Exception as error:
            raise DomainError(f'Не удалось прочитать изображение: {error}')

    def refresh_batches(self):
        with self.db.transaction() as connection:
            batches = connection.execute("SELECT id FROM batches WHERE state IN ('running','paused')").fetchall()
            for batch in batches:
                states = [row[0] for row in connection.execute('SELECT state FROM generations WHERE batch_id=?', (batch['id'],))]
                if states and all(state in TERMINAL for state in states):
                    state = 'completed' if all(s == 'succeeded' for s in states) else 'completed_with_errors'
                    connection.execute('UPDATE batches SET state=? WHERE id=?', (state, batch['id']))

    def upload_scene_image(self, scene_id, image_bytes, image_validator=None, fidelity_candidate_id=None):
        scene = self.scene(scene_id)
        project_id = scene['project_id']
        processed_data = self._process_image_bytes(image_bytes, (1024, 576))
        new_sha256 = hashlib.sha256(processed_data).hexdigest()

        # Reject duplicate images across different scenes in this project
        duplicate_scene = self.db.one('''
            SELECT s.position FROM scenes s
            JOIN generations g ON g.id = s.active_generation_id
            JOIN assets a ON a.id = g.asset_id
            WHERE s.project_id = ? AND s.id != ? AND a.sha256 = ?
        ''', (project_id, scene_id, new_sha256))
        if duplicate_scene:
            raise DomainError(
                f'Это изображение дублирует уже существующий кадр {duplicate_scene["position"]}. Дождитесь новой генерации.',
                409
            )

        # Reject duplicate reference images (locations, character portraits, or props)
        reference_assets = self.db.all('''
            SELECT g.purpose, a.path, a.sha256,
                   CASE
                       WHEN g.purpose='location' THEN coalesce(l.name, 'локации')
                       WHEN g.purpose='prop' THEN coalesce(p.name, 'реквизита')
                       ELSE coalesce(c.name, 'персонажа')
                   END as name
            FROM generations g
            JOIN assets a ON a.id = g.asset_id
            LEFT JOIN locations l ON l.id = g.location_id
            LEFT JOIN characters c ON c.id = g.character_id
            LEFT JOIN props p ON p.id = g.prop_id
            WHERE g.project_id = ? AND g.purpose IN ('location', 'casting', 'prop')
        ''', (project_id,))

        try:
            from PIL import Image
            import numpy as np
            new_img_pil = Image.open(io.BytesIO(processed_data))
            new_arr = np.array(new_img_pil).astype(float)
        except Exception:
            new_arr = None

        for ref in reference_assets:
            if ref['sha256'] == new_sha256:
                ref_type_label = 'локации' if ref['purpose'] == 'location' else 'персонажа'
                raise DomainError(
                    f'Это изображение является референсом {ref_type_label} "{ref["name"]}". '
                    f'Google Flow ещё генерирует новый кадр, дождитесь завершения.',
                    409
                )
            if new_arr is not None:
                ref_file = self.root / ref['path']
                if ref_file.exists():
                    try:
                        with Image.open(ref_file) as rimg:
                            rimg_resized = rimg.convert('RGB').resize((1024, 576), Image.Resampling.BICUBIC)
                            # Same setting is expected across storyboard frames.
                            # A small global mean difference is not proof of a duplicate.
                            if np.array_equal(new_arr, np.array(rimg_resized).astype(float)):
                                ref_type_label = 'локации' if ref['purpose'] == 'location' else 'персонажа'
                                raise DomainError(
                                    f'Это изображение дублирует референс {ref_type_label} "{ref["name"]}". '
                                    f'Google Flow ещё генерирует новый кадр, дождитесь завершения.',
                                    409
                                )
                    except DomainError:
                        raise
                    except Exception:
                        pass

        snapshot = self.scene_snapshot(scene, allow_unlocked=True)
        retry_parent=self.db.one("SELECT snapshot FROM generations WHERE scene_id=? AND scene_revision=? AND state='queued' AND cancel_requested=0 AND json_extract(snapshot,'$.auto_source_retry_of') IS NOT NULL ORDER BY rowid LIMIT 1",(scene_id,scene['current_revision']))
        if retry_parent:
            original=json.loads(retry_parent['snapshot'])
            for key in ('auto_source_retry_of','auto_source_retry_parent'):snapshot[key]=original[key]
        if fidelity_candidate_id:
            snapshot['fidelity_candidate_id'] = str(fidelity_candidate_id)[:200]
        if image_validator:
            try:
                image_validator(snapshot, processed_data)
            except DomainError as error:
                # Keep bounded, deduplicated evidence. A rejected candidate is
                # never attached to the scene or counted as a completed job.
                if error.__class__.__name__=='ContentRejectedError':
                    try:
                        directory=self.root/'composition-rejected'/hashlib.sha256(scene_id.encode()).hexdigest()
                        directory.mkdir(parents=True,exist_ok=True)
                        path=directory/(new_sha256+'.png')
                        if path.exists() or len(list(directory.glob('*.png')))<6:
                            path.write_bytes(processed_data)
                            path.with_suffix('.json').write_text(json.dumps({
                                'scene_id':scene_id,'revision':scene['current_revision'],
                                'image_sha256':new_sha256,'reason':str(error),
                                'details':getattr(error,'details',{}),'prompt':snapshot.get('prompt'),
                                'created_at':now()},ensure_ascii=False),encoding='utf-8')
                    except OSError:
                        pass  # Evidence failure cannot accept a bad image.
                raise
        self.validate_scene_source_image(snapshot, processed_data)

        # Check if identical image was already uploaded for this scene & revision (bridge retry)
        existing_scene_gen = self.db.one('''
            SELECT g.id, g.asset_id, g.state FROM generations g
            JOIN assets a ON a.id = g.asset_id
            WHERE g.scene_id = ? AND g.scene_revision = ? AND a.sha256 = ?
            ORDER BY g.created_at DESC LIMIT 1
        ''', (scene_id, scene['current_revision'], new_sha256))
        if existing_scene_gen:
            pending_rows = self.db.all('''SELECT g.id FROM generations g
                LEFT JOIN batches b ON b.id=g.batch_id
                WHERE g.scene_id=? AND g.scene_revision=? AND g.state='queued'
                  AND g.cancel_requested=0
                  AND (g.batch_id IS NULL OR b.state IN ('running','paused'))
                ORDER BY CASE WHEN b.state='running' THEN 0 ELSE 1 END,
                         g.created_at ASC, g.rowid ASC''',
                                       (scene_id, scene['current_revision']))
            if pending_rows:
                with self.db.transaction() as connection:
                    progress = dump({'stage': 'Загружено из Google Flow', 'percent': 100})
                    for prow in pending_rows:
                        connection.execute('''UPDATE generations
                            SET state='succeeded', asset_id=?, snapshot=?, started_at=coalesce(started_at,?),
                                finished_at=?, error=NULL, progress=?
                            WHERE id=? AND state='queued' AND cancel_requested=0''',
                            (existing_scene_gen['asset_id'], dump(snapshot), now(), now(), progress, prow['id']))
                        self.db.event(connection, project_id, 'generation_completed', {'id': prow['id'], 'state': 'succeeded'})
                self.refresh_batches()
                return {'scene_id': scene_id, 'generation_id': pending_rows[0]['id'], 'asset_id': existing_scene_gen['asset_id']}
            return {'scene_id': scene_id, 'generation_id': existing_scene_gen['id'], 'asset_id': existing_scene_gen['asset_id']}

        # A Flow batch creates a queued proxy generation first. Completing
        # that exact row keeps the batch lifecycle and candidate variants intact.
        pending_rows = self.db.all('''SELECT g.id FROM generations g
            LEFT JOIN batches b ON b.id=g.batch_id
            WHERE g.scene_id=? AND g.scene_revision=? AND g.state='queued'
              AND g.cancel_requested=0
              AND (g.batch_id IS NULL OR b.state IN ('running','paused'))
            ORDER BY CASE WHEN b.state='running' THEN 0 ELSE 1 END,
                     g.created_at ASC, g.rowid ASC''',
                                   (scene_id, scene['current_revision']))
        pending = pending_rows[0] if pending_rows else None
        generation_id = pending['id'] if pending else uid()
        asset_id = uid()
        relative = f"projects/{project_id}/{generation_id}.png"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(processed_data)

        metadata = {
            'id': generation_id,
            'project_id': project_id,
            'purpose': 'scene',
            'snapshot': snapshot,
            'sha256': new_sha256,
            'output_size': (1024, 576),
            'saved_at': now(),
            'state': 'succeeded',
            'source': 'upload_google_flow',
            'error': None
        }
        metadata_dir = self.root / 'metadata'
        metadata_dir.mkdir(exist_ok=True)
        (metadata_dir / f"{generation_id}.json").write_text(dump(metadata), encoding='utf-8')

        with self.db.transaction() as connection:
            progress = dump({'stage': 'Загружено из Google Flow', 'percent': 100})
            if pending_rows:
                for prow in pending_rows:
                    connection.execute('''UPDATE generations
                        SET state='succeeded', asset_id=?, snapshot=?, started_at=coalesce(started_at,?),
                            finished_at=?, error=NULL, progress=?
                        WHERE id=? AND state='queued' AND cancel_requested=0''',
                        (asset_id, dump(snapshot), now(), now(), progress, prow['id']))
                    self.db.event(connection, project_id, 'generation_completed', {'id': prow['id'], 'state': 'succeeded'})
            else:
                connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                    scene_id,scene_revision,asset_id,created_at,started_at,finished_at,progress)
                    VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                    (generation_id, project_id, 'scene', dump(snapshot), 0,
                     scene_id, scene['current_revision'], asset_id, now(), now(), now(), progress))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 new_sha256, 1024, 576, len(processed_data)))

            # Auto-select the first successful generation of this revision;
            # subsequent regenerations remain stored candidate variants until chosen.
            is_first = True
            if scene.get('active_generation_id'):
                active_gen = connection.execute(
                    "SELECT id FROM generations WHERE id=? AND scene_id=? AND scene_revision=? AND state='succeeded' AND asset_id IS NOT NULL",
                    (scene['active_generation_id'], scene_id, scene['current_revision'])
                ).fetchone()
                if active_gen:
                    is_first = False
            if is_first:
                connection.execute("""
                    UPDATE veo_jobs SET status='cancelled', completed_at=?
                    WHERE scene_id=? AND status IN ('queued_omni', 'generating_omni', 'queued_veo', 'generating_veo')
                """, (now(), scene_id))
                connection.execute('UPDATE scenes SET active_generation_id=? WHERE id=?', (generation_id, scene_id))

            if not pending_rows:
                self.db.event(connection, project_id, 'generation_completed', {'id': generation_id, 'state': 'succeeded'})

        self.refresh_batches()
        from .source_corrections import select_checked
        select_checked(self,generation_id)
        return {'scene_id': scene_id, 'generation_id': generation_id, 'asset_id': asset_id}

    def upload_scene_video(self, scene_id, video_bytes, filename='video.webm',
                           flow_media_id=None, idempotency_key=None, source='auto',
                           expected_scene_revision=None, source_image_asset_id=None,
                           source_image_sha256=None, local_render_info=None):
        if source == 'flow_manual':
            return self.import_scene_video(
                scene_id, video_bytes, filename=filename,
                expected_scene_revision=expected_scene_revision,
                source_image_asset_id=source_image_asset_id,
                source_image_sha256=source_image_sha256,
                idempotency_key=idempotency_key, source='flow_manual'
            )
        scene = self.scene(scene_id)
        self._assert_scene_still_approved(scene)
        project_id = scene['project_id']
        cur_data = dict(scene['data'] or {})
        anim = dict(cur_data.get('animation') or {})
        cur_rev = scene.get('current_revision', 1)
        if local_render_info is not None and (anim.get('selected_engine') != 'ken_burns' or expected_scene_revision != cur_rev):
            raise DomainError('Кадр изменился во время создания 2.5D. Обновите проект и повторите.', 409)

        active_gen_id = scene.get('active_generation_id')
        active_image_asset_id = scene.get('asset_id')
        active_source_rev = scene.get('current_revision', 1)
        if active_gen_id:
            active_gen = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,))
            if active_gen and active_gen.get('asset_id'):
                active_image_asset_id = active_gen['asset_id']
                active_source_rev = active_gen.get('scene_revision') or active_source_rev

        active_sha256 = None
        if active_image_asset_id:
            asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (active_image_asset_id,))
            if asset_rec and asset_rec.get('sha256'):
                active_sha256 = asset_rec['sha256']

        engine_label = 'Omni' if (anim.get('selected_engine') == 'omni_flash' or (idempotency_key and 'omni' in str(idempotency_key))) else ('Veo' if (anim.get('selected_engine') == 'veo_lite' or (idempotency_key and 'veo' in str(idempotency_key))) else 'анимации')

        # 0. Check cancelled jobs
        if idempotency_key:
            job_rec = self.db.one("SELECT * FROM veo_jobs WHERE scene_id=? AND idempotency_key=?", (scene_id, idempotency_key))
            if job_rec and job_rec.get('status') == 'cancelled':
                raise DomainError(f'Источник кадра изменён после постановки {engine_label} в очередь. Запустите анимацию заново.', 409)

        # 1. Source image asset validation
        valid_source_ids = {v for v in (active_gen_id, active_image_asset_id, scene.get('asset_id')) if v}
        if source_image_asset_id is not None and valid_source_ids and source_image_asset_id not in valid_source_ids:
            raise DomainError(f'Источник кадра изменён после постановки {engine_label} в очередь. Запустите анимацию заново.', 409)

        # 2. Source image sha256 validation
        if source_image_sha256 is not None and active_sha256:
            if source_image_sha256 != active_sha256:
                raise DomainError(f'Источник кадра изменён после постановки {engine_label} в очередь. Запустите анимацию заново.', 409)

        # 3. Revision validation: only reject if source was not verified and expected revision does not match
        if expected_scene_revision is not None:
            exp_rev = int(expected_scene_revision)
            source_verified = (
                (source_image_sha256 and active_sha256 and source_image_sha256 == active_sha256) or
                (source_image_asset_id and source_image_asset_id in valid_source_ids) or
                (exp_rev in (cur_rev, active_source_rev))
            )
            if not source_verified:
                raise DomainError(f'Источник кадра изменён после постановки {engine_label} в очередь. Запустите анимацию заново.', 409)

        if idempotency_key:
            # 1. Check current scene data & revisions for matching idempotency_key, completed status, and existing video_asset_id
            existing_key = anim.get('idempotency_key')
            existing_status = anim.get('status')
            existing_video_asset_id = scene.get('video_asset_id') or anim.get('video_asset_id')

            # Also check previously completed veo_job
            existing_job = self.db.one(
                "SELECT * FROM veo_jobs WHERE scene_id=? AND idempotency_key=?",
                (scene_id, idempotency_key)
            )

            # Check previously created scene_video generations for this scene
            prev_generations = self.db.all(
                "SELECT id, asset_id, snapshot, state FROM generations WHERE scene_id=? AND purpose='scene_video' ORDER BY created_at DESC",
                (scene_id,)
            )
            matching_gen = None
            for g in prev_generations:
                try:
                    s_data = json.loads(g['snapshot']) if isinstance(g['snapshot'], str) else (g['snapshot'] or {})
                except Exception:
                    s_data = {}
                if s_data.get('idempotency_key') == idempotency_key and g.get('state') == 'succeeded' and g.get('asset_id'):
                    matching_gen = g
                    break

            # 2. If the scene already has the same idempotency_key, status == "completed", and existing video_asset_id:
            # Return previous result without creating new asset, generation or event!
            matched_asset_id = None
            if existing_key == idempotency_key and existing_status == 'completed' and existing_video_asset_id:
                matched_asset_id = existing_video_asset_id
            elif existing_job and existing_job.get('status') == 'completed' and existing_job.get('asset_id'):
                matched_asset_id = existing_job['asset_id']
            elif matching_gen and matching_gen.get('asset_id'):
                matched_asset_id = matching_gen['asset_id']

            if matched_asset_id:
                anim['status'] = 'completed'
                anim['video_asset_id'] = matched_asset_id
                if anim.get('selected_engine') == 'veo_lite':
                    anim['credit_cost'] = 10
                anim['idempotency_key'] = idempotency_key
                if flow_media_id:
                    anim['flow_media_id'] = flow_media_id
                return {
                    'scene_id': scene_id,
                    'video_asset_id': matched_asset_id,
                    'url': f'/api/assets/{matched_asset_id}',
                    'animation': anim
                }

        if not video_bytes:
            raise DomainError('Файл видео пустой.')

        asset_id = uid()
        generation_id = uid()
        ext = '.mp4' if filename.lower().endswith('.mp4') else '.webm'
        relative = f"projects/{project_id}/videos/{scene_id}_{asset_id[:8]}{ext}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(video_bytes)

        snapshot = self.scene_snapshot(scene, allow_unlocked=True)
        video_metadata = scene_video_metadata(io.BytesIO(video_bytes))
        if video_metadata:
            anim['actual_duration_seconds'] = video_metadata['duration_seconds']
            snapshot['video_metadata'] = video_metadata
        if local_render_info is not None:
            anim.update(source='ken_burns', ken_burns_preset=local_render_info['safe_crop']['actual_preset'],
                        safe_crop=local_render_info['safe_crop'], ken_burns_renderer_version=local_render_info['renderer_version'])
            if local_render_info.get('reason'):
                anim['ken_burns_reason'] = local_render_info['reason']
            snapshot['local_render_info'] = local_render_info
        src_img_id = source_image_asset_id or active_image_asset_id
        if src_img_id:
            snapshot['source_image_asset_id'] = src_img_id
            anim['source_image_asset_id'] = src_img_id
        if idempotency_key:
            snapshot['idempotency_key'] = idempotency_key

        engine = anim.get('selected_engine')
        anim['status'] = 'completed'
        anim['video_asset_id'] = asset_id
        anim['error'] = None
        if engine == 'veo_lite':
            anim['credit_cost'] = 10
        if flow_media_id:
            anim['flow_media_id'] = flow_media_id
        if idempotency_key:
            anim['idempotency_key'] = idempotency_key
        cur_data['animation'] = anim

        with self.db.transaction() as connection:
            if local_render_info is not None:
                live = connection.execute('SELECT current_revision,active_generation_id FROM scenes WHERE id=?', (scene_id,)).fetchone()
                if live['current_revision'] != cur_rev or live['active_generation_id'] != active_gen_id:
                    raise DomainError('Кадр изменился во время сохранения 2.5D. Обновите проект и повторите.', 409)
            if idempotency_key:
                concur_job = connection.execute(
                    "SELECT asset_id FROM veo_jobs WHERE scene_id=? AND idempotency_key=? AND status='completed'",
                    (scene_id, idempotency_key)
                ).fetchone()
                if concur_job and concur_job['asset_id']:
                    return {
                        'scene_id': scene_id,
                        'video_asset_id': concur_job['asset_id'],
                        'url': f'/api/assets/{concur_job["asset_id"]}',
                        'animation': anim
                    }

            cur_rev = scene['current_revision']
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(cur_data), now()))
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                scene_id,scene_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                (generation_id, project_id, 'scene_video', dump(snapshot), 0,
                 scene_id, cur_rev + 1, asset_id, now(), now(), now(),
                 dump({'stage': 'Видео кадра создано', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 hashlib.sha256(video_bytes).hexdigest(), video_metadata.get('width') or 1280, video_metadata.get('height') or 720, len(video_bytes)))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=? WHERE id=?',
                               (cur_rev + 1, asset_id, scene_id))

            if idempotency_key:
                connection.execute("""
                    INSERT INTO veo_jobs(id, project_id, scene_id, idempotency_key, model, status, generation_id, asset_id, flow_media_id, created_at, completed_at)
                    VALUES(?,?,?,?,?,'completed',?,?,?,?,?)
                    ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET
                        status='completed',
                        generation_id=excluded.generation_id,
                        asset_id=excluded.asset_id,
                        flow_media_id=excluded.flow_media_id,
                        completed_at=excluded.completed_at
                """, (uid(), project_id, scene_id, idempotency_key,
                    'gemini-omni-1.1-flash' if engine == 'omni_flash' else 'veo-3.1-lite',
                    generation_id, asset_id, flow_media_id, now(), now()))

            self.db.event(connection, project_id, 'scene_video_updated', {
                'scene_id': scene_id, 'asset_id': asset_id,
                'flow_media_id': flow_media_id, 'idempotency_key': idempotency_key,
                'credit_cost': anim.get('credit_cost', 0)
            })

        return {'scene_id': scene_id, 'video_asset_id': asset_id, 'url': f'/api/assets/{asset_id}', 'animation': anim}

    def delete_scene_video(self, scene_id):
        """Detach animation from a scene while preserving its static image.

        Video assets and generations are retained for auditability, but no longer
        participate in playback, assembly, Flow polling, or the current scene
        revision.  Calling this twice is deliberately idempotent.
        """
        scene = self.scene(scene_id)
        project_id = scene['project_id']
        cur_rev = scene.get('current_revision', 1)
        cur_data = dict(scene['data'])
        anim = dict(cur_data.get('animation') or {})

        anim.pop('video_asset_id', None)
        anim.pop('actual_duration_seconds', None)
        anim.pop('source', None)
        anim.pop('imported_at', None)
        anim.pop('file_sha256', None)
        if scene.get('video_asset_id') or anim.get('status') in ('queued_omni','queued_veo','generating_omni','generating_veo','completed'):
            anim['request_nonce'] = uid()
        anim['selected_engine'] = None
        anim['status'] = 'not_planned'
        anim.pop('idempotency_key', None)
        anim['error'] = None
        cur_data['animation'] = anim

        with self.db.transaction() as connection:
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(cur_data), now()))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?',
                               (cur_rev + 1, scene_id))
            connection.execute("""
                UPDATE veo_jobs SET status='cancelled', completed_at=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (now(), scene_id))
            self.db.event(connection, project_id, 'scene_video_deleted', {'scene_id': scene_id})
        refreshed = self.scene(scene_id)
        return {
            'scene_id': scene_id,
            'video_asset_id': None,
            'static_image_asset_id': refreshed.get('asset_id'),
            'animation': refreshed['data'].get('animation', {}),
            'deleted': bool(scene.get('video_asset_id') or cur_data.get('animation', {}).get('video_asset_id'))
        }

    def import_scene_video(self, scene_id, video_bytes, filename='video.mp4',
                           expected_scene_revision=None, source_image_asset_id=None,
                           source_image_sha256=None,
                           idempotency_key=None, source='flow_manual'):
        scene = self.scene(scene_id)
        self._assert_scene_still_approved(scene)
        project_id = scene['project_id']
        cur_rev = scene.get('current_revision', 1)

        active_generation_id = scene.get('active_generation_id')
        active_image_asset_id = scene.get('asset_id')
        active_source_rev = cur_rev
        if active_generation_id:
            active_generation = self.db.one(
                'SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_generation_id,)
            )
            if active_generation and active_generation.get('asset_id'):
                active_image_asset_id = active_generation['asset_id']
                active_source_rev = active_generation.get('scene_revision') or active_source_rev
        valid_source_ids = {
            value for value in (active_generation_id, active_image_asset_id, scene.get('asset_id')) if value
        }
        active_sha256 = None
        if active_image_asset_id:
            asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (active_image_asset_id,))
            if asset_rec and asset_rec.get('sha256'):
                active_sha256 = asset_rec['sha256']

        if source_image_asset_id is not None:
            if not valid_source_ids or source_image_asset_id not in valid_source_ids:
                raise DomainError('Исходное изображение кадра изменилось или отсутствует. Видео должно соответствовать актуальному первому кадру.', 409)

        if source_image_sha256 is not None and active_sha256:
            if asset_rec and asset_rec.get('sha256') and asset_rec['sha256'] != source_image_sha256:
                raise DomainError('Хеш исходного изображения кадра не совпадает. Видео должно соответствовать актуальному первому кадру.', 409)

        if expected_scene_revision is not None:
            exp_rev = int(expected_scene_revision)
            source_verified = (
                (source_image_sha256 and active_sha256 and source_image_sha256 == active_sha256) or
                (source_image_asset_id and source_image_asset_id in valid_source_ids) or
                (exp_rev in (cur_rev, active_source_rev))
            )
            if not source_verified:
                raise DomainError(f'Ревизия кадра изменилась (ожидалась {expected_scene_revision}, текущая {cur_rev}). Проверьте актуальность импортируемого клипа.', 409)

        cur_data = dict(scene['data'] or {})
        anim = dict(cur_data.get('animation') or {})

        # 3. Server-side idempotency check
        if idempotency_key:
            existing_key = anim.get('idempotency_key')
            existing_status = anim.get('status')
            existing_video_asset_id = scene.get('video_asset_id') or anim.get('video_asset_id')
            if existing_key == idempotency_key and existing_status == 'completed' and existing_video_asset_id:
                return {
                    'scene_id': scene_id,
                    'video_asset_id': existing_video_asset_id,
                    'url': f'/api/assets/{existing_video_asset_id}',
                    'animation': anim
                }

        # 4. Deep video validation (container, streams, aspect ratio, duration, frames)
        video_info = validate_scene_video_bytes(video_bytes, filename=filename)
        file_sha256 = video_info['sha256']

        existing_asset = self.db.one(
            "SELECT a.id FROM assets a JOIN generations g ON a.generation_id=g.id WHERE g.scene_id=? AND g.purpose='scene_video' AND a.sha256=?",
            (scene_id, file_sha256)
        )
        if existing_asset and existing_asset.get('id'):
            matched_id = existing_asset['id']
            anim['status'] = 'completed'
            anim['video_asset_id'] = matched_id
            anim['source'] = source
            anim['imported_at'] = now()
            anim['file_sha256'] = file_sha256
            anim['duration_seconds'] = round(video_info['duration'], 2)
            anim['credit_cost'] = 0
            anim['error'] = None
            if idempotency_key:
                anim['idempotency_key'] = idempotency_key
            cur_data['animation'] = anim

            if scene.get('video_asset_id') != matched_id:
                with self.db.transaction() as connection:
                    connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                                       (scene_id, cur_rev + 1, dump(cur_data), now()))
                    connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=? WHERE id=?',
                                       (cur_rev + 1, matched_id, scene_id))
                    connection.execute("""
                        UPDATE veo_jobs
                        SET status='completed', completed_at=?, asset_id=?
                        WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
                    """, (now(), matched_id, scene_id))
                    self.db.event(connection, project_id, 'scene_video_updated', {
                        'scene_id': scene_id, 'asset_id': matched_id,
                        'source': source, 'idempotency_key': idempotency_key,
                        'credit_cost': 0
                    })
            return {
                'scene_id': scene_id,
                'video_asset_id': matched_id,
                'url': f'/api/assets/{matched_id}',
                'animation': anim
            }

        asset_id = uid()
        generation_id = uid()
        ext = '.mp4' if filename.lower().endswith('.mp4') else '.webm'
        relative = f"projects/{project_id}/videos/{scene_id}_{asset_id[:8]}{ext}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(video_bytes)

        snapshot = self.scene_snapshot(scene, allow_unlocked=True)
        if idempotency_key:
            snapshot['idempotency_key'] = idempotency_key
        snapshot['source'] = source
        snapshot['source_image_asset_id'] = active_image_asset_id or active_generation_id
        snapshot['file_sha256'] = file_sha256

        anim['status'] = 'completed'
        anim['video_asset_id'] = asset_id
        anim['source'] = source
        anim['imported_at'] = now()
        anim['file_sha256'] = file_sha256
        anim['duration_seconds'] = round(video_info['duration'], 2)
        anim['credit_cost'] = 0
        anim['error'] = None
        if idempotency_key:
            anim['idempotency_key'] = idempotency_key
        cur_data['animation'] = anim

        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                scene_id,scene_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                (generation_id, project_id, 'scene_video', dump(snapshot), 0,
                 scene_id, cur_rev + 1, asset_id, now(), now(), now(),
                 dump({'stage': 'Видео кадра импортировано вручную', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 file_sha256, video_info['width'], video_info['height'], len(video_bytes)))

            # 5. ATOMIC CANCELLATION: cancel any pending automated veo_jobs for this scene (after asset is created)
            connection.execute("""
                UPDATE veo_jobs
                SET status='completed', completed_at=?, asset_id=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (now(), asset_id, scene_id))

            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(cur_data), now()))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=? WHERE id=?',
                               (cur_rev + 1, asset_id, scene_id))

            self.db.event(connection, project_id, 'scene_video_updated', {
                'scene_id': scene_id, 'asset_id': asset_id,
                'source': source, 'idempotency_key': idempotency_key,
                'credit_cost': 0
            })

        return {'scene_id': scene_id, 'video_asset_id': asset_id, 'url': f'/api/assets/{asset_id}', 'animation': anim}

    def animation_passport(self, scene_id, *, _detail=None):
        scene = self.scene(scene_id)
        project_id = scene['project_id']
        detail = _detail if _detail is not None else self.detail(project_id)

        char_map = {c['name']: c for c in detail.get('characters', [])}
        for c in detail.get('characters', []):
            if c.get('canonical_name'):
                char_map[c['canonical_name']] = c
            if c.get('cast_id'):
                char_map[c['cast_id']] = c

        loc_map = {l['name']: l for l in detail.get('locations', [])}
        for l in detail.get('locations', []):
            if l.get('id'):
                loc_map[l['id']] = l

        prop_map = {p['name']: p for p in detail.get('props', [])}
        for p in detail.get('props', []):
            if p.get('id'):
                prop_map[p['id']] = p

        matched_scene = next((s for s in detail['scenes'] if s['id'] == scene_id), scene)
        source_gen_id = matched_scene.get('active_generation_id')
        source_asset_id = matched_scene.get('asset_id')
        assets_map = {}
        row = None
        if source_asset_id:
            row = self.db.one('SELECT id, sha256, path FROM assets WHERE id=?', (source_asset_id,))
        if not row and source_gen_id:
            row = self.db.one('SELECT id, sha256, path FROM assets WHERE generation_id=? OR id=?', (source_gen_id, source_gen_id))
        if row:
            assets_map[row['id']] = row
            if source_gen_id:
                assets_map[source_gen_id] = row
            if source_asset_id:
                assets_map[source_asset_id] = row
            matched_scene['asset_id'] = row['id']
            matched_scene['source_image_asset_id'] = row['id']

        return build_animation_passport(
            detail, matched_scene, scenes=detail.get('scenes', []),
            cast_map=char_map, locations_map=loc_map, props_map=prop_map, assets_map=assets_map
        )

    def upload_character_portrait(self, character_id, image_bytes, lock=True, generation_id=None, request_id=None):
        character = self.character(character_id)
        project_id = character['project_id']
        project = self.project(project_id)
        processed_data = self._normalize_reference_image(image_bytes)

        requested_id = generation_id
        pending = None
        if requested_id:
            pending = self.db.one("SELECT * FROM generations WHERE id=? AND character_id=? AND purpose='casting'",(requested_id,character_id))
            if not pending or pending['character_revision'] != character['revision'] or pending['cancel_requested']:
                raise DomainError('Задание портрета отменено или изменилось.',409)
            if pending['state'] == 'succeeded' and pending.get('asset_id'):
                return {**self.character(character_id),'asset_id':pending['asset_id'],'generation_id':requested_id}
            if pending['state'] != 'running' or json.loads(pending['snapshot']).get('flow_request_id') != request_id:
                raise DomainError('Результат не соответствует отправленному заданию портрета.',409)
        generation_id = requested_id or uid()
        asset_id = uid()
        snapshot = json.loads(pending['snapshot']) if pending else self.casting_snapshot(project, character)
        from .portrait_contract import validate_portrait
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(processed_data)) as portrait_image:
                portrait_review = validate_portrait(portrait_image,snapshot)
        except DomainError as error:
            if requested_id:
                with self.db.transaction() as connection:
                    connection.execute("UPDATE generations SET state='failed',error=?,finished_at=?,progress=? WHERE id=? AND state='running'",
                        (str(error),now(),dump({'stage':'Портрет отклонён: несколько персонажей','percent':100}),requested_id))
            error.details = {**getattr(error,'details',{}),'code':'PORTRAIT_REJECTED'}
            raise
        if portrait_review:
            snapshot['portrait_review'] = portrait_review
            if portrait_review['status'] == 'unverified': lock = False
        if requested_id: lock = False  # A replacement stays a candidate until the user chooses it.

        relative = f"projects/{project_id}/{generation_id}.png"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)

        metadata = {
            'id': generation_id,
            'project_id': project_id,
            'purpose': 'casting',
            'snapshot': snapshot,
            'sha256': hashlib.sha256(processed_data).hexdigest(),
            'output_size': (768, 432),
            'saved_at': now(),
            'state': 'succeeded',
            'source': 'upload_google_flow',
            'error': None
        }
        metadata_dir = self.root / 'metadata'
        metadata_dir.mkdir(exist_ok=True)

        references = {'portrait': asset_id}
        with self.db.transaction() as connection:
            if requested_id:
                current = connection.execute('SELECT g.state,g.asset_id,g.cancel_requested,g.character_revision,c.revision FROM generations g JOIN characters c ON c.id=g.character_id WHERE g.id=?',(requested_id,)).fetchone()
                if current['state'] == 'succeeded' and current['asset_id']:
                    return {**character,'asset_id':current['asset_id'],'generation_id':requested_id}
                if current['state'] != 'running' or current['cancel_requested'] or current['character_revision'] != current['revision']:
                    raise DomainError('Задание изменилось во время получения портрета.',409)
            path.write_bytes(processed_data)
            (metadata_dir / f"{generation_id}.json").write_text(dump(metadata),encoding='utf-8')
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                character_id,character_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET state='succeeded',snapshot=excluded.snapshot,asset_id=excluded.asset_id,
                  finished_at=excluded.finished_at,progress=excluded.progress,error=NULL''',
                (generation_id, project_id, 'casting', dump(snapshot), 0,
                 character_id, character['revision'], asset_id, now(), now(), now(),
                 dump({'stage': 'Портрет загружен', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 hashlib.sha256(processed_data).hexdigest(), 768, 432, len(processed_data)))

            if lock:
                connection.execute('UPDATE characters SET locked=1,references_json=? WHERE id=?', (dump(references), character_id))
                self.db.event(connection, project_id, 'character_locked', {'id': character_id})
            else:
                self.db.event(connection, project_id, 'generation_completed', {'id': generation_id, 'state': 'succeeded'})

        return {**self.character(character_id),'asset_id':asset_id,'generation_id':generation_id}

    def upload_location_image(self, location_id, image_bytes, lock=True):
        location = self.location(location_id)
        project_id = location['project_id']
        project = self.project(project_id)
        processed_data = self._normalize_reference_image(image_bytes)

        generation_id = uid()
        asset_id = uid()
        snapshot = self.location_snapshot(project, location)

        relative = f"projects/{project_id}/{generation_id}.png"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(processed_data)

        metadata = {
            'id': generation_id,
            'project_id': project_id,
            'purpose': 'location',
            'snapshot': snapshot,
            'sha256': hashlib.sha256(processed_data).hexdigest(),
            'output_size': (768, 432),
            'saved_at': now(),
            'state': 'succeeded',
            'source': 'upload_google_flow',
            'error': None
        }
        metadata_dir = self.root / 'metadata'
        metadata_dir.mkdir(exist_ok=True)
        (metadata_dir / f"{generation_id}.json").write_text(dump(metadata), encoding='utf-8')

        references = {'environment': asset_id}
        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                location_id,location_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                (generation_id, project_id, 'location', dump(snapshot), 0,
                 location_id, location['revision'], asset_id, now(), now(), now(),
                 dump({'stage': 'Локация загружена', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, relative,
                 hashlib.sha256(processed_data).hexdigest(), 768, 432, len(processed_data)))

            if lock:
                connection.execute('UPDATE locations SET locked=1,references_json=? WHERE id=?', (dump(references), location_id))
                self._cancel_superseded_reference(connection, 'location', location_id, location['revision'])
                self.db.event(connection, project_id, 'location_locked', {'id': location_id})
            else:
                self.db.event(connection, project_id, 'generation_completed', {'id': generation_id, 'state': 'succeeded'})

        return self.location(location_id)

    def accept_ready_references(self, project_id):
        """Auto-lock unconfirmed characters, locations, and props that have ready generated assets."""
        self.project(project_id)
        characters = self.db.all('SELECT * FROM characters WHERE project_id=? AND locked=0', (project_id,))
        locations = self.db.all('SELECT * FROM locations WHERE project_id=? AND locked=0', (project_id,))
        props = self.db.all('SELECT * FROM props WHERE project_id=? AND locked=0', (project_id,))

        locked_chars = []
        locked_locs = []
        locked_props = []

        for c in characters:
            gen = self.db.one('''SELECT g.*, a.id AS asset_id FROM generations g
                                 JOIN assets a ON a.generation_id=g.id
                                 WHERE g.project_id=? AND g.character_id=? AND g.character_revision=?
                                   AND g.state='succeeded' AND g.cancel_requested=0
                                 ORDER BY g.created_at DESC, g.rowid DESC LIMIT 1''',
                              (project_id, c['id'], c['revision']))
            if gen and gen.get('asset_id'):
                try:
                    self.lock(c['id'], {'portrait': gen['asset_id']}, c['revision'], review={'one_person_confirmed': True})
                    locked_chars.append(c['name'])
                except Exception:
                    pass

        for l in locations:
            gen = self.db.one('''SELECT g.*, a.id AS asset_id FROM generations g
                                 JOIN assets a ON a.generation_id=g.id
                                 WHERE g.project_id=? AND g.location_id=? AND g.location_revision=?
                                   AND g.state='succeeded' AND g.cancel_requested=0
                                 ORDER BY g.created_at DESC, g.rowid DESC LIMIT 1''',
                              (project_id, l['id'], l['revision']))
            if gen and gen.get('asset_id'):
                try:
                    self.lock_location(l['id'], {'environment': gen['asset_id']}, l['revision'])
                    locked_locs.append(l['name'])
                except Exception:
                    pass

        for p in props:
            gen = self.db.one('''SELECT g.*, a.id AS asset_id FROM generations g
                                 JOIN assets a ON a.generation_id=g.id
                                 WHERE g.project_id=? AND g.prop_id=? AND g.prop_revision=?
                                   AND g.state='succeeded' AND g.cancel_requested=0
                                 ORDER BY g.created_at DESC, g.rowid DESC LIMIT 1''',
                              (project_id, p['id'], p['revision']))
            if gen and gen.get('asset_id'):
                try:
                    self.lock_prop(p['id'], {'item': gen['asset_id'], 'prop': gen['asset_id']}, p['revision'])
                    locked_props.append(p['name'])
                except Exception:
                    pass

        return {
            'ok': True,
            'characters': locked_chars,
            'locations': locked_locs,
            'props': locked_props,
            'total_accepted': len(locked_chars) + len(locked_locs) + len(locked_props)
        }

    def unlock_character(self, character_id):
        character = self.character(character_id)
        project_id = character['project_id']
        with self.db.transaction() as connection:
            connection.execute('UPDATE characters SET locked=0 WHERE id=?', (character_id,))
            self.db.event(connection, project_id, 'character_unlocked', {'id': character_id})
        return self.character(character_id)

    def unlock_location(self, location_id):
        location = self.location(location_id)
        project_id = location['project_id']
        with self.db.transaction() as connection:
            connection.execute('UPDATE locations SET locked=0 WHERE id=?', (location_id,))
            self.db.event(connection, project_id, 'location_unlocked', {'id': location_id})
        return self.location(location_id)

    def cast_diff(self, project_id):
        detail = self.detail(project_id)
        return detail.get('cast_bible', {}).get('diff', {})

    def resolve_cast_diff(self, project_id, data):
        detail = self.detail(project_id)
        existing_bible = detail.get('cast_bible') or {}
        resolved_bible = apply_cast_diff_resolution(existing_bible, existing_bible, data)
        with self.db.transaction() as connection:
            connection.execute('UPDATE projects SET cast_bible=? WHERE id=?',
                               (dump(resolved_bible), project_id))
            if data.get('action') == 'map' and data.get('cast_id') and (data.get('new_name') or data.get('name')):
                target_cid = data['cast_id']
                mapped_name = data.get('new_name') or data.get('name')
                connection.execute('UPDATE characters SET cast_id=? WHERE (name=? OR canonical_name=?) AND project_id=?',
                                   (target_cid, mapped_name, mapped_name, project_id))
            elif data.get('action') == 'keep_old' and data.get('cast_id'):
                target_cid = data['cast_id']
                if not connection.execute('SELECT id FROM characters WHERE (id=? OR cast_id=?) AND project_id=?', (target_cid, target_cid, project_id)).fetchone():
                    for c in resolved_bible.get('characters', []):
                        if c.get('cast_id') == target_cid or c.get('id') == target_cid:
                            connection.execute('''INSERT INTO characters(id,project_id,name,description,wardrobe,cast_id,canonical_name,aliases,role,status,evidence)
                                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                                               (c['id'], project_id, c['canonical_name'], c.get('description', ''),
                                                dump(c.get('wardrobe', {})), target_cid, c['canonical_name'],
                                                dump(c.get('aliases', [])), c.get('role', ''), c.get('status', 'active'),
                                                dump(c.get('evidence', []))))
                            break
        return self.detail(project_id)

    def build_animation_plan(self, project_id, plan_mode=None):
        project = self.detail(project_id)
        if not project.get('scenes'):
            raise DomainError('В проекте нет сцен для анимации.')
        settings = dict(project.get('animation_settings') or DEFAULT_ANIMATION_SETTINGS)
        if plan_mode:
            settings['plan_mode'] = plan_mode
        project['animation_settings'] = settings

        # The planner is pure, so resolve the confirmed current image here.
        # A scene record stores it through active_generation_id rather than a
        # top-level asset_id; supplying it prevents a false-ready Omni plan.
        scenes = []
        for scene in project['scenes']:
            item = dict(scene)
            if scene.get('active_generation_id'):
                generation = self.db.one('SELECT asset_id FROM generations WHERE id=?', (scene['active_generation_id'],))
                if generation and generation.get('asset_id'):
                    item['asset_id'] = generation['asset_id']
            scenes.append(item)
        plan = build_animation_plan(project, scenes)

        with self.db.transaction() as connection:
            connection.execute('UPDATE projects SET animation_settings=? WHERE id=?',
                               (dump(plan['settings']), project_id))
            for item in plan['scenes']:
                sc_id = item['scene_id']
                anim_data = item['animation']
                sc_obj = next((s for s in scenes if s['id'] == sc_id), None)
                if not sc_obj:
                    continue
                if anim_data.get('selected_engine')=='ken_burns':
                    from .voice_timing import scene_voice_timing
                    timing=scene_voice_timing(project,sc_obj)
                    if timing:anim_data['duration_seconds']=math.ceil(timing['duration']*24)/24
                cur_rev = sc_obj['current_revision']
                updated_data = dict(sc_obj['data'])
                updated_data['animation'] = anim_data
                connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                                   (sc_id, cur_rev + 1, dump(updated_data), now()))
                connection.execute('UPDATE scenes SET current_revision=? WHERE id=?',
                                   (cur_rev + 1, sc_id))
            self.db.event(connection, project_id, 'animation_plan_updated', plan['summary'])
        return plan

    def apply_animation_selection(self, project_id, data):
        """Persist selected cloud frames atomically; never starts jobs."""
        project=self.detail(project_id)
        scenes=project['scenes']
        engine_choice=data.get('animation_engine','omni_flash')
        if engine_choice not in ('omni_flash','veo_lite'):
            raise DomainError('Выберите Omni или Veo 3.1 Lite.')
        selected=data.get('selected_scene_ids',data.get('omni_scene_ids'))
        revisions=data.get('expected_revisions')
        known={s['id'] for s in scenes}
        if (not isinstance(selected,list) or not all(isinstance(s,str) for s in selected)
                or len(set(selected))!=len(selected) or not set(selected)<=known):
            raise DomainError('Выберите кадры для анимации из этого проекта.')
        if not isinstance(revisions,dict) or set(revisions)!=known:
            raise DomainError('Передайте текущие версии кадров; обновите страницу.',409)
        settings=validate_animation_settings({**project.get('animation_settings',{}),'plan_mode':'selective','default_duration_seconds':6,'selected_animation_engine':engine_choice})
        selected=set(selected)
        updates=[];skipped=[];counts={'omni_flash':0,'veo_lite':0,'ken_burns':0}
        for scene in scenes:
            # Store original scene content, never the enriched display copy.
            raw=self.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?',(scene['id'],scene['current_revision']))
            original=json.loads(raw['data'])
            previous=original.get('animation') or {}
            if scene.get('video_asset_id') or previous.get('status') in ('completed','queued_omni','generating_omni','queued_veo','generating_veo'):
                skipped.append(scene['id']);continue
            motion=smart_ken_burns_motion(original,scene['position'])
            manual_kb=previous.get('ken_burns_mode')=='manual' and previous.get('ken_burns_preset') in KEN_BURNS_PRESETS
            engine=engine_choice if scene['id'] in selected else 'ken_burns'
            counts[engine]+=1
            generation=self.db.one('SELECT asset_id FROM generations WHERE id=?',(scene.get('active_generation_id'),))
            ready=bool(generation and generation.get('asset_id'))
            animation={**previous,'mode':'manual','selection_origin':'cloud_checkbox','selected_engine':engine,
                'status':'planned_'+engine if ready else 'waiting_source_image','error':None,
                'omni':{**previous.get('omni',{}),'resolution':previous.get('omni',{}).get('resolution','720p'),'duration_seconds':previous.get('omni',{}).get('duration_seconds',6)},
                'veo':{**previous.get('veo',{}),'camera_motion':previous.get('veo',{}).get('camera_motion') or previous.get('camera_motion') or determine_camera_motion(original),'duration_seconds':previous.get('veo',{}).get('duration_seconds',5)},
                'ken_burns_mode':'manual' if manual_kb else 'auto','ken_burns_preset':previous['ken_burns_preset'] if manual_kb else motion['preset'],
                'ken_burns_reason':'Движение выбрано вручную.' if manual_kb else motion['reason'],'credit_cost':settings['veo_lite_credit_cost'] if engine=='veo_lite' else 0}
            animation['duration_seconds']=animation['omni']['duration_seconds'] if engine=='omni_flash' else animation['veo']['duration_seconds'] if engine=='veo_lite' else 6
            if engine == 'ken_burns':
                from .voice_timing import scene_voice_timing
                timing=scene_voice_timing(project,{'id':scene['id'],'data':original})
                if timing:animation['duration_seconds']=math.ceil(timing['duration']*24)/24
            if engine=='veo_lite':
                animation['camera_motion']=animation['veo']['camera_motion']
            original['animation']=animation
            if previous!=animation:
                updates.append((scene,original))
        with self.db.transaction() as connection:
            for scene in scenes:
                live=connection.execute('SELECT current_revision FROM scenes WHERE id=?',(scene['id'],)).fetchone()
                if revisions[scene['id']]!=live[0]:
                    raise DomainError('Кадры изменились. Выбор оставлен в интерфейсе; обновите проект и повторите сохранение.',409)
            connection.execute('UPDATE projects SET animation_settings=? WHERE id=?',(dump(settings),project_id))
            for scene,original in updates:
                connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(scene['id'],scene['current_revision']+1,dump(original),now()))
                connection.execute('UPDATE scenes SET current_revision=? WHERE id=?',(scene['current_revision']+1,scene['id']))
            self.db.event(connection,project_id,'animation_selection_saved',{'selected_scene_ids':sorted(selected),'animation_engine':engine_choice,'skipped':skipped})
        return {'selected_omni':counts['omni_flash'],'selected_veo':counts['veo_lite'],'selected_cloud':counts['omni_flash']+counts['veo_lite'],'animation_engine':engine_choice,'local_2_5d':counts['ken_burns'],'preserved_scenes':skipped}

    def select_scene_animation_engine(self, scene_id, engine, camera_motion=None, duration_seconds=None,
                                      ken_burns_preset=None, omni_resolution=None, motion_scope=None):
        """Persist one engine choice without erasing another engine's controls.

        ``animation`` is deliberately a small per-frame contract.  The legacy
        top-level Veo fields remain for existing Flow queues; new values are
        namespaced under ``veo`` and ``omni`` so changing a Ken Burns preset
        never changes a cloud camera or Omni quality.
        """
        if engine and engine not in ('veo_lite', 'omni_flash', 'ken_burns', 'none', 'unselected'):
            raise DomainError('Движок должен быть Veo 3.1 Lite, Omni 1.1 Flash, Ken Burns или none.')
        if ken_burns_preset is not None and ken_burns_preset != 'auto' and ken_burns_preset not in KEN_BURNS_PRESETS:
            raise DomainError('Неизвестный тип движения Ken Burns.')
        if omni_resolution is not None and omni_resolution not in ('360p', '720p'):
            raise DomainError('Omni 1.1 Flash поддерживает только 360p или 720p.')
        if motion_scope is not None and motion_scope not in ('story_action', 'camera_only'):
            raise DomainError('Выберите действие сцены или только движение камеры.')
        if engine == 'omni_flash':
            if duration_seconds is not None and int(duration_seconds) not in (4, 6, 8, 10):
                raise DomainError('Omni 1.1 Flash поддерживает 4, 6, 8 или 10 секунд.')
        scene = self.scene(scene_id)
        project_id = scene['project_id']
        project = self.detail(project_id)
        settings = validate_animation_settings(project.get('animation_settings'))

        if settings['plan_mode'] == 'economy' and engine == 'veo_lite':
            veo_count = sum(
                1 for s in project['scenes']
                if s['id'] != scene_id and s['data'].get('animation', {}).get('selected_engine') == 'veo_lite'
            )
            if veo_count >= settings['max_veo_frames']:
                raise DomainError(
                    f'В экономном режиме нельзя выбрать более {settings["max_veo_frames"]} кадров Veo 3.1 Lite (50 кредитов). '
                    'Включите ручной режим для выбора дополнительных кадров.'
                )

        current_anim = dict(scene['data'].get('animation') or {})
        score, reasons, default_camera = score_scene_for_animation(
            scene['data'], scene['position'], len(project['scenes'])
        )

        current_anim['mode'] = 'manual'
        current_anim['error'] = None
        current_anim.pop('idempotency_key', None)
        if motion_scope is not None:
            current_anim['motion_scope'] = motion_scope
        veo = dict(current_anim.get('veo') or {})
        omni = dict(current_anim.get('omni') or {})
        if camera_motion:
            veo['camera_motion'] = camera_motion
            # Legacy Flow bridge contract.
            current_anim['camera_motion'] = camera_motion
        elif not veo.get('camera_motion'):
            veo['camera_motion'] = current_anim.get('camera_motion') or default_camera
            current_anim['camera_motion'] = veo['camera_motion']
        current_anim['veo'] = veo
        if engine == 'veo_lite':
            veo['duration_seconds'] = int(duration_seconds) if duration_seconds is not None else int(veo.get('duration_seconds',5))
        if omni_resolution:
            omni['resolution'] = omni_resolution
        omni.setdefault('resolution', '720p')
        if engine == 'omni_flash':
            # Six seconds is an explicit Omni default, never the legacy 5s.
            omni['duration_seconds'] = int(duration_seconds) if duration_seconds is not None else int(omni.get('duration_seconds', 6))
        else:
            omni.setdefault('duration_seconds', 6)
        current_anim['omni'] = omni

        if ken_burns_preset == 'auto':
            motion=smart_ken_burns_motion(scene['data'],scene['position'])
            current_anim.update(ken_burns_preset=motion['preset'],ken_burns_mode='auto',ken_burns_reason=motion['reason'])
        elif ken_burns_preset:
            current_anim['ken_burns_preset'] = ken_burns_preset
            current_anim['ken_burns_mode'] = 'manual'
        elif not current_anim.get('ken_burns_preset'):
            current_anim['ken_burns_preset'] = 'slow_zoom_in'

        if engine in ('none', None, '', 'unselected'):
            current_anim['selected_engine'] = None
            current_anim['status'] = 'not_planned'
            current_anim['credit_cost'] = 0
            current_anim['video_asset_id'] = None
        else:
            current_anim['selected_engine'] = engine
            current_anim['animation_score'] = current_anim.get('animation_score', score)
            current_anim['reasons'] = current_anim.get('reasons', reasons)
            current_anim['duration_seconds'] = (
                omni['duration_seconds'] if engine == 'omni_flash'
                else veo['duration_seconds'] if engine == 'veo_lite'
                else int(duration_seconds) if duration_seconds else current_anim.get('duration_seconds', settings['default_duration_seconds'])
            )
            if engine=='ken_burns' and duration_seconds is None:
                from .voice_timing import scene_voice_timing
                timing=scene_voice_timing(project,scene)
                if timing:current_anim['duration_seconds']=math.ceil(timing['duration']*24)/24
            current_anim['credit_cost'] = settings['veo_lite_credit_cost'] if engine == 'veo_lite' else 0
            current_anim['status'] = f'planned_{engine}'
            current_anim.setdefault('safe_crop', {
                'overscan': 0.15, 'max_pan_x': 0.08, 'max_pan_y': 0.05, 'edge_safe': True, 'fallback': 'slow_zoom_in'
            })
            current_anim['safe_crop']['actual_preset'] = current_anim.get('ken_burns_preset', 'slow_zoom_in')

        updated_data = dict(scene['data'])
        updated_data['animation'] = current_anim
        with self.db.transaction() as connection:
            cur_rev = scene['current_revision']
            connection.execute("""
                UPDATE veo_jobs SET status='cancelled', completed_at=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (now(), scene_id))
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(updated_data), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?',
                               (cur_rev + 1, scene_id))
            self.db.event(connection, project_id, 'scene_animation_selected', {
                'scene_id': scene_id, 'engine': engine, 'credit_cost': current_anim.get('credit_cost', 0)
            })

        reloaded = self.detail(project_id)
        veo_total = sum(1 for s in reloaded['scenes'] if s['data'].get('animation', {}).get('selected_engine') == 'veo_lite')
        kb_total = sum(1 for s in reloaded['scenes'] if s['data'].get('animation', {}).get('selected_engine') == 'ken_burns')
        omni_total = sum(1 for s in reloaded['scenes'] if s['data'].get('animation', {}).get('selected_engine') == 'omni_flash')
        summary = {
            'plan_mode': settings['plan_mode'], 'credit_limit': settings['credit_limit'],
            'total_credits': veo_total * settings['veo_lite_credit_cost'],
            'omni_frames_count': omni_total, 'veo_frames_count': veo_total,
            'ken_burns_frames_count': kb_total, 'total_frames': len(reloaded['scenes'])
        }
        return {'scene': self.scene(scene_id), 'summary': summary}

    def queue_single_scene_veo(self, scene_id, camera_motion=None):
        scene = self.scene(scene_id)
        self._assert_scene_still_approved(scene)
        project_id = scene['project_id']
        project = self.detail(project_id)
        settings = validate_animation_settings(project.get('animation_settings'))

        current_anim = dict(scene['data'].get('animation') or {})
        score, reasons, default_camera = score_scene_for_animation(
            scene['data'], scene['position'], len(project['scenes'])
        )

        cur_rev = scene.get('current_revision', 1)
        active_gen_id = scene.get('active_generation_id')
        active_gen = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,)) if active_gen_id else None
        source_asset_id = (active_gen.get('asset_id') if active_gen else None) or scene.get('asset_id')
        source_rev = (active_gen.get('scene_revision') if active_gen else None) or cur_rev
        if not source_asset_id:
            raise DomainError('Сначала создайте статичное изображение кадра.')
        asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (source_asset_id,))
        source_sha256 = asset_rec.get('sha256') if asset_rec else None

        camera = camera_motion or current_anim.get('camera_motion') or default_camera
        duration = current_anim.get('duration_seconds', settings.get('default_duration_seconds', 5))
        veo_prompt = compile_veo_prompt(scene['data'], camera)
        fingerprint = self._animation_fingerprint(f"veo:{scene_id}:{camera}:{duration}:{source_asset_id}:{source_sha256}:{veo_prompt}", current_anim)
        idempotency_key = f"veo_{scene_id[:8]}_{fingerprint}"

        # Deduplication check: if identical job is already queued or generating, reuse it
        if (current_anim.get('status') in ('queued_veo', 'generating_veo') and
                current_anim.get('idempotency_key') == idempotency_key and
                not scene.get('video_asset_id')):
            return {'scene': self.scene(scene_id), 'queued': True, 'engine': 'veo_lite', 'reused': True}

        current_anim['mode'] = 'manual'
        current_anim['selected_engine'] = 'veo_lite'
        current_anim['camera_motion'] = camera
        current_anim['animation_score'] = current_anim.get('animation_score', score)
        current_anim['reasons'] = current_anim.get('reasons', reasons)
        current_anim['duration_seconds'] = duration
        current_anim['credit_cost'] = settings['veo_lite_credit_cost']
        current_anim['status'] = 'queued_veo'
        current_anim['error'] = None
        current_anim['idempotency_key'] = idempotency_key
        current_anim['source_image_asset_id'] = source_asset_id
        current_anim['source_image_sha256'] = source_sha256
        current_anim['source_generation_id'] = active_gen_id
        current_anim['source_revision'] = source_rev

        updated_data = dict(scene['data'])
        updated_data['animation'] = current_anim

        with self.db.transaction() as connection:
            # Cancel prior active animation jobs for this scene if parameters changed
            connection.execute("""
                UPDATE veo_jobs SET status='cancelled', completed_at=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (now(), scene_id))

            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(updated_data), now()))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?',
                               (cur_rev + 1, scene_id))
            connection.execute("""
                INSERT INTO veo_jobs(
                    id, project_id, scene_id, idempotency_key, model, status,
                    engine, source_image_asset_id, source_image_sha256,
                    source_generation_id, source_revision, duration_seconds,
                    resolution, prompt, camera_motion, created_at
                ) VALUES(?,?,?,?,?,'queued_veo',?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET
                    status='queued_veo', completed_at=NULL, error=NULL
            """, (
                uid(), project_id, scene_id, idempotency_key, 'veo-3.1-lite',
                'veo_lite', source_asset_id, source_sha256,
                active_gen_id, source_rev, int(duration),
                None, veo_prompt, camera, now()
            ))
            self.db.event(connection, project_id, 'scene_animation_queued', {
                'scene_id': scene_id, 'engine': 'veo_lite', 'idempotency_key': idempotency_key
            })

        return {'scene': self.scene(scene_id), 'queued': True, 'engine': 'veo_lite'}

    def queue_single_scene_omni(self, scene_id, duration_seconds=6, resolution='720p'):
        """Queue one Omni I2V job for the connected Google Flow bridge."""
        if resolution not in ('360p', '720p'):
            raise DomainError('Omni 1.1 Flash поддерживает только 360p или 720p.')
        if int(duration_seconds) not in (4, 6, 8, 10):
            raise DomainError('Omni 1.1 Flash поддерживает 4, 6, 8 или 10 секунд.')
        scene = self.scene(scene_id)
        self._assert_scene_still_approved(scene)
        cur_rev = scene.get('current_revision', 1)
        active_gen_id = scene.get('active_generation_id')
        active_gen = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,)) if active_gen_id else None
        source_asset_id = (active_gen.get('asset_id') if active_gen else None) or scene.get('asset_id')
        source_rev = (active_gen.get('scene_revision') if active_gen else None) or cur_rev
        if not source_asset_id:
            raise DomainError('Сначала создайте статичное изображение кадра.')
        asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (source_asset_id,))
        source_sha256 = asset_rec.get('sha256') if asset_rec else None

        project_id = scene['project_id']
        data = dict(scene['data'] or {})
        anim = dict(data.get('animation') or {})
        omni = dict(anim.get('omni') or {})
        omni.update({'resolution': resolution, 'duration_seconds': int(duration_seconds)})

        prompt = self._omni_animation_prompt(scene, int(duration_seconds))
        fingerprint = self._animation_fingerprint(f"omni:{scene_id}:{resolution}:{duration_seconds}:{source_asset_id}:{source_sha256}:{prompt}", anim)
        idempotency_key = f"omni_{scene_id[:8]}_{fingerprint}"

        # Deduplication check: if identical job is already queued or generating, reuse it
        if (anim.get('status') in ('queued_omni', 'generating_omni') and
                anim.get('idempotency_key') == idempotency_key and
                not scene.get('video_asset_id')):
            return {'scene': self.scene(scene_id), 'queued': True, 'engine': 'omni_flash', 'reused': True}

        anim.update({
            'mode': 'manual', 'selected_engine': 'omni_flash', 'omni': omni,
            'duration_seconds': int(duration_seconds), 'credit_cost': 0,
            'status': 'queued_omni', 'error': None, 'video_asset_id': None,
            'idempotency_key': idempotency_key,
            'source_image_asset_id': source_asset_id,
            'source_image_sha256': source_sha256,
            'source_generation_id': active_gen_id,
            'source_revision': source_rev,
        })
        data['animation'] = anim
        with self.db.transaction() as connection:
            # Cancel prior active animation jobs for this scene if parameters changed
            connection.execute("""
                UPDATE veo_jobs SET status='cancelled', completed_at=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (now(), scene_id))

            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(data), now()))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?',
                               (cur_rev + 1, scene_id))
            connection.execute("""
                INSERT INTO veo_jobs(
                    id, project_id, scene_id, idempotency_key, model, status,
                    engine, source_image_asset_id, source_image_sha256,
                    source_generation_id, source_revision, duration_seconds,
                    resolution, prompt, camera_motion, created_at
                ) VALUES(?,?,?,?,?,'queued_omni',?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET
                    status='queued_omni', completed_at=NULL, error=NULL
            """, (
                uid(), project_id, scene_id, idempotency_key, 'gemini-omni-1.1-flash',
                'omni_flash', source_asset_id, source_sha256,
                active_gen_id, source_rev, int(duration_seconds),
                resolution, prompt, None, now()
            ))
            self.db.event(connection, project_id, 'scene_animation_queued', {
                'scene_id': scene_id, 'engine': 'omni_flash',
                'resolution': resolution, 'duration_seconds': int(duration_seconds),
                'idempotency_key': idempotency_key
            })
        return {'scene': self.scene(scene_id), 'queued': True, 'engine': 'omni_flash'}

    @staticmethod
    def _animation_fingerprint(payload, animation):
        nonce = animation.get('request_nonce')
        if nonce:
            payload += ':request:' + str(nonce)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]

    def _omni_animation_prompt(self, scene, duration, *, _detail=None):
        """Bind the queued request to the same direction that the bridge receives."""
        passport = self.animation_passport(scene['id'], _detail=_detail)
        return compile_story_aligned_veo_prompt(
            scene['data'], characters=passport['characters'], location=passport['location'],
            props=passport['active_props'], camera_motion=passport['camera_motion'],
            duration_seconds=duration,
        )

    def start_animation_generation(self, project_id, scene_ids=None, *, regenerate_local=False):
        requested_scenes = None
        if scene_ids is not None:
            requested_scenes = [self.scene(scene_id) for scene_id in dict.fromkeys(scene_ids)]
            if any(scene['project_id'] != project_id for scene in requested_scenes):
                raise DomainError('Кадр принадлежит другому проекту.', 409)
        local_only = requested_scenes is not None and all(
            (scene['data'].get('animation') or {}).get('selected_engine') == 'ken_burns'
            for scene in requested_scenes
        )
        if requested_scenes is not None and (regenerate_local or local_only):
            # Updating one local clip does not need the story audit and every
            # generation in a large project. Validate its live source instead.
            project = self.project(project_id)
            project['animation_settings'] = json.loads(project.get('animation_settings') or '{}') or dict(DEFAULT_ANIMATION_SETTINGS)
            project['scenes'] = []
            for scene in requested_scenes:
                generation = self.db.one('SELECT * FROM generations WHERE id=?', (scene.get('active_generation_id'),))
                scene['stale'] = bool(generation and not self._generation_matches_current_scene(generation, scene))
                project['scenes'].append(scene)
        else:
            project = self.detail(project_id)
        if not project.get('scenes'):
            raise DomainError('В проекте нет сцен для генерации анимации.')

        settings = validate_animation_settings(project.get('animation_settings'))
        is_economy = settings.get('plan_mode') == 'economy'
        max_veo = settings.get('max_veo_frames', 5) if is_economy else None

        results = []
        target_scenes = project['scenes']
        if scene_ids is not None:
            scene_ids_set = set(scene_ids)
            target_scenes = [s for s in target_scenes if s['id'] in scene_ids_set]
        if regenerate_local:
            target_scenes = [s for s in target_scenes if (s['data'].get('animation') or {}).get('selected_engine') == 'ken_burns']
        # Validate the whole requested set before the first job or scene write.
        # Otherwise an unplanned/unapproved last still can be skipped, or fail
        # only after earlier clips have already entered the animation queue.
        for scene in target_scenes:
            self._assert_scene_still_approved(scene)
            if (scene['data'].get('animation') or {}).get('selected_engine')=='ken_burns' and (not scene.get('video_asset_id') or regenerate_local):
                from .voice_timing import scene_voice_timing
                timing=scene_voice_timing(project,scene)
                if timing:scene['data']['animation']['duration_seconds']=math.ceil(timing['duration']*24)/24
        remote_ids = [s['id'] for s in target_scenes if (s['data'].get('animation') or {}).get('selected_engine') in ('veo_lite','omni_flash')]
        prompts_data = self.project_prompts(project_id, include_all_scenes=True, scene_ids=remote_ids, _detail=project) if remote_ids else {'scenes': []}
        prompt_by_scene_id = {s['scene_id']: s for s in prompts_data.get('scenes', [])}

        veo_queued_count = 0
        for scene in target_scenes:
            s_id = scene['id']
            anim = dict(scene['data'].get('animation') or {})
            engine = anim.get('selected_engine')
            if not engine:
                results.append({'scene_id': s_id, 'engine': None, 'status': 'not_planned'})
                continue
            self._assert_scene_still_approved(scene)
            pr_scene = prompt_by_scene_id.get(s_id, {})

            if anim.get('status') == 'completed' and scene.get('video_asset_id') and not regenerate_local:
                results.append({'scene_id': s_id, 'engine': engine, 'status': 'completed', 'video_asset_id': scene.get('video_asset_id')})
                continue

            if engine == 'veo_lite':
                cur_rev = scene.get('current_revision', 1)
                active_gen_id = scene.get('active_generation_id')
                active_gen = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,)) if active_gen_id else None
                source_asset_id = (active_gen.get('asset_id') if active_gen else None) or scene.get('asset_id')
                source_rev = (active_gen.get('scene_revision') if active_gen else None) or cur_rev
                asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (source_asset_id,)) if source_asset_id else None
                source_sha256 = asset_rec.get('sha256') if asset_rec else None

                # Check economy mode limit
                if is_economy and veo_queued_count >= max_veo:
                    anim['status'] = 'failed'
                    anim['error'] = f'Превышен лимит {max_veo} кадров Veo 3.1 Lite в экономном режиме.'
                elif not pr_scene.get('ready_for_flow') or not source_asset_id:
                    err = pr_scene.get('flow_error') or 'Кадр не готов к отправке в Flow.'
                    anim['status'] = 'failed'
                    anim['error'] = err
                else:
                    veo_queued_count += 1
                    anim['status'] = 'queued_veo'
                    anim['error'] = None
                    camera = anim.get('camera_motion') or determine_camera_motion(scene['data'])
                    duration = anim.get('duration_seconds', 5)
                    veo_prompt = compile_veo_prompt(scene['data'], camera)
                    fingerprint = self._animation_fingerprint(f"veo:{s_id}:{camera}:{duration}:{source_asset_id}:{source_sha256}:{veo_prompt}", anim)
                    anim['idempotency_key'] = f"veo_{s_id[:8]}_{fingerprint}"
                    anim['source_image_asset_id'] = source_asset_id
                    anim['source_image_sha256'] = source_sha256
                    anim['source_generation_id'] = active_gen_id
                    anim['source_revision'] = source_rev
                results.append({'scene_id': s_id, 'engine': 'veo_lite', 'status': anim['status'], 'error': anim.get('error')})
                updated_data = dict(scene['data'])
                updated_data['animation'] = anim
                with self.db.transaction() as connection:
                    cur_rev = scene['current_revision']
                    connection.execute("""
                        UPDATE veo_jobs SET status='cancelled', completed_at=?
                        WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
                    """, (now(), s_id))
                    connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                                       (s_id, cur_rev + 1, dump(updated_data), now()))
                    connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?',
                                       (cur_rev + 1, s_id))
                    if anim.get('idempotency_key') and anim.get('status') == 'queued_veo':
                        connection.execute("""
                            INSERT INTO veo_jobs(
                                id, project_id, scene_id, idempotency_key, model, status,
                                engine, source_image_asset_id, source_image_sha256,
                                source_generation_id, source_revision, duration_seconds,
                                resolution, prompt, camera_motion, created_at
                            ) VALUES(?,?,?,?,?,'queued_veo',?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET
                                status='queued_veo', completed_at=NULL, error=NULL
                        """, (
                            uid(), project_id, s_id, anim['idempotency_key'], 'veo-3.1-lite',
                            'veo_lite', source_asset_id, source_sha256,
                            active_gen_id, source_rev, int(duration),
                            None, veo_prompt, camera, now()
                        ))
            elif engine == 'omni_flash':
                # Omni is only performed by the connected Flow bridge.  Never
                # render a local substitute or charge Veo credits for it.
                cur_rev = scene.get('current_revision', 1)
                active_gen_id = scene.get('active_generation_id')
                active_gen = self.db.one('SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,)) if active_gen_id else None
                source_asset_id = (active_gen.get('asset_id') if active_gen else None) or scene.get('asset_id')
                source_rev = (active_gen.get('scene_revision') if active_gen else None) or cur_rev
                asset_rec = self.db.one('SELECT sha256 FROM assets WHERE id=?', (source_asset_id,)) if source_asset_id else None
                source_sha256 = asset_rec.get('sha256') if asset_rec else None

                if not source_asset_id:
                    anim['status'] = 'waiting_source_image'
                    anim['error'] = 'Для Omni требуется готовое статичное изображение кадра.'
                else:
                    omni = dict(anim.get('omni') or {})
                    resolution = omni.get('resolution', '720p')
                    duration = int(omni.get('duration_seconds', 6))
                    prompt = self._omni_animation_prompt(scene, duration, _detail=project)
                    fingerprint = self._animation_fingerprint(f"omni:{s_id}:{resolution}:{duration}:{source_asset_id}:{source_sha256}:{prompt}", anim)
                    anim.update({
                        'selected_engine': 'omni_flash', 'omni': {**omni, 'resolution': resolution, 'duration_seconds': duration},
                        'duration_seconds': duration, 'credit_cost': 0, 'status': 'queued_omni', 'error': None,
                        'idempotency_key': f"omni_{s_id[:8]}_{fingerprint}",
                        'source_image_asset_id': source_asset_id,
                        'source_image_sha256': source_sha256,
                        'source_generation_id': active_gen_id,
                        'source_revision': source_rev,
                    })
                results.append({'scene_id': s_id, 'engine': 'omni_flash', 'status': anim['status'], 'error': anim.get('error')})
                updated_data = dict(scene['data']); updated_data['animation'] = anim
                with self.db.transaction() as connection:
                    cur_rev = scene['current_revision']
                    connection.execute("""
                        UPDATE veo_jobs SET status='cancelled', completed_at=?
                        WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
                    """, (now(), s_id))
                    connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (s_id, cur_rev + 1, dump(updated_data), now()))
                    connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?', (cur_rev + 1, s_id))
                    if anim.get('idempotency_key') and anim.get('status') == 'queued_omni':
                        connection.execute("""
                            INSERT INTO veo_jobs(
                                id, project_id, scene_id, idempotency_key, model, status,
                                engine, source_image_asset_id, source_image_sha256,
                                source_generation_id, source_revision, duration_seconds,
                                resolution, prompt, camera_motion, created_at
                            ) VALUES(?,?,?,?,?,'queued_omni',?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET
                                status='queued_omni', completed_at=NULL, error=NULL
                        """, (
                            uid(), project_id, s_id, anim['idempotency_key'], 'gemini-omni-1.1-flash',
                            'omni_flash', source_asset_id, source_sha256,
                            active_gen_id, source_rev, duration,
                            resolution, prompt, None, now()
                        ))
            else:
                # Ken Burns local generation
                img_bytes = None
                if scene.get('active_generation_id') and not scene.get('stale'):
                    gen = self.db.one('SELECT asset_id FROM generations WHERE id=?', (scene['active_generation_id'],))
                    if gen and gen['asset_id']:
                        asset = self.db.one('SELECT path FROM assets WHERE id=?', (gen['asset_id'],))
                        if asset:
                            img_path = self.root / asset['path']
                            if img_path.is_file():
                                img_bytes = img_path.read_bytes()

                if not img_bytes:
                    raise DomainError(f'Кадр {scene["position"]}: сначала сохраните изображение для 2.5D.')

                out_rel = f"projects/{project_id}/videos/ken_burns_{s_id[:8]}_{uid()}.mp4"
                out_abs = self.root / out_rel
                motion = smart_ken_burns_motion(scene['data'], scene['position']) if anim.get('ken_burns_mode') != 'manual' else None
                render_res = render_ken_burns_video(
                    img_bytes, out_abs,
                    preset_name=motion['preset'] if motion else anim.get('ken_burns_preset') or anim.get('safe_crop', {}).get('actual_preset', 'slow_zoom_in'),
                    duration=anim.get('duration_seconds', 5),
                    fps=project.get('animation_settings', {}).get('fps', 24),
                    target_size=(1920,1080)
                )

                if motion: render_res['reason'] = motion['reason']
                try:
                    upload_res = self.upload_scene_video(s_id, out_abs.read_bytes(), filename=f"ken_burns_{s_id}.mp4",
                        expected_scene_revision=scene['current_revision'], source_image_asset_id=gen['asset_id'],
                        local_render_info={k:v for k,v in render_res.items() if k != 'path'})
                finally:
                    out_abs.unlink(missing_ok=True)
                results.append({'scene_id': s_id, 'engine': 'ken_burns', 'status': 'completed', 'video_asset_id': upload_res['video_asset_id']})

        with self.db.transaction() as connection:
            self.db.event(connection, project_id, 'animation_generation_started', {'results': results})

        if regenerate_local or local_only:
            return {'project_id': project_id, 'scenes': results}
        return self.animation_status(project_id)

    def animation_jobs(self, project_id):
        project = self.detail(project_id)
        settings = validate_animation_settings(project.get('animation_settings'))
        remote_ids = [s['id'] for s in project['scenes'] if (s['data'].get('animation') or {}).get('selected_engine') in ('veo_lite','omni_flash') and (s['data'].get('animation') or {}).get('status') in ('queued_veo','queued_omni')]
        prompts_data = self.project_prompts(project_id, include_all_scenes=True, scene_ids=remote_ids, _detail=project) if remote_ids else {'scenes': []}
        prompt_by_scene_id = {s['scene_id']: s for s in prompts_data.get('scenes', [])}

        is_economy = settings.get('plan_mode') == 'economy'
        max_veo = settings.get('max_veo_frames', 5) if is_economy else None

        jobs = []
        veo_count = 0
        for scene in sorted(project['scenes'], key=lambda s: s['position']):
            anim = scene['data'].get('animation') or {}
            engine = anim.get('selected_engine')
            status = anim.get('status')
            pr_scene = prompt_by_scene_id.get(scene['id'], {})
            ready = pr_scene.get('ready_for_flow', False)

            if self._requires_still_approval(project_id):
                approval = self._current_still_approval(scene)
                if approval.get('status') != 'approved':
                    continue

            # Never return scenes that have completed video or manual import
            if status == 'completed' or (anim.get('source') == 'flow_manual' and scene.get('video_asset_id')):
                continue

            if engine in ('veo_lite', 'omni_flash') and status in ('queued_veo', 'queued_omni') and ready:
                # Check passport story continuity and staleness before queueing
                story_prompt = None
                passport = None
                try:
                    passport = self.animation_passport(scene['id'], _detail=project)
                    if passport.get('continuity_error'):
                        continue
                    if scene.get('stale'):
                        continue
                    story_prompt = passport.get('prompt')
                except Exception:
                    story_prompt = None

                if engine == 'veo_lite':
                    veo_count += 1
                if engine == 'veo_lite' and is_economy and veo_count > max_veo:
                    break

                cur_rev = scene.get('current_revision', 1)
                active_gen_id = scene.get('active_generation_id')
                source_rev = cur_rev
                source_asset_id = scene.get('source_image_asset_id') or scene.get('asset_id')
                if active_gen_id:
                    source_generation = self.db.one(
                        'SELECT asset_id, scene_revision FROM generations WHERE id=?', (active_gen_id,)
                    )
                    if source_generation and source_generation.get('asset_id'):
                        source_asset_id = source_generation['asset_id']
                        source_rev = source_generation.get('scene_revision') or source_rev

                if not source_asset_id:
                    continue

                asset_record = self.db.one('SELECT sha256, path FROM assets WHERE id=?', (source_asset_id,))
                if not asset_record or not asset_record.get('sha256'):
                    continue
                if not (self.root / asset_record['path']).exists():
                    continue

                source_sha256 = asset_record['sha256']

                camera = (anim.get('veo') or {}).get('camera_motion') or anim.get('camera_motion') or determine_camera_motion(scene['data'])
                omni = dict(anim.get('omni') or {})
                duration = omni.get('duration_seconds', 6) if engine == 'omni_flash' else anim.get('duration_seconds', settings.get('default_duration_seconds', 5))
                resolution = omni.get('resolution', '720p') if engine == 'omni_flash' else None
                veo_prompt = compile_veo_prompt(scene['data'], camera)
                stored_key = anim.get('idempotency_key')
                if not stored_key:
                    stored_key = f"{'omni' if engine == 'omni_flash' else 'veo'}_{scene['id'][:8]}_{source_sha256[:12]}"

                job = {
                    'kind': 'scene_video',
                    'scene_id': scene['id'],
                    'position': scene['position'],
                    'scene_revision': cur_rev,
                    'source_revision': source_rev,
                    'source_generation_id': active_gen_id,
                    'source_image_asset_id': source_asset_id,
                    'source_image_url': f"/api/assets/{source_asset_id}",
                    'source_image_sha256': source_sha256,
                    'engine': engine,
                    'model': 'gemini-omni-1.1-flash' if engine == 'omni_flash' else 'veo-3.1-lite',
                    'camera_motion': camera if engine == 'veo_lite' else None,
                    'prompt': story_prompt or veo_prompt,
                    'story_prompt': story_prompt or veo_prompt,
                    'micro_actions': passport.get('micro_actions') if passport else None,
                    'negative_constraints': passport.get('negative_constraints') if passport else None,
                    'characters': passport.get('characters') if passport else None,
                    'location': passport.get('location') if passport else None,
                    'active_props': passport.get('active_props') if passport else None,
                    'source_quote': scene['data'].get('source_quote', ''),
                    'beat': scene['data'].get('beat', ''),
                    'duration_seconds': duration,
                    'resolution': resolution,
                    'aspect_ratio': '16:9',
                    'referenceImageMediaIds': [],
                    'idempotency_key': stored_key
                }
                jobs.append(job)

        return {
            'project_id': project_id,
            'plan_mode': settings.get('plan_mode', 'economy'),
            'jobs': jobs
        }

    def retry_scene_animation(self, scene_id):
        scene = self.scene(scene_id)
        self._assert_scene_still_approved(scene)
        project_id = scene['project_id']
        cur_data = dict(scene['data'] or {})
        anim = dict(cur_data.get('animation') or {})
        anim['mode'] = 'manual'
        anim['selected_engine'] = 'veo_lite'
        anim['status'] = 'queued_veo'
        anim['video_asset_id'] = None
        anim['source'] = 'auto'
        anim['error'] = None
        new_salt = secrets.token_hex(4)
        anim['idempotency_key'] = f"veo_{scene_id[:8]}_{new_salt}"
        cur_data['animation'] = anim
        with self.db.transaction() as connection:
            cur_rev = scene['current_revision']
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(cur_data), now()))
            connection.execute('UPDATE scenes SET current_revision=?, video_asset_id=NULL WHERE id=?',
                               (cur_rev + 1, scene_id))
            connection.execute("""
                INSERT INTO veo_jobs(id, project_id, scene_id, idempotency_key, model, status, created_at)
                VALUES(?,?,?,?,?,'queued_veo',?)
                ON CONFLICT(scene_id, idempotency_key) DO UPDATE SET status='queued_veo'
            """, (uid(), project_id, scene_id, anim['idempotency_key'], 'veo-3.1-lite', now()))
            self.db.event(connection, project_id, 'scene_animation_retried', {
                'scene_id': scene_id, 'engine': 'veo_lite'
            })
        return {'scene': self.scene(scene_id), 'status': 'queued_veo'}

    def set_scene_animation_status(self, scene_id, status, error=None, *, expected_scene_revision=None,
                                   idempotency_key=None, source_image_asset_id=None, source_image_sha256=None):
        if status not in ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni', 'completed', 'failed'):
            raise DomainError(f'Недопустимый статус анимации: {status}')
        scene = self.scene(scene_id)
        cur_data = dict(scene['data'] or {})
        anim = dict(cur_data.get('animation') or {})
        bound_request = any(value is not None for value in (
            expected_scene_revision, idempotency_key, source_image_asset_id, source_image_sha256))
        if bound_request:
            generation = self.db.one('SELECT asset_id FROM generations WHERE id=?', (scene.get('active_generation_id'),))
            asset = self.db.one('SELECT sha256 FROM assets WHERE id=?', (source_image_asset_id,))
            if (scene.get('video_asset_id') or type(expected_scene_revision) is not int
                    or expected_scene_revision != scene['current_revision']
                    or not idempotency_key or idempotency_key != anim.get('idempotency_key')
                    or not generation or generation.get('asset_id') != source_image_asset_id
                    or not asset or not source_image_sha256 or asset['sha256'] != source_image_sha256):
                raise DomainError('Ответ Flow относится к изменённой или уже завершённой анимации.', 409)
        anim['status'] = status
        if error is not None:
            anim['error'] = error
        cur_data['animation'] = anim
        with self.db.transaction() as connection:
            cur_rev = scene['current_revision']
            if bound_request:
                live = connection.execute('SELECT current_revision,active_generation_id,video_asset_id FROM scenes WHERE id=?', (scene_id,)).fetchone()
                if (live['current_revision'] != cur_rev or live['active_generation_id'] != scene.get('active_generation_id') or live['video_asset_id']):
                    raise DomainError('Кадр изменился во время записи ответа Flow.', 409)
            connection.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',
                               (scene_id, cur_rev + 1, dump(cur_data), now()))
            connection.execute('UPDATE scenes SET current_revision=? WHERE id=?',
                               (cur_rev + 1, scene_id))
            connection.execute("""
                UPDATE veo_jobs SET status=?, error=?
                WHERE scene_id=? AND status IN ('queued_veo', 'generating_veo', 'queued_omni', 'generating_omni')
            """, (status, error, scene_id))
            self.db.event(connection, scene['project_id'], 'scene_animation_status_updated', {
                'scene_id': scene_id, 'status': status, 'error': error
            })
        return {'scene_id': scene_id, 'status': status}

    def animation_status(self, project_id):
        """Return independent Omni, Veo and local-animation accounting.

        Omni is intentionally cost-neutral here: it must not inherit Veo's
        credit estimate until a real Omni pricing model is configured.
        """
        project = self.detail(project_id)
        scenes_status = []
        counters = {key: 0 for key in (
            'veo_completed', 'veo_queued', 'veo_planned', 'omni_completed',
            'omni_queued', 'omni_planned', 'ken_burns_completed',
            'ken_burns_planned', 'waiting_source_image', 'failed')}
        total_spent = total_projected = 0
        veo_manual_completed = veo_auto_completed = 0
        for scene in project['scenes']:
            anim = scene['data'].get('animation', {})
            engine = anim.get('selected_engine')
            status = 'completed' if scene.get('video_asset_id') else anim.get('status', 'not_planned')
            if status == 'completed':
                if engine == 'veo_lite':
                    counters['veo_completed'] += 1
                    total_projected += 10
                    if anim.get('source') == 'flow_manual': veo_manual_completed += 1
                    else: veo_auto_completed += 1; total_spent += 10
                elif engine == 'omni_flash': counters['omni_completed'] += 1
                else: counters['ken_burns_completed'] += 1
            elif status in ('queued_veo', 'generating_veo'):
                counters['veo_queued'] += 1; total_projected += 10
            elif status == 'planned_veo':
                counters['veo_planned'] += 1; total_projected += 10
            elif status in ('queued_omni', 'generating_omni'):
                counters['omni_queued'] += 1
            elif status == 'planned_omni_flash': counters['omni_planned'] += 1
            elif status == 'planned_ken_burns': counters['ken_burns_planned'] += 1
            elif status == 'waiting_source_image': counters['waiting_source_image'] += 1
            elif status == 'failed': counters['failed'] += 1
            scenes_status.append({'scene_id': scene['id'], 'position': scene['position'], 'engine': engine,
                'status': status, 'source': anim.get('source', 'auto'), 'video_asset_id': scene.get('video_asset_id'),
                'video_url': f"/api/assets/{scene.get('video_asset_id')}" if scene.get('video_asset_id') else None,
                'error': anim.get('error'),
                'still_approval': scene.get('still_approval') if self._requires_still_approval(project_id) else None})
        assembled = self.db.one("SELECT asset_id FROM generations WHERE project_id=? AND purpose='full_storyboard_video' AND state='succeeded' ORDER BY created_at DESC LIMIT 1", (project_id,))
        return {'project_id': project_id, 'plan_mode': project.get('animation_settings', {}).get('plan_mode', 'economy'),
            'total_credits_spent': total_spent, 'total_credits_projected': total_projected,
            **counters, 'veo_pending': counters['veo_planned'] + counters['veo_queued'],
            'omni_pending': counters['omni_planned'] + counters['omni_queued'],
            'veo_manual_completed': veo_manual_completed, 'veo_auto_completed': veo_auto_completed,
            'total_scenes': len(project['scenes']),
            'assembled_video_url': f"/api/assets/{assembled['asset_id']}" if assembled and assembled['asset_id'] else None,
            'scenes': scenes_status}

    def assemble_animation_timeline(self, project_id, transition_seconds=0.3):
        project = self.detail(project_id)
        scenes = sorted(project['scenes'], key=lambda s: s['position'])
        if not scenes:
            raise DomainError('В проекте нет сцен для сборки.')
        missing_videos = [s['position'] for s in scenes if not s.get('video_asset_id')]
        if missing_videos:
            preview = ', '.join(map(str, missing_videos[:12]))
            suffix = '…' if len(missing_videos) > 12 else ''
            raise DomainError(f"MP4 пока нельзя собрать: нет анимации у {len(missing_videos)} кадров ({preview}{suffix}). Сначала завершите анимацию всех кадров.", 400)
        stale_videos = [s['position'] for s in scenes if s.get('video_stale') or s.get('stale')]
        if stale_videos:
            raise DomainError('MP4 нельзя собрать: обновите анимацию изменённых кадров: ' +
                              ', '.join(map(str, stale_videos[:12])), 409)
        for scene in scenes:
            self._assert_scene_still_approved(scene)

        clip_paths = []
        for s in scenes:
            asset = self.db.one('SELECT path FROM assets WHERE id=?', (s['video_asset_id'],))
            if not asset:
                raise DomainError(f"Файл видео для кадра {s['position']} не найден.", 404)
            p = (self.root / asset['path']).resolve()
            if not p.is_file():
                raise DomainError(f"Файл видео на диске отсутствует: {asset['path']}", 404)
            clip_paths.append(str(p))

        out_rel = f"projects/{project_id}/videos/full_storyboard_{uid()[:8]}.mp4"
        out_abs = self.root / out_rel
        export_settings = self.editing.get(project_id)['document']['export_settings']
        export_size = (export_settings['width'], export_settings['height'])
        try:
            if project.get('analysis_metadata', {}).get('narration_timeline'):
                from .voice_workflow import VoiceWorkflow
                from .narration_expansion import NarrationExpansion
                from .export_worker import render
                saved = NarrationExpansion(VoiceWorkflow(self)).montage(project_id)
                out_abs.parent.mkdir(parents=True, exist_ok=True)
                duration = render(self, saved['document'], out_abs, lambda *_: None, lambda: False, size=export_size)
                assemble_res = {'duration': duration, 'clips_count': len(clip_paths)}
            else:
                assemble_res = assemble_storyboard_video(
                    clip_paths, out_abs,
                    fps=project.get('animation_settings', {}).get('fps', 24),
                    target_size=export_size,
                    transition_seconds=transition_seconds
                )
        except Exception as error:
            raise DomainError(f'Не удалось собрать MP4: {error}', 400) from error
        digest = hashlib.sha256()
        with out_abs.open('rb') as video_file:
            for chunk in iter(lambda: video_file.read(1024 * 1024), b''):
                digest.update(chunk)
        asset_id = uid()
        generation_id = uid()
        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,
                scene_id,scene_revision,asset_id,created_at,started_at,finished_at,progress)
                VALUES(?,?,?,?,?,'succeeded',?,?,?,?,?,?,?)''',
                (generation_id, project_id, 'full_storyboard_video', dump({'clips': len(clip_paths)}), 0,
                 None, None, asset_id, now(), now(), now(),
                 dump({'stage': 'Полный фильм собран', 'percent': 100})))

            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (asset_id, project_id, generation_id, out_rel,
                 digest.hexdigest(), export_size[0], export_size[1], out_abs.stat().st_size))
            self.db.event(connection, project_id, 'storyboard_assembled', {
                'asset_id': asset_id, 'duration': assemble_res['duration'], 'clips': len(clip_paths)
            })

        return {
            'asset_id': asset_id,
            'url': f'/api/assets/{asset_id}',
            'duration': assemble_res['duration'],
            'clips_count': len(clip_paths)
        }






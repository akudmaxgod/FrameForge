"""Prepare project-owned layers and evaluate foreground motion on the clip clock."""
import base64
import binascii
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image

from .db import dump, now, uid
from .domain import DomainError
from .parallax_layers import LAYOUT, prepare_registered

PURPOSE = 'edit_foreground'


def unit(value):
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
        raise DomainError('Выберите точку или область внутри изображения.')
    return float(value)


def validate_effect(service, project_id, clip, source):
    effect = clip.get('foreground_effect')
    if effect is None:
        return
    if (not isinstance(effect, dict) or clip.get('media_type') != 'image'
            or effect.get('source') != clip.get('source_image_asset_id')
            or effect.get('mode') not in ('drift', 'glint', 'paired', 'local_25d', 'parallax')):
        raise DomainError('Эффект переднего плана относится к другому изображению.')
    if effect['mode'] == 'local_25d':
        validate_legacy_local(service, project_id, effect)
        return
    if effect['mode'] == 'parallax':
        travel(effect.get('shift_x'))
        travel(effect.get('shift_y'))
        if 'motion' in effect:
            raise DomainError('Для параллакса нужны два изображения.')
        if effect.get('layout') not in (None, LAYOUT):
            raise DomainError('Неизвестная версия слоёв переднего плана.')
    else:
        unit(effect.get('strength'))
    if effect['mode'] == 'glint':
        for key in ('x', 'y', 'peak'):
            unit(effect.get(key))
        return
    if effect['mode'] == 'paired':
        unit(effect.get('pivot_x'))
        unit(effect.get('pivot_y'))
        if effect.get('direction') not in (-1, 1):
            raise DomainError('Выберите направление жеста.')
    elif effect['mode'] == 'drift':
        region = effect.get('selection')
        if not isinstance(region, dict):
            raise DomainError('У переднего плана нет сохранённой области.')
        for key in ('x0', 'y0', 'x1', 'y1'):
            unit(region.get(key))
        if unit(effect.get('bottom')) - unit(effect.get('top')) < .01:
            raise DomainError('У переднего плана нет достаточной высоты.')
    roles = ('background', 'foreground', 'motion') if effect.get('motion') else ('background', 'foreground')
    for role in roles:
        asset = service.db.one(
            '''SELECT a.project_id,a.path,a.width,a.height,g.purpose,g.snapshot
               FROM assets a JOIN generations g ON a.generation_id=g.id WHERE a.id=?''',
            (effect.get(role),))
        meta = json.loads(asset['snapshot']) if asset and asset['purpose'] == PURPOSE else {}
        if (not asset or asset['project_id'] != project_id or asset['purpose'] != PURPOSE
                or (role != 'motion' and (asset['width'], asset['height']) != (source['width'], source['height']))
                or (role == 'motion' and (not effect.get('flow_pair') or meta.get('seed') != effect['foreground']
                    or meta.get('duration') != effect.get('motion_seconds')
                    or bool(meta.get('center_crop')) != bool(effect.get('motion_center_crop'))))
                or meta.get('source') != effect['source'] or meta.get('role') != role
                or bool(meta.get('flow_layer')) != bool(effect.get('flow_pair'))
                or (meta.get('flow_layer') and meta.get('scene_revision') != clip.get('source_scene_revision'))
                or (effect['mode'] == 'parallax' and meta.get('layout') != (
                    effect.get('layout') or ('parallax-v1' if effect.get('flow_pair') else None)))
                or (effect.get('layout') == LAYOUT and (meta.get('shift_x'), meta.get('shift_y')) != (
                    effect['shift_x'], effect['shift_y']))
                or not (service.root / asset['path']).is_file()):
            raise DomainError('Слой переднего плана недоступен. Подготовьте эффект заново.')


def travel(value):
    if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > .06:
        raise DomainError('Смещение слоя должно быть в пределах ±6% кадра.')
    return float(value)


def validate_legacy_local(service, project_id, effect):
    if effect.get('motion_seconds')!=5:
        raise DomainError('Некорректная длительность 2.5D.')
    render_id=None
    for role in ('background','foreground','motion'):
        row=service.db.one("""SELECT a.path,g.snapshot FROM assets a JOIN generations g ON g.id=a.generation_id
            WHERE a.id=? AND a.project_id=? AND g.purpose=?""",(effect.get(role),project_id,PURPOSE))
        info=json.loads(row['snapshot']) if row else {}
        if (not info.get('local_25d') or info.get('source')!=effect['source'] or info.get('role')!=role
                or info.get('regions')!=effect.get('regions')
                or (render_id is not None and info.get('render_id')!=render_id)
                or not (service.root/row['path']).is_file()):
            raise DomainError('Не удалось найти слой 2.5D для этого кадра.')
        render_id=info['render_id']


def save_layers(service, project_id, effect, background, foreground):
    source_id = effect['source']
    created = []
    try:
        with service.db.transaction() as connection:
            for role, image in (('background', background), ('foreground', foreground)):
                aid, gid = uid(), uid()
                relative = Path('assets') / project_id / (aid + '.png')
                output = service.root / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                buffer = io.BytesIO()
                image.save(buffer, format='PNG')
                content = buffer.getvalue()
                created.append(output)
                output.write_bytes(content)
                meta = {'source': source_id, 'role': role}
                if effect.get('layout'):
                    meta['layout'] = effect['layout']
                    meta.update(shift_x=effect['shift_x'], shift_y=effect['shift_y'])
                connection.execute(
                    "INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at) VALUES(?,?,?,'succeeded',?,0,?,?)",
                    (gid, project_id, PURPOSE, dump(meta), aid, now()))
                connection.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',
                                   (aid, project_id, gid, str(relative), hashlib.sha256(content).hexdigest(),
                                    image.width, image.height, len(content)))
                effect[role] = aid
    except Exception:
        for output in created:
            output.unlink(missing_ok=True)
        raise
    return effect


def uploaded_layer(value, size, transparent):
    if not isinstance(value, str) or len(value) > 17_000_000:
        raise DomainError('Загрузите слой размером до 12 МБ.', 413)
    try:
        raw = base64.b64decode(value, validate=True)
        if not 0 < len(raw) <= 12_000_000:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                raise ValueError()
            if image.size != size:
                raise DomainError('Слои должны совпадать по размеру с кадром сцены.')
            layer = image.convert('RGBA')
    except (OSError, ValueError, binascii.Error) as error:
        raise DomainError('Не удалось открыть слой. Выберите PNG, JPG или WebP.') from error
    alpha = layer.getchannel('A')
    if transparent:
        histogram = alpha.histogram()
        pixels = size[0] * size[1]
        if sum(histogram[:8]) < .08 * pixels or sum(histogram[128:]) < .002 * pixels:
            raise DomainError('На переднем плане нужен видимый объект и прозрачный фон.')
        return layer
    if alpha.getextrema()[0] != 255:
        raise DomainError('Фон должен быть непрозрачным по всему кадру.')
    return layer.convert('RGB')


def prepare_pair(service, project_id, data):
    edit = service.editing.get(project_id)
    if data.get('expected_revision') != edit['revision']:
        raise DomainError('Монтаж изменился. Откройте слои заново.', 409)
    base = next((clip for clip in edit['document']['clips']
                 if clip['edit_clip_id'] == data.get('clip_id')), None)
    clip = base.get('footage_overlay') if base and data.get('layer') == 'footage' else base
    if not clip or clip.get('media_type') != 'image':
        raise DomainError('Выберите фото на таймлайне.')
    source = service.asset(clip['source_image_asset_id'])
    size = source['width'], source['height']
    if size[0] * size[1] > 25_000_000:
        raise DomainError('Для двух слоёв выберите кадр до 25 мегапикселей.')
    effect = {'mode': 'parallax', 'source': source['id'], 'layout': LAYOUT,
              'shift_x': travel(data.get('shift_x', .028)),
              'shift_y': travel(data.get('shift_y', .010))}
    background = uploaded_layer(data.get('background'), size, False)
    foreground = uploaded_layer(data.get('foreground'), size, True)
    with Image.open(service.root / source['path']) as approved:
        background, foreground = prepare_registered(approved, background, foreground, effect['shift_x'], effect['shift_y'])
    if service.editing.get(project_id)['revision'] != edit['revision']:
        raise DomainError('Монтаж изменился. Откройте слои заново.', 409)
    return {'effect': save_layers(service, project_id, effect, background, foreground),
            'revision': edit['revision']}


def gesture_angle(t, strength, direction):
    phase_in_gesture = max(0, min(1, (t - .30) / .40))
    return 3.2 * strength * direction * math.sin(math.pi * phase_in_gesture) ** 2


def phase(clip, seconds):
    elapsed_ms = (seconds * 1000 - clip['trim_in_ms']) / clip.get('playback_rate', 1)
    return max(0, min(1, elapsed_ms / max(1000 / 24, clip['base_duration_ms'] - 1000 / 24)))


def drift(t, width, strength):
    return width * .014 * strength * math.sin(math.pi * t) ** 2


def pulse(t, peak, strength):
    return strength * math.exp(-.5 * ((t - peak) / .075) ** 2)

def key_foreground(rgb):
    import numpy as np

    raw = np.asarray(rgb, dtype=np.int16)
    neutral = np.maximum(raw[:, :, 0], raw[:, :, 2])
    green = raw[:, :, 1] - neutral
    alpha = np.clip((78 - green) / 52, 0, 1)
    raw[:, :, 1] = np.minimum(raw[:, :, 1], neutral + np.where(green > 22, 14, 255))
    return np.dstack((raw, np.rint(alpha * 255))).astype(np.uint8)


class ForegroundReader:
    """The same motion, pulse and fit/fill geometry as the montage canvas."""

    def __init__(self, path, clip, resolve_asset):
        self.clip = clip
        self.effect = clip['foreground_effect']
        self.geometry = None
        with Image.open(path) as source:
            self.source = source.convert('RGB')
        self.background = self.source
        self.foreground = None
        self.motion = None
        if self.effect.get('motion'):
            from .export_worker import VideoReader
            self.motion = VideoReader(resolve_asset(self.effect['motion']), 0)
        if self.effect['mode'] in ('drift', 'paired', 'parallax') and (self.effect['mode']=='parallax' or not self.effect.get('flow_pair') or self.motion):
            with Image.open(resolve_asset(self.effect['background'])) as layer:
                self.background = layer.convert('RGB')
            with Image.open(resolve_asset(self.effect['foreground'])) as layer:
                self.foreground = layer.convert('RGBA')

    def _prepare(self, size, fill):
        scale = (max if fill else min)(size[0] / self.source.width, size[1] / self.source.height)
        width = max(1, round(self.source.width * scale))
        height = max(1, round(self.source.height * scale))
        self.width = width
        self.height = height
        self.origin = ((size[0] - width) // 2, (size[1] - height) // 2)
        self.frame = Image.new('RGB', size, 'black')
        if self.effect.get('layout') == LAYOUT:
            overscan = 1 / (1 - max(abs(self.effect['shift_x']), abs(self.effect['shift_y'])) - .006)
            zoomed = math.ceil(width * overscan), math.ceil(height * overscan)
            self.padding = (zoomed[0] - width) / 2, (zoomed[1] - height) / 2
            self.padded_layer = self.foreground.resize(zoomed, Image.Resampling.BICUBIC)
            px, py = self.padding
            plate = self.background.resize(zoomed, Image.Resampling.BICUBIC)
            plate = plate.transform((width, height), Image.Transform.AFFINE, (1, 0, px, 0, 1, py),
                                    resample=Image.Resampling.BICUBIC)
            self.frame.paste(plate, self.origin)
            return
        self.frame.paste(self.background.resize((width, height), Image.Resampling.BICUBIC), self.origin)
        self.original = self.frame
        if self.effect['mode'] == 'drift':
            self.original = Image.new('RGB', size, 'black')
            self.original.paste(self.source.resize((width, height), Image.Resampling.BICUBIC), self.origin)
        if self.foreground is not None:
            layer = self.foreground.resize((width, height), Image.Resampling.BICUBIC)
            if self.effect['mode']=='parallax':
                import numpy as np
                pad_x, pad_y = math.ceil(width*.03)+2, math.ceil(height*.03)+2
                self.padded_layer = Image.fromarray(np.pad(np.asarray(layer), ((pad_y,pad_y),(pad_x,pad_x),(0,0)), mode='symmetric'))
                self.padding = pad_x, pad_y
                return
            x, y, right, bottom = layer.getbbox() or (0, 0, 1, 1)
            self.layer = layer.crop((x, y, right, bottom))
            self.layer_origin = (self.origin[0] + x, self.origin[1] + y)
        elif self.effect['mode'] == 'glint':
            self.glint_radius = max(2, round(.026 * width))
            radius = self.glint_radius
            self.glint = Image.new('L', (2 * radius + 1, 2 * radius + 1))
            data = []
            for y in range(-radius, radius + 1):
                for x in range(-radius, radius + 1):
                    d = min(1, math.hypot(x, y) / radius)
                    value = .94 + (.27 - .94) * d / .27 if d <= .27 else .27 * (1-d) / .73
                    data.append(round(255 * value))
            self.glint.putdata(data)
            self.glint_origin = (
                round(self.origin[0] + width * self.effect['x']) - radius,
                round(self.origin[1] + height * self.effect['y']) - radius,
            )
            # Keep the effect inside the visible source, including in fit framing.
            left, top = self.glint_origin
            for y in range(self.glint.height):
                for x in range(self.glint.width):
                    if not (self.origin[0] <= left + x < self.origin[0] + width
                            and self.origin[1] <= top + y < self.origin[1] + height):
                        self.glint.putpixel((x, y), 0)

    def image(self, seconds, size, fill=False):
        if self.geometry != (size, fill):
            self._prepare(size, fill)
            self.geometry = (size, fill)
        t = phase(self.clip, seconds)
        if self.effect['mode'] == 'local_25d':
            stream = self.motion.container.streams.video[0]
            motion = self.motion.image(t * self.effect['motion_seconds'], (stream.width, stream.height))
            frame = self.frame.copy()
            frame.paste(motion.resize((self.width, self.height), Image.Resampling.BICUBIC), self.origin)
            return frame
        frame = self.frame.copy()
        if self.effect['mode']=='parallax':
            x = self.width * self.effect['shift_x'] * (t-.5)
            y = self.height * self.effect['shift_y'] * (t-.5)
            pad_x,pad_y = self.padding
            moving = self.padded_layer.transform((self.width,self.height), Image.Transform.AFFINE,
                (1,0,pad_x-x,0,1,pad_y-y), resample=Image.Resampling.BICUBIC)
            frame.paste(moving,self.origin,moving)
            return frame
        strength = self.effect['strength']
        if self.effect['mode'] == 'paired':
            if self.effect.get('flow_pair'):
                if self.motion:
                    import numpy as np
                    duration = self.effect['motion_seconds']
                    stream = self.motion.container.streams.video[0]
                    rgb = self.motion.image(t * duration, (stream.width, stream.height))
                    if self.effect.get('motion_center_crop'):
                        left = (rgb.width - rgb.height) // 2
                        rgb = rgb.crop((left, 0, left + rgb.height, rgb.height))
                    layer = Image.fromarray(key_foreground(np.asarray(rgb)), 'RGBA')
                    layer = layer.resize((self.width, self.height), Image.Resampling.BILINEAR)
                    frame.paste(layer, self.origin, layer)
                return frame
            x, y = self.layer_origin
            pivot = (self.origin[0] + self.width * self.effect['pivot_x'] - x,
                     self.origin[1] + self.height * self.effect['pivot_y'] - y)
            angle = gesture_angle(t, strength, self.effect['direction'])
            # Pad the object on all sides so the slight turn does not clip its outline.
            padding = math.ceil(max(self.width, self.height) * .06)
            layer = self.layer
            if abs(angle) > .001:
                padded = Image.new('RGBA', (layer.width + 2*padding, layer.height + 2*padding))
                padded.paste(layer, (padding, padding))
                layer = padded.rotate(-angle, Image.Resampling.BICUBIC,
                                      center=(pivot[0]+padding, pivot[1]+padding))
                x, y = x - padding, y - padding
            frame.paste(layer, (x, y), layer)
        elif self.foreground is not None:
            shift = drift(t, self.width, strength)
            if shift < .5:
                return self.original
            x, y = self.layer_origin
            pivot = self.origin[1] + self.height * self.effect['bottom'] - y
            span = max(1, self.height * (self.effect['bottom'] - self.effect['top']))
            slope = shift / span
            # Shear the cutout so that the point where it touches the ground stays fixed.
            moving = self.layer.transform((self.layer.width + math.ceil(shift) + 1, self.layer.height),
                Image.Transform.AFFINE, (1, slope, -slope * pivot, 0, 1, 0),
                resample=Image.Resampling.BICUBIC)
            frame.paste(moving, (x, y), moving)
        else:
            opacity = pulse(t, self.effect['peak'], strength)
            if opacity >= .003:
                x, y = self.glint_origin
                frame.paste((255, 229, 166), (x, y, x + self.glint.width, y + self.glint.height),
                            self.glint.point([round(opacity * a) for a in range(256)]))
        return frame

    def close(self):
        if self.motion:
            self.motion.close()
        self.source.close()
        if self.foreground is not None:
            self.background.close()
            self.foreground.close()

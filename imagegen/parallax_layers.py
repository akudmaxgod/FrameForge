"""Keep prepared parallax layers registered to the approved still."""

import numpy as np
from PIL import Image, ImageFilter

from .domain import DomainError

LAYOUT = 'parallax-v2'


class BadBackground(DomainError):
    pass


def _expand(mask, radius):
    span = 2 * radius + 1
    for _ in range(2):
        padded = np.pad(mask, ((0, 0), (radius + 1, radius)), mode='edge')
        padded[:, 0] = False
        counts = padded.cumsum(axis=1, dtype='int32')
        mask = (counts[:, span:] != counts[:, :-span]).T
    return mask


def _has_bad_patch(bad, band, patch):
    """Catch local tears even when they cover little of the full image."""
    height, width = band.shape
    pad = ((0, (-height) % patch), (0, (-width) % patch))
    shape = ((height + patch - 1) // patch, patch, (width + patch - 1) // patch, patch)
    samples = np.pad(band, pad).reshape(shape).sum(axis=(1, 3))
    errors = np.pad(bad & band, pad).reshape(shape).sum(axis=(1, 3))
    return np.any((errors >= max(12, patch // 2)) & (errors > .42 * samples))


def _has_cloned_edge(source, background, detail, exposed, visible, size, patch, reveal):
    """Identify copied neighboring texture in the newly exposed strip."""
    def packed(image):
        rgb = np.asarray(image.resize(size, Image.Resampling.NEAREST), dtype='uint32')
        return (rgb[:, :, 0] << 16) | (rgb[:, :, 1] << 8) | rgb[:, :, 2]

    still, plate = packed(source), packed(background)
    height, width = exposed.shape
    for y in range(0, height, patch):
        for x in range(0, width, patch):
            tile = np.s_[y:y + patch, x:x + patch]
            samples = plate[tile][exposed[tile] & detail[tile]]
            if samples.size < 24:
                continue
            nearby = np.s_[max(0, y - 2 * reveal):y + patch + 2 * reveal,
                           max(0, x - 2 * reveal):x + patch + 2 * reveal]
            donor = still[nearby][visible[nearby] & detail[nearby]]
            matches = np.isin(samples, donor)
            if matches.mean() > .68 and np.unique(samples[matches]).size >= 18:
                return True
    return False


def prepare_registered(source, background, foreground, shift_x, shift_y):
    """Validate the opening edge and use generated RGB only behind the matte."""
    source = source.convert('RGB')
    background = background.convert('RGB')
    foreground = foreground.convert('RGBA')
    alpha = foreground.getchannel('A')
    matte = alpha.point(lambda value: 255 if value >= 128 else 0)
    # Evaluate at a bounded resolution so upload cost doesn't grow with 4K assets.
    scale = min(1, 960 / max(source.size))
    size = tuple(max(1, round(d * scale)) for d in source.size)
    mask = matte.resize(size, Image.Resampling.NEAREST)
    width, height = size
    # Worst-case half-travel around the original pose, plus subpixel sampling.
    reveal = max(4, round(max(abs(shift_x) * width, abs(shift_y) * height) / 2) + 3)
    inside = np.asarray(mask) > 0
    outside = _expand(inside, reveal) & ~inside
    edge = _expand(~inside, reveal) & inside
    still, far, near = (np.asarray(img.convert('RGB').resize(size, Image.Resampling.BILINEAR), dtype='int16')
                        for img in (source, background, foreground))
    # Ignore the antialiased pixel itself. The adjacent band must belong to
    # the same geometry on both plates; an average over the whole frame hides thin props.
    solid = np.asarray(alpha.resize(size, Image.Resampling.BILINEAR))
    far_error = np.abs(still - far).mean(axis=2)
    near_error = np.abs(still - near).mean(axis=2)
    patch = max(20, round(max(size) / 32))
    if _has_bad_patch(near_error > 42, edge & (solid > 245), patch):
        raise DomainError('Слои не совмещаются с исходным кадром у контура. Уточните маску переднего плана.')
    if _has_bad_patch(far_error > 36, outside & (solid < 10), patch):
        raise BadBackground('Фон не совмещается с исходным кадром у контура. Пересоздайте чистый фон.')

    # A clean plate must remove visible objects. Check that textured pixels
    # exposed at the ends of the move aren't identical to the original subject.
    exposed = np.zeros_like(edge)
    for sign in (-1, 1):
        moved = mask.transform(size, Image.Transform.AFFINE, (
            1, 0, sign * shift_x * width / 2, 0, 1, sign * shift_y * height / 2),
            resample=Image.Resampling.NEAREST)
        exposed |= (np.asarray(moved) == 0) & edge
    border = min(reveal, width // 4, height // 4)
    if border:
        exposed[:border] = exposed[-border:] = False
        exposed[:, :border] = exposed[:, -border:] = False
    detail = np.abs(still - np.asarray(source.resize(size).filter(ImageFilter.GaussianBlur(1.4)),
                                      dtype='int16')).mean(axis=2) > 5
    if _has_bad_patch(far_error < 5, exposed & detail, patch):
        raise BadBackground('На чистом фоне остался след переднего плана. Пересоздайте фон без объектов.')
    if _has_cloned_edge(source, background, detail, exposed, outside & (solid < 10), size, patch, reveal):
        raise BadBackground('За контуром скопировалась соседняя текстура. Нужен чистый фон без отражённых деталей.')

    # Retain source pixels on both visible planes, and preserve the supplied
    # antialiasing at the boundary. The background donor is used under the cutout only.
    stable_background = Image.composite(background, source, alpha.point(lambda a: min(a * 4, 255)))
    stable_foreground = Image.composite(source, foreground.convert('RGB'),
                                        alpha.point(lambda a: max(0, min((a - 220) * 8, 255))))
    stable_foreground.putalpha(alpha)
    return stable_background, stable_foreground

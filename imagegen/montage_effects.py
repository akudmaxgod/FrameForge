"""Procedural overlays evaluated on the montage clock."""
from functools import lru_cache
import math

import numpy as np
from PIL import Image, ImageDraw


@lru_cache(maxsize=4)
def _light(size):
    width, height = size
    y, x = np.ogrid[:height, :width]
    radius = np.hypot((x - .05 * width) / width, (y - .17 * height) / width)
    reflection = np.hypot((x - .68 * width) / width, (y - .64 * height) / width)
    opacity = np.maximum(.48 * np.maximum(0, 1 - radius / .61),
                         .11 * np.maximum(0, 1 - reflection / .09))
    return Image.new('RGB', size, (255, 171, 98)), Image.fromarray((255 * opacity).astype('uint8'))


def apply_effects(frame, settings, index):
    film = settings.get('old_film', 0) / 100
    flare = settings.get('light_leaks', 0) / 100
    if not film and not flare:
        return frame
    if flare:
        light, mask = _light(frame.size)
        level = flare * (.45 + .55 * (.5 + .5 * math.sin(index * .055)) ** 3)
        frame = Image.composite(light, frame, mask.point(lambda opacity: round(opacity * level)))
    if film:
        frame = frame.copy()
        draw = ImageDraw.Draw(frame, 'RGBA')
        width, height = frame.size
        seed = index + 1

        def sample():
            nonlocal seed
            seed = (1664525 * seed + 1013904223) & 0xffffffff
            return seed / 4294967296

        for point in range(180):
            x, y = sample() * width, sample() * height
            dust = point < 3
            radius = (.0012 + sample() * .0018 if dust else .0006) * width
            tone = 18 if dust or point % 2 == 0 else 240
            alpha = round((125 if dust else 29) * film)
            draw.ellipse((x-radius, y-radius*.55, x+radius, y+radius*.55),
                         fill=(tone, tone, tone, alpha))
    return frame

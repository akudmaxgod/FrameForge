"""Conservative input reservations without treating image base64 as text.

Gemini 3's documented maximum is 2240 tokens per still image. Reserve 8192
instead, retaining the byte upper estimate for all text and unrecognised media.
https://ai.google.dev/gemini-api/docs/media-resolution (2026-09-23).
Only models actually verified in a free registry are enabled here.
"""
import base64
import copy
import io
import json

from PIL import Image

STILL_MODELS = {'gemini-3.1-flash-lite', 'gemini-3.8-flash'}
FORMATS = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
IMAGE_RESERVATION = 8192


def encoded_size(value):
    return len(json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')) if value is not None else 0


def input_reservation(payload, model):
    if model not in STILL_MODELS or not isinstance(payload, dict):
        return max(1, encoded_size(payload))
    reduced = copy.deepcopy(payload)
    request = reduced.get('generateContentRequest', reduced)
    if not isinstance(request, dict):
        return max(1, encoded_size(payload))
    contents = request.get('contents', [])
    if not isinstance(contents, list):
        return max(1, encoded_size(payload))
    contents = list(contents)
    if isinstance(request.get('systemInstruction'), dict):
        contents.append(request['systemInstruction'])
    images = 0
    for content in contents:
        parts = content.get('parts', []) if isinstance(content, dict) else []
        if not isinstance(parts, list):
            continue
        for part in parts:
            media = part.get('inlineData') if isinstance(part, dict) else None
            if not isinstance(media, dict) or media.get('mimeType') not in FORMATS:
                continue
            data = media.get('data')
            if not isinstance(data, str) or len(data) > 40_000_000:
                continue
            try:
                raw = base64.b64decode(data, validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format != FORMATS[media['mimeType']] or getattr(image, 'n_frames', 1) != 1:
                        continue
                    image.verify()
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
                continue
            media['data'] = ''
            images += 1
    return max(1, encoded_size(reduced) + images * IMAGE_RESERVATION)

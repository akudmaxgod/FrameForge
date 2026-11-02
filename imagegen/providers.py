"""Real HTTP integrations for image and media generation; no synthetic fallback."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from .domain import DomainError
from .google_keys import GeminiKeyPool, GeminiPoolError

log = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


class GroqRateLimitError(ProviderError):
    """Legacy exception retained for backwards compatibility."""
    pass


class ExecutionError(ProviderError):
    """Backend has explicitly reported a terminal failure, not a transport loss."""
    pass


class RejectedError(ProviderError):
    """A request was rejected before execution."""
    pass


class JsonHTTP:
    def __init__(self, base, timeout=30, headers=None):
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise DomainError('Для локального backend допустим только http://localhost или loopback IP.')
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.headers = headers or {}

    def request(self, path, payload=None, raw=None, content_type=None):
        body = json.dumps(payload).encode() if payload is not None else raw
        request = urllib.request.Request(self.base + path, data=body)
        for key, value in self.headers.items():
            request.add_header(key, value)
        if body is not None:
            request.add_header('Content-Type', content_type or 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(8000).decode(errors='replace')
            cls = RejectedError if 400 <= error.code < 500 else ProviderError
            raise cls(f'HTTP {error.code} {path}: {detail}') from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as error:
            raise ProviderError(f'Нет связи с {self.base}{path}: {error}') from error

    def json(self, path, payload=None):
        try:
            return json.loads(self.request(path, payload))
        except (ValueError, UnicodeError) as error:
            raise ProviderError('Неверный JSON от backend: ' + str(error)) from error


def node(kind, **inputs):
    return {'class_type': kind, 'inputs': inputs}


def build_workflow(profile, prompt, seed, references, prefix, detail_faces=False):
    """Build the Flux2 graph and optionally refine detected faces after decoding."""
    graph = {
        '1': node('UnetLoaderGGUF', unet_name=profile['unet']) if profile['unet'].endswith('.gguf')
             else node('UNETLoader', unet_name=profile['unet'], weight_dtype='default'),
        '2': node('CLIPLoaderGGUF', clip_name=profile['clip'], type='flux2')
             if profile['clip'].endswith('.gguf') else
             node('CLIPLoader', clip_name=profile['clip'], type='flux2', device='cpu'),
        '3': node('VAELoader', vae_name=profile['vae']),
        '4': node('CLIPTextEncode', text=prompt, clip=['2', 0]),
        '5': node('EmptyFlux2LatentImage', width=profile['width'], height=profile['height'], batch_size=1),
        '6': node('RandomNoise', noise_seed=seed),
        '7': node('KSamplerSelect', sampler_name='euler'),
        '8': node('Flux2Scheduler', steps=4, width=profile['width'], height=profile['height']),
    }
    conditioning = ['4', 0]
    for index, filename in enumerate(references):
        offset = 20 + index * 4
        graph[str(offset)] = node('LoadImage', image=filename)
        graph[str(offset + 1)] = node('ImageScaleToTotalPixels', image=[str(offset), 0],
                                      upscale_method='lanczos', megapixels=0.85, resolution_steps=1)
        graph[str(offset + 2)] = node('VAEEncode', pixels=[str(offset + 1), 0], vae=['3', 0])
        graph[str(offset + 3)] = node('ReferenceLatent', conditioning=conditioning,
                                     latent=[str(offset + 2), 0])
        conditioning = [str(offset + 3), 0]
    graph['9'] = node('BasicGuider', model=['1', 0], conditioning=conditioning)
    graph['10'] = node('SamplerCustomAdvanced', noise=['6', 0], guider=['9', 0], sampler=['7', 0],
                       sigmas=['8', 0], latent_image=['5', 0])
    graph['11'] = node('VAEDecode', samples=['10', 0], vae=['3', 0])
    output = ['11', 0]
    if detail_faces:
        graph['13'] = node('CLIPTextEncode', text='', clip=['2', 0])
        graph['14'] = node('UltralyticsDetectorProvider', model_name='bbox/face_yolov8m.pt')
        graph['15'] = node(
            'FaceDetailer', image=['11', 0], model=['1', 0], clip=['2', 0], vae=['3', 0],
            guide_size=512.0, guide_size_for=True, max_size=768.0, seed=seed,
            steps=4, cfg=1.0, sampler_name='euler', scheduler='simple',
            positive=conditioning, negative=['13', 0], denoise=0.22,
            feather=12, noise_mask=True, force_inpaint=True,
            bbox_threshold=0.5, bbox_dilation=10, bbox_crop_factor=3.0,
            sam_detection_hint='none', sam_dilation=0, sam_threshold=0.93,
            sam_bbox_expansion=0, sam_mask_hint_threshold=0.7,
            sam_mask_hint_use_negative='False', drop_size=10,
            bbox_detector=['14', 0], wildcard='', cycle=1,
            inpaint_model=False, noise_mask_feather=20,
            tiled_encode=False, tiled_decode=False)
        output = ['15', 0]
    graph['12'] = node('SaveImage', images=output, filename_prefix=prefix)
    return graph


class ComfyProvider:
    def __init__(self, base):
        self.http = JsonHTTP(base, 30)

    def health(self):
        return JsonHTTP(self.http.base, 3).json('/system_stats')

    def models(self):
        info = self.http.json('/object_info')
        result = {'unet': [], 'clip': [], 'vae': [], 'nodes': list(info)}
        for key, classes, field in [('unet', ['UnetLoaderGGUF', 'UNETLoader'], 'unet_name'),
                                     ('clip', ['CLIPLoaderGGUF', 'CLIPLoader'], 'clip_name'),
                                     ('vae', ['VAELoader'], 'vae_name')]:
            for kind in classes:
                value = info.get(kind, {}).get('input', {}).get('required', {}).get(field, [[]])[0]
                if isinstance(value, list):
                    result[key].extend(value)
        return result

    def validate(self, graph):
        info = self.http.json('/object_info')
        for key, item in graph.items():
            kind = item['class_type']
            if kind not in info:
                raise ProviderError(f'В ComfyUI отсутствует node {kind}; требуется совместимая версия.')
            schema = info[kind].get('input', {})
            allowed = set(schema.get('required', {})) | set(schema.get('optional', {}))
            if not set(item['inputs']) <= allowed:
                raise ProviderError(f'Workflow {kind}: неизвестные входы {set(item["inputs"]) - allowed}.')
            for name, spec in info[kind].get('input', {}).get('required', {}).items():
                if name not in item['inputs']:
                    raise ProviderError(f'Workflow {kind}.{name}: обязательный вход отсутствует.')
                value = item['inputs'][name]
                if isinstance(spec[0], list) and not isinstance(value, list) and value not in spec[0]:
                    raise ProviderError(f'Workflow {kind}.{name}: значение {value} недоступно в backend.')

    def upload(self, data, filename):
        boundary = 'imagegen' + uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n').encode()
        body += data + f'\r\n--{boundary}--\r\n'.encode()
        result = json.loads(self.http.request('/upload/image', raw=body,
                            content_type='multipart/form-data; boundary=' + boundary))
        return (result.get('subfolder', '') + '/' + result['name']).lstrip('/')

    def submit(self, graph, generation_id):
        result = self.http.json('/prompt', {'prompt': graph, 'client_id': generation_id,
                               'extra_data': {'imagegen_id': generation_id}})
        if 'prompt_id' not in result:
            raise RejectedError('ComfyUI отклонил workflow: ' + json.dumps(result))
        return result['prompt_id']

    def status(self, prompt_id):
        history = self.http.json('/history/' + urllib.parse.quote(prompt_id))
        if prompt_id in history:
            result = history[prompt_id]
            if result.get('status', {}).get('status_str') == 'error':
                raise ExecutionError('Ошибка ComfyUI: ' + json.dumps(result.get('status'), ensure_ascii=False))
            images = result.get('outputs', {}).get('12', {}).get('images', [])
            if images:
                return {'state': 'succeeded', 'image': images[0]}
            if result.get('status', {}).get('completed'):
                raise ExecutionError('ComfyUI завершил работу без ожидаемого SaveImage output.')
        queue = self.http.json('/queue')
        for state, key in [('running', 'queue_running'), ('queued', 'queue_pending')]:
            if any(row[1] == prompt_id for row in queue.get(key, [])):
                return {'state': state}
        return {'state': 'missing'}

    def reconcile(self, generation_id):
        history = self.http.json('/history')
        queue = self.http.json('/queue')
        candidates = list(history.values())
        for row in queue.get('queue_running', []) + queue.get('queue_pending', []):
            candidates.append({'prompt': row})
        matches = []
        for item in candidates:
            prompt = item.get('prompt', [])
            if len(prompt) > 3 and prompt[3].get('imagegen_id') == generation_id:
                matches.append(prompt[1])
        matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            raise ProviderError('Найдено несколько provider jobs для одной попытки. Нужна проверка ComfyUI.')
        return matches[0] if matches else None

    def cancel(self, prompt_id):
        queue = self.http.json('/queue')
        if any(row[1] == prompt_id for row in queue.get('queue_pending', [])):
            self.http.json('/queue', {'delete': [prompt_id]})
        if any(row[1] == prompt_id for row in queue.get('queue_running', [])):
            self.http.request('/interrupt', {})

    def fetch(self, image):
        return self.http.request('/view?' + urllib.parse.urlencode(image))


class GoogleNanoBananaProvider:
    """Cloud image generation and character editing via Google Gemini multimodal API."""
    def __init__(self, api_key, model='gemini-3.1-flash-image', extra_api_keys='', *, dispatcher=None, project_id='application'):
        self.key_pool = GeminiKeyPool(api_key, extra_api_keys, dispatcher=dispatcher, project_id=project_id, category='image_api')
        if not self.key_pool.keys:
            raise DomainError('API-ключ Google не указан в настройках.')
        self.api_key = self.key_pool.keys[0]
        self.model = model or 'gemini-3.1-flash-image'
        self.endpoint = f'https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent'

    def health(self):
        try:
            data = self.key_pool.json('https://generativelanguage.googleapis.com/v1beta/models', model=self.model, timeout=10)
            models = [m.get('name', '').split('/')[-1] for m in data.get('models', [])]
            return {'ok': True, 'models_count': len(models), 'selected': self.model}
        except GeminiPoolError as error:
            raise ProviderError(str(error)) from None

    def generate(self, prompt, references=None, width=1024, height=576, seed=None):
        from PIL import Image
        parts = [{'text': prompt}]
        if references:
            for item in references:
                img_data = item.get('data') if isinstance(item, dict) else item
                if isinstance(img_data, bytes):
                    b64 = base64.b64encode(img_data).decode('ascii')
                    parts.append({
                        'inline_data': {
                            'mime_type': 'image/png',
                            'data': b64
                        }
                    })

        payload = {
            'contents': [{'parts': parts}],
            'generationConfig': {
                'responseModalities': ['IMAGE']
            }
        }
        if seed is not None:
            try:
                payload['generationConfig']['seed'] = int(seed) % (2**31 - 1)
            except (ValueError, TypeError):
                pass

        try:
            data = self.key_pool.json(self.endpoint, payload, model=self.model, timeout=60)
        except GeminiPoolError as error:
            cls = RejectedError if error.status and 400 <= error.status < 500 and error.status != 429 else ProviderError
            raise cls(str(error)) from None

        candidates = data.get('candidates', [])
        if not candidates:
            feedback = data.get('promptFeedback', {})
            raise RejectedError(f'Google API не вернул кандидатов генерации: {feedback}')

        candidate = candidates[0]
        content_parts = candidate.get('content', {}).get('parts', [])
        img_b64 = None
        for p in content_parts:
            inline = p.get('inlineData') or p.get('inline_data')
            if inline and inline.get('data'):
                img_b64 = inline['data']
                break

        if not img_b64:
            finish_reason = candidate.get('finishReason', 'UNKNOWN')
            raise ExecutionError(f'Google Nano Banana не сформировал изображение (причина: {finish_reason}).')

        raw_img_bytes = base64.b64decode(img_b64)
        with Image.open(io.BytesIO(raw_img_bytes)) as img:
            img = img.convert('RGB')
            if img.size != (width, height):
                img = img.resize((width, height), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return buf.getvalue()


class GeminiOmniFlashProvider:
    """Gemini Interactions API adapter for real Omni image-to-video requests."""
    model = 'gemini-omni-1.1-flash'
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/interactions'

    def __init__(self, api_key, request=None, *, dispatcher=None, project_id='application'):
        if not api_key:
            raise DomainError('API-ключ Google не указан для Omni 1.1 Flash.')
        self.api_key = api_key.strip()
        self.key_pool = GeminiKeyPool(self.api_key, dispatcher=dispatcher, project_id=project_id, category='animation_api')
        self.dispatcher = dispatcher
        self._request = request or self._http_request

    def _http_request(self, payload):
        if self.dispatcher is not None:
            try:
                return self.key_pool.json(self.endpoint, payload, model=self.model, timeout=180)
            except GeminiPoolError as error:
                raise ProviderError(str(error)) from None
        url = self.endpoint
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'x-goog-api-key': self.api_key}
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            detail = error.read(8000).decode(errors='replace')
            try:
                detail = json.loads(detail).get('error', {}).get('message', detail)
            except Exception:
                pass
            cls = RejectedError if 400 <= error.code < 500 else ProviderError
            raise cls(f'Gemini Omni ({error.code}): {detail}') from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as error:
            raise ProviderError(f'Нет связи с Gemini Omni: {error}') from error

    def image_to_video(self, prompt, image_bytes, mime_type='image/png', *, resolution='720p', duration_seconds=6):
        if resolution not in ('360p', '720p'):
            raise DomainError('Omni поддерживает 360p или 720p.')
        if duration_seconds not in (4, 6, 8, 10):
            raise DomainError('Omni поддерживает длительность 4, 6, 8 или 10 секунд.')
        payload = {
            'model': self.model,
            'input': [{
                'role': 'user',
                'parts': [
                    {'text': prompt},
                    {'inlineData': {'mimeType': mime_type, 'data': base64.b64encode(image_bytes).decode('ascii')}}
                ]
            }],
            'generationConfig': {'responseModalities': ['VIDEO'], 'video': {
                'resolution': resolution, 'durationSeconds': duration_seconds
            }}
        }
        data = self._request(payload)
        candidates = data.get('outputs') or data.get('candidates') or data.get('response', {}).get('outputs') or []
        for candidate in candidates:
            content = candidate.get('content', candidate)
            parts = content.get('parts', []) if isinstance(content, dict) else []
            for part in parts:
                inline = part.get('inlineData') or part.get('inline_data') or part.get('video')
                if isinstance(inline, dict) and inline.get('data'):
                    mime = inline.get('mimeType') or inline.get('mime_type') or 'video/mp4'
                    if not str(mime).startswith('video/'):
                        continue
                    return base64.b64decode(inline['data']), mime
        raise ExecutionError('Gemini Omni не вернул видео-данные. Результат не был сохранён как успешный.')


class PollinationsProvider:
    """Free cloud image generation via Pollinations.ai (Flux, Flux Realism, Turbo)."""
    def __init__(self, model='flux'):
        self.model = model or 'flux'
        self.base = 'https://image.pollinations.ai'

    def health(self):
        req = urllib.request.Request(f'{self.base}/models', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return {'ok': True, 'selected': self.model}
        except Exception:
            return {'ok': True, 'selected': self.model}

    def generate(self, prompt, width=1024, height=576, seed=None):
        from PIL import Image
        encoded_prompt = urllib.parse.quote(prompt)
        params = {
            'model': self.model,
            'width': str(width),
            'height': str(height),
            'nologo': 'true',
            'nofeed': 'true'
        }
        if seed is not None:
            try:
                params['seed'] = str(int(seed) % (2**31 - 1))
            except (ValueError, TypeError):
                pass

        query_str = urllib.parse.urlencode(params)
        url = f'{self.base}/prompt/{encoded_prompt}?{query_str}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36', 'Accept': 'application/json'})

        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                img_bytes = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(8000).decode(errors='replace')
            cls = RejectedError if 400 <= error.code < 500 else ProviderError
            raise cls(f'Pollinations AI ({error.code}): {detail}') from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as error:
            raise ProviderError(f'Ошибка соединения с Pollinations AI: {error}') from error

        if not img_bytes:
            raise ExecutionError('Pollinations AI вернул пустой ответ.')

        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                img = img.convert('RGB')
                if img.size != (width, height):
                    img = img.resize((width, height), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                return buf.getvalue()
        except Exception as error:
            raise ExecutionError(f'Не удалось декодировать изображение от Pollinations: {error}') from error

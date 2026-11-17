"""Post-process a finished MP4 while keeping its source timestamps and audio."""
from fractions import Fraction
import logging
import math
import threading
from uuid import UUID

import av
from PIL import Image, ImageEnhance

from .db import uid
from .domain import DomainError
from .montage_effects import apply_effects


def _grade(path, duration):
    from .export_worker import VideoReader

    levels = []
    for fraction in (.08, .22, .37, .53, .68, .82, .94):
        reader = VideoReader(path, duration * fraction)
        try:
            sample = reader.image(duration * fraction, (160, 90)).convert('L')
            levels.append(sample.histogram())
        finally:
            reader.close()
    histogram = [sum(counts) for counts in zip(*levels)]
    total = sum(histogram)
    cumulative = 0
    for median, count in enumerate(histogram):
        cumulative += count
        if cumulative >= total / 2:
            break
    offset = max(-7, min(8, (112 - median) * .14))
    return [max(0, min(255, round(value + (value - 128) * .025 +
                                 offset * 4 * value * (255 - value) / 255**2)))
            for value in range(256)] * 3


def _leak(seconds):
    # One short, tapered warm flare about every 31 seconds.
    phase = (seconds - 5) % 31
    return round(42 * math.sin(math.pi * phase / .9) ** 2) if phase < .9 else 0


_NVENC_OPTIONS = {'preset': 'p4', 'rc': 'vbr', 'cq': '19', 'bf': '0'}


def _encoder(size, time_base):
    try:
        codec = av.CodecContext.create('h264_nvenc', 'w')
        codec.width, codec.height = size
        codec.pix_fmt = 'yuv420p'
        codec.time_base = time_base
        codec.options = _NVENC_OPTIONS.copy()
        codec.open()
        return 'h264_nvenc', _NVENC_OPTIONS.copy()
    except (av.FFmpegError, ValueError):
        raise RuntimeError('NVENC недоступен; программное кодирование отключено.')


def _decode_probe(source, effects):
    from av.codec.hwaccel import HWAccel

    if effects:
        try:
            with av.open(str(source), hwaccel=HWAccel('cuda', allow_software_fallback=False)) as inp:
                frame = next(inp.decode(video=0), None) if inp.streams.video else None
                if frame is not None and inp.streams.video[0].codec_context.is_hwaccel:
                    return frame, HWAccel('cuda', allow_software_fallback=False)
        except (av.FFmpegError, ValueError, RuntimeError) as error:
            raise RuntimeError('NVDEC недоступен; CPU-декодирование видео отключено.') from error
    with av.open(str(source)) as inp:
        return next(inp.decode(video=0), None) if inp.streams.video else None, None


def render(source, output, effects, progress, backend=lambda value: None):
    first, decoder = _decode_probe(source, effects)
    with av.open(str(source), hwaccel=decoder) as inp:
        if 'mp4' not in inp.format.name.split(',') or not inp.streams.video:
            raise DomainError('Выберите MP4 с видеодорожкой.')
        stream = inp.streams.video[0]
        rate = stream.average_rate or Fraction(30)
        duration = float(stream.duration * stream.time_base) if stream.duration else (inp.duration or 0) / av.time_base
        lut = _grade(source, duration) if 'color' in effects else None
        if first is None:
            raise DomainError('В MP4 не удалось прочитать ни одного кадра.')
        gpu = None
        if effects:
            from .finished_video_gpu import VideoEffectsGPU

            width, height = first.width, first.height
            if first.rotation % 180:
                width, height = height, width
            size = width + width % 2, height + height % 2
            encoder, options = _encoder(size, stream.time_base)
            if (width, height) == size and first.rotation % 90 == 0:
                gpu = VideoEffectsGPU.create(size, lut, effects)
            if gpu is None:raise RuntimeError('GPU-эффекты недоступны для этого формата; CPU-обработка отключена.')
            stages = ['NVDEC' if decoder else 'CPU-декодер',
                      'OpenCL' if gpu else 'CPU-эффекты',
                      'NVENC' if encoder == 'h264_nvenc' else 'CPU-кодировщик']
            backend(' · '.join(stages))
        else:
            backend('Копирование дорожек')
        with av.open(str(output), 'w', format='mp4', options={'movflags': '+faststart'}) as out:
            audio = {}
            for original in inp.streams.audio:
                track = out.add_stream_from_template(original)
                track.metadata.clear()
                audio[original.index] = track
            video = out.add_stream(encoder, rate=rate) if effects else out.add_stream_from_template(stream)
            video.metadata.clear()
            if effects:
                video.width, video.height = size
                video.pix_fmt = 'yuv420p'
                video.codec_context.time_base = stream.time_base
                video.options = options
                aspect = stream.sample_aspect_ratio
                if aspect:
                    video.codec_context.sample_aspect_ratio = 1 / aspect if first.rotation % 180 else aspect
            index = 0
            for packet in inp.demux(stream, *inp.streams.audio):
                if not effects or packet.stream.index in audio:
                    if packet.dts is not None:
                        packet.stream = audio.get(packet.stream.index, video)
                        out.mux(packet)
                    if not effects and duration and packet.pts is not None:
                        progress(min(99, max(0, round(100 * float(packet.pts * packet.time_base) / duration))))
                    continue
                for decoded in packet.decode():
                    seconds = max(0, float((decoded.pts - (stream.start_time or 0)) * decoded.time_base)) if decoded.pts is not None else index / float(rate)
                    leak = _leak(seconds) if 'leaks' in effects else 0
                    if gpu:
                        pixels = gpu.frame(decoded.to_ndarray(format='bgr24'), decoded.rotation, index, leak)
                        encoded = av.VideoFrame.from_ndarray(pixels, format='yuv420p')
                    else:
                        frame = decoded.to_image()
                        if decoded.rotation:
                            frame = frame.rotate(decoded.rotation, expand=True)
                        if lut:
                            frame = ImageEnhance.Color(frame.point(lut)).enhance(1.025)
                        frame = apply_effects(frame, {'old_film': 48 if 'grain' in effects else 0,
                                                      'light_leaks': leak}, index)
                        encoded = av.VideoFrame.from_image(frame).reformat(video.width, video.height, 'yuv420p')
                    encoded.time_base = decoded.time_base or stream.time_base
                    encoded.pts = decoded.pts if decoded.pts is not None else round(index / rate / encoded.time_base)
                    for result in video.encode(encoded):
                        out.mux(result)
                    index += 1
                    if index % 24 == 0 and duration:
                        progress(min(99, round(100 * seconds / duration)))
            if effects:
                for result in video.encode():
                    out.mux(result)


class FinishedVideo:
    def __init__(self, root):
        self.directory = root / 'outputs' / 'finished_video'
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, source, effects):
        if not effects <= {'color', 'grain', 'leaks'}:
            raise DomainError('Неизвестный эффект обработки.')
        with self.lock:
            if any(job['state'] == 'running' for job in self.jobs.values()):
                raise DomainError('Уже обрабатывается другой ролик.', 409)
            key = uid()
            self.jobs[key] = {'id': key, 'state': 'running', 'progress': 0}
            threading.Thread(target=self._run, args=(key, source, effects), daemon=True).start()
        return self.get(key)

    def get(self, key):
        with self.lock:
            if key in self.jobs:
                return dict(self.jobs[key])
        try:
            valid = str(UUID(key)) == key
        except ValueError:
            valid = False
        if valid and (self.directory / (key + '.mp4')).is_file():
            return {'id': key, 'state': 'ready', 'progress': 100,
                    'url': f'/api/finished-video/{key}/download'}
        raise DomainError('Обработка не найдена.', 404)

    def path(self, key):
        if self.get(key)['state'] != 'ready':
            raise DomainError('Ролик ещё не готов.', 409)
        return self.directory / (key + '.mp4')

    def _run(self, key, source, effects):
        output = self.directory / (key + '.mp4')
        partial = output.with_suffix('.partial')
        job = self.jobs[key]

        def progress(percent):
            with self.lock:
                job['progress'] = percent

        def backend(value):
            with self.lock:
                job['backend'] = value

        try:
            render(source, partial, effects, progress, backend)
            partial.replace(output)
            with self.lock:
                job.update(state='ready', progress=100, url=f'/api/finished-video/{key}/download')
        except Exception as error:
            logging.getLogger(__name__).exception('Finished video export failed')
            message = str(error) if isinstance(error, DomainError) else 'Не удалось обработать MP4. Проверьте, что файл воспроизводится.'
            with self.lock:
                job.update(state='failed', error=message)
        finally:
            source.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)

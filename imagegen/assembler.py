from collections import deque
from fractions import Fraction
import math
from pathlib import Path
from PIL import Image

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False


def _clip_frames(path, fps, target_size):
    """Resample by presentation time, retaining adjacent input frames only."""
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f'В клипе нет видеодорожки: {Path(path).name}')
        stream = container.streams.video[0]
        rate = float(stream.average_rate or fps)
        previous = None
        origin = None
        output_index = 0
        previous_time = 0.0
        duration = 1 / rate
        for index, frame in enumerate(container.decode(stream)):
            timestamp = float(frame.pts * frame.time_base) if frame.pts is not None else index / rate
            if origin is None:
                origin = timestamp
            timestamp = max(previous_time, timestamp - origin)
            if previous is not None:
                while output_index / fps < timestamp - 1e-9:
                    yield previous
                    output_index += 1
            previous = frame.to_image().convert('RGB')
            if previous.size != target_size:
                previous = previous.resize(target_size, Image.Resampling.BICUBIC)
            previous_time = timestamp
            duration = float(frame.duration * frame.time_base) if frame.duration else 1 / rate
        if previous is None:
            raise ValueError(f'Не удалось прочитать кадры клипа: {Path(path).name}')
        while output_index / fps < previous_time + duration - 1e-9:
            yield previous
            output_index += 1


def assemble_storyboard_video(clip_paths, output_path, fps=24, target_size=(1280, 720),
                              transition_seconds=0.3):
    """Stream a silent storyboard, buffering only the short crossfade tail."""
    if not HAS_AV:
        raise RuntimeError('Для сборки MP4 требуется PyAV. Установите зависимости приложения.')
    fps = int(fps)
    transition_seconds = float(transition_seconds)
    if not 1 <= fps <= 120 or not math.isfinite(transition_seconds) or not 0 <= transition_seconds <= 5:
        raise ValueError('Некорректная частота кадров или длительность перехода.')
    if not clip_paths:
        raise ValueError('Список клипов для сборки пуст.')
    for path in clip_paths:
        if not Path(path).is_file():
            raise FileNotFoundError(f'Файл клипа не найден: {Path(path).name}')
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + '.partial')
    transition_frames = int(round(transition_seconds * fps))
    tail = deque()
    written = 0
    try:
        with av.open(str(temporary), mode='w', format='mp4', options={'movflags': '+faststart'}) as container:
            stream = container.add_stream('libx264', rate=fps)
            stream.width, stream.height = target_size
            stream.pix_fmt = 'yuv420p'
            stream.options = {'crf': '20', 'preset': 'veryfast'}

            def write(image):
                nonlocal written
                frame = av.VideoFrame.from_image(image)
                frame.pts = written
                frame.time_base = Fraction(1, fps)
                written += 1
                for packet in stream.encode(frame):
                    container.mux(packet)

            for path in clip_paths:
                frames = iter(_clip_frames(path, fps, target_size))
                head = []
                if tail:
                    for _ in range(len(tail)):
                        image = next(frames, None)
                        if image is None:
                            break
                        head.append(image)
                    overlap = len(head)
                    while len(tail) > overlap:
                        write(tail.popleft())
                    tail = deque(Image.blend(image, head[i], (i + 1) / (overlap + 1))
                                 for i, image in enumerate(tail))
                for image in frames:
                    tail.append(image)
                    if len(tail) > transition_frames:
                        write(tail.popleft())
            while tail:
                write(tail.popleft())
            for packet in stream.encode():
                container.mux(packet)
        temporary.replace(out)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {'path': str(out), 'clips_count': len(clip_paths), 'duration': written / fps,
            'fps': fps, 'resolution': target_size}

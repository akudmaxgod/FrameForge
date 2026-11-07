import io
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False

KEN_BURNS_PRESETS = {
    'gentle_push_in': {'scale_start':1.0,'scale_end':1.12,'pan_x_start':0,'pan_x_end':0,'pan_y_start':0,'pan_y_end':0},
    'gentle_pull_back': {'scale_start':1.12,'scale_end':1.0,'pan_x_start':0,'pan_x_end':0,'pan_y_start':0,'pan_y_end':0},
    'gentle_pan_left': {'scale_start':1.10,'scale_end':1.10,'pan_x_start':0.07,'pan_x_end':-0.07,'pan_y_start':0,'pan_y_end':0},
    'gentle_pan_right': {'scale_start':1.10,'scale_end':1.10,'pan_x_start':-0.07,'pan_x_end':0.07,'pan_y_start':0,'pan_y_end':0},
    'portrait_push_in': {
        'scale_start': 1.05,
        'scale_end': 1.15,
        'pan_x_start': 0.0,
        'pan_x_end': 0.0,
        'pan_y_start': -0.02,
        'pan_y_end': -0.04
    },
    'subject_parallax': {
        'scale_start': 1.06,
        'scale_end': 1.14,
        'pan_x_start': -0.03,
        'pan_x_end': 0.03,
        'pan_y_start': 0.0,
        'pan_y_end': -0.01
    },
    'slow_panorama': {
        'scale_start': 1.12,
        'scale_end': 1.12,
        'pan_x_start': -0.05,
        'pan_x_end': 0.05,
        'pan_y_start': 0.0,
        'pan_y_end': 0.0
    },
    'object_reveal': {
        'scale_start': 1.04,
        'scale_end': 1.14,
        'pan_x_start': 0.0,
        'pan_x_end': 0.0,
        'pan_y_start': 0.02,
        'pan_y_end': 0.0
    },
    'balanced_two_shot': {
        'scale_start': 1.08,
        'scale_end': 1.14,
        'pan_x_start': 0.02,
        'pan_x_end': -0.02,
        'pan_y_start': 0.0,
        'pan_y_end': 0.0
    },
    'slow_pull_back': {
        'scale_start': 1.14,
        'scale_end': 1.04,
        'pan_x_start': 0.0,
        'pan_x_end': 0.0,
        'pan_y_start': 0.0,
        'pan_y_end': 0.0
    },
    'slow_zoom_in': {
        'scale_start': 1.02,
        'scale_end': 1.14,
        'pan_x_start': 0.0,
        'pan_x_end': 0.0,
        'pan_y_start': 0.0,
        'pan_y_end': 0.0
    },
    'slow_zoom_out': {
        'scale_start': 1.14,
        'scale_end': 1.02,
        'pan_x_start': 0.0,
        'pan_x_end': 0.0,
        'pan_y_start': 0.0,
        'pan_y_end': 0.0
    }
}


def calculate_safe_crop(width, height, preset_name='slow_zoom_in',
                        overscan=0.15, max_pan_x=0.08, max_pan_y=0.05):
    """Verify that camera trajectory never exposes source image borders or black bars.
    
    If trajectory exceeds image boundaries at any sampled keyframe, automatically switches
    to the safe fallback 'slow_zoom_in'.
    """
    preset = KEN_BURNS_PRESETS.get(preset_name, KEN_BURNS_PRESETS['slow_zoom_in'])
    base_scale = 1.0 + overscan

    # Check keyframe boundary safety
    edge_safe = True
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        scale = preset['scale_start'] + t * (preset['scale_end'] - preset['scale_start'])
        total_scale = base_scale * scale
        crop_w = width / total_scale
        crop_h = height / total_scale

        pan_x = preset['pan_x_start'] + t * (preset['pan_x_end'] - preset['pan_x_start'])
        pan_y = preset['pan_y_start'] + t * (preset['pan_y_end'] - preset['pan_y_start'])

        # Enforce maximum safe pan constraints
        if abs(pan_x) > max_pan_x or abs(pan_y) > max_pan_y:
            edge_safe = False
            break

        center_x = (width / 2.0) + pan_x * (width / 2.0)
        center_y = (height / 2.0) + pan_y * (height / 2.0)

        x1 = center_x - crop_w / 2.0
        x2 = center_x + crop_w / 2.0
        y1 = center_y - crop_h / 2.0
        y2 = center_y + crop_h / 2.0

        if x1 < 0.0 or x2 > width or y1 < 0.0 or y2 > height:
            edge_safe = False
            break

    actual_preset = preset_name if edge_safe else 'slow_zoom_in'
    return {
        'overscan': overscan,
        'max_pan_x': max_pan_x,
        'max_pan_y': max_pan_y,
        'edge_safe': edge_safe,
        'fallback': 'slow_zoom_in',
        'actual_preset': actual_preset
    }


def _ken_burns_sequence(image, preset_name='slow_zoom_in', duration=5.0, fps=24,
                            target_size=(1280, 720), overscan=0.15, workers=1):
    """Generate PIL frames for a Ken Burns camera move with guaranteed edge safety."""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image)).convert('RGB')
    else:
        image = image.convert('RGB')

    if preset_name.startswith('gentle_'):
        overscan = 0
    w, h = image.size
    crop_info = calculate_safe_crop(w, h, preset_name, overscan=overscan)
    preset = KEN_BURNS_PRESETS[crop_info['actual_preset']]
    base_scale = 1.0 + overscan

    total_frames = max(1, int(round(duration * fps)))
    def render_frame(i):
        # A steady camera speed avoids the repeated accelerate/brake pulse.
        t = i / max(1, total_frames - 1)

        scale = preset['scale_start'] + t * (preset['scale_end'] - preset['scale_start'])
        total_scale = base_scale * scale
        crop_w = w / total_scale
        crop_h = h / total_scale

        pan_x = preset['pan_x_start'] + t * (preset['pan_x_end'] - preset['pan_x_start'])
        pan_y = preset['pan_y_start'] + t * (preset['pan_y_end'] - preset['pan_y_start'])

        center_x = (w / 2.0) + pan_x * (w / 2.0)
        center_y = (h / 2.0) + pan_y * (h / 2.0)

        x1 = max(0.0, min(w - crop_w, center_x - crop_w / 2.0))
        y1 = max(0.0, min(h - crop_h, center_y - crop_h / 2.0))
        x2 = min(w, x1 + crop_w)
        y2 = min(h, y1 + crop_h)

        # The specialized separable resize kernel is substantially faster
        # than the general per-pixel affine transform. A FLOAT source box is
        # essential: an integer crop would quantize slow camera motion into
        # visible jumps. Pillow handles the fractional sampling origin here.
        frame = image.resize(target_size, Image.Resampling.BICUBIC,
                             box=(x1, y1, x2, y2))
        return frame

    def frames():
        if workers <= 1 or total_frames == 1:
            for i in range(total_frames):
                yield render_frame(i)
            return
        # Two read-only transforms run in native Pillow code. Retain at most
        # two futures and emit frames in their original order, never a full clip.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='frameforge-2.5d') as pool:
            indices=iter(range(total_frames))
            pending=deque(pool.submit(render_frame,i) for i in range(min(2,total_frames)))
            for _ in range(min(2,total_frames)):
                next(indices)
            while pending:
                frame=pending.popleft().result()
                index=next(indices,None)
                if index is not None:
                    pending.append(pool.submit(render_frame,index))
                yield frame

    return frames(), crop_info


def render_ken_burns_frames(image, preset_name='slow_zoom_in', duration=5.0, fps=24,
                            target_size=(1280, 720), overscan=0.15):
    frames, crop_info = _ken_burns_sequence(image, preset_name, duration, fps, target_size, overscan)
    return list(frames), crop_info


def _render_ken_burns_video(image_bytes_or_path, output_path, preset_name='slow_zoom_in',
                           duration=5.0, fps=24, target_size=(1280, 720), encoder="libx264"):
    """Stream a local camera move into H.264, retaining a few frames only."""
    if isinstance(image_bytes_or_path, (str, Path)):
        with open(image_bytes_or_path, 'rb') as f:
            image_bytes = f.read()
    else:
        image_bytes = image_bytes_or_path

    frames, crop_info = _ken_burns_sequence(
        image_bytes, preset_name=preset_name, duration=duration, fps=fps, target_size=target_size,
        workers=2 if (os.cpu_count() or 1) >= 4 else 1
    )

    written = 0
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if HAS_AV:
        container = av.open(str(out_p), mode='w', format='mp4')
        stream = container.add_stream(encoder, rate=fps)
        stream.width = target_size[0]
        stream.height = target_size[1]
        stream.pix_fmt = 'yuv420p'
        stream.options = ({'preset':'p4','cq':'20','rc':'vbr','b':'0'} if encoder=='h264_nvenc' else {'crf':'20','preset':'veryfast'})

        try:
            for pil_frame in frames:
                written += 1
                frame = av.VideoFrame.from_image(pil_frame)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        finally:
            frames.close()
            container.close()
    else:
        raise RuntimeError('Для 2.5D требуется PyAV. Установите зависимости приложения.')

    return {
        'path': str(out_p),
        'duration': duration,
        'fps': fps,
        'frames': written,
        'renderer_version': 4,
        'encoder': encoder,
        'safe_crop': crop_info
    }


_gpu_available = None
_gpu_lock = __import__('threading').Lock()
_gpu_slots = __import__('threading').BoundedSemaphore(2)

def render_ken_burns_video(image_bytes_or_path, output_path, preset_name='slow_zoom_in',
                           duration=5.0, fps=24, target_size=(1280,720)):
    """Prefer NVENC; retain a software path on machines without NVIDIA."""
    global _gpu_available
    args=(image_bytes_or_path,output_path,preset_name,duration,fps,target_size)
    with _gpu_slots:
        if _gpu_available is None:
            with _gpu_lock:
                if _gpu_available is None:
                    # Probe the encoder independently of the user's source.
                    import tempfile
                    try:
                        with tempfile.TemporaryDirectory() as directory:
                            p=Path(directory)/'probe.mp4'
                            with av.open(str(p),'w') as c:
                                stream=c.add_stream('h264_nvenc',rate=24)
                                stream.width=256;stream.height=256;stream.pix_fmt='yuv420p'
                                for packet in stream.encode(av.VideoFrame.from_image(Image.new('RGB',(256,256)))):c.mux(packet)
                                for packet in stream.encode():c.mux(packet)
                        _gpu_available=True
                    except Exception:
                        _gpu_available=False
        if _gpu_available:
            try:
                return _render_ken_burns_video(*args,encoder='h264_nvenc')
            except Exception:
                import logging
                logging.getLogger(__name__).exception('NVENC render failed; retrying local clip with software encoder')
        return _render_ken_burns_video(*args,encoder='libx264')


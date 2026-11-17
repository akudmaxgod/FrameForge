"""GPU effects for finished video using OpenCV's OpenCL backend."""
import numpy as np
from PIL import Image

from .montage_effects import _light, apply_effects


class VideoEffectsGPU:
    def __init__(self, cv, size, lut, effects):
        self.cv = cv
        self.lut = cv.UMat(np.asarray(lut[:256], dtype=np.uint8)) if lut else None
        self.grain = []
        if 'grain' in effects:
            neutral = Image.new('RGB', size, (128, 128, 128))
            for index in range(8):
                sample = np.asarray(apply_effects(neutral, {'old_film': 48}, index))
                delta = sample.astype(np.int16) - 128
                self.grain.append(tuple(cv.UMat(np.maximum(0, sign * delta).astype(np.uint8))
                                        for sign in (1, -1)))
        self.flare = None
        if 'leaks' in effects:
            light, mask = _light(size)
            self.flare = (cv.UMat(np.ascontiguousarray(np.asarray(light)[:, :, ::-1])),
                          cv.UMat(np.asarray(mask, dtype=np.float32) / 255))

    @classmethod
    def create(cls, size, lut, effects):
        try:
            import cv2
        except ImportError:
            return None
        try:
            cv2.ocl.setUseOpenCL(True)
            if cv2.ocl.useOpenCL() and not cv2.ocl.Device_getDefault().type() & cv2.ocl.DEVICE_TYPE_CPU:
                renderer = cls(cv2, size, lut, effects)
                renderer.frame(np.zeros((size[1], size[0], 3), dtype=np.uint8), 0, 0, 20)
                return renderer
        except (cv2.error, RuntimeError, AttributeError):
            pass
        return None

    def frame(self, pixels, rotation, index, leak):
        cv = self.cv
        frame = cv.UMat(pixels)
        turns = rotation % 360
        if turns:
            frame = cv.rotate(frame, {90: cv.ROTATE_90_COUNTERCLOCKWISE,
                                     180: cv.ROTATE_180,
                                     270: cv.ROTATE_90_CLOCKWISE}[turns])
        if self.lut is not None:
            frame = cv.LUT(frame, self.lut)
            gray = cv.cvtColor(cv.cvtColor(frame, cv.COLOR_BGR2GRAY), cv.COLOR_GRAY2BGR)
            frame = cv.addWeighted(frame, 1.025, gray, -.025, 0)
        if leak and self.flare:
            light, mask = self.flare
            strength = leak / 100 * (.45 + .55 * (.5 + .5 * np.sin(index * .055)) ** 3)
            opacity = cv.multiply(mask, strength)
            frame = cv.blendLinear(frame, light, cv.subtract(1., opacity), opacity)
        if self.grain:
            add, subtract = self.grain[index % len(self.grain)]
            frame = cv.subtract(cv.add(frame, add), subtract)
        return cv.cvtColor(frame, cv.COLOR_BGR2YUV_I420).get()

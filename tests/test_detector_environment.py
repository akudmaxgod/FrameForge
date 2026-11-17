import io
import builtins
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from imagegen.service import Service
from imagegen.worker import Worker, DetectorUnavailableError, ContentRejectedError


class DetectorEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = Worker(Service(self.root), runtime=SimpleNamespace(backends=self.root / 'backends'))
        self.model = self.root / 'backends/ComfyUI/test/models/ultralytics/segm/person_yolov8m-seg.pt'
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b'fake model; never loaded')
        self.python = self.root / 'backends/comfy-venv/Scripts/python.exe'
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b'fake executable; subprocess is mocked')
        self.image = Image.new('RGB', (128, 72), 'gray')

    def test_missing_ui_package_uses_bundled_environment_and_preserves_threshold(self):
        original = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'ultralytics':
                raise ModuleNotFoundError("No module named 'ultralytics'", name='ultralytics')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=missing), \
             patch.object(self.worker, '_bundled_detector_count', return_value=2) as count:
            self.assertEqual(self.worker._detected_person_count(self.image, .65), 2)
        count.assert_called_once_with(self.image, self.model, .65)

    def test_subprocess_result_is_cached_and_input_is_removed(self):
        response = subprocess.CompletedProcess([], 0, 'model log\nFF_DETECTOR_RESULT:{"count":2}\n', '')
        with patch('imagegen.worker.subprocess.run', return_value=response) as run:
            self.assertEqual(self.worker._bundled_detector_count(self.image, self.model, .4), 2)
            self.assertEqual(self.worker._bundled_detector_count(self.image, self.model, .4), 2)
        self.assertEqual(run.call_count, 1)
        args = run.call_args.args[0]
        self.assertEqual(args[0], str(self.python))
        self.assertEqual(run.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(list((self.root / 'detector-inputs').glob('*')), [])

    def test_failure_is_infrastructure_error_not_rejected_content_and_keeps_cleanup(self):
        for response in (subprocess.CompletedProcess([], 1, '', 'missing library'),
                         subprocess.CompletedProcess([], 0, 'FF_DETECTOR_RESULT:{"count":true}', '')):
            with patch('imagegen.worker.subprocess.run', return_value=response):
                with self.assertRaises(DetectorUnavailableError) as caught:
                    self.worker._bundled_detector_count(self.image, self.model, .4)
                self.assertNotIsInstance(caught.exception, ContentRejectedError)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(caught.exception.details['code'], 'DETECTOR_UNAVAILABLE')
            self.assertEqual(list((self.root / 'detector-inputs').glob('*')), [])


if __name__ == '__main__':
    unittest.main()

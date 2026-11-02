"""Own the bundled local inference processes and never expose them off loopback."""
import logging
import os
import subprocess
import time
import secrets
import re
from pathlib import Path

from .providers import JsonHTTP, ProviderError

log = logging.getLogger(__name__)


class RuntimeManager:
    def __init__(self, application_root, data_root=None):
        self.root = Path(application_root).resolve()
        from .release_paths import component_root
        self.backends = component_root() / 'backends'
        self.data_root = Path(data_root or self.root / 'data').resolve()
        self.comfy = None
        self.text = None
        self.log_handles = []
        key_path = self.data_root / 'runtime-api-key'
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.is_file():
            self.text_key = key_path.read_text(encoding='utf-8').strip()
        else:
            self.text_key = secrets.token_urlsafe(32)
            temporary = key_path.with_suffix('.tmp')
            temporary.write_text(self.text_key, encoding='utf-8')
            temporary.replace(key_path)
        if len(self.text_key) < 32:
            raise ProviderError('Повреждён локальный ключ backend. Удалите data/runtime-api-key и перезапустите приложение.')

    @property
    def _text_pid_path(self):
        return self.data_root / 'llama-server.pid'

    @property
    def _comfy_pid_path(self):
        return self.data_root / 'comfyui.pid'

    def _wait(self, url, path, process, label, timeout=180, headers=None):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ProviderError(f'{label} завершился с кодом {process.returncode}. Проверьте журнал backend.')
            try:
                return JsonHTTP(url, 3, headers).json(path)
            except ProviderError as error:
                last_error = error
                time.sleep(1)
        raise ProviderError(f'{label} не запустился за {timeout} секунд: {last_error}')

    def _stop(self, name):
        process = getattr(self, name)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        setattr(self, name, None)
        if name == 'text' and process is not None:
            self._text_pid_path.unlink(missing_ok=True)
        if name == 'comfy' and process is not None:
            self._comfy_pid_path.unlink(missing_ok=True)

    def stop_all(self):
        self._stop('text')
        self._stop('comfy')
        for handle in self.log_handles:
            handle.close()
        self.log_handles.clear()

    def _launch(self, command, cwd, logfile, env=None):
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handle = logfile.open('a', encoding='utf-8')
        self.log_handles.append(handle)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return subprocess.Popen(command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
                                creationflags=flags, env=env or os.environ.copy())

    def ensure_text(self, settings):
        """DeepSeek runs as a cloud API; no local text server process is needed."""
        return

    def ensure_comfy(self, settings):
        if settings.get('local_image_generation_enabled') is False:
            raise ProviderError('Локальный генератор удалён. Выберите Google Flow.')
        self._stop('text')
        if self.comfy and self.comfy.poll() is None:
            return
        candidates = sorted((self.backends / 'ComfyUI').glob('Comfy-Org-ComfyUI-*'))
        python = self.backends / 'comfy-venv' / 'Scripts' / 'python.exe'
        config = self.backends / 'extra_model_paths.yaml'
        if not candidates or not python.is_file():
            raise ProviderError('ComfyUI не установлен полностью. Запустите Install.ps1.')
        model_root = (self.backends / 'models').as_posix()
        config.write_text(
            f'image_generator_models:\n  base_path: "{model_root}"\n'
            '  diffusion_models: diffusion_models\n  text_encoders: text_encoders\n  vae: vae\n',
            encoding='utf-8')
        source = candidates[-1]
        environment = os.environ.copy()
        # Ultralytics otherwise writes to %APPDATA%, which is not available to a
        # packaged/sandboxed application. Keep all detector state with our data.
        yolo_config = self.data_root / 'ultralytics'
        yolo_config.mkdir(parents=True, exist_ok=True)
        environment['YOLO_CONFIG_DIR'] = str(yolo_config)
        self.comfy = self._launch([
            str(python), str(source / 'main.py'), '--listen', '127.0.0.1', '--port', '8188',
            '--disable-auto-launch', '--lowvram', '--preview-method', 'none',
            '--extra-model-paths-config', str(config)
        ], source, self.data_root / 'comfyui.log', environment)
        temporary_pid = self._comfy_pid_path.with_suffix('.tmp')
        temporary_pid.write_text(str(self.comfy.pid), encoding='utf-8')
        temporary_pid.replace(self._comfy_pid_path)
        self._wait(settings['comfy_url'], '/system_stats', self.comfy, 'ComfyUI')

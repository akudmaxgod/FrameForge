"""Reuse an existing local instance before loading models or starting workers."""
import argparse
import json
import logging
from pathlib import Path
import sys
import urllib.error
import urllib.request
import webbrowser

from .instance import AlreadyRunningError, InstanceLock


def open_running(error, requested_port, open_browser):
    port = error.port or requested_port
    url = f'http://127.0.0.1:{port}'
    try:
        request = urllib.request.Request(url + '/api/editorial-capabilities')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            capabilities = json.loads(response.read(4096))
        if not isinstance(capabilities, dict) or capabilities.get('scoped_scene_batches') is not True:
            raise ValueError('Unrecognized application')
    except (OSError, urllib.error.URLError, ValueError) as error:
        logging.getLogger(__name__).warning('Existing instance check on port %s failed: %s: %s', port, type(error).__name__, error, exc_info=True)
        print('Программа уже запущена, но её интерфейс пока недоступен. Подождите завершения запуска и повторите открытие.', flush=True)
        return 1
    print(f'Программа уже работает: {url}', flush=True)
    if open_browser:
        webbrowser.open(url)
    return 0


def reuse_running(root, port, open_browser):
    lock_path = Path(root) / 'worker.lock'
    if not lock_path.exists():
        return 10  # No instance: proceed with normal startup.
    try:
        with InstanceLock(lock_path):
            return 10
    except AlreadyRunningError as error:
        return open_running(error, port, open_browser)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    try:
        return reuse_running(args.data, args.port, args.open)
    except OSError:
        print('Не удалось проверить папку данных программы. Проверьте доступ к ней.', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())

import argparse
import logging
import threading
import webbrowser
import sys
from pathlib import Path
from .instance import AlreadyRunningError, InstanceLock
from .launch import open_running


def main():
    parser = argparse.ArgumentParser(description='Local Story Image Generator')
    parser.add_argument('--data', default=None)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='Open the local interface in a browser')
    args = parser.parse_args()
    from .release_paths import data_root
    root = data_root(args.data)
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(root / 'app.log', encoding='utf-8')])
    session_marker = root / 'active-session.marker'
    try:
        instance = InstanceLock(root / 'worker.lock')
    except AlreadyRunningError as error:
        return open_running(error, args.port, args.open)
    with instance:
        from .server import Application, make_server
        interrupted = session_marker.exists()
        session_marker.write_text('running', encoding='utf-8')
        app = Application(root)
        try:
            server = make_server(app, args.port)
        except OSError:
            session_marker.unlink(missing_ok=True)
            print(f'Ошибка: порт {args.port} уже занят или недоступен. Закройте прежний экземпляр или выберите другой порт.', file=sys.stderr, flush=True)
            return 1
        instance.record_port(server.server_port)
        app.worker.start(interrupted=interrupted)
        app.voice.start_worker()
        app.google_queue_worker.start()
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'Image Generator: {url}', flush=True)
        if args.open:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        except Exception:
            logging.exception('Fatal server exception in serve_forever')
            raise
        finally:
            app.google_queue_worker.stop()
            app.voice.stop()
            app.worker.stop()
            from .media_analysis import LlamaServerManager
            if LlamaServerManager._instance is not None:
                LlamaServerManager._instance.stop()
            app.runtime.stop_all()
            server.server_close()
            session_marker.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())

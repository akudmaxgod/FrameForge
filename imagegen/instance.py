"""An OS-released process lock prevents two workers from sharing one data directory."""
import os
import json


class AlreadyRunningError(RuntimeError):
    def __init__(self, path):
        self.port = None
        try:
            with open(path, 'rb') as record:
                record.seek(1)  # Windows locks the first byte only.
                value = json.loads(record.read(200))
                port = value.get('port')
                if type(port) is int and 1 <= port <= 65535:
                    self.port = port
        except (OSError, ValueError, AttributeError):
            pass  # Older instances have a one-byte lock file.
        super().__init__('Эта папка данных уже используется другим экземпляром приложения.')


class InstanceLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise AlreadyRunningError(path) from error

    def record_port(self, port):
        self.file.seek(1)
        self.file.truncate()
        self.file.write(json.dumps({'port': port}).encode('utf-8'))
        self.file.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

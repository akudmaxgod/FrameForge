"""Offline release verification and side-by-side update preparation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from uuid import uuid4

from .instance import InstanceLock
from .release_paths import install_root, profile


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_atomic(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'SHA256SUMS.json').read_text(encoding='utf-8'))
    if not isinstance(manifest.get('files'), dict) or not manifest['files']:
        raise ValueError('Пустой манифест выпуска.')
    for name, expected in manifest['files'].items():
        path = root / name
        if Path(name).is_absolute() or ':' in name or '\\' in name or '..' in Path(name).parts:
            raise ValueError('Небезопасный путь в манифесте.')
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ValueError(f'Отсутствует файл выпуска: {name}')
        if sha256(path) != expected:
            raise ValueError(f'Повреждён файл выпуска: {name}')
    import ssl
    context = ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError('Проверка TLS-сертификатов отключена.')
    return {'ok': True, 'version': manifest['version'], 'verified_files': len(manifest['files']), 'tls_context_ready': True}


def prepare_update(previous, destination=None, previous_data=None):
    """No overwrites of previous code/data. Back up offline before binding new code."""
    destination = Path(destination or install_root()).resolve()
    previous = Path(previous).resolve(strict=True)
    verified = verify(destination)
    if destination == previous:
        raise ValueError('Распакуйте новую версию в отдельную папку.')
    config = profile(previous)
    data = Path(previous_data or config.get('data_path') or previous / 'data').resolve(strict=True)
    if not (data / 'app.sqlite').is_file():
        raise ValueError('Не найдена база данных старой версии. Укажите --previous-data.')
    if destination.is_relative_to(data) or previous.is_relative_to(data):
        raise ValueError('Папка программы не должна находиться внутри пользовательских данных.')
    if (destination / '.frameforge-profile.json').exists():
        raise ValueError('Эта новая папка уже настроена. Используйте новую распакованную копию.')
    # Junctions/symlinks can escape the backup and recurse forever.
    for path in data.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('В данных есть ссылка/junction. Создайте проверенную резервную копию вручную.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    backup = destination.parent / 'FrameForge-backups' / stamp
    if backup.is_relative_to(data) or data.is_relative_to(backup):
        raise ValueError('Резервная копия должна находиться за пределами папки данных.')
    total = sum(p.stat().st_size for p in data.rglob('*') if p.is_file())
    if shutil.disk_usage(destination.parent).free < total + 256 * 1024 * 1024:
        raise ValueError('Недостаточно места для полной резервной копии данных.')
    # All supported application versions take this exact OS lock while running.
    with InstanceLock(data / 'worker.lock'):
        backup.mkdir(parents=True)
        ignore = shutil.ignore_patterns('worker.lock', 'active-session.marker')
        shutil.copytree(data, backup / 'data', ignore=ignore)
        for source in data.rglob('*'):
            if source.is_file() and source.name not in {'worker.lock', 'active-session.marker'}:
                copy = backup / 'data' / source.relative_to(data)
                if sha256(source) != sha256(copy):
                    raise ValueError('Резервная копия не прошла проверку. Новая версия не подключена.')
        components = str(Path(config.get('components_path') or previous).resolve())
        new_config = {'data_path': str(data), 'components_path': components,
                      'env_file': str(Path(config.get('env_file') or previous / '.env').resolve()),
                      'previous_installation': str(previous), 'backup_path': str(backup),
                      'target_version': verified['version']}
        write_atomic(backup / 'backup-info.json', {'data_path':str(data), 'target_version':verified['version'],
                                                  'created_at':stamp, 'verified':True})
        write_atomic(destination / '.frameforge-profile.json', new_config)
    return {'ok':True, 'backup':str(backup), 'data_path':str(data),
            'message':'Обновление подготовлено. Запустите Start.cmd в НОВОЙ папке. Старую папку и резервную копию сохраните.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='FrameForge offline release tools')
    parser.add_argument('--verify-release', action='store_true')
    parser.add_argument('--prepare-update', type=Path)
    parser.add_argument('--previous-data', type=Path)
    args = parser.parse_args(argv)
    try:
        result = (prepare_update(args.prepare_update, previous_data=args.previous_data)
                  if args.prepare_update else verify(install_root()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f'FrameForge: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())


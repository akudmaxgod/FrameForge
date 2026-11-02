"""Installation paths, separated from persistent user data and optional runtimes."""
import json
import os
from pathlib import Path
import sys


def install_root():
    return (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1])


def profile(root=None):
    path = Path(root or install_root()) / '.frameforge-profile.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('Некорректный профиль установки FrameForge.')
    return data


def component_root():
    return Path(os.environ.get('FRAMEFORGE_COMPONENTS') or profile().get('components_path') or install_root()).resolve()


def data_root(explicit=None):
    return Path(explicit or os.environ.get('FRAMEFORGE_DATA') or profile().get('data_path') or install_root() / 'data').resolve()


def configure():
    root = install_root()
    config = profile(root)
    os.environ.setdefault('FRAMEFORGE_COMPONENTS', str(component_root()))
    # Optional external voice Python needs source modules even in a frozen build.
    os.environ['PYTHONPATH'] = str(root)
    from dotenv import load_dotenv
    for env_file in (config.get('env_file'), root / '.env'):
        if env_file and Path(env_file).is_file():
            load_dotenv(env_file, override=False, encoding='utf-8-sig')


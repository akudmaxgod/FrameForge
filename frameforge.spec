# PyInstaller onedir Windows x64. Build from this directory using Build.ps1.
from pathlib import Path
import ssl
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / 'imagegen/static'), 'imagegen/static')]
# This Python toolchain's ssl module loads a companion public trust-policy PEM.
# Preserve its TLS verification behavior without depending on the build machine.
for certificate in Path(ssl.__file__).parent.glob('codex-*.pem'):
    datas.append((str(certificate), '.'))
for package in ('natasha', 'pymorphy3_dicts_ru', 'pymorphy2_dicts_ru', 'certifi'):
    datas += collect_data_files(package)
for package in ('Pillow', 'av', 'numpy', 'openai', 'pydantic', 'natasha', 'pymorphy3'):
    datas += copy_metadata(package)
a = Analysis([str(root / 'frameforge_launcher.py')], pathex=[str(root)],
    binaries=[], datas=datas, hiddenimports=collect_submodules('imagegen'),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['torch', 'ultralytics', 'insightface', 'faster_whisper',
              'tkinter', 'matplotlib', 'scipy', 'pytest', 'IPython', 'notebook'],
    noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FrameForge',
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FrameForge')


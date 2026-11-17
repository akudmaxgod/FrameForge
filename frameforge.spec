# PyInstaller onedir Windows x64. Build from this directory using Build.ps1.
from pathlib import Path
import ssl
import site
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / 'imagegen/static'), 'imagegen/static')]+collect_data_files('cupy')
# This Python toolchain's ssl module loads a companion public trust-policy PEM.
# Preserve its TLS verification behavior without depending on the build machine.
for certificate in Path(ssl.__file__).parent.glob('codex-*.pem'):
    datas.append((str(certificate), '.'))
for package in ('natasha', 'pymorphy3_dicts_ru', 'pymorphy2_dicts_ru', 'certifi'):
    datas += collect_data_files(package)
for package in ('Pillow', 'av', 'numpy', 'openai', 'pydantic', 'natasha', 'pymorphy3'):
    datas += copy_metadata(package)
cuda_binaries=[]
for folder in site.getsitepackages():
    nvidia=Path(folder)/'nvidia'
    if nvidia.is_dir():
        for file in nvidia.rglob('*'):
            if not file.is_file() or '__pycache__' in file.parts:continue
            destination='nvidia/'+str(file.parent.relative_to(nvidia)).replace('\\','/')
            if file.suffix.lower()=='.dll':cuda_binaries.append((str(file),destination))
            elif file.suffix.lower() in ('.h','.hpp','.cuh'):datas.append((str(file),destination))

a = Analysis([str(root / 'frameforge_launcher.py')], pathex=[str(root)],
    binaries=cuda_binaries, datas=datas, hiddenimports=['graphlib']+collect_submodules('imagegen')+collect_submodules('cupy')+collect_submodules('cupy_backends')+collect_submodules('cuda.pathfinder'),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['torch', 'ultralytics', 'insightface', 'faster_whisper',
              'tkinter', 'matplotlib', 'scipy', 'pytest', 'IPython', 'notebook'],
    noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FrameForge',
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FrameForge')


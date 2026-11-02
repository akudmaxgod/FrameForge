"""Create manifests and ZIPs without copying any local user data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def hash_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(root):
    data={'version':(ROOT/'VERSION').read_text().strip(), 'algorithm':'sha256',
          'files':{p.relative_to(root).as_posix():hash_file(p)
                   for p in sorted(root.rglob('*')) if p.is_file() and p.name!='SHA256SUMS.json'
                   and '__pycache__' not in p.parts}}
    (root/'SHA256SUMS.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--portable',type=Path);args=parser.parse_args()
    version=(ROOT/'VERSION').read_text().strip()
    destination=ROOT.parent/'artifacts';destination.mkdir(exist_ok=True)
    # Explicit allowlist: this script can also run after a local installation.
    names=['imagegen','integrations','scripts','tests','docs','requirements.txt',
           'requirements-build.txt','requirements-voice.txt','requirements-semantic.txt',
           'requirements-face.txt','pyproject.toml','VERSION','release.json','frameforge.spec',
           'frameforge_launcher.py','README.md','CHANGELOG.md','THIRD_PARTY_NOTICES.md',
           'THIRD_PARTY_NOTICES-legacy.md','.gitignore','.gitattributes','.env.example','Launch.ps1',
           'Start.cmd','Install.ps1','Build.ps1','Prepare-Update.ps1','StopFrameForge.ps1',
           'InstallVoice.ps1','InstallSemanticFit.ps1','InstallQwen.ps1','components-lock.json',
           '.github']
    selected=[]
    for name in names:
        path=ROOT/name
        if not path.exists():continue
        selected.extend([p for p in path.rglob('*') if p.is_file()] if path.is_dir() else [path])
    selected=[p for p in selected if '__pycache__' not in p.parts and '.pytest_cache' not in p.parts and '.bak' not in p.name and p.suffix not in {'.pyc','.pyo'}]
    from audit_release import audit
    audit(ROOT,selected)
    source_manifest={'version':version,'algorithm':'sha256','files':{p.relative_to(ROOT).as_posix():hash_file(p) for p in sorted(selected)}}
    (ROOT/'SHA256SUMS.json').write_text(json.dumps(source_manifest,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(destination/f'FrameForge-{version}-source.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for p in selected+[ROOT/'SHA256SUMS.json']:
            archive.write(p,'FrameForge/'+p.relative_to(ROOT).as_posix())
    if args.portable:
        portable=args.portable.resolve()
        if not portable.is_relative_to(ROOT/'dist') or not (portable/'FrameForge.exe').is_file():
            raise ValueError('Unexpected portable output directory.')
        # Include source modules for separately installed voice workers and notices.
        for p in selected:
            rel=p.relative_to(ROOT)
            if rel.parts[0] in {'.github','tests'}:continue
            dest=portable/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
        audit(portable,[p for p in portable.rglob('*') if p.is_file()],binary=True)
        manifest(portable)
        with zipfile.ZipFile(destination/f'FrameForge-{version}-windows-x64.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(portable.rglob('*')):
                if p.is_file() and '__pycache__' not in p.parts:
                    archive.write(p,'FrameForge/'+p.relative_to(portable).as_posix())
    lines=[hash_file(p)+'  '+p.name for p in sorted(destination.glob('*.zip'))]
    (destination/'SHA256SUMS.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines))


if __name__=='__main__':main()


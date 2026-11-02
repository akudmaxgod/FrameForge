"""Fail closed on user data and obvious credentials; never print secret values."""
from pathlib import Path
import re
import sys


def audit(root,files,binary=False):
    problems=[]
    for path in files:
        rel=path.relative_to(root)
        if rel.parts[0].lower() in {'data','secrets','.git','.venv','node_modules','.script-analysis'}:
            problems.append((str(rel),'private runtime directory'))
        if path.name in {'.env','settings.json','app.sqlite','.frameforge-profile.json'} or '.bak' in path.name:
            problems.append((str(rel),'local configuration/data'))
        if not binary and path.stat().st_size>25*1024*1024:
            problems.append((str(rel),'unexpected large source file'))
        if path.suffix.lower() not in {'.py','.js','.mjs','.tsx','.json','.md','.txt','.ps1','.toml','.html','.yml'}:continue
        text=path.read_text(encoding='utf-8-sig',errors='replace')
        # Avoid matching empty example strings or synthetic runtime validation code.
        patterns=[r'\bsk-[A-Za-z0-9_-]{24,}',r'\bgh[pousr]_[A-Za-z0-9]{20,}',
                  r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                  r'(?i)(?:api_key|apikey|token|password)\s*[=:]\s*[\x22\x27][A-Za-z0-9_-]{24,}[\x22\x27]']
        if any(re.search(pattern,text) for pattern in patterns):
            problems.append((str(rel),'possible credential (value withheld)'))
        if re.search(r'C:[\\/]+Users[\\/]+Administrator[\\/]+Documents[\\/]+Codex',text,re.I):
            problems.append((str(rel),'machine-specific workspace path'))
    if problems:
        raise ValueError('Release audit failed: '+repr(problems))
    return {'checked_files':len(files),'issues':0}


if __name__=='__main__':
    root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
    print(audit(root,[p for p in root.rglob('*') if p.is_file() and not any(x in p.relative_to(root).parts for x in ('__pycache__','build','dist','.git'))]))

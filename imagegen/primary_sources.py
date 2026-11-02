"""Read inspected project sources without promoting context to frame identity.

Files are pinned to both source bytes and story bytes. This gallery is for
manual reference selection, not a model verdict or automatic frame approval.
"""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from .domain import fingerprint

VERSION = 'primary-source-context/1'


def sources(root, project):
    pid = project['id']
    if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', pid):
        return []
    folder = Path(root)/'data/primary_sources'/pid
    if folder.is_symlink() or folder.parent.is_symlink():
        return []
    try:
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if (not isinstance(manifest, dict) or manifest.get('version') != VERSION
            or manifest.get('project_id') != pid or not isinstance(manifest.get('sources'), list)):
        return []
    current = manifest.get('story_hash') == fingerprint(project['story'])
    result = []
    for item in manifest['sources']:
        if not isinstance(item, dict):
            continue
        filename = item.get('filename')
        kind = item.get('scope')
        expected_suffix = { 'architecture_context':{'.jpg','.jpeg','.png'}, 'document_context':{'.pdf'} }.get(kind, set()) if isinstance(kind, str) else set()
        if (not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', filename)
                or Path(filename).suffix.lower() not in expected_suffix
                or item.get('inspected') is not True
                or not all(isinstance(item.get(k),str) and item[k].strip() for k in ('title','limits','publisher'))
                or not isinstance(item.get('features'),list)
                or not all(isinstance(v,str) and v.strip() for v in item['features'])):
            continue
        try:
            urls = [urlparse(str(item.get(k,''))) for k in ('source_url','asset_source_url')]
        except ValueError:
            continue
        if any(u.scheme!='https' or not u.hostname or u.username or u.password for u in urls):
            continue
        try:
            file = folder/filename
            intact = (not file.is_symlink() and file.resolve().is_relative_to(folder.resolve())
                      and hashlib.sha256(file.read_bytes()).hexdigest()==item.get('sha256'))
        except OSError:
            intact = False
        state = 'source_changed' if not current else 'file_changed' if not intact else 'available'
        result.append({k:item[k] for k in ('title','limits','publisher','source_url','asset_source_url')} | {
            'scope':kind,'state':state,'features':item['features'] if state=='available' else [],
            'url':f'/data/primary_sources/{pid}/{filename}' if state=='available' else None,
            'image_sha256':item.get('sha256'), 'automatic_application':False,
            'target_frame_approved':False, 'historical_accuracy_verified':False,
            'identity_scope':'context_only'})
    return result

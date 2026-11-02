"""Explicit, scoped editorial records for primary-source reference images.

No model response or scene snapshot can register evidence. Inspection records
are local, bind exact image bytes, and establish only the named object/person.
They never approve a generated frame or the whole historical narration.
"""
import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from .db import now
from .domain import DomainError


class CheckedVisualEvidence:
    def __init__(self, root):
        self.root = Path(root) / 'checked_visual_evidence'

    def record(self, data, *, identity, aliases, scope, source_url, image_url, title, note, features, spatial_features=None, parent_binding=None):
        if (scope not in ('specific_person', 'specific_artifact', 'inscription')
                or not isinstance(data, bytes) or not data
                or not all(isinstance(value, str) and value.strip() for value in (identity, title, note))
                or not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases)
                or not isinstance(features, list) or not features or not all(isinstance(f, str) and f.strip() for f in features)):
            raise DomainError('Для эталона нужны точная идентичность, область подтверждения и результаты сверки.')
        for url in (source_url, image_url):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise DomainError('Нужны ссылки на первоисточник и его изображение.')
        spatial_features = spatial_features or []
        if (not isinstance(spatial_features, list) or any(not isinstance(f, dict)
                or type(f.get('feature_index')) is not int or not 0 <= f['feature_index'] < len(features)
                or f.get('image_side') not in ('left', 'right', 'center') for f in spatial_features)
                or len({f['feature_index'] for f in spatial_features}) != len(spatial_features)):
            raise DomainError('Некорректная пространственная привязка признака эталона.')
        digest = hashlib.sha256(data).hexdigest()
        value = {'version': 'checked-visual-evidence/1', 'image_sha256': digest,
                 'authority': 'checked_primary_source', 'scope': scope, 'identity': identity,
                 'aliases': list(dict.fromkeys([identity, *aliases])), 'source_url': source_url,
                 'image_url': image_url, 'title': title, 'inspection_note': note,
                 'supported_features': features, 'checked_at': now(),
                 'spatial_features': spatial_features,
                 'limits': 'Only the named reference identity and listed visible features; no target-frame approval.'}
        if parent_binding is not None:
            if (not isinstance(parent_binding, dict) or not all(isinstance(parent_binding.get(k), str)
                    and len(parent_binding[k]) == 64 for k in ('image_sha256', 'record_sha256'))):
                raise DomainError('Некорректное происхождение нормализованного эталона.')
            value['parent_binding'] = parent_binding
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f'{digest}.{os.getpid()}.{threading.get_ident()}.tmp'
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.root / f'{digest}.json')
        return value

    def match(self, data):
        digest = hashlib.sha256(data).hexdigest()
        try:
            value = json.loads((self.root / f'{digest}.json').read_text(encoding='utf-8'))
            if (value['version'] != 'checked-visual-evidence/1' or value['image_sha256'] != digest
                    or value['authority'] != 'checked_primary_source'
                    or value['scope'] not in ('specific_person', 'specific_artifact', 'inscription')
                    or not value['identity'].strip() or not value['aliases'] or not value['supported_features']
                    or urlparse(value['source_url']).scheme != 'https' or urlparse(value['image_url']).scheme != 'https'):
                return None
            parent = value.get('parent_binding')
            if parent:
                original=json.loads((self.root / (parent['image_sha256'] + '.json')).read_text(encoding='utf-8'))
                if self.binding(original) != parent:
                    return None
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def binding(record):
        return {'image_sha256':record['image_sha256'],
                'record_sha256':hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()}

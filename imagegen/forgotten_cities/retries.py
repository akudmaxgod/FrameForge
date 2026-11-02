"""Durable semantic retry budget; one image hash consumes at most one attempt."""
import hashlib
import json
from ..db import dump, now

MAX_CANDIDATES = 3


def retry_context(snapshot):
    scene = snapshot.get('scene') or {}
    return hashlib.sha256(dump({
        # UI export normalizes cast/presence fields; that must not silently start
        # a new budget compared with the upload or worker snapshot of this beat.
        'story': snapshot.get('story_hash'),
        'scene': {k: scene.get(k) for k in ('source_quote', 'source_start', 'source_end',
                  'source_fidelity_plan', 'manual_prompt')},
        'revision': snapshot.get('scene_revision'),
        'references': sorted(str(r.get('asset_id') or '') for r in snapshot.get('references') or []),
        'style': snapshot.get('style'), 'world': snapshot.get('world'),
        'channel_references': snapshot.get('channel_reference_hashes'),
    }).encode()).hexdigest()


def retry_prompt(base, retry):
    if not retry or not retry.get('correction'):
        return base
    # Highest-priority correction goes first so the Flow length cap cannot cut it off.
    return ('SOURCE FIDELITY CORRECTION (visual instructions, never printed in the image): '
            + retry['correction'][:1800]
            + '\nFollow the authoritative source, identities, time layer and required artifacts below. '
              'Do not replace the stated subject with a generic city view.\n' + base)


class RetryStore:
    def __init__(self, db):
        self.db = db

    def get(self, snapshot):
        if not snapshot.get('scene_id'):
            return {}
        row = self.db.one('SELECT state FROM scene_fidelity_runs WHERE scene_id=? AND context_hash=?',
                          (snapshot['scene_id'], retry_context(snapshot)))
        return json.loads(row['state']) if row else {}

    def reject(self, snapshot, verdict):
        sid = snapshot.get('scene_id')
        if not sid:
            return {'attempts': MAX_CANDIDATES, 'max_attempts': MAX_CANDIDATES, 'exhausted': True,
                    'reason': verdict['reason'], 'correction': verdict.get('correction', '')}
        context = retry_context(snapshot)
        with self.db.transaction() as conn:
            row = conn.execute('SELECT state FROM scene_fidelity_runs WHERE scene_id=? AND context_hash=?',
                               (sid, context)).fetchone()
            state = json.loads(row['state']) if row else {'rejected': []}
            image_hash = verdict['image_sha256']
            candidate_id = snapshot.get('fidelity_candidate_id') or image_hash
            if candidate_id not in [v.get('candidate_id', v['image_sha256']) for v in state['rejected']]:
                if len(state['rejected']) < MAX_CANDIDATES:
                    state['rejected'].append({**verdict, 'candidate_id': candidate_id, 'at': now()})
            state.update(attempts=len(state['rejected']), max_attempts=MAX_CANDIDATES,
                         exhausted=len(state['rejected']) >= MAX_CANDIDATES,
                         reason=verdict['reason'], correction=str(verdict.get('correction') or
                         'Depict the exact source subject and its stated condition; correct the contradiction: ' + verdict['reason']))
            conn.execute('INSERT OR REPLACE INTO scene_fidelity_runs VALUES(?,?,?)', (sid, context, dump(state)))
        return state

    def reset(self, sid):
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM scene_fidelity_runs WHERE scene_id=?', (sid,))

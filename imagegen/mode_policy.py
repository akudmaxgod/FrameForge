"""Single source of truth for persistent reference kinds by story mode."""
KINDS = {
    'characters': frozenset(('character','location','prop')),
    'dynamic': frozenset(('character',)),
    'illustration': frozenset(),
    'objects': frozenset(('character','location','prop')),
}

def allows(project, kind):
    return kind in KINDS.get(project.get('mode','characters'), KINDS['characters'])

def filter_references(project, references):
    result=[]
    for ref in references:
        kind = 'location' if ref.get('location_id') or ref.get('role')=='environment' else 'prop' if ref.get('prop_id') or ref.get('role')=='prop' else 'character' if ref.get('character_id') or ref.get('role')=='portrait' else None
        if kind is None or allows(project,kind):result.append(ref)
    return result

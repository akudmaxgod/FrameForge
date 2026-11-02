"""Literal object dimensions and states, independent of model-written framing."""
import re


NUMBERS = {
    'один': 1, 'одного': 1, 'одной': 1, 'два': 2, 'двух': 2,
    'три': 3, 'трех': 3, 'четыре': 4, 'четырех': 4, 'пять': 5, 'пяти': 5,
    'шесть': 6, 'шести': 6, 'семь': 7, 'семи': 7, 'восемь': 8, 'восьми': 8,
    'девять': 9, 'девяти': 9, 'десять': 10, 'десяти': 10,
}


def metric_dimensions(description):
    """Do not mistake dates, weights or an adjacent object's size for height."""
    pattern = (r'(?P<axis>высот\w*|длин\w*|ширин\w*)\s+(?:около\s+|примерно\s+)?'
               r'(?P<number>\d+(?:[.,]\d+)?|[а-яё]+)(?P<half>\s+с\s+половиной)?\s+'
               r'(?P<unit>миллиметр\w*|сантиметр\w*|метр\w*)')
    result = []
    for match in re.finditer(pattern, description, re.I):
        number = match['number'].casefold().replace('ё', 'е')
        value = float(number.replace(',', '.')) if number[:1].isdigit() else NUMBERS.get(number)
        if value is None:
            continue
        value += .5 if match['half'] else 0
        factor = .001 if match['unit'].casefold().startswith('милли') else .01 if match['unit'].casefold().startswith('санти') else 1
        axis = 'height' if match['axis'].casefold().startswith('высот') else 'length' if match['axis'].casefold().startswith('длин') else 'width'
        result.append({'axis': axis, 'metres': value * factor, 'quote': match.group()})
    return result


def artifact_evidence(names, story):
    from .source_inventory import inventory, normal_name
    _, objects = inventory(story)
    requested = {normal_name(name) for name in names}
    selected = []
    for item in objects:
        if normal_name(item['name']) not in requested:
            continue
        item = dict(item)
        end = item['evidence'][0].end
        following = story[end:end + 600].lstrip().split('\n\n')[0]
        faces = re.findall(r'(?:^|(?<=[.!?]))\s*(На\s+(?:одной\s+стороне|другой)[^.!?]*[.!?]?)', following, re.I)
        item['description'] += ' ' + ' '.join(face for face in faces if face not in item['description'])
        selected.append(item)
    return selected


def artifact_rules(names, story):
    rules = []
    for item in artifact_evidence(names, story):
        for dimension in metric_dimensions(item['description']):
            size = dimension['metres']
            rule = f"PHYSICAL SCALE: {item['name']} has source-stated {dimension['axis']} about {size:g} metres. "
            if dimension['axis'] == 'height' and size >= 3:
                rule += f"It is approximately {size / 1.75:.1f} times an adult's standing height. Workers stay small beside its feet; a detail crop must not shrink the complete object. "
            rules.append(rule + 'Preserve this size across all shots and reference images.')
        description = item['description']
        if re.search(r'на\s+одной\s+стороне', description, re.I) and re.search(r'на\s+другой', description, re.I):
            faces = ' '.join(s.strip() for s in re.split(r'(?<=[.!?])\s+', description)
                             if re.search(r'на\s+(?:одной\s+стороне|другой)', s, re.I))
            rule = 'OBJECT IDENTITY: opposite faces belong to ONE unchanged specimen. Source-described faces: ' + faces
            if re.search(r'афинск\w*\s+монет', faces, re.I) and re.search(r'голов\w*\s+баран', faces, re.I):
                rule += ' Athenian coin impression on one face; ram head on the opposite face. Preserve these motifs rather than substituting an anchor, ankh, sun face or invented animal.'
            rules.append(rule)
    return rules


def reference_time_rule(name):
    if re.search(r'историческ\w*\s+вид|главный\s+вид', name, re.I):
        return ('REFERENCE TIME TARGET: HISTORICAL RECONSTRUCTION of the maintained source-described place before its final abandonment or submergence. '
                'Modern underwater archaeological remains are not this historical reference. Preserve the named place and its actual spatial type; do not replace an underground place with a surface city. ')
    if re.search(r'нынешнее\s+состояние|современные\s+руины', name, re.I):
        return ('REFERENCE TIME TARGET: PRESENT-DAY REMAINS, in the source-described present environment. '
                'A submerged site stays underwater, not a dry terrestrial excavation or intact ancient port. ')
    return ''


def motif_errors(description, direction):
    """Reject an affirmative replacement; allow explicit prohibitions."""
    if not (re.search(r'афинск\w*\s+монет', description, re.I) and
            re.search(r'голов\w*\s+баран', description, re.I)):
        return []
    for clause in re.split(r'[.;\n]', direction):
        for match in re.finditer(r'\banchor (?:motif|symbol|relief)\b|\bankh\b|\bsun face\b|(?:рельеф|изображение|символ)\s+якор\w*', clause, re.I):
            prefix = clause[:match.start()]
            if re.search(r'\b(?:no|not|without|never|avoid|forbid|не|без|никаких)\b[^.;]*$', prefix, re.I):
                continue
            return ['The selected artifact has an Athenian coin impression and a ram head in the source; the proposed motif contradicts these defining faces.']
    return []


def source_backed_direction(names, story, quote, direction):
    """A legacy erroneous face cannot override the explicit object's evidence."""
    for item in artifact_evidence(names, story):
        if not motif_errors(item['description'], direction):
            continue
        face = 'ram-head motif' if re.search(r'голов\w*\s+баран', quote, re.I) else 'Athenian coin impression'
        # Keep framing, participants and negations; replace only the known
        # contradictory affirmative clause. Do not alter unrelated artifacts.
        clauses = re.split(r'([.;\n])', direction)
        for i in range(0, len(clauses), 2):
            if motif_errors(item['description'], clauses[i]):
                clauses[i] = re.sub(r'\banchor (?:motif|symbol|relief)\b|\bankh\b|\bsun face\b|(?:рельеф|изображение|символ)\s+якор\w*', face, clauses[i], flags=re.I)
        direction = ''.join(clauses)
    return direction

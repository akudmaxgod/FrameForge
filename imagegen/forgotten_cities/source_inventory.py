"""Literal document inventory, independent of named-entity guesses or city presets."""
import hashlib
import re
from functools import lru_cache

from imagegen.analyzer_v3.models import SourceSpan
from .models import AnchorType, TimeLayer, VisualAnchorIR

DECLARED_CITY = re.compile(r'\bгород\s+(?:называется|назывался|носит\s+название)\s+[«"“]?([А-ЯЁ][\w-]+)', re.I)
DECLARED_COMPLEX = re.compile(r'\bТак\s+выглядит\s+([А-ЯЁ][\w-]+)\s*[—–-]\s*[^.!?\n]{0,45}\b(?:комплекс|город)\b')
NAMED_ISLAND = re.compile(r'\bостров(?:ок|ка|ке|ки|ку|ом|а|е|у)?\s+([А-ЯЁ][\w-]+)')
NAMED_SITE = re.compile(r'\b(?:мест\w*\s+комплекса|комплекс\w*|сооружени\w*)\s*[—–]\s*([А-ЯЁ][\w-]+)')
PLACE = re.compile(r'\b(?:залив\w*\s+[А-ЯЁ][\w-]+|[А-ЯЁ][\w-]*ск\w*\s+рукав\w*\s+[А-ЯЁ][\w-]+|'
                   r'храм\w*\s+(?:богин\w*\s+)?[А-ЯЁ][\w-]+|портов\w*\s+бассейн\w*)')
SHRINE = re.compile(r'\bсвятилищ\w*[^.!?\n]{0,65}?с\s+([А-ЯЁ][\w-]+)')
ARTIFACT_NOUNS = {'стела', 'гиря', 'статуя', 'фигура', 'статуэтка', 'пластинка', 'сосуд', 'корабль', 'монета'}


@lru_cache(maxsize=2048)
def normal_name(value):
    from imagegen.analyzer_v3.nlp import RussianNLPAdapter
    morph = RussianNLPAdapter.get_instance().morph_analyzer
    return '-'.join(' '.join(morph.parse(w)[0].normal_form for w in part.split())
                    for part in value.casefold().replace('ё', 'е').split('-'))


def declared_city(story):
    match = DECLARED_CITY.search(story) or DECLARED_COMPLEX.search(story)
    return match.group(1).strip('»"”') if match else ''


def is_same_place(left, right):
    if not left or not right: return False
    if normal_name(left) == normal_name(right): return True
    # Unknown proper names can have an incorrect dictionary gender. A
    # source-declared consonant-final name supplies the base, not a new entity.
    forms = re.split(r'[\s-]+',left.casefold())
    bases = re.split(r'[\s-]+',right.casefold())
    return len(forms)==len(bases) and all(form==base or (
        base[-1:] in 'бвгджзклмнпрстфхцчшщ' and form.startswith(base) and
        form[len(base):] in ('а','у','е','ом','ы','и')) for form,base in zip(forms,bases))


def _phrase_name(text):
    from imagegen.analyzer_v3.nlp import RussianNLPAdapter
    morph = RussianNLPAdapter.get_instance().morph_analyzer
    parts = text.split()
    # Normalize the head and its agreeing adjective, preserving named genitives.
    head_seen = False
    for i, word in enumerate(parts):
        parsed = morph.parse(word)[0]
        if head_seen: break
        if parsed.tag.POS in ('NOUN', 'ADJF') and not (word[:1].isupper() and i > 0):
            value = parsed.inflect({'nomn'})
            if value: parts[i] = value.word.capitalize() if i == 0 else value.word
        if parsed.tag.POS=='NOUN': head_seen=True
    return ' '.join(parts)


@lru_cache(maxsize=4)
def inventory(story):
    """Every name and property retains exact evidence; no external fact is inferred."""
    from imagegen.analyzer_v3.nlp import RussianNLPAdapter
    nlp = RussianNLPAdapter.get_instance()
    parsed = nlp.parse_document(story)
    places, artifacts = {}, {}
    def add(store, name, kind, sentence, match_start, match_end, description=None):
        key = normal_name(name)
        item = store.setdefault(key, {'name':name, 'kind':kind, 'evidence':[], 'matches':[], 'description':description or sentence.text})
        evidence = SourceSpan(sentence.start, sentence.end, sentence.text)
        if evidence not in item['evidence']: item['evidence'].append(evidence)
        item['matches'].append({'start':match_start, 'end':match_end, 'quote':story[match_start:match_end]})
    for sentence in parsed.sentences:
        text, a, b = sentence.text, sentence.start, sentence.end
        for pattern, kind in ((NAMED_ISLAND,'natural'), (NAMED_SITE,'district')):
            for match in pattern.finditer(text):
                name=match.group(1)
                if is_same_place(name,declared_city(story)): continue
                paragraph_start=story.rfind('\n\n',0,a)+2
                paragraph_end=story.find('\n\n',b)
                if paragraph_end<0: paragraph_end=len(story)
                add(places,name,kind,sentence,a+match.start(1),a+match.end(1),
                    story[paragraph_start:min(paragraph_end,paragraph_start+1600)])
        for match in PLACE.finditer(text):
            name = _phrase_name(match.group())
            kind = 'natural' if re.search(r'залив|рукав', name, re.I) else 'district'
            add(places, name, kind, sentence, a+match.start(), a+match.end())
        for match in SHRINE.finditer(text):
            god = nlp.morph_analyzer.parse(match.group(1))[0]
            genitive = god.inflect({'gent'})
            name = 'Святилище ' + (genitive.word.capitalize() if genitive else match.group(1))
            add(places, name, 'district', sentence, a+match.start(), a+match.end())
        tokens = parsed.find_tokens_in_span(a, b)
        # Explicit find lists establish objects even when NER has no model for
        # them. Keep the full defining phrase, not a broad material keyword.
        if re.search(r'среди\s+предметов|при\s+раскопках|находил\w*|найден\w*',text,re.I):
            for pattern,name in (
                (r'кольц\w*\s+из\s+раковин\w*','Кольцо из раковины'),
                (r'подвеск\w*\s+из\s+перламутр\w*','Подвеска из перламутра'),
                (r'просверленн\w*\s+зуб\w*\s+животн\w*','Просверленные зубы животных'),
                (r'кристалл\w*\s+кварц\w*','Кристалл кварца')):
                for match in re.finditer(pattern,text,re.I):
                    add(artifacts,name,'artifact',sentence,a+match.start(),a+match.end())
        for index, token in enumerate(tokens):
            lemma = nlp.analyze_token_morph(token.text).lemma
            if lemma not in ARTIFACT_NOUNS: continue
            # A metaphor, generic comparison, denied structure, or anonymous
            # fleet does not establish an individual reference object.
            if re.search(r'не\s+(?:означает|превращает)|представьте|почему|может\s+быть', text, re.I): continue
            adjectives = []
            for previous in tokens[max(0,index-3):index][::-1]:
                m = nlp.analyze_token_morph(previous.text)
                if m.pos != 'ADJF' and previous.pos != 'ADJ': break
                value = nlp.morph_analyzer.parse(previous.text)[0].inflect({'nomn', 'sing', 'femn' if lemma not in ('сосуд','корабль') else 'masc'})
                adjectives.insert(0,value.word if value else previous.text)
            window = story[a:min(len(story),b+500)]
            name = ''
            if lemma == 'стела':
                named = re.search(r'стел\w*\s+фараона\s+([А-ЯЁ][\w-]+(?:\s+[А-ЯЁ][\w-]+)?)', window)
                provenance = re.search(r'стел\w*[^.!?]{0,35}?найденн\w*\s+в\s+([А-ЯЁ][\w-]+)',text)
                if named: name = 'Стела фараона ' + named.group(1)
                elif provenance: name = 'Стела, найденная в ' + provenance.group(1)
                elif adjectives: name = ' '.join(adjectives + [lemma]).capitalize()
            elif lemma in ('статуя','фигура'):
                named = re.search(r'связывают\s+с\s+([А-ЯЁ][\w-]+)', window)
                if named and re.search(r'гранит|огромн|метр', text, re.I): name = 'Статуя ' + named.group(1)
                elif lemma == 'фигура' and re.search(r'Осирис\w*', text): name = 'Фигура Осириса из земли и зерна'
            elif lemma == 'корабль':
                number = re.search(r'номер\s+([\w-]+)', text, re.I)
                if number: name = 'Корабль № ' + number.group(1)
            elif lemma == 'монета':
                dated = re.search(r'монет\w*\s+([^.!?]{1,60}?век\w*\s+(?:нашей\s+эры|до\s+нашей\s+эры))', text, re.I)
                if dated: name = 'Монеты ' + dated.group(1)
            elif adjectives and re.search(r'свинц|золот|гранит|ритуаль|камен|бронз|алебастр', ' '.join(adjectives), re.I):
                name = ' '.join(adjectives+[lemma]).capitalize()
            if not name: continue
            end = story.find('\n\n', b)
            if end < 0: end = b
            description = story[a:min(end,b+700)]
            add(artifacts,name,lemma,sentence,token.start,token.stop,description)
    # Once a named place is explicitly introduced, its case forms retain the
    # same place and local evidence. Proper nouns alone never invent locations.
    for sentence in parsed.sentences:
        for match in re.finditer(r'\b[А-ЯЁ][\w-]+\b',sentence.text):
            candidates=[item for item in places.values() if is_same_place(match.group(),item['name'])]
            if len(candidates)!=1: continue
            item=candidates[0]
            span=SourceSpan(sentence.start,sentence.end,sentence.text)
            if span not in item['evidence']: item['evidence'].append(span)
            evidence={'start':sentence.start+match.start(),'end':sentence.start+match.end(),'quote':match.group()}
            if evidence not in item['matches']: item['matches'].append(evidence)
    # Unnamed and named introductions of the same stele are one object only
    # when the document explicitly supplies exactly one named stele.
    named_steles = [v for v in artifacts.values() if v['kind']=='стела' and 'фараона' in v['name']]
    if len(named_steles)==1:
        for key,item in list(artifacts.items()):
            if item['kind']=='стела' and item is not named_steles[0] and not item['name'].startswith('Стела, найденная в '):
                named_steles[0]['evidence'].extend(item['evidence']);named_steles[0]['matches'].extend(item['matches']);del artifacts[key]
    return list(places.values()), list(artifacts.values())


def inventory_anchors(story, existing=()):
    places, artifacts = inventory(story)
    occupied = {normal_name(a.canonical_name) for a in existing}
    results=[]
    for item in places+artifacts:
        if normal_name(item['name']) in occupied: continue
        occupied.add(normal_name(item['name']))
        artifact = item in artifacts
        digest=hashlib.sha256((item['kind']+normal_name(item['name'])).encode()).hexdigest()[:12]
        # An adjacent sentence about making another ritual object must not
        # reclassify an explicitly excavated vessel as a textual reconstruction.
        textual_object=bool(artifact and any(re.search(
            r'по\s+[^.!?]{0,45}текст\w*[^.!?]{0,40}изготовлен\w*',span.text,re.I)
            for span in item['evidence']))
        states=[TimeLayer.HISTORICAL_RECONSTRUCTION if textual_object else TimeLayer.MUSEUM_DISPLAY] if artifact else [TimeLayer.HISTORICAL_RECONSTRUCTION]
        # A bay's underwater remains are contemporary; a reconstructed temple
        # and its excavated remains must have separate reference states.
        local_evidence = ' '.join(span.text for span in item['evidence'])
        if not artifact and re.search(r'современн|подвод|раскоп|руин|на\s+дне|обнаруж|исследован|исследовател|сегодня|видны', local_evidence+' '+item['description'], re.I):
            states.append(TimeLayer.MODERN_RUINS)
        for layer in states:
            name=item['name'] if artifact else item['name']+' — '+('исторический вид' if layer==TimeLayer.HISTORICAL_RECONSTRUCTION else 'нынешнее состояние')
            results.append(VisualAnchorIR(anchor_id=('anchor_artifact_source_' if artifact else 'anchor_place_source_')+digest+'_'+layer.value,
                anchor_type=AnchorType.ARTIFACT if artifact else AnchorType.LANDSCAPE if item['kind']=='natural' else AnchorType.DISTRICT,
                canonical_name=name, description=item['description'],time_layer=layer,
                evidence_spans=item['evidence'], recurrence_count=len(item['evidence']),importance=.8,
                reference_required=artifact or len(item['evidence'])>=2,
                source_properties={'source_names':[item['name']], 'source_noun':item['kind'],
                                   **({'evidence_kind':'textual_manufacture'} if textual_object else {})},
                match_spans=item['matches']))
    return results


def reconcile_reference_inventory(conn, pid, metadata):
    """Reconcile empty cards only; locked references and generated media survive."""
    from imagegen.db import uid
    for table, artifact in (('locations',False),('props',True)):
        desired={a['canonical_name']:a for a in metadata['anchors'] if (a['anchor_type']=='artifact')==artifact}
        rows=conn.execute('SELECT * FROM '+table+' WHERE project_id=?',(pid,)).fetchall()
        existing={r['name']:r for r in rows}
        for name,anchor in desired.items():
            if name in existing: continue
            description=anchor['description'][:4000]
            if artifact:
                conn.execute('INSERT INTO props(id,project_id,name,description,importance,first_appearance,owner,default_state) VALUES(?,?,?,?,?,?,?,?)',
                    (uid(),pid,name,description,'story',1,'','absent'))
            else:
                conn.execute('INSERT INTO locations(id,project_id,name,description) VALUES(?,?,?,?)',(uid(),pid,name,description))
        for row in rows:
            if row['name'] in desired or row['locked'] or json_reference_present(row):continue
            column='prop_id' if artifact else 'location_id'
            if conn.execute('SELECT id FROM generations WHERE '+column+'=? LIMIT 1',(row['id'],)).fetchone():continue
            conn.execute('DELETE FROM '+table+' WHERE id=?',(row['id'],))


def json_reference_present(row):
    import json
    return bool(json.loads(row['references_json'] if 'references_json' in row.keys() else row['references'] or '{}'))

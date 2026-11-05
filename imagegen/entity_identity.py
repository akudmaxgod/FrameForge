"""Conservative identity matching shared by storyboard and presence validation.

Roles and relationship words are context, never global identity aliases.
Unknown full names must not silently resolve through a known first name.
"""
import json
import re
from functools import lru_cache

VERSION = 'entity-identity/1'
WORD = re.compile(r"[^\W_]+(?:[-’'][^\W_]+)*", re.UNICODE)
GENERIC = {'отец','мать','сын','дочь','брат','сестра','муж','жена','король','королева',
           'лорд','командующий','лорд-командующий','доктор','врач','учитель','капитан',
           'главнокомандующий','правитель','принц','принцесса','десница','начальник',
           'мужчина','женщина','человек','герой','father','mother','son','daughter',
           'king','queen','doctor','captain','commander','teacher','lord','man','woman'}

@lru_cache(maxsize=16000)
def lemma(word):
    from .named_event_coverage import _morph
    morph = _morph()
    if not morph:return word.casefold()
    parsed=morph.parse(word.casefold())
    if 'Name' in parsed[0].tag and 'Fixd' in parsed[0].tag:
        regular=[p for p in parsed if 'Name' in p.tag and 'Fixd' not in p.tag]
        if regular:return regular[0].normal_form
    return parsed[0].normal_form

@lru_cache(maxsize=16000)
def words(text): return tuple(lemma(m.group()) for m in WORD.finditer(text or ''))

def specific_label(label):
    parts=words(label)
    return bool(parts) and not any(p in GENERIC for p in parts)

def labels(character):
    aliases=character.get('aliases') or []
    if isinstance(aliases,str): aliases=json.loads(aliases)
    primary=character.get('canonical_name') or character.get('name') or ''
    return list(dict.fromkeys(x for x in [primary,character.get('name'),*aliases]
                              if x and (x==primary or specific_label(x))))

def gender(character):
    explicit=str(character.get('gender') or character.get('sex') or '').casefold()
    if explicit in ('female','femn','женский','женщина'):return 'femn'
    if explicit in ('male','masc','мужской','мужчина'):return 'masc'
    first=WORD.search(character.get('canonical_name') or character.get('name') or '')
    if first:
        from .named_event_coverage import _morph
        morph=_morph()
        if morph:
            candidates=morph.parse(first.group())
            named=[p for p in candidates if 'Name' in p.tag and p.tag.gender]
            if named:return named[0].tag.gender
    # Only a self-description at the start, never a relative elsewhere in biography.
    description=str(character.get('description') or '').casefold()
    if re.match(r'^(?:an?\s+)?(?:adult\s+|young\s+|elderly\s+)?(?:woman|female)\b|^(?:молодая\s+|пожилая\s+)?(?:женщина|девушка)\b',description):return 'femn'
    if re.match(r'^(?:an?\s+)?(?:adult\s+|young\s+|elderly\s+)?(?:man|male)\b|^(?:молодой\s+|пожилой\s+)?(?:мужчина|юноша)\b',description):return 'masc'
    return None

def _proper_neighbor(token):
    if not token or not token[0].isupper():return False
    from .named_event_coverage import _morph
    morph=_morph()
    if not morph:return True
    parses=morph.parse(token)
    return any(('Surn' in p.tag or 'Patr' in p.tag) for p in parses) or parses[0].tag.POS is None

def mentions(text, characters, diagnostics=None):
    """Return non-overlapping longest, unambiguous identity spans."""
    spans=list(WORD.finditer(text));normalized=tuple(lemma(m.group()) for m in spans)
    matches=[]
    for index,c in enumerate(characters):
        for label in labels(c):
            key=words(label)
            for start in range(len(spans)-len(key)+1):
                end=start+len(key)
                if normalized[start:end]!=key:continue
                # A short name inside a different/unknown explicit full name is unsafe.
                if len(key)==1:
                    neighbors=[k for k in (start-1,end) if 0<=k<len(spans) and _proper_neighbor(spans[k].group())
                               and not re.search(r'[.!?;,\n]',text[min(spans[k].end(),spans[start].end()):max(spans[k].start(),spans[start].start())])]
                    if neighbors and not any(len(words(full))>1 and
                        any(normalized[a:a+len(words(full))]==words(full) and a<=start<a+len(words(full))
                            for a in range(max(0,start-len(words(full))+1),start+1))
                        for full in labels(c)):
                        if diagnostics is not None:
                            lo=min([start,*neighbors]);hi=max([end,*[n+1 for n in neighbors]])
                            diagnostics.append({'code':'short_alias_conflicts_with_full_name','start':spans[lo].start(),'end':spans[hi-1].end(),'quote':text[spans[lo].start():spans[hi-1].end()],'candidate':c.get('canonical_name') or c.get('name')})
                        continue
                matches.append((len(key),start,end,index))
    selected=[];occupied=set()
    for length,start,end,index in sorted(matches,key=lambda x:(-x[0],x[1],x[3])):
        owners={m[3] for m in matches if m[:3]==(length,start,end)}
        if len(owners)!=1:
            if diagnostics is not None:diagnostics.append({'code':'ambiguous_name','start':spans[start].start(),'end':spans[end-1].end(),'quote':text[spans[start].start():spans[end-1].end()]})
            occupied.update(range(start,end));continue
        if occupied.intersection(range(start,end)):continue
        selected.append((index,spans[start].start(),spans[end-1].end()))
        occupied.update(range(start,end))
    return sorted(selected,key=lambda item:item[1])

def presence_kind(text,start,end):
    """Explicit grammatical mention cues; a death/action itself is not excluded."""
    prefix=text[max(0,start-100):start].casefold()
    suffix=text[end:end+50].casefold()
    if re.search(r'(?:портрет\w*|стату\w*|бюст\w*|изображени\w*|portrait of|statue of)\s+(?:\w+\s+){0,3}$',prefix):return 'depicted'
    if re.search(r'(?:по словам|как писал\w*|как отмечал\w*|автор\w*|according to|as written by)\s+(?:\w+\s+){0,2}$',prefix):return 'mentioned'
    if re.search(r'(?:письм\w*\s+(?:от\s+)?|сообщени\w*\s+от\s+|letter from\s+|message from\s+)$',prefix):
        from .named_event_coverage import _morph
        morph=_morph();first=WORD.search(text[start:end])
        if morph and first and not prefix.endswith('от ') and any('datv' in p.tag for p in morph.parse(first.group())):return 'physical'
        return 'mentioned'
    if re.search(r'(?:вспомина\w*|вспомни\w*|обсужда\w*|помни\w*|дума\w*\s+о|рассказ\w*\s+о|говор\w*\s+о|remember\w*|discuss\w*|about)\s+(?:\w+\s+){0,2}$',prefix):return 'mentioned'
    if re.search(r'(?:после\s+смерти|смерти|гибели|происхождении|родств\w*\s+с|сыном|дочерью|отцом|матерью)\s+(?:\w+\s+){0,2}$',prefix):return 'mentioned'
    # Possessive/genitive relatives are not the acting owner of a short-name alias.
    if re.search(r'(?:отец|мать|сын|дочь|жена|муж|брат|сестра)\s+$',prefix):return 'mentioned'
    if re.search(r'\b(?:если бы|if)\b[^.!?]*$',prefix):return 'mentioned'
    return 'physical'

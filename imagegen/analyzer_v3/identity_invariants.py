"""Identity invariants shared by cloud synthesis and local certification."""
import re
from functools import lru_cache
from .models import EntityType
from .nlp import RussianNLPAdapter


@lru_cache(maxsize=4)
def _parsed_trait_story(story):
    return RussianNLPAdapter.get_instance().parse_document(story)


def is_abstract_person_trait(span, story, parsed_doc=None):
    """Distinguish a human's ability from a human acting under that title."""
    nlp = RussianNLPAdapter.get_instance()
    words = re.findall(r'\w+', span.text)
    if len(words) > 3 or not any(nlp.analyze_token_morph(w).lemma in {'гений', 'талант'} for w in words):
        return False
    parsed = parsed_doc if parsed_doc is not None else _parsed_trait_story(story)
    qualities = {'инженерный', 'технический', 'человеческий', 'творческий',
                 'художественный', 'музыкальный', 'изобретательский'}
    for token in parsed.find_tokens_in_span(span.start, span.end):
        head = parsed.token_by_id.get(token.head_id)
        trait_coordination = (token.rel == 'conj' and head is not None
                              and head.feats.get('Animacy') == 'Inan'
                              and head.feats.get('Case') == 'Gen')
        if (nlp.analyze_token_morph(token.text).lemma not in {'гений', 'талант'}
                or not (token.rel == 'nmod' or trait_coordination) or token.feats.get('Case') != 'Gen'):
            continue
        children = parsed.children_by_head_id.get(token.id, [])
        # "Гений Мартин" still denotes the named person, even in oblique cases.
        if any(c.rel in {'appos', 'flat', 'flat:name'} for c in children):
            continue
        if any(c.rel == 'amod' and nlp.analyze_token_morph(c.text).lemma in qualities for c in children):
            return True
    return False


def mixes_named_individual_and_group(doc, members, canonical_name, aliases=(), evidence=()):
    nlp = RussianNLPAdapter.get_instance()
    mention_map = {m.mention_id: m for m in doc.mentions}
    def named(value):
        return any(any('Name' in p.tag or 'Surn' in p.tag or 'Patr' in p.tag
                       for p in nlp.morph_analyzer.parse(word))
                   for word in re.findall(r'\w+', value))
    names = []
    for entity in members:
        if entity.entity_type != EntityType.PERSON or not named(entity.canonical_name):
            continue
        mentions = [mention_map[mid] for mid in entity.mention_ids if mid in mention_map]
        proper = [m for m in mentions if m.mention_type == 'proper_noun']
        if proper and all(m.grammatical_number == 'plural' for m in proper):
            continue
        names.append(entity.canonical_name)
    if not members and named(canonical_name):
        names.append(canonical_name)
    if not names:
        return False
    references = list(aliases)
    for member in members:
        references.extend(member.aliases + [member.canonical_name])
    # Full sentences can mention another participant as an object; they are
    # evidence for the person, not necessarily aliases of that person.
    references.extend(s.text for s in evidence
                      if not any(name.casefold() in s.text.casefold() for name in names))
    for reference in references:
        for word in re.findall(r'\w+', reference):
            morph = nlp.analyze_token_morph(word)
            if morph.pos == 'NOUN' and morph.animacy == 'anim' and morph.number == 'plural':
                return True
    return False

"""Occurrence-level evidence for local source guards, never a global name blacklist."""
import re

HUMAN_TITLES = {'врач','доктор','пациент','пациентка','капитан','брат','сестра',
    'инженер','археолог','исследователь','царь','король','правитель','князь','солдат',
    'профессор','учитель','женщина','мужчина','девушка','мальчик','девочка'}
PLACE_HEADS = {'город','поселение','посёлок','регион','страна','река','озеро','остров',
    'порт','крепость','комплекс','храм','дворец','музей','улица','гора','долина'}
GROUPS = {'население','семья','община','группа','команда','отряд','армия'}

def classify(token,parsed,story,nlp,place_names=(),declared=''):
    """Conflicting evidence stays unknown and reaches the semantic reviewer."""
    morph=nlp.analyze_token_morph(token.text)
    labels={s.type for s in parsed.spans if s.start<=token.start<s.stop}
    parent=parsed.get_parent_token(token)
    parent_lemma=nlp.analyze_token_morph(parent.text).lemma if parent else ''
    children=parsed.children_by_head_id.get(token.id,[])
    # An explicit human title in apposition beats a same-spelling city elsewhere.
    title=(parent_lemma in HUMAN_TITLES and token.rel in ('appos','flat','nmod','nsubj')
           or any(nlp.analyze_token_morph(t.text).lemma in HUMAN_TITLES and
                  t.rel in ('appos','flat') for t in children)
           )
    if title:return 'person','явная роль человека в текущем предложении'
    if morph.lemma in GROUPS:return 'group','явная группа участников'
    if parent_lemma in PLACE_HEADS and token.rel in ('appos','flat','nmod'):
        return 'place','название при географическом или архитектурном объекте'
    tail=story[token.stop:token.stop+140]
    if token.rel.startswith('nsubj') and re.match(
            r'\s+(?:находит\w*|располож\w*|лежит|лежала|лежал)\s+(?:на\s+территории|в\s+(?:долине|регионе|стране)|между\s+)',tail,re.I):
        if 'PER' in labels and 'LOC' not in labels:
            return 'unknown','локативная конструкция неоднозначна: человек или место'
        return 'place','явное географическое положение'
    from .forgotten_cities.source_inventory import is_same_place
    known=token.text.casefold() in place_names or declared and is_same_place(token.text,declared)
    # UD analysis uses sentence context; dictionary morphology lacks that context.
    if known:
        if ('PER' in labels and token.feats.get('Animacy')=='Anim'
                and token.feats.get('Case')=='Nom'):
            return 'unknown','конфликт имени человека и названия места'
        return 'place','название места подтверждено предыдущим исходным текстом'
    if 'ORG' in labels:return 'organization','организация по контексту'
    if 'LOC' in labels:return 'place','географическая сущность по контексту'
    if token.feats.get('Animacy')=='Inan':return 'object','неодушевлённая сущность в предложении'
    if 'PER' in labels or morph.animacy=='anim':return 'person','контекст имени или одушевлённой роли'
    return 'object','нет подтверждения роли человека'


def actor_actions(actor,parsed,story):
    """Only predicates governing this actor; never another sentence's action."""
    from .nlp import RussianNLPAdapter
    nlp=RussianNLPAdapter.get_instance()
    evidence=actor.get('link') or actor.get('evidence') or {}
    tokens=parsed.find_tokens_in_span(evidence.get('start',0),evidence.get('end',0))
    actions=[]
    for token in tokens:
        seen=set()
        while token and token.id not in seen:
            seen.add(token.id)
            if token.pos=='VERB':
                negated=any(t.text.casefold()=='не' for t in parsed.children_by_head_id.get(token.id,[]))
                if not negated:
                    sentence=next((s for s in parsed.sentences if s.start<=token.start<s.end),None)
                    prefix=story[sentence.start if sentence else token.start:token.start]
                    hypothetical=bool(re.search(r'\b(?:представьте|представим|вообразим|воображаем\w*|'
                        r'предположим|если\s+бы|мог\w*\s+бы|предполож\w*\s*,?\s*что)\b',prefix,re.I))
                    actions.append({'start':token.start,'end':token.stop,'quote':story[token.start:token.stop],
                                    'asserted':not hypothetical})
                break
            # A genitive owner/author is mentioned, not a patient/actor of the event.
            if token.rel in ('nmod','appos','flat'):
                parent=parsed.get_parent_token(token)
                if not (parent and token.rel in ('appos','flat') and
                        nlp.analyze_token_morph(parent.text).lemma in HUMAN_TITLES):
                    break
            token=parsed.get_parent_token(token)
    return actions

"""Grounded, forward-only narrative context shared by all shot consumers."""
import copy
import hashlib
import re
from functools import lru_cache

VERSION = 'source-event-context/5-typed-actors'
DATE = re.compile(r'\b(?:в|к|с|до|после)\s+(?:\d{3,4}|тысяча\s+[^.!?\n]{0,100})\s+год\w*', re.I)
ANCIENT = re.compile(r'до\s+н\.\s*э|до\s+наш\w*\s+эры|тысячелети\w*|'
                     r'\bв\s+древност\w*|\bв\s+средневеков\w*|'
                     r'\b(?:[\w-]+\s+){0,5}тысяч\w*\s+лет\s+назад|'
                     r'\b(?:древн\w*\s+корабл\w*|древн\w*\s+капитан\w*)', re.I)
PRESENT = re.compile(r'\b(?:сегодня|ныне|сейчас|в\s+наши\s+дни)\b', re.I)
PERIOD = re.compile(r'\b(?:во?|с|после|до)\s+(?:(?:[\w-]+)\s+){0,7}(?:век[аеу]?|веках|тысячелети[еяи])'
                    r'(?:\s+(?:до\s+)?(?:нашей\s+)?эры)?|'
                    r'\b(?:в|с|до|после)\s+(?:наступления\s+)?(?:до\s+)?нашей\s+эры|'
                    r'\bв\s+(?:эпоху\s+[\w-]+|[\w-]+\s+(?:период|эпох[уе]))', re.I)
TIME_LINK = re.compile(r'^\s*(?:в\s+(?:эту|ту)\s+эпоху|в\s+(?:этот|тот)\s+период)\b', re.I)
COLLECTIVES = {'население', 'семья', 'община', 'группа', 'команда', 'отряд', 'армия'}
SPACE = re.compile(r'^(?:дом|помещение|подвал|комната|коридор|проход|тоннель|туннель|галерея|зал|лестница|шахта|'
                   r'улица|переулок|двор|мастерская|храм|дворец|берег|озеро|река|поле|пещера|траншея|музей|ворота)$', re.I)
SHIFT = re.compile(r'^\s*(?:тем\s+временем|в\s+другом\s+месте|много\s+лет\s+спустя|столетия\s+спустя|'
                   r'перенес[её]мся|верн[её]мся|теперь\s+обратимся|следующая\s+история)\b', re.I)
DEAD = re.compile(r'погреб\w*|похорон\w*|захорон\w*|останки|скелет|умерш\w*|погибш\w*', re.I)


def span(story, start, end):
    return {'start': start, 'end': end, 'quote': story[start:end]}


@lru_cache(maxsize=4)
def _contexts(story):
    from imagegen.analyzer_v3.nlp import RussianNLPAdapter
    from imagegen.analyzer_v3.entities import ANIMAL_KEYWORDS, RE_VOICE_ONLY, RE_PERCEIVED_VOICE, PresenceClassifier
    from imagegen.analyzer_v3.models import MentionIR, EntityIR, EntityType, EventIR, SourceSpan, SyntacticRole
    nlp = RussianNLPAdapter.get_instance()
    parsed = nlp.parse_document(story)
    presence = PresenceClassifier(nlp)
    result, previous_subject, current_time, current_place = [], None, None, None
    last_explicit_time = None
    # An NER guess about a single occurrence cannot turn a known geographical
    # name into a person elsewhere. Retain literal forms, no city exceptions.
    place_evidence = [(s.stop, s.text.casefold()) for s in parsed.spans if s.type == 'LOC']
    place_evidence.extend((m.end(), m.group(1).casefold()) for m in re.finditer(
        r'(?:под\s+названием|город(?:а|е|у|ом)?|регион(?:а|е|у|ом)?)\s+([А-ЯЁ][а-яёА-ЯЁ-]+)', story))
    from .source_inventory import declared_city, normal_name, is_same_place
    declared = declared_city(story)
    if declared:
        place_evidence.append((0, declared.casefold()))
    # Literal declarations and geographic predicates recover places NER calls PER.
    for m in re.finditer(r'Это\s+был[ао]?\s+([А-ЯЁ][а-яё-]+)\s*[—–-]\s*(?:место|город|поселение|порт)\b',story):
        place_evidence.append((m.end(),m[1].casefold()))
    from ..source_entity_types import classify, actor_actions
    previous_end, scope_start = 0, 0
    for sentence in parsed.sentences:
        quote, a, b = sentence.text, sentence.start, sentence.end
        place_names = {name for end, name in place_evidence if end <= b}
        if re.search(r'\n\s*\n', story[previous_end:a]) or SHIFT.search(quote):
            previous_subject, current_time, current_place = None, None, None
            scope_start = a
        until_transition = re.search(r'\bпока\b[^.!?]{0,180}\bне\s+\w+', quote, re.I)
        if until_transition:
            # An "until" event closes the previously dated interval; the old
            # date cannot date the new actor's action without further evidence.
            previous_subject, current_time, current_place = None, None, None
            scope_start = a
        if TIME_LINK.search(quote) and last_explicit_time:
            current_time = copy.deepcopy(last_explicit_time)
        date = DATE.search(quote)
        period = PERIOD.search(quote)
        present = PRESENT.search(quote)
        ancient = ANCIENT.search(quote)
        mixed_time_evidence = None
        if date:
            current_time = {'layer': 'documented_event',
                            'evidence': span(story, a + date.start(), a + date.end())}
            previous_subject = None
        elif period and not TIME_LINK.search(quote):
            current_time = {'layer': 'historical_reconstruction',
                            'evidence': span(story, a + period.start(), a + period.end())}
            previous_subject = None
        elif ancient:
            current_time = {'layer': 'historical_reconstruction', 'evidence': span(story, a+ancient.start(), a+ancient.end())}
            previous_subject = None
        elif present and not re.search(r'(?:сегодня|ныне|сейчас)\s+(?:\w+\s+){0,2}(?:извест\w*|называ\w*)', quote, re.I):
            if re.search(r'[.!?]', quote[:present.start()]) and any(
                    nlp.analyze_token_morph(t.text).pos == 'VERB' and nlp.analyze_token_morph(t.text).tense == 'past'
                    for t in parsed.find_tokens_in_span(a,a+present.start())):
                mixed_time_evidence = span(story, a+present.start(), a+present.end())
            current_time = {'layer': 'modern_ruins', 'evidence': span(story, a+present.start(), a+present.end())}
            previous_subject = None
        if date or (period and not TIME_LINK.search(quote)) or ANCIENT.search(quote):
            last_explicit_time = copy.deepcopy(current_time)
        tokens = parsed.find_tokens_in_span(a, b)
        subjects, actors, animals, predicates, place_candidates = [], [], [], [], []
        physical_animal_ids = set()
        suppressed = bool(RE_VOICE_ONLY.search(quote) or RE_PERCEIVED_VOICE.search(quote))
        for token in tokens:
            morph = nlp.analyze_token_morph(token.text)
            if morph.lemma in ANIMAL_KEYWORDS:
                mention = MentionIR('source', token.text, token.text, SourceSpan(token.start, token.stop, token.text),
                                    syntactic_role=SyntacticRole.SUBJECT if token.rel.startswith('nsubj') else SyntacticRole.OBJECT)
                modality, physical, _ = presence._classify_mention_modality(mention,
                    EntityIR('source', token.text, EntityType.ANIMAL),
                    EventIR('source', 'source', SourceSpan(a, b, quote)), parsed, story)
                if (not physical and modality.value == 'mentioned' and token.rel == 'conj'
                        and token.head_id in physical_animal_ids
                        and not any(DEAD.search(t.text) or re.match(r'м[её]ртв\w*',t.text,re.I)
                                    for t in parsed.children_by_head_id.get(token.id, []))):
                    physical = True
                if physical:
                    physical_animal_ids.add(token.id)
                    animals.append({'name': token.text, 'kind': morph.lemma,
                                    'evidence': span(story, token.start, token.stop)})
            if token.pos == 'VERB' and token.feats.get('VerbForm') == 'Fin':
                predicates.append(span(story, token.start, token.stop))
            if morph.pos == 'NOUN' and SPACE.fullmatch(morph.lemma):
                place_candidates.append({'name': morph.lemma, 'evidence': span(story, token.start, token.stop)})
            entity_kind, type_reason = classify(token,parsed,story,nlp,place_names,declared)
            named_place = entity_kind in ('place','organization','object','unknown')
            if (token.rel in ('obj', 'iobj', 'obl', 'obl:agent', 'conj', 'nmod') and morph.animacy == 'anim' and not named_place
                    and morph.lemma not in ANIMAL_KEYWORDS and not DEAD.search(token.text) and not suppressed):
                parent = parsed.get_parent_token(token)
                controller = (morph.case == 'datv' and parent is not None and
                    nlp.analyze_token_morph(parent.text).lemma in {'требоваться', 'предстоять', 'следовать', 'нужно', 'необходимо'} and
                    any(t.pos == 'VERB' and t.feats.get('VerbForm') == 'Inf'
                        for t in parsed.children_by_head_id.get(parent.id, [])))
                mention = MentionIR('source', token.text, token.text, SourceSpan(token.start, token.stop, token.text),
                                    syntactic_role=SyntacticRole.OBJECT)
                _, physical, _ = presence._classify_mention_modality(mention,
                    EntityIR('source', token.text, EntityType.PERSON),
                    EventIR('source', 'source', SourceSpan(a, b, quote)), parsed, story)
                if physical or controller:
                    actors.append({'name': token.text, 'human': True, 'gender': morph.gender,
                                   'number': morph.number, 'evidence': span(story, token.start, token.stop),
                                   'source_role': 'infinitive_controller' if controller else 'physical_participant'})
            if token.rel in ('nsubj', 'nsubj:pass'):
                if token.pos == 'PRON' and morph.lemma in ('он', 'она', 'они'):
                    # A preceding subject of this sentence outranks the actor
                    # of the previous one ("wells ... they", after engineers).
                    antecedent = subjects[-1] if subjects else previous_subject
                    compatible = antecedent and (not morph.gender or not antecedent.get('gender') or
                                                       morph.gender == antecedent['gender'])
                    compatible = compatible and (not morph.number or not antecedent.get('agreement_number', antecedent.get('number')) or
                                                 morph.number == antecedent.get('agreement_number', antecedent.get('number')))
                    if not compatible:
                        # A descriptor can refer to a named inanimate component
                        # ("property of tuff ... it is cut") rather than the
                        # sentence's abstract grammatical subject. Ambiguous
                        # candidates remain unresolved; never guess a person.
                        candidates = []
                        for earlier in tokens:
                            if earlier.start >= token.start: break
                            m = nlp.analyze_token_morph(earlier.text)
                            if (m.pos == 'NOUN' and m.animacy == 'inan' and m.gender == morph.gender
                                    and m.number == morph.number):
                                candidates.append(earlier)
                        if len(candidates) == 1:
                            t = candidates[0]
                            antecedent = {'name':t.text, 'human':False, 'gender':morph.gender,
                                          'number':morph.number, 'evidence':span(story,t.start,t.stop)}
                            compatible = True
                    if compatible:
                        subject = {**antecedent, 'link': span(story, token.start, token.stop)}
                        subjects.append(subject)
                        if subject['human'] and not suppressed:
                            actors.append(subject)
                    else:
                        subjects.append({'name': token.text, 'human': False, 'unresolved': True,
                                         'evidence': span(story, token.start, token.stop), 'gender': morph.gender})
                elif morph.pos == 'NOUN' or token.pos == 'PROPN' or (token.pos == 'ADJ' and
                        any(p.tag.POS == 'NOUN' and p.tag.animacy == 'anim' for p in nlp.morph_analyzer.parse(token.text))):
                    human = (morph.animacy == 'anim' or any(s.type == 'PER' and s.start <= token.start < s.stop for s in parsed.spans))
                    collective = morph.lemma in COLLECTIVES
                    if token.pos == 'ADJ':
                        human = any(p.tag.POS == 'NOUN' and p.tag.animacy == 'anim' for p in nlp.morph_analyzer.parse(token.text))
                    human = (human or collective) and not named_place and morph.lemma not in ANIMAL_KEYWORDS and not DEAD.search(token.text)
                    if human:
                        mention = MentionIR('source', token.text, token.text, SourceSpan(token.start, token.stop, token.text),
                                            syntactic_role=SyntacticRole.SUBJECT)
                        _, human, _ = presence._classify_mention_modality(mention,
                            EntityIR('source', token.text, EntityType.PERSON),
                            EventIR('source', 'source', SourceSpan(a, b, quote)), parsed, story)
                    subject = {'name': token.text, 'human': bool(human), 'gender': morph.gender,
                               'number': 'plural' if collective else ('plural' if token.feats.get('Number') == 'Plur' else morph.number),
                               'agreement_number': morph.number,
                               'evidence': span(story, token.start, token.stop)}
                    subjects.append(subject)
                    if human and not suppressed:
                        actors.append(subject)
                    # "Thousands of families" is one aggregate presence role,
                    # never one person and never an invented exact crowd size.
                    for child in parsed.children_by_head_id.get(token.id, []):
                        child_morph = nlp.analyze_token_morph(child.text)
                        if not human and child.rel == 'nmod' and child_morph.lemma in COLLECTIVES and token.feats.get('Case') == 'Nom':
                            group = {'name': story[token.start:child.stop], 'human': True,
                                     'number': 'plural', 'gender': None,
                                     'evidence': span(story, token.start, child.stop)}
                            subjects[-1] = group
                            if not suppressed: actors.append(group)
        # Ellipsis is allowed only for one immediately preceding human subject
        # and an agreeing finite verb; an inanimate subject closes that link.
        if not subjects:
            # Recover a clearly nominative inanimate noun mislabelled as an
            # object, only before its finite predicate. An accusative tool
            # after an implicit actor's verb is not promoted to a subject.
            finite = [t for t in tokens if t.pos == 'VERB' and t.feats.get('VerbForm') == 'Fin'
                      or nlp.analyze_token_morph(t.text).pos == 'VERB']
            first_verb = min((v.start for v in finite), default=a)
            candidates = [t for t in tokens if t.feats.get('Case') == 'Nom' and t.start < first_verb
                and (t.pos == 'PROPN' and t.text.casefold() in place_names or
                     nlp.analyze_token_morph(t.text).pos == 'NOUN' and nlp.analyze_token_morph(t.text).animacy == 'inan')
                and (any(v.pos == 'VERB' and v.feats.get('VerbForm') == 'Fin' and t.start < v.start
                         and t.feats.get('Number') == v.feats.get('Number') for v in tokens)
                     or t.pos == 'PROPN' and t.text.casefold() in place_names)]
            common = [t for t in candidates if t.pos != 'PROPN']
            if common: candidates = common
            if len(candidates) == 1:
                t=candidates[0];m=nlp.analyze_token_morph(t.text)
                subjects.append({'name':t.text, 'human':False, 'gender':m.gender, 'number':m.number,
                                 'evidence':span(story,t.start,t.stop)})
            elif not common and period and period.start() < 20:
                # A period prefix can make the tagger attach an ambiguous
                # nominative plural to "epoch" as a genitive singular. Recover
                # only one agreeing noun outside that explicit time phrase;
                # preposition-governed nouns and later verb objects stay out.
                alternatives = []
                verbs = [v for v in finite if v.start == first_verb]
                for t in tokens:
                    if (t.pos != 'NOUN' or not a+period.end() <= t.start < first_verb
                            or any(c.rel == 'case' for c in parsed.children_by_head_id.get(t.id, []))):
                        continue
                    parses = [p for p in nlp.morph_analyzer.parse(t.text)
                              if p.tag.POS == 'NOUN' and p.tag.animacy == 'inan' and p.tag.case == 'nomn'
                              and any(p.tag.number == ('plur' if v.feats.get('Number') == 'Plur' else 'sing') for v in verbs)]
                    if parses: alternatives.append((t, parses[0]))
                if len(alternatives) == 1:
                    t, morph = alternatives[0]
                    subjects.append({'name':t.text, 'human':False, 'gender':morph.tag.gender,
                                     'number':'plural' if morph.tag.number == 'plur' else 'singular',
                                     'evidence':span(story,t.start,t.stop)})
        if not subjects:
            # Syntax parsers sometimes tag an inverted inanimate subject as an
            # object. It still terminates human ellipsis; do not invent a man
            # opening himself when the source says a passage opened.
            candidates = [t for t in tokens if SPACE.fullmatch(nlp.analyze_token_morph(t.text).lemma)
                          and nlp.analyze_token_morph(t.text).case == 'nomn']
            if candidates:
                t = candidates[-1]
                subjects.append({'name': t.text, 'human': False, 'gender': nlp.analyze_token_morph(t.text).gender,
                                 'evidence': span(story, t.start, t.stop)})
        if (not subjects and predicates and previous_subject and previous_subject['human'] and not suppressed
                and re.match(r'^\s*(?:затем|после\s+этого|далее|и\s+тогда|во-вторых|в-третьих)\b', quote, re.I)):
            verbs = [nlp.analyze_token_morph(t.text) for t in tokens if t.pos == 'VERB' and t.feats.get('VerbForm') == 'Fin']
            if any(v.tense == 'past' and (
                    v.number == 'plural' and previous_subject.get('agreement_number',previous_subject.get('number')) == 'plural' or
                    v.number != 'plural' and v.gender == previous_subject.get('gender')) for v in verbs):
                actors.append({**previous_subject, 'link': predicates[0]})
        if subjects:
            previous_subject = subjects[-1]
        if place_candidates:
            current_place = place_candidates[-1]
            if current_place['name']=='ворота' and declared and re.search(r'ворот\w*\s+[А-ЯЁ][\w-]+',quote):
                # An epithet for the declared port is not evidence of a gate.
                current_place = {'name':declared,'evidence':span(story,a,b),'figurative':True}
        if actors and re.search(r'\b(?:исследовател\w*|автор\w*)\b',quote,re.I) and re.search(
                r'\b(?:связывают|считают|сочли|рассматривают|предполагают)\b',quote,re.I):
            # A scholar interpreting an ancient event is not an ancient actor.
            actors = [p for p in actors if not re.match(r'исследовател|автор',p['name'],re.I)]
        # "Doctor Anna" is one actor with a role and a name, not a doctor
        # plus an optional second person. Preserve the name in obligations.
        for actor in actors:
            evidence=actor.get('evidence') or {}
            head=next(iter(parsed.find_tokens_in_span(evidence.get('start',a),evidence.get('end',a))),None)
            if head:
                names=[t for t in parsed.children_by_head_id.get(head.id,[]) if
                    t.rel in ('appos','flat') and t.pos=='PROPN' and
                    classify(t,parsed,story,nlp,place_names,declared)[0]=='person']
                if len(names)==1:
                    actor['role_name']=actor['name'];actor['name']=names[0].text
                    actor['evidence']=span(story,names[0].start,names[0].stop)
        for actor in actors:
            actor['action_evidence'] = actor_actions(actor,parsed,story)
            actor['entity_kind'] = 'group' if actor.get('number')=='plural' else 'person'
        mentioned_actors=[p for p in actors if p['action_evidence'] and
                          not any(e.get('asserted',True) for e in p['action_evidence'])]
        actors=[p for p in actors if p not in mentioned_actors]
        ambiguous = [t for t in tokens if classify(t,parsed,story,nlp,place_names,declared)[0]=='unknown']
        unresolved = any(s.get('unresolved') for s in subjects)
        # Only participant clauses need ambiguous-pronoun review. A descriptor
        # of an inanimate subject is never populated with an earlier human.
        reasons = ['Не определён субъект местоимения в текущем эпизоде.'] if unresolved else []
        entity_review_reasons=[]
        if ambiguous:
            entity_review_reasons.append('Неоднозначный тип сущности: '+', '.join(t.text for t in ambiguous)+'. Требуется смысловая проверка исходной цитаты.')
        if mentioned_actors:
            entity_review_reasons.append('Участники воображаемой или условной версии упомянуты, но их реальное присутствие не установлено.')
        if until_transition:
            reasons.append('Фраза завершает прежний период и вводит новое событие; его дата требует отдельного основания.')
        context = {'version': VERSION, 'source': span(story, a, b),
            'scope': span(story, scope_start, b), 'time': copy.deepcopy(current_time),
            'place': copy.deepcopy(current_place), 'participants': actors, 'mentioned_participants':mentioned_actors,
            'animals': animals, 'predicates': predicates,
            'subjects': subjects, 'needs_review': bool(reasons), 'review_reasons': reasons}
        # NLP type uncertainty is evidence for the semantic reviewer, not a
        # factual contradiction that independently blocks an audited image.
        context['entity_review_reasons']=entity_review_reasons
        context['implicit_actor'] = not subjects and not actors and any(
            t.pos == 'VERB' and t.feats.get('VerbForm') == 'Fin' and t.feats.get('Number') == 'Plur'
            and t.feats.get('Voice') == 'Act' and re.match(
                r'^(?:выставля|мета|пропуска|закрыва|отступа|стро|носи|готови|руб|выруба|поднима|спуска|'
                r'встреча|проверя|собира|отправля|перемеща)', t.text, re.I)
            for t in tokens)
        if context['implicit_actor'] and predicates:
            implicit = {'name':'участники описанного действия','human':True,'number':'plural',
                'gender':None,'source_role':'implicit_action_group','evidence':predicates[0],
                'entity_kind':'group'}
            implicit['action_evidence']=actor_actions(implicit,parsed,story)
            if implicit['action_evidence'] and not any(e.get('asserted',True) for e in implicit['action_evidence']):
                context['mentioned_participants'].append(implicit)
            else:
                context['participants'] = [implicit]
        context['past_participant_action'] = bool(actors) and any(
            t.pos == 'VERB' and t.feats.get('VerbForm') == 'Fin' and t.feats.get('Tense') == 'Past' for t in tokens)
        context['past_predicate'] = any(nlp.analyze_token_morph(t.text).pos == 'VERB' and
                                       nlp.analyze_token_morph(t.text).tense == 'past' for t in tokens)
        context['mixed_time_evidence'] = mixed_time_evidence
        result.append(context)
        previous_end = b
    return result


def source_events(story):
    """Return isolated source events; callers cannot alter cached parser evidence."""
    return copy.deepcopy(_contexts(story))


def for_source(story, quote, start=None):
    if type(start) is not int or story[start:start + len(quote)] != quote:
        start = story.find(quote)
        if start < 0 or story.find(quote, start + 1) >= 0:
            return None  # Repeated legacy quotes need explicit offsets.
    rows = [r for r in _contexts(story) if r['source']['start'] < start + len(quote) and r['source']['end'] > start]
    if not rows:
        return None
    context = copy.deepcopy(rows[-1])
    context['source'] = span(story, start, start + len(quote))
    context['scope'] = span(story, context['scope']['start'], start + len(quote))
    # Voice cuts may select only a clause. Do not transfer actors or locations
    # from a later clause of the parent sentence into that cut.
    def applicable(item):
        evidence = item.get('link') or item['evidence']
        return evidence['start'] < start + len(quote)
    context['participants'] = [p for r in rows for p in r['participants'] if applicable(p)]
    context['mentioned_participants'] = [p for r in rows for p in r.get('mentioned_participants',[]) if applicable(p)]
    context['animals'] = [p for r in rows for p in r.get('animals', []) if start <= p['evidence']['start'] < start+len(quote)]
    context['subjects'] = [p for r in rows for p in r['subjects'] if applicable(p)]
    context['predicates'] = [p for r in rows for p in r['predicates'] if start <= p['start'] < start + len(quote)]
    from ..narrative_continuity import selected_event_source, protagonist_action
    parent = selected_event_source(story,context['source'])
    if protagonist_action(parent['quote']) and not any(p.get('source_role')=='narrated_protagonist' for p in context['participants']):
        context['participants'].append({'name':'главный герой','human':True,'gender':None,
            'number':'singular','source_role':'narrated_protagonist','evidence':parent})
    layers = {r['time']['layer'] if r['time'] else 'undated_past' for r in rows if r['time'] or r.get('past_predicate')}
    if any(r.get('mixed_time_evidence') and r['mixed_time_evidence']['end'] <= start+len(quote) for r in rows):
        layers.add('undated_past')
    context['time_layers'] = sorted(layers)
    context['implicit_actor'] = any(r.get('implicit_actor') for r in rows)
    context['past_participant_action'] = any(r.get('past_participant_action') for r in rows)
    context['review_reasons'] = list(dict.fromkeys(reason for r in rows for reason in r['review_reasons']))
    context['entity_review_reasons'] = list(dict.fromkeys(reason for r in rows for reason in r.get('entity_review_reasons',[])))
    if len(layers) > 1:
        context['review_reasons'].append('В одном фрагменте смешаны разные временные состояния.')
    context['needs_review'] = bool(context['review_reasons'])
    for field in ('time', 'place'):
        if context.get(field) and context[field]['evidence']['end'] > start + len(quote):
            context[field] = None
    context['story_hash'] = hashlib.sha256(story.encode()).hexdigest()
    return context


def narrative_treatment(context, source_layer):
    """Concrete source events outrank illustrative city/ruins template cycles."""
    quote = context['source']['quote']
    actors = context['participants']
    layer = (context.get('time') or {}).get('layer') or source_layer
    if (not context.get('time') and context.get('past_participant_action') and source_layer == 'modern_ruins'
            and re.search(r'(?:сегодня|ныне|сейчас)\s+(?:\w+\s+){0,2}(?:извест\w*|называ\w*)', quote, re.I)):
        layer = 'documented_event'
        context['review_reasons'].append('Указано действие в прошлом и современное название; дата самого действия не связана явно.')
        context['needs_review'] = True
    direction = 'Depict exactly this selected source event: ' + quote + '.'
    from imagegen.narrative_obligations import legend_context, ILLUSTRATION_RULE
    if legend_context(context):
        layer = 'historical_reconstruction'
        direction = ILLUSTRATION_RULE + direction
    if context.get('time'):
        direction += ' Keep the narrated temporal context: ' + context['time']['evidence']['quote'] + '.'
    if context.get('place'):
        direction += ' Keep the source-backed setting: ' + context['place']['evidence']['quote'] + '.'
        if context['place'].get('figurative'):
            direction += ' This is a figurative name for the port, not physical gates; do not invent a gate structure.'
    if actors:
        direction += ' Include the source participants performing the narrated action: ' + ', '.join(p['name'] for p in actors) + '.'
    else:
        direction += ' Frame the described subject; do not add people from an earlier scene.'
    if context.get('animals'):
        direction += ' Keep the source-described animals visible: ' + ', '.join(p['name'] for p in context['animals']) + '. They are not human participants; do not erase them merely because no humans are present.'
    direction += ' Do not replace this event with an unrelated city panorama, ruined walls or invented architecture/materials.'
    from imagegen.scene_semantics import signals, direction_rules
    direction += ' ' + ' '.join(direction_rules(signals(quote)))
    return {'shot_type': 'main_action' if actors else 'establishing', 'visual_time_layer': layer,
            'treatment': 'source_backed_narrative_event', 'source_binding': 'narrated_event', 'direction': direction}

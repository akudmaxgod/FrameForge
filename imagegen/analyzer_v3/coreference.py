"""Coreference Resolution, Discourse Tracking, and Dialogue Graph for Story Analyzer V3.

Modular Architecture:
- DialogueTracker: Extracts speaker turns, addressees, and inquiry topics across events.
- CoreferenceCandidateGenerator: Generates gender/number-compatible antecedent candidates.
- CoreferenceFilterService: Evaluates Binding Theory Principle A (reflexives) and Principle B (disjoint reference).
- CoreferenceScorer: Calculates hierarchical ranking scores, detects genuine ambiguity, and assigns VisualPolicy.
- CoreferenceResolverV3: Facade coordinator integrating with ParsedDocument and DocumentContext.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from imagegen.analyzer_v3.context import DocumentContext
from imagegen.analyzer_v3.models import (
    CoreferenceDecision,
    CoreferenceReasonCode,
    CoreferenceStatus,
    DocumentIR,
    EntityIR,
    EntityModalityV3,
    EntityType,
    EventIR,
    MentionIR,
    SourceSpan,
    SyntacticRole,
    VisualPolicy,
)
from imagegen.analyzer_v3.nlp import ParsedDocument, RussianNLPAdapter


# Scoring Constants
SCORE_SAME_CLAUSE_ANTECEDENT = 1.8
SCORE_QUESTION_TOPIC = 1.8
SCORE_ANSWER_CONTINUATION = 1.5
SCORE_SUBSEQUENT_ACTION = 1.4
SCORE_PREVIOUS_EVENT_SUBJECT = 1.0
SCORE_CURRENT_EVENT_SUBJECT = 0.9
SCORE_ACTION_CHAIN = 0.8
SCORE_DIALOGUE_INTERLOCUTOR = 0.5
SCORE_LOCAL_RECENCY = 0.4
SCORE_GLOBAL_PROTAGONIST = 0.1

MIN_AMBIGUITY_SCORE_MARGIN = 0.20
CONFIDENCE_THRESHOLD = 0.60

PRONOUN_LEXICON: Dict[str, Dict[str, str]] = {
    # Masculine singular
    "он": {"gender": "male", "number": "singular", "case": "nomn", "person": "3per"},
    "его": {"gender": "male", "number": "singular", "case": "accs", "person": "3per"},
    "него": {"gender": "male", "number": "singular", "case": "accs", "person": "3per"},
    "ему": {"gender": "male", "number": "singular", "case": "datv", "person": "3per"},
    "нему": {"gender": "male", "number": "singular", "case": "datv", "person": "3per"},
    "им": {"gender": "male", "number": "singular", "case": "ablt", "person": "3per"},
    "нем": {"gender": "male", "number": "singular", "case": "loct", "person": "3per"},
    "нём": {"gender": "male", "number": "singular", "case": "loct", "person": "3per"},
    "тот": {"gender": "male", "number": "singular", "case": "nomn", "person": "3per"},
    "того": {"gender": "male", "number": "singular", "case": "accs", "person": "3per"},
    "тому": {"gender": "male", "number": "singular", "case": "datv", "person": "3per"},
    # Feminine singular
    "она": {"gender": "female", "number": "singular", "case": "nomn", "person": "3per"},
    "её": {"gender": "female", "number": "singular", "case": "accs", "person": "3per"},
    "ее": {"gender": "female", "number": "singular", "case": "accs", "person": "3per"},
    "неё": {"gender": "female", "number": "singular", "case": "accs", "person": "3per"},
    "нее": {"gender": "female", "number": "singular", "case": "accs", "person": "3per"},
    "ей": {"gender": "female", "number": "singular", "case": "datv", "person": "3per"},
    "ней": {"gender": "female", "number": "singular", "case": "loct", "person": "3per"},
    "ею": {"gender": "female", "number": "singular", "case": "ablt", "person": "3per"},
    "нею": {"gender": "female", "number": "singular", "case": "ablt", "person": "3per"},
    "та": {"gender": "female", "number": "singular", "case": "nomn", "person": "3per"},
    "той": {"gender": "female", "number": "singular", "case": "datv", "person": "3per"},
    # Neuter singular
    "оно": {"gender": "neuter", "number": "singular", "case": "nomn", "person": "3per"},
    "то": {"gender": "neuter", "number": "singular", "case": "nomn", "person": "3per"},
    # Plural
    "они": {"gender": "plural", "number": "plural", "case": "nomn", "person": "3per"},
    "их": {"gender": "plural", "number": "plural", "case": "accs", "person": "3per"},
    "них": {"gender": "plural", "number": "plural", "case": "accs", "person": "3per"},
    "ими": {"gender": "plural", "number": "plural", "case": "ablt", "person": "3per"},
    "ними": {"gender": "plural", "number": "plural", "case": "ablt", "person": "3per"},
    "те": {"gender": "plural", "number": "plural", "case": "nomn", "person": "3per"},
    "тех": {"gender": "plural", "number": "plural", "case": "accs", "person": "3per"},
    # Ambiguous gender/number (nim: с ним / к ним, тем: тем / к тем)
    "ним": {"gender": "any", "number": "any", "case": "any", "person": "3per"},
    "тем": {"gender": "any", "number": "any", "case": "any", "person": "3per"},
    # Reflexive
    "себя": {"gender": "any", "number": "any", "case": "accs", "person": "reflexive"},
    "себе": {"gender": "any", "number": "any", "case": "datv", "person": "reflexive"},
    "собой": {"gender": "any", "number": "any", "case": "ablt", "person": "reflexive"},
    "собою": {"gender": "any", "number": "any", "case": "ablt", "person": "reflexive"},
}

TOPIC_PREPOSITIONS = {"о", "об", "обо", "про", "насчёт", "насчет", "с", "со"}

RE_INQUIRY_VERBS = re.compile(
    r"\b(?:спросил|спросила|спросили|поинтересовался|поинтересовалась|поинтересовались|"
    r"выяснял|выясняла|выясняли|уточнил|уточнила|уточнили|задал\s+вопрос|"
    r"передал|передала|передали|запросил|запросила|запросили|"
    r"рассказали|рассказал|рассказала|говорил|говорила|говорили|вспомнил|вспомнила)\w*\b",
    re.IGNORECASE
)

RE_EXPLANATION_START = re.compile(
    r"^[—–-]?\s*(?:потому\s+что|дело\s+в\s+том|из-за\s+того|так\s+как|оттого\s+что)\b",
    re.IGNORECASE
)

RE_SUBSEQUENT_START = re.compile(
    r"^[—–-]?\s*(?:а\s+потом|затем|после\s+этого|позже|вскоре|и\s+потом)\b",
    re.IGNORECASE
)


@dataclass
class DialogueTurnInfo:
    """Represents dialogue turn tracking and topic state for an event."""
    event_id: str
    event_index: int
    is_dialogue: bool
    speaker_entity_id: Optional[str] = None
    addressee_entity_id: Optional[str] = None
    inquiry_target_entity_id: Optional[str] = None
    is_explanation: bool = False
    is_subsequent_continuation: bool = False
    evidence_span: Optional[SourceSpan] = None


class DialogueTracker:
    """Extracts speaker turns, addressees, and inquiry topics across events."""

    def __init__(self, nlp_adapter: RussianNLPAdapter):
        self.nlp = nlp_adapter

    def track_dialogue_flow(
        self,
        doc_ir: DocumentIR,
        ctx: DocumentContext,
        entity_map: Dict[str, EntityIR],
    ) -> List[DialogueTurnInfo]:
        """Processes events chronologically and tracks dialogue context."""
        turns: List[DialogueTurnInfo] = []
        last_speaker_id: Optional[str] = None
        last_addressee_id: Optional[str] = None
        active_inquiry_target_id: Optional[str] = None
        active_inquiry_span: Optional[SourceSpan] = None
        last_explanation_span: Optional[SourceSpan] = None
        last_explanation_topic_id: Optional[str] = None

        for idx, ev in enumerate(doc_ir.events):
            ev_text = ev.span.text.strip()
            is_dialogue = bool(
                ev_text.startswith(("—", "–", "-", "«", "\""))
                or ev.event_type == "dialogue"
                or ev.dialogue_speakers
            )

            speaker_id = ev.actor_ids[0] if (is_dialogue and ev.actor_ids) else None
            addressee_id = None
            inquiry_target_id = None
            is_explanation = False
            is_subsequent = False

            has_inquiry = bool(RE_INQUIRY_VERBS.search(ev_text) or "?" in ev_text)
            if has_inquiry:
                for ent_id in ev.mentioned_entity_ids:
                    ent = entity_map.get(ent_id)
                    if not ent:
                        continue
                    for m_id in ent.mention_ids:
                        m = ctx.get_mention(m_id)
                        if m and m.event_id == ev.event_id:
                            pre_slice = ev_text[max(0, m.span.start - ev.span.start - 10):m.span.start - ev.span.start].lower()
                            if any(f" {prep} " in f" {pre_slice} " or pre_slice.endswith(f"{prep} ") for prep in TOPIC_PREPOSITIONS):
                                inquiry_target_id = ent.entity_id
                                active_inquiry_target_id = ent.entity_id
                                active_inquiry_span = ev.span
                                break
                    if inquiry_target_id:
                        break

                if not inquiry_target_id:
                    q_idx = ev_text.find("?")
                    for ent_id in ev.mentioned_entity_ids:
                        ent = entity_map.get(ent_id)
                        if ent and ent.entity_type == EntityType.PERSON:
                            if speaker_id and ent.entity_id == speaker_id:
                                continue
                            if last_speaker_id and ent.entity_id == last_speaker_id:
                                continue
                            m_in_q = any(
                                m.event_id == ev.event_id and (q_idx == -1 or (m.span.start - ev.span.start) < q_idx)
                                for m_id in ent.mention_ids
                                for m in [ctx.get_mention(m_id)]
                                if m
                            )
                            if m_in_q:
                                inquiry_target_id = ent.entity_id
                                active_inquiry_target_id = ent.entity_id
                                active_inquiry_span = ev.span
                                break

            is_reply_to_inquiry = False
            if active_inquiry_target_id:
                if RE_EXPLANATION_START.search(ev_text):
                    is_reply_to_inquiry = True
                elif is_dialogue and idx > 0 and turns[idx - 1].inquiry_target_entity_id:
                    is_reply_to_inquiry = True

            if is_reply_to_inquiry:
                is_explanation = True
                last_explanation_span = ev.span
                last_explanation_topic_id = active_inquiry_target_id
                inquiry_target_id = active_inquiry_target_id
                if not speaker_id and last_speaker_id and len(ev.mentioned_entity_ids) > 0:
                    speaker_id = next((e_id for e_id in ev.mentioned_entity_ids if e_id != active_inquiry_target_id), None)

            if RE_SUBSEQUENT_START.search(ev_text) and last_explanation_topic_id and not ev.actor_ids:
                is_subsequent = True

            if is_dialogue:
                if not speaker_id and last_addressee_id:
                    speaker_id = last_addressee_id
                if speaker_id and last_speaker_id and speaker_id != last_speaker_id:
                    addressee_id = last_speaker_id
                elif speaker_id:
                    other_part = next((e_id for e_id in ev.mentioned_entity_ids if e_id != speaker_id), None)
                    if other_part:
                        addressee_id = other_part

                if speaker_id:
                    last_speaker_id = speaker_id
                if addressee_id:
                    last_addressee_id = addressee_id

            turn = DialogueTurnInfo(
                event_id=ev.event_id,
                event_index=idx,
                is_dialogue=is_dialogue,
                speaker_entity_id=speaker_id,
                addressee_entity_id=addressee_id,
                inquiry_target_entity_id=inquiry_target_id or (active_inquiry_target_id if is_explanation else None),
                is_explanation=is_explanation,
                is_subsequent_continuation=is_subsequent,
                evidence_span=active_inquiry_span if is_explanation else (last_explanation_span if is_subsequent else ev.span),
            )
            turns.append(turn)

        return turns


class CoreferenceCandidateGenerator:
    """Generates grammatically compatible prior candidates for a pronoun."""

    @staticmethod
    def generate_candidates(
        mention: MentionIR,
        p_info: Dict[str, str],
        doc_ir: DocumentIR,
    ) -> List[EntityIR]:
        p_gender = p_info.get("gender")
        candidates: List[EntityIR] = []

        for ent in doc_ir.entities:
            if ent.first_mention_start >= mention.span.start:
                continue

            # Third-person forms cannot refer to the first-person discourse
            # participant. Keeping that entity in the pool creates a false
            # subject/object ambiguity in texts such as "Я встретил Бориса.
            # Он кивнул".
            if "first_person_narrator" in ent.attributes and p_info.get("person") == "3per":
                continue

            if p_gender == "male":
                if ent.gender == "female":
                    continue
                if ent.entity_type not in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE, EntityType.VEHICLE, EntityType.OBJECT):
                    continue
            elif p_gender == "female":
                if ent.gender == "male":
                    continue
                if ent.entity_type not in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE):
                    continue
            elif p_gender == "plural":
                if ent.entity_type not in (EntityType.PERSON, EntityType.ANIMAL, EntityType.CREATURE, EntityType.VEHICLE, EntityType.OBJECT):
                    continue

            candidates.append(ent)

        return candidates


class CoreferenceFilterService:
    """Applies syntactic constraints and Binding Theory Principle A & B."""

    @staticmethod
    def is_reporting_clause(mention: MentionIR, event: Optional[EventIR]) -> Tuple[bool, Optional[int]]:
        if not event:
            return False, None
        ev_text = event.span.text
        rel_start = mention.span.start - event.span.start
        rel_end = mention.span.end - event.span.start
        before_text = ev_text[:rel_start]

        reporting_verbs = (
            "спросил", "спросила", "ответил", "ответила", "сказал", "сказала",
            "передал", "передала", "доложил", "доложила", "воскликнул", "воскликнула",
            "прошептал", "прошептала", "пояснил", "пояснила", "уточнил", "уточнила"
        )
        window = ev_text[max(0, rel_start - 30):min(len(ev_text), rel_end + 30)].lower()
        has_verb = any(v in window for v in reporting_verbs)

        dashes = [m.start() for m in re.finditer(r"[—–»\"\-]", before_text)]
        if has_verb and dashes:
            quote_end_offset = event.span.start + dashes[-1]
            return True, quote_end_offset
        return False, None

    @staticmethod
    def find_local_predicate_subject(
        mention: MentionIR,
        event: Optional[EventIR],
        ctx: DocumentContext,
        parsed_doc: ParsedDocument,
        decisions_by_mention_id: Optional[Dict[str, CoreferenceDecision]],
    ) -> Optional[str]:
        tok_by_id = parsed_doc.token_by_id
        m_tokens = parsed_doc.find_tokens_in_span(mention.span.start, mention.span.end)
        if not m_tokens:
            return None
        m_tok = m_tokens[0]

        pred_tok = parsed_doc.get_parent_token(m_tok)
        if not pred_tok:
            return None

        if pred_tok.rel == "case" and pred_tok.head_id in tok_by_id:
            pred_tok = tok_by_id[pred_tok.head_id]

        subj_tok = None
        curr = pred_tok
        visited = set()
        while curr and curr.id not in visited and subj_tok is None:
            visited.add(curr.id)
            children = parsed_doc.get_children_tokens(curr)
            subj_tok = next((t for t in children if t.rel in ("nsubj", "nsubj:pass")), None)
            if subj_tok:
                break
            if curr.rel == "conj" and curr.head_id in tok_by_id:
                curr = tok_by_id[curr.head_id]
            else:
                break

        if not subj_tok:
            return None

        ev_mentions = ctx.get_event_mentions(event.event_id) if event else []
        subj_m = next(
            (m for m in ev_mentions if (m.span.start <= subj_tok.start and m.span.end >= subj_tok.stop)
             or (m.span.start < subj_tok.stop and m.span.end > subj_tok.start)),
            None
        )
        if subj_m:
            if decisions_by_mention_id and subj_m.mention_id in decisions_by_mention_id:
                dec = decisions_by_mention_id[subj_m.mention_id]
                if dec.resolved_entity_ids:
                    return dec.resolved_entity_ids[0]
            ent = ctx.get_entity_for_mention(subj_m.mention_id)
            if ent:
                return ent.entity_id

        return None


class CoreferenceScorer:
    """Calculates candidate scores, ambiguity overrides, and visual policies."""

    def __init__(self, filter_service: CoreferenceFilterService):
        self.filter = filter_service

    def score_candidate(
        self,
        candidate: EntityIR,
        mention: MentionIR,
        p_info: Dict[str, str],
        event: Optional[EventIR],
        ev_index: int,
        turn: Optional[DialogueTurnInfo],
        turns: List[DialogueTurnInfo],
        doc_ir: DocumentIR,
        ctx: DocumentContext,
        event_subject_history: Dict[str, Optional[str]],
        parsed_doc: ParsedDocument,
        decisions_by_mention_id: Optional[Dict[str, CoreferenceDecision]] = None,
    ) -> Tuple[float, List[str], List[SourceSpan]]:
        score = 0.0
        reasons: List[str] = []
        evidence: List[SourceSpan] = []

        is_reporting, quote_end_offset = self.filter.is_reporting_clause(mention, event)
        p_person = p_info.get("person")
        is_oblique_or_obj = mention.syntactic_role in (SyntacticRole.OBJECT, SyntacticRole.OBLIQUE) or p_info.get("case") != "nomn"
        local_subj_ent_id = self.filter.find_local_predicate_subject(
            mention, event, ctx, parsed_doc, decisions_by_mention_id
        )

        # Priority 1: Same Clause Antecedent
        if event:
            allow_same_clause = True
            if local_subj_ent_id and p_person != "reflexive" and is_oblique_or_obj:
                if candidate.entity_id == local_subj_ent_id:
                    allow_same_clause = False

            if allow_same_clause:
                ev_mentions = ctx.get_event_mentions(event.event_id)
                same_event_mentions = [
                    m for m in ev_mentions
                    if m.span.end < mention.span.start
                    and (not is_reporting or (quote_end_offset is not None and m.span.start >= quote_end_offset))
                    and m.mention_id in candidate.mention_ids
                ]
                if same_event_mentions:
                    score += SCORE_SAME_CLAUSE_ANTECEDENT
                    reasons.append("SAME_CLAUSE_ANTECEDENT")
                    evidence.append(same_event_mentions[-1].span)

        # Binding Theory Principle A & B
        if local_subj_ent_id and is_oblique_or_obj:
            if p_person == "reflexive":
                if candidate.entity_id == local_subj_ent_id:
                    score += 3.5
                    reasons.append("REFLEXIVE_LOCAL_SUBJECT")
                else:
                    score -= 3.5
            else:
                if candidate.entity_id == local_subj_ent_id:
                    score -= 5.0
                    reasons.append("DISQUALIFIED_LOCAL_SUBJECT")
                else:
                    if ev_index > 0:
                        prev_ev = doc_ir.events[ev_index - 1]
                        if candidate.entity_id in prev_ev.mentioned_entity_ids and candidate.entity_id != local_subj_ent_id:
                            score += 2.5
                            reasons.append("INTERLOCUTOR_COMPLEMENT")
                            evidence.append(prev_ev.span)

        # Priority 1c: Reporting clause speaker attribution
        p_text = mention.text.lower()
        is_demonstrative = p_text in ("тот", "та", "то", "те")
        if is_reporting and not is_demonstrative and ev_index > 0:
            prev_ev = doc_ir.events[ev_index - 1]
            prev_subj = event_subject_history.get(prev_ev.event_id)
            if candidate.entity_id == prev_subj or (turn and candidate.entity_id == turn.speaker_entity_id):
                score += 1.5
                reasons.append("REPORTING_SPEAKER")
                evidence.append(prev_ev.span)

        # Priority 1b: Demonstrative 'тот' preference for remote antecedent
        if is_demonstrative and ev_index > 0:
            prev_ev = doc_ir.events[ev_index - 1]
            prev_subj = event_subject_history.get(prev_ev.event_id)
            if candidate.entity_id in prev_ev.mentioned_entity_ids and candidate.entity_id != prev_subj:
                score += 2.5
                reasons.append("REMOTE_DEMONSTRATIVE")
                evidence.append(prev_ev.span)
            elif candidate.entity_id == prev_subj:
                score -= 2.0

        # Priority 2: Question Topic and Answer Continuation
        if turn and turn.inquiry_target_entity_id == candidate.entity_id:
            if turn.is_explanation:
                score += SCORE_QUESTION_TOPIC + SCORE_ANSWER_CONTINUATION
                reasons.append("QUESTION_TOPIC")
                reasons.append("ANSWER_CONTINUATION")
                if turn.evidence_span:
                    evidence.append(turn.evidence_span)

        # Priority 3: Subsequent Action Continuation
        if turn and turn.is_subsequent_continuation and turn.evidence_span:
            prior_turn = turns[ev_index - 1] if ev_index > 0 else None
            if prior_turn and prior_turn.inquiry_target_entity_id == candidate.entity_id:
                score += SCORE_SUBSEQUENT_ACTION
                reasons.append("SUBSEQUENT_ACTION")
                evidence.append(prior_turn.evidence_span)

        # Priority 4: Subject of Preceding Event
        if ev_index > 0:
            prev_ev = doc_ir.events[ev_index - 1]
            prev_subj = event_subject_history.get(prev_ev.event_id)
            is_disqualified_obj = (
                local_subj_ent_id is not None
                and p_person != "reflexive"
                and is_oblique_or_obj
                and candidate.entity_id == local_subj_ent_id
            )
            if prev_subj == candidate.entity_id and not is_disqualified_obj:
                score += SCORE_PREVIOUS_EVENT_SUBJECT
                reasons.append("PREVIOUS_EVENT_SUBJECT")
                evidence.append(prev_ev.span)

            prev_mentions = ctx.get_event_mentions(prev_ev.event_id)
            if re.search(r"\b(?:позвонил\w*|набрал\w*|вызвал\w*)\b", prev_ev.span.text.lower()):
                if any(
                    m.mention_id in candidate.mention_ids
                    and m.syntactic_role in (SyntacticRole.OBJECT, SyntacticRole.OBLIQUE)
                    for m in prev_mentions
                ):
                    score += 2.0
                    reasons.append(CoreferenceReasonCode.CALL_RECIPIENT_CONTINUATION.value)
                    evidence.append(prev_ev.span)
            prev_quote_matches = [m for m in prev_mentions if m.mention_id in candidate.mention_ids]
            for m in prev_quote_matches:
                rel_pos = m.span.start - prev_ev.span.start
                text_before = prev_ev.span.text[:rel_pos]
                text_after = prev_ev.span.text[rel_pos:]
                if "«" in text_before and "»" in text_after:
                    if m.syntactic_role == SyntacticRole.SUBJECT:
                        score += 0.8
                        reasons.append("EMBEDDED_SUBJECT")
                        evidence.append(m.span)
                        break

        # Priority 4c: Syntactic Parallelism
        if mention.syntactic_role == SyntacticRole.SUBJECT and candidate.entity_type == EntityType.PERSON:
            cand_mentions = [
                ctx.get_mention(mid)
                for mid in candidate.mention_ids
                if ctx.get_mention(mid) and ctx.get_mention(mid).span.start < mention.span.start
            ]
            if cand_mentions:
                last_cand_m = cand_mentions[-1]
                if last_cand_m.syntactic_role == SyntacticRole.SUBJECT:
                    score += 0.6
                    reasons.append("SYNTACTIC_PARALLELISM")
                elif last_cand_m.syntactic_role == SyntacticRole.ATTRIBUTE:
                    score -= 0.3

        # Priority 5: Action Chain
        chain_len = 0
        for back_idx in range(ev_index - 1, max(-1, ev_index - 4), -1):
            b_ev = doc_ir.events[back_idx]
            if event_subject_history.get(b_ev.event_id) == candidate.entity_id:
                chain_len += 1
            else:
                break
        if chain_len >= 2:
            score += SCORE_ACTION_CHAIN
            reasons.append("ACTION_CHAIN")

        # Priority 6: Dialogue Interlocutor
        if turn and turn.is_dialogue:
            if turn.is_explanation and turn.speaker_entity_id == candidate.entity_id:
                if p_info.get("person") == "3per":
                    score -= 1.5
            elif turn.speaker_entity_id == candidate.entity_id or turn.addressee_entity_id == candidate.entity_id:
                score += SCORE_DIALOGUE_INTERLOCUTOR
                reasons.append("DIALOGUE_INTERLOCUTOR")

        # Priority 7: Local Recency Distance Decay
        last_seen_event_dist = 999
        for past_idx in range(ev_index, -1, -1):
            past_ev = doc_ir.events[past_idx]
            if candidate.entity_id in past_ev.mentioned_entity_ids or candidate.entity_id in past_ev.actor_ids:
                last_seen_event_dist = ev_index - past_idx
                break
        if last_seen_event_dist < 6:
            decay_score = SCORE_LOCAL_RECENCY * (1.0 / (last_seen_event_dist + 1))
            score += decay_score
            reasons.append("LOCAL_RECENCY")

        # Global Protagonist
        if len(candidate.mention_ids) >= 3:
            score += SCORE_GLOBAL_PROTAGONIST

        return score, reasons, evidence

    def is_subject_object_ambiguity(
        self,
        candidates: List[EntityIR],
        scored_candidates: List[Tuple[EntityIR, float, List[str], List[SourceSpan]]],
        mention: MentionIR,
        event: Optional[EventIR],
        ev_index: int,
        ctx: DocumentContext,
        doc_ir: DocumentIR,
        top_reasons: List[str],
    ) -> bool:
        if ev_index <= 0 or not event:
            return False

        if mention.syntactic_role != SyntacticRole.SUBJECT:
            return False

        strong_context_reasons = {
            "QUESTION_TOPIC", "ANSWER_CONTINUATION", "SUBSEQUENT_ACTION",
            "ACTION_CHAIN", "REPORTING_SPEAKER", "REMOTE_DEMONSTRATIVE"
        }
        if any(r in strong_context_reasons for r in top_reasons):
            return False

        ev_mentions = ctx.get_event_mentions(event.event_id)
        current_event_pronouns = [m for m in ev_mentions if m.mention_type == "pronoun"]
        # A second participant-marking oblique pronoun ("Он подошёл к нему")
        # supplies a relational complement. Bare possessive/accusative forms
        # such as "спрятал его в карман" may denote an inanimate object and do
        # not resolve competition between the prior subject and recipient.
        participant_complements = {
            "нему", "него", "ней", "неё", "нее", "ним", "них",
            "ему", "ей", "им", "ними", "себе", "собой", "собою",
        }
        if any(
            other.mention_id != mention.mention_id
            and (other.text.casefold() in participant_complements
                 or other.syntactic_role == SyntacticRole.SUBJECT)
            for other in current_event_pronouns
        ):
            return False

        prev_ev = doc_ir.events[ev_index - 1]
        prev_mentions = ctx.get_event_mentions(prev_ev.event_id)

        prev_subj_cand_ids = set()
        prev_obj_cand_ids = set()

        cand_map = {c.entity_id: c for c in candidates if c.entity_type == EntityType.PERSON}

        for pm in prev_mentions:
            for cid, c in cand_map.items():
                if pm.mention_id in c.mention_ids:
                    if pm.syntactic_role == SyntacticRole.SUBJECT:
                        prev_subj_cand_ids.add(cid)
                    elif pm.syntactic_role in (SyntacticRole.OBJECT, SyntacticRole.OBLIQUE):
                        prev_obj_cand_ids.add(cid)

        for cid, c in cand_map.items():
            if cid in prev_ev.mentioned_entity_ids and cid not in prev_subj_cand_ids:
                prev_obj_cand_ids.add(cid)

        if prev_subj_cand_ids and (prev_obj_cand_ids - prev_subj_cand_ids):
            return True

        return False

    def determine_visual_policy(
        self,
        candidate: EntityIR,
        mention: MentionIR,
        event: Optional[EventIR],
        turn: Optional[DialogueTurnInfo],
        reasons: List[str],
    ) -> VisualPolicy:
        if "QUESTION_TOPIC" in reasons or "ANSWER_CONTINUATION" in reasons:
            return VisualPolicy.MENTION_ONLY

        if candidate.modality == EntityModalityV3.MENTIONED:
            return VisualPolicy.MENTION_ONLY

        if candidate.modality == EntityModalityV3.VOICE_ONLY:
            return VisualPolicy.VOICE_ONLY

        if candidate.modality == EntityModalityV3.DEPICTED:
            return VisualPolicy.DEPICTED_ONLY

        if "SUBSEQUENT_ACTION" in reasons:
            return VisualPolicy.REFERENT_CONFIRMED

        if event and candidate.entity_id in event.actor_ids:
            return VisualPolicy.REFERENT_CONFIRMED

        return VisualPolicy.REFERENT_CONFIRMED


class CoreferenceResolverV3:
    """Facade for deterministic narrative coreference resolution."""

    def __init__(self, nlp_adapter: Optional[RussianNLPAdapter] = None):
        self.nlp = nlp_adapter or RussianNLPAdapter.get_instance()
        self.dialogue_tracker = DialogueTracker(self.nlp)
        self.candidate_generator = CoreferenceCandidateGenerator()
        self.filter_service = CoreferenceFilterService()
        self.scorer = CoreferenceScorer(self.filter_service)

    def resolve(
        self,
        story_text: str,
        doc_ir: DocumentIR,
        parsed_doc: Optional[ParsedDocument] = None,
    ) -> List[CoreferenceDecision]:
        """Resolves all pronoun mentions in the document into strict CoreferenceDecisions."""
        if parsed_doc is None:
            parsed_doc = self.nlp.parse_document(story_text)

        ctx = DocumentContext(doc_ir, max_len=len(story_text))
        decisions: List[CoreferenceDecision] = []
        entity_map = {ent.entity_id: ent for ent in doc_ir.entities}
        event_map = {ev.event_id: ev for ev in doc_ir.events}

        turns = self.dialogue_tracker.track_dialogue_flow(doc_ir, ctx, entity_map)
        turns_by_event = {t.event_id: t for t in turns}

        decisions_by_mention_id: Dict[str, CoreferenceDecision] = {}
        pronoun_mentions = self._gather_pronoun_mentions(story_text, doc_ir, ctx)

        event_subject_history: Dict[str, Optional[str]] = {}
        for ev in doc_ir.events:
            ev_subj = None
            if len(ev.actor_ids) == 1:
                ev_subj = ev.actor_ids[0]
            elif not ev.actor_ids:
                subj_mentions = [m for m in ctx.get_event_mentions(ev.event_id) if m.syntactic_role == SyntacticRole.SUBJECT]
                subj_ent_ids = {
                    ctx.get_entity_for_mention(sm.mention_id).entity_id
                    for sm in subj_mentions
                    if ctx.get_entity_for_mention(sm.mention_id)
                }
                if len(subj_ent_ids) == 1:
                    ev_subj = list(subj_ent_ids)[0]
            event_subject_history[ev.event_id] = ev_subj

        for p_mention in pronoun_mentions:
            p_text = p_mention.text.lower()
            p_info = PRONOUN_LEXICON.get(p_text, {})
            ev = event_map.get(p_mention.event_id) if p_mention.event_id else None
            ev_index = turns_by_event[ev.event_id].event_index if (ev and ev.event_id in turns_by_event) else 0
            turn = turns_by_event.get(ev.event_id) if ev else None

            if self._is_inanimate_reference(p_mention, ev, doc_ir):
                decision = self._resolve_inanimate_pronoun(p_mention, ev, ev_index, doc_ir)
                decisions.append(decision)
                decisions_by_mention_id[p_mention.mention_id] = decision
                continue

            candidates = self.candidate_generator.generate_candidates(p_mention, p_info, doc_ir)
            if not candidates:
                decision = CoreferenceDecision(
                    mention_id=p_mention.mention_id,
                    resolved_entity_ids=[],
                    status=CoreferenceStatus.UNRESOLVED_SAFE,
                    confidence=0.5,
                    margin=0.0,
                    evidence_spans=[p_mention.span],
                    rejected_candidates=[],
                    candidate_entity_ids=[],
                    reason="No grammatically compatible antecedent candidates found in prior text.",
                    reason_codes=[CoreferenceReasonCode.NO_CANDIDATES.value],
                    visual_policy=VisualPolicy.SAFE_NEUTRAL,
                    event_id=p_mention.event_id,
                    event_index=ev_index,
                )
                decisions.append(decision)
                decisions_by_mention_id[p_mention.mention_id] = decision
                continue

            scored_candidates: List[Tuple[EntityIR, float, List[str], List[SourceSpan]]] = []
            for cand in candidates:
                score, reason_codes, evidence_spans = self.scorer.score_candidate(
                    cand, p_mention, p_info, ev, ev_index, turn, turns, doc_ir, ctx, event_subject_history,
                    parsed_doc=parsed_doc, decisions_by_mention_id=decisions_by_mention_id
                )
                scored_candidates.append((cand, score, reason_codes, evidence_spans))

            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            top_cand, top_score, top_reasons, top_evidence = scored_candidates[0]
            second_score = scored_candidates[1][1] if len(scored_candidates) > 1 else 0.0
            margin = top_score - second_score if len(scored_candidates) > 1 else top_score

            visual_policy = self.scorer.determine_visual_policy(top_cand, p_mention, ev, turn, top_reasons)

            status = CoreferenceStatus.RESOLVED_LOCAL
            is_subj_obj_ambiguity = False
            if "QUESTION_TOPIC" in top_reasons or "ANSWER_CONTINUATION" in top_reasons:
                status = CoreferenceStatus.RESOLVED_LOCAL
            elif len(candidates) == 1 and top_score > 0:
                status = CoreferenceStatus.RESOLVED_LOCAL
            elif len(candidates) > 1 and margin < MIN_AMBIGUITY_SCORE_MARGIN:
                status = CoreferenceStatus.UNRESOLVED_SAFE
                visual_policy = VisualPolicy.SAFE_NEUTRAL
            elif self.scorer.is_subject_object_ambiguity(candidates, scored_candidates, p_mention, ev, ev_index, ctx, doc_ir, top_reasons):
                status = CoreferenceStatus.UNRESOLVED_SAFE
                visual_policy = VisualPolicy.SAFE_NEUTRAL
                is_subj_obj_ambiguity = True
            elif top_score < 0.6:
                status = CoreferenceStatus.UNRESOLVED_SAFE
                visual_policy = VisualPolicy.SAFE_NEUTRAL

            rejected = [
                {"entity_id": c.entity_id, "canonical_name": c.canonical_name, "score": round(sc, 3)}
                for c, sc, _, _ in scored_candidates[1:]
            ]
            all_candidate_ids = [c.entity_id for c, _, _, _ in scored_candidates]

            evidence_spans = [p_mention.span]
            for s in top_evidence:
                if s not in evidence_spans:
                    evidence_spans.append(s)

            is_unresolved = status in (CoreferenceStatus.UNRESOLVED_BLOCKING, CoreferenceStatus.UNRESOLVED_SAFE)
            resolved_ids = [] if is_unresolved else [top_cand.entity_id]

            group_event = next((
                doc_ir.events[idx]
                for idx in range(ev_index - 1, max(-1, ev_index - 4), -1)
                if re.search(r"\bмы\s+с\b", doc_ir.events[idx].span.text.lower())
            ), None) if p_text == "они" else None
            if group_event is not None:
                coordinated_ids = [
                    c.entity_id for c in candidates
                    if c.entity_id in group_event.mentioned_entity_ids
                    and "first_person_narrator" not in c.attributes
                ]
                coordinated_ids = list(dict.fromkeys(coordinated_ids))
                if len(coordinated_ids) >= 2:
                    status = CoreferenceStatus.RESOLVED_LOCAL
                    visual_policy = VisualPolicy.REFERENT_CONFIRMED
                    resolved_ids = coordinated_ids
                    is_unresolved = False
                    top_reasons = [CoreferenceReasonCode.COORDINATED_GROUP_ANTECEDENT.value]

            if is_unresolved:
                cand_names = [entity_map[cid].canonical_name for cid in all_candidate_ids if cid in entity_map]
                if is_subj_obj_ambiguity:
                    reason = f"Subject-object ambiguity detected between ({', '.join(cand_names)}); raw heuristic margin {round(margin, 2)} overridden by safety rule."
                    reason_codes = [CoreferenceReasonCode.AMBIGUOUS_CANDIDATES.value, CoreferenceReasonCode.SUBJECT_OBJECT_AMBIGUITY.value]
                elif margin < MIN_AMBIGUITY_SCORE_MARGIN:
                    reason = f"Ambiguous between candidates ({', '.join(cand_names)}); margin {round(margin, 2)} < threshold {MIN_AMBIGUITY_SCORE_MARGIN}"
                    reason_codes = [CoreferenceReasonCode.AMBIGUOUS_CANDIDATES.value]
                elif top_score < 0.6:
                    reason = f"Low candidate score ({round(top_score, 2)} < 0.6) for candidates ({', '.join(cand_names)})."
                    reason_codes = [CoreferenceReasonCode.LOW_CONFIDENCE.value]
                else:
                    reason = f"Ambiguous between candidates ({', '.join(cand_names)})."
                    reason_codes = [CoreferenceReasonCode.AMBIGUOUS_CANDIDATES.value]
            else:
                reason = f"Resolved via {', '.join(top_reasons)} (score: {round(top_score, 2)}, margin: {round(margin, 2)})."
                reason_codes = top_reasons

            decision = CoreferenceDecision(
                mention_id=p_mention.mention_id,
                resolved_entity_ids=resolved_ids,
                status=status,
                confidence=min(0.98, max(0.50, round(top_score / (top_score + 0.3), 3))) if not is_unresolved else 0.5,
                margin=round(margin, 3),
                evidence_spans=evidence_spans,
                rejected_candidates=rejected,
                candidate_entity_ids=all_candidate_ids,
                reason=reason,
                reason_codes=reason_codes,
                visual_policy=visual_policy,
                event_id=p_mention.event_id,
                event_index=ev_index,
            )
            decisions.append(decision)
            decisions_by_mention_id[p_mention.mention_id] = decision

        return decisions

    def _gather_pronoun_mentions(
        self,
        story_text: str,
        doc_ir: DocumentIR,
        ctx: DocumentContext,
    ) -> List[MentionIR]:
        existing_pronoun_mentions = [
            m for m in doc_ir.mentions
            if m.mention_type == "pronoun" and m.text.lower() in PRONOUN_LEXICON
        ]
        existing_starts = {m.span.start for m in doc_ir.mentions}

        tokens = self.nlp.split_tokens(story_text)
        new_mentions: List[MentionIR] = []
        for t in tokens:
            w_lower = t.text.lower()
            if w_lower in PRONOUN_LEXICON and t.start not in existing_starts:
                m_info = PRONOUN_LEXICON[w_lower]
                m_id = f"m_{len(doc_ir.mentions) + len(new_mentions)}"
                sec = ctx.find_section_at_offset(t.start)
                ev = ctx.find_event_at_offset(t.start)
                mention = MentionIR(
                    mention_id=m_id,
                    text=t.text,
                    normalized_text=w_lower,
                    span=t,
                    mention_type="pronoun",
                    grammatical_gender=m_info.get("gender"),
                    grammatical_number=m_info.get("number"),
                    section_id=sec.section_id if sec else "sec_0",
                    event_id=ev.event_id if ev else None,
                    syntactic_role=SyntacticRole.SUBJECT if m_info.get("case") == "nomn" else SyntacticRole.OBJECT,
                )
                new_mentions.append(mention)
                existing_starts.add(t.start)

        if new_mentions:
            doc_ir.mentions.extend(new_mentions)
            existing_pronoun_mentions.extend(new_mentions)
            ctx.rebuild()

        existing_pronoun_mentions.sort(key=lambda m: m.span.start)
        return existing_pronoun_mentions

    def _is_inanimate_reference(
        self,
        mention: MentionIR,
        event: Optional[EventIR],
        doc_ir: DocumentIR,
    ) -> bool:
        w_lower = mention.text.lower()
        if w_lower in ("нём", "нем"):
            spatial = doc_ir.metadata.get("spatial_grounding", {})
            if any(r.get("start", mention.span.start) < mention.span.start for r in spatial.get("evidence", [])):
                return True
        if w_lower in ("их", "они", "тот", "оно", "то"):
            if event:
                ev_text = event.span.text.lower()
                if re.search(r"\b(?:открыл|закрыл|взял|положил|спрятал|достал|прочитал|нашел|нашёл|уронил)\w*\b", ev_text):
                    return True
        return False

    def _resolve_inanimate_pronoun(
        self,
        mention: MentionIR,
        event: Optional[EventIR],
        ev_index: int,
        doc_ir: DocumentIR,
    ) -> CoreferenceDecision:
        prior_objects = [
            ent for ent in doc_ir.entities
            if ent.entity_type in (EntityType.OBJECT, EntityType.VEHICLE) and ent.first_mention_start < mention.span.start
        ]
        if prior_objects:
            prior_objects.sort(key=lambda x: x.first_mention_start, reverse=True)
            target = prior_objects[0]
            return CoreferenceDecision(
                mention_id=mention.mention_id,
                resolved_entity_ids=[target.entity_id],
                status=CoreferenceStatus.RESOLVED_LOCAL,
                confidence=0.85,
                margin=0.5,
                evidence_spans=[mention.span],
                rejected_candidates=[],
                candidate_entity_ids=[target.entity_id],
                reason=f"Resolved to inanimate object '{target.canonical_name}' via manipulation context.",
                reason_codes=[CoreferenceReasonCode.INANIMATE_OBJECT.value],
                visual_policy=VisualPolicy.SAFE_NEUTRAL,
                event_id=mention.event_id,
                event_index=ev_index,
            )
        else:
            return CoreferenceDecision(
                mention_id=mention.mention_id,
                resolved_entity_ids=[],
                status=CoreferenceStatus.UNRESOLVED_SAFE,
                confidence=0.7,
                margin=0.0,
                evidence_spans=[mention.span],
                rejected_candidates=[],
                candidate_entity_ids=[],
                reason="Inanimate object reference without explicit prior object entity.",
                reason_codes=[CoreferenceReasonCode.INANIMATE_OBJECT_SAFE.value],
                visual_policy=VisualPolicy.SAFE_NEUTRAL,
                event_id=mention.event_id,
                event_index=ev_index,
            )

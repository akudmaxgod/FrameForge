"""Validate catalogue claims against exact, immutable source coordinates."""
FIELDS = ('material', 'shape', 'scale', 'era', 'origin', 'distinctive_features')


def comparison_value(field, value):
    if field in ('material', 'shape') and isinstance(value, str) and ' ' not in value and any('а' <= c.casefold() <= 'я' for c in value):
        from imagegen.nlp import RussianNLPAdapter
        return RussianNLPAdapter.get_instance().analyze_token_morph(value).lemma
    return value.casefold() if isinstance(value, str) else value


def valid_span(story, candidate):
    if not isinstance(candidate, dict):
        return None
    quote = candidate.get('quote', candidate.get('text'))
    start, end = candidate.get('start'), candidate.get('end')
    if (not isinstance(quote, str) or not quote.strip() or type(start) is not int or type(end) is not int
            or start < 0 or end > len(story) or end - start != len(quote) or story[start:end] != quote):
        return None
    return {'start': start, 'end': end, 'quote': quote}


def anchor_evidence(story, anchor):
    candidates = list(anchor.get('evidence_spans') or [])
    quote = anchor.get('source_quote') or anchor.get('quote')
    if isinstance(quote, str) and quote.strip():
        start = anchor.get('source_start')
        if start is None and story.count(quote) == 1:
            start = story.find(quote)
        if type(start) is int:
            candidates.append({'quote': quote, 'start': start, 'end': anchor.get('source_end', start + len(quote))})
    result = {}
    for candidate in candidates:
        item = valid_span(story, candidate)
        if item:
            result[(item['start'], item['end'])] = item
    return sorted(result.values(), key=lambda item: item['start'])


def properties(story, anchor, evidence):
    result = {}
    for field in FIELDS:
        claims = []
        explicit = (anchor.get('source_properties') or {}).get(field, [])
        for claim in explicit if isinstance(explicit, list) else []:
            if not isinstance(claim, dict):
                continue
            span = valid_span(story, claim.get('evidence'))
            value = claim.get('value')
            if (span and isinstance(value, str) and value.strip() and value.casefold() in span['quote'].casefold()
                    and any(e['start'] <= span['start'] and span['end'] <= e['end'] for e in evidence)):
                claims.append({'value':value, 'evidence':span})
        proposed = anchor.get(field)
        if not explicit and isinstance(proposed, str) and proposed.strip():
            claims.extend({'value':proposed, 'evidence':e} for e in evidence if proposed.casefold() in e['quote'].casefold())
        unique = {(c['value'], c['evidence']['start'], c['evidence']['end']):c for c in claims}
        claims = list(unique.values())
        values = {comparison_value(field, c['value']):c['value'] for c in reversed(claims)}
        # Type examples and distinct physical objects must never silently become
        # one object with an arbitrarily chosen dimension or material.
        result[field] = {'value':next(iter(values.values())) if len(values) == 1 else None,
            'canonical_value':next(iter(values)) if len(values) == 1 else None,
            'status':'source_claim' if len(values) == 1 else 'multiple_source_claims' if values else 'unknown',
            'claims':claims, 'proposed_unverified':None if values else proposed}
    return result

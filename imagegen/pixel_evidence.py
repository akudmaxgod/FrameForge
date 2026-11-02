"""Evidence protocol for pixels; model summaries cannot approve a frame alone.

References supplied for visual consistency have no historical authority. A
source excerpt establishes a narrated claim, not the authenticity of a face,
publication or archaeological inscription. Unknown central claims stay unknown.
"""
import hashlib
import base64
import json
import unicodedata

from .domain import DomainError

VERSION = 'pixel-evidence/3'
AXES = ('subject_action', 'participants_state', 'time_place', 'scale_function',
        'artifact_details', 'visible_text', 'identity')
LABELS = dict(zip(AXES, ('Предмет и действие', 'Участники и состояние', 'Время и место',
                       'Масштаб и назначение', 'Детали находки', 'Надписи', 'Личность и идентичность')))
STATES = ('supported', 'contradicted', 'uncertain', 'not_applicable')
CLAIMS = ('specific_person', 'specific_artifact', 'document', 'inscription', 'explanatory_label')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def image_part(data):
    mime = 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else 'image/png'
    return {'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode('ascii')}}


def contract(snapshot, data, source, references, checked_references=(), *, story_hash=None):
    final = snapshot.get('source_contract') or {}
    plan = snapshot['scene'].get('source_fidelity_plan') or {}
    packet = snapshot['scene'].get('forgotten_cities_prompt_contract') or {}
    required = {'subject_action'}
    if final.get('action') or packet.get('source_action'):
        required.add('subject_action')
    if final.get('participants') or plan.get('expected_people') or (packet.get('narrative_context') or {}).get('participants'):
        required.add('participants_state')
    if final.get('time_layer') or plan.get('time_layer') or packet.get('visual_time_layer'):
        required.add('time_place')
    if final.get('required_artifacts') or plan.get('required_artifacts') or packet.get('required_artifacts'):
        required.add('artifact_details')
    value = {'version': VERSION, 'candidate_sha256': sha(data), 'source': source,
             'axes': list(AXES), 'required_axes': [axis for axis in AXES if axis in required],
             'references': [{'index': i + 1, 'sha256': sha(ref), 'authority': 'visual_comparison_only'}
                            for i, ref in enumerate(references)],
             'scene_requirements': final or {'source_plan': plan, 'treatment': packet}}
    if story_hash is not None:
        value['story_hash'] = story_hash
    for reference, checked in zip(value['references'], checked_references):
        if checked and checked.get('image_sha256') == reference['sha256']:
            reference.update(authority='checked_primary_source', provenance=checked)
    value['required_reference_features'] = [
        {'reference_index': ref['index'], 'feature_index': i, 'expected': feature,
         'reference_image_side': next((f['image_side'] for f in ref['provenance'].get('spatial_features', [])
                                      if f['feature_index'] == i), None)}
        for ref in value['references'] if ref.get('authority') == 'checked_primary_source'
        for i, feature in enumerate(ref['provenance']['supported_features'])]
    value['id'] = sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return value


def schema():
    check = {'type': 'object', 'properties': {
        'axis': {'type': 'string', 'enum': list(AXES)},
        'status': {'type': 'string', 'enum': list(STATES)},
        'assessment_scope': {'type': 'string', 'enum': ['source_fact', 'presentation', 'absence']},
        **{key: {'type': 'string'} for key in ('expected', 'observed', 'reason', 'source_quote')},
        'reference_indices': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}}},
        'required': ['axis', 'status', 'assessment_scope', 'expected', 'observed', 'reason', 'source_quote', 'reference_indices'],
        'additionalProperties': False}
    claim = {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': list(CLAIMS)},
        **{key: {'type': 'string'} for key in ('assertion', 'visible_text', 'source_quote', 'reason')},
        'status': {'type': 'string', 'enum': ['supported', 'contradicted', 'uncertain']},
        'reference_indices': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}}},
        'required': ['kind', 'assertion', 'visible_text', 'source_quote', 'status', 'reference_indices', 'reason'],
        'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'evidence_contract_id': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['pass', 'fail', 'uncertain']},
        **{key: {'type': 'string'} for key in ('observed', 'reason', 'correction')},
        'checks': {'type': 'array', 'items': check, 'minItems': len(AXES), 'maxItems': len(AXES)},
        'claims': {'type': 'array', 'items': claim},
        'feature_checks': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'reference_index': {'type': 'integer', 'minimum': 1}, 'feature_index': {'type': 'integer', 'minimum': 0},
            'status': {'type': 'string', 'enum': list(STATES[:3])},
            'candidate_image_side': {'type': 'string', 'enum': ['left', 'right', 'center', 'not_visible', 'not_applicable']},
            'view_relation': {'type': 'string', 'enum': ['same_orientation', 'different_orientation', 'uncertain', 'not_applicable']},
            'observed': {'type': 'string'}, 'reason': {'type': 'string'}},
            'required': ['reference_index', 'feature_index', 'status', 'candidate_image_side', 'view_relation', 'observed', 'reason'], 'additionalProperties': False}}},
        'required': ['evidence_contract_id', 'status', 'observed', 'reason', 'correction', 'checks', 'claims', 'feature_checks'],
        'additionalProperties': False}


INSTRUCTIONS = (
    'EVIDENCE PROTOCOL: return evidence_contract_id exactly from evidence_contract.id. '
    'Return checks with exactly one entry for EACH axis in evidence_contract.axes, even when not applicable. '
    'Each check contains axis, status (supported|contradicted|uncertain|not_applicable), assessment_scope '
    '(source_fact|presentation|absence), expected, observed, '
    'reason, source_quote (an EXACT substring of the supplied source.context, or empty), reference_indices '
    '(1-based later image numbers, or []). Separate observed pixels from expected source facts. '
    'A supported applicable check needs an exact source excerpt or an actual supplied reference comparison. '
    'A neutral museum/background setting is presentation, not a historical time/place claim. '
    'The observable absence of text or a named identity is absence, not a claim needing a source citation. '
    'Use source_fact for important narrated facts, persons, eras, actions, scales and artifact features. '
    'Never mark a required axis not_applicable. Do not claim inferred likeness proves identity. '
    'Return claims for EVERY prominent specific person/artifact identity, readable publication, attributed quotation, '
    'historical inscription or modern explanatory label asserted by the image. Each claim contains kind '
    '(specific_person|specific_artifact|document|inscription|explanatory_label), assertion, visible_text '
    '(transcribe the ACTUAL readable text, or empty), status (supported|contradicted|uncertain), source_quote, '
    'reference_indices, reason. Claims=[] only when no such assertion is present. Anonymous researchers, '
    'ordinary object types and unreadable background books do not assert a particular identity. '
    'Only references explicitly marked checked_primary_source have inspected provenance; their named identity '
    'and supported_features are scoped evidence. Compare each listed feature (pose, handedness, eyes, ornaments, '
    'material, scale) separately under artifact_details; similarity of general type is insufficient. '
    'Return feature_checks with exactly one entry for EACH evidence_contract.required_reference_features item: '
    'reference_index, feature_index, status (supported|contradicted|uncertain), observed, reason, '
    'candidate_image_side (left|right|center|not_visible|not_applicable), view_relation '
    '(same_orientation|different_orientation|uncertain|not_applicable). '
    'Image side is SCREEN left/right, never the anatomical left/right hand of a depicted person. '
    'For a feature with reference_image_side, locate that physical feature in the candidate independently '
    'before comparing it to the reference. A horizontal mirror reverses SCREEN sides even if a caption names the same object. '
    'Compare that exact feature in the candidate with the reference and its checked description. '
    'If it is cropped, hidden or cannot be measured in the candidate, use uncertain; never assume it matches. '
    'Feature_checks=[] only when the required_reference_features list is empty. '
    'All other supplied references have visual_comparison_only authority: they can establish likeness, never authentic '
    'person/object identity or document provenance. If the image presents such an exact identity as central '
    'evidence without an authenticated source, mark that claim uncertain. For documents, a paraphrase, translation '
    'or relevant title is not proof of an actual original quotation/publication. Supported readable document '
    'text must occur literally in the cited source; otherwise uncertain unless explicitly contradicted. '
    'Mark clear observed contradictions contradicted, with exact expected versus observed details. '
    'Do not turn missing evidence into a factual contradiction. The application aggregates checks and claims; '
    'a global pass cannot override contradicted, uncertain or unsupported central evidence. '
    'Also return status, observed, reason, correction; correction is empty if none is needed. '
)


def _text(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def adjudicate(result, evidence):
    if not isinstance(evidence, dict) or result.get('evidence_contract_id') != evidence.get('id'):
        raise DomainError('Ответ не связан с текущими доказательствами кадра.', 503)
    rows, claims = result.get('checks'), result.get('claims')
    if (not isinstance(rows, list) or len(rows) != len(AXES) or not isinstance(claims, list)
            or any(not isinstance(row, dict) for row in rows + claims)
            or {row.get('axis') for row in rows} != set(AXES)):
        raise DomainError('Проверка не покрыла все важные признаки кадра.', 503)
    source = evidence['source']['context']
    refs = {ref['index'] for ref in evidence['references']}
    normalized, blockers = [], []
    features = result.get('feature_checks')
    required_features = {(item['reference_index'], item['feature_index']): item
                         for item in evidence.get('required_reference_features', [])}
    if (not isinstance(features, list) or len(features) != len(required_features)
            or any(not isinstance(item, dict) or type(item.get('reference_index')) is not int
                   or type(item.get('feature_index')) is not int for item in features)
            or {(item['reference_index'], item['feature_index']) for item in features} != set(required_features)):
        raise DomainError('Проверка не сравнила все подтверждённые признаки эталона.', 503)
    normalized_features = []
    for feature in features:
        if (feature.get('status') not in STATES[:3] or not all(isinstance(feature.get(key), str)
                and feature[key].strip() for key in ('observed', 'reason'))
                or feature.get('candidate_image_side') not in ('left', 'right', 'center', 'not_visible', 'not_applicable')
                or feature.get('view_relation') not in ('same_orientation', 'different_orientation', 'uncertain', 'not_applicable')):
            raise DomainError('Некорректный результат сравнения с эталоном.', 503)
        checked_feature = dict(feature)
        item = required_features[(feature['reference_index'], feature['feature_index'])]
        expected_side = item.get('reference_image_side')
        if expected_side and feature['status'] == 'supported':
            if feature['view_relation'] == 'same_orientation' and feature['candidate_image_side'] in ('left', 'right', 'center'):
                if feature['candidate_image_side'] != expected_side:
                    checked_feature.update(status='contradicted',model_status='supported',
                        reason='При одинаковом ракурсе признак находится с другой стороны изображения: вероятное зеркальное отражение.')
            else:
                checked_feature.update(status='uncertain',model_status='supported',reason='Пространственный признак эталона не сопоставлен при подтверждённом ракурсе.')
        normalized_features.append(checked_feature)
        if checked_feature['status'] != 'supported':
            blockers.append({'axis': 'artifact_details', 'status': checked_feature['status'],
                             'reason': item['expected'] + ': ' + checked_feature['reason']})
    for row in rows + claims:
        is_check = 'axis' in row
        name = row.get('axis') if is_check else row.get('kind')
        fields = ('expected', 'observed', 'reason', 'source_quote') if is_check else ('assertion', 'visible_text', 'reason', 'source_quote')
        if (name not in (AXES if is_check else CLAIMS) or row.get('status') not in (STATES if is_check else STATES[:3])
                or (is_check and row.get('assessment_scope') not in ('source_fact', 'presentation', 'absence'))
                or not all(isinstance(row.get(key), str) for key in fields)
                or not row['reason'].strip() or not row['observed' if is_check else 'assertion'].strip()
                or not isinstance(row.get('reference_indices'), list)
                or any(type(i) is not int or i not in refs for i in row['reference_indices'])
                or len(set(row['reference_indices'])) != len(row['reference_indices'])):
            raise DomainError('Некорректные доказательства проверки кадра.', 503)
        quote = row['source_quote']
        if quote and (not quote.strip() or quote not in source):
            raise DomainError('Проверка ссылается на отсутствующую цитату.', 503)
        checked = dict(row)
        status = row['status']
        why = None
        if is_check and name in evidence['required_axes'] and status == 'not_applicable':
            status, why = 'uncertain', 'Обязательный признак не проверен.'
        observable_only = (is_check and name not in evidence['required_axes'] and (
            (name == 'time_place' and row['assessment_scope'] == 'presentation')
            or (name == 'visible_text' and row['assessment_scope'] == 'absence' and not any(c.get('kind') in ('document', 'inscription', 'explanatory_label') for c in claims))
            or (name == 'identity' and row['assessment_scope'] == 'absence' and not any(c.get('kind') in ('specific_person', 'specific_artifact') for c in claims))))
        if status == 'supported' and not quote and not row['reference_indices'] and not observable_only:
            status, why = 'uncertain', 'Соответствие заявлено без исходной цитаты или сравнения с эталоном.'
        if not is_check and status == 'supported':
            if name in ('specific_person', 'specific_artifact', 'inscription') and not any(
                    ref.get('authority') == 'checked_primary_source'
                    and ref.get('provenance', {}).get('scope') == name
                    and any(_text(alias) in _text(row['assertion']) for alias in ref['provenance']['aliases'])
                    for ref in evidence['references'] if ref['index'] in row['reference_indices']):
                status, why = 'uncertain', 'Подлинность личности, находки или надписи не подтверждена источником; визуального сходства недостаточно.'
            elif name in ('document', 'explanatory_label') and (not row['visible_text'].strip()
                    or not quote or _text(row['visible_text']) not in _text(quote)):
                status, why = 'uncertain', 'Читаемый текст не подтверждён буквальной цитатой источника.'
        checked['status'] = status
        if why:
            checked['model_status'] = row['status']
            checked['evidence_gap'] = why
        normalized.append(checked)
        if status in ('contradicted', 'uncertain'):
            blockers.append({'axis': name, 'status': status, 'reason': why or row['reason']})
    verdict = 'fail' if any(item['status'] == 'contradicted' for item in blockers) else 'uncertain' if blockers else 'pass'
    # A conservative model summary can withhold approval, never grant it.
    if verdict == 'pass' and result['status'] != 'pass':
        verdict = 'uncertain'
        blockers.append({'axis': 'summary', 'status': 'uncertain', 'reason': result['reason']})
    return {'status': verdict, 'model_status': result['status'], 'checks': normalized[:len(rows)],
            'claims': normalized[len(rows):], 'evidence_contract_id': evidence['id'],
            'feature_checks': normalized_features,
            'evidence_gaps': blockers,
            'reason': '; '.join(f"{LABELS.get(b['axis'], 'Документальное утверждение')}: {b['reason']}" for b in blockers)
                      if blockers else result['reason']}

"""Blocking preflight checks for Forgotten Cities generation."""
from __future__ import annotations

from typing import Dict, List
import json

from .models import (
    AnchorType,
    ClaimType,
    EvidenceLevel,
    ForgottenCitiesDocumentIR,
    GeographyType,
    PeoplePolicy,
    TimeLayer,
    VisualizationPolicy,
)


class ForgottenCitiesPreflightValidator:
    def validate(
        self,
        document: ForgottenCitiesDocumentIR,
        story_text: str,
        require_locked_references: bool = False,
    ) -> Dict[str, object]:
        errors: List[Dict[str, object]] = []
        warnings: List[Dict[str, object]] = []

        def error(code, message, **details):
            errors.append({"code": code, "message": message, "details": details})

        def warning(code, message, **details):
            warnings.append({"code": code, "message": message, "details": details})

        for message in document.validate(story_text):
            error("ir_invariant", message)
        if not document.city_bible.canonical_name or document.city_bible.canonical_name.startswith("Неопределён"):
            error("missing_primary_city", "Не определён главный город выпуска.")

        anchor_map = {anchor.anchor_id: anchor for anchor in document.anchors}
        visual_sequences = [s for s in document.sequences if s.time_layer not in (TimeLayer.MAP, TimeLayer.ABSTRACT_EXPLANATION)]
        missing_location = [s.sequence_id for s in visual_sequences if not s.location_anchor_id]
        if visual_sequences and len(missing_location) / len(visual_sequences) > 0.2:
            error("too_many_unknown_locations", "Более 20% визуальных эпизодов не имеют осмысленного якоря локации.", sequences=missing_location)

        forbidden_geo = [g for g in document.geography_mentions if g.geography_type in (GeographyType.RESEARCHER_NAME, GeographyType.ORIGIN_REGION, GeographyType.COMPARISON_LOCATION)]
        anchor_names = {anchor.canonical_name.casefold() for anchor in document.anchors}
        promoted = [g.text for g in forbidden_geo if g.text.casefold() in anchor_names]
        if promoted:
            error("invalid_location_promotion", "Исследователь или упомянутый регион превращён в визуальную локацию.", names=promoted)

        for sequence in document.sequences:
            anchor = anchor_map.get(sequence.location_anchor_id or "")
            if anchor and sequence.time_layer in (TimeLayer.MODERN_RUINS, TimeLayer.HISTORICAL_RECONSTRUCTION) and anchor.time_layer is not sequence.time_layer:
                error("mixed_time_layers", "Современное и реконструированное состояние используют один неверный якорь.", sequence=sequence.sequence_id, anchor=anchor.anchor_id)
            if sequence.evidence_level is EvidenceLevel.ILLUSTRATIVE_HYPOTHESIS:
                linked = [claim for claim in document.claims if claim.claim_id in sequence.claim_ids]
                if any(claim.visualization_policy is VisualizationPolicy.DIRECT for claim in linked):
                    error("hypothesis_as_fact", "Гипотеза подготовлена к прямой визуализации как факт.", sequence=sequence.sequence_id)
            if sequence.people_policy is not PeoplePolicy.NONE and sequence.time_layer in (TimeLayer.MODERN_RUINS, TimeLayer.MAP, TimeLayer.ABSTRACT_EXPLANATION):
                warning("people_layer_review", "Люди запрошены в слое, где их присутствие требует ручной проверки.", sequence=sequence.sequence_id)

        for anchor in document.anchors:
            if anchor.anchor_type is AnchorType.ARTIFACT and (not anchor.description.strip() or not anchor.evidence_spans):
                error("artifact_without_evidence", "Ключевой артефакт не имеет описания или доказательства.", anchor=anchor.anchor_id)
            if require_locked_references and anchor.reference_required and (not anchor.locked or not anchor.reference_asset_id):
                error("missing_required_reference", f"Закрепите эталон «{anchor.canonical_name}».",
                      anchor=anchor.anchor_id, anchor_name=anchor.canonical_name)

        if len(document.sequences) > 1:
            layers = [sequence.time_layer for sequence in document.sequences]
            switches = sum(1 for left, right in zip(layers, layers[1:]) if left is not right)
            if switches > max(3, len(layers) // 2):
                warning("frequent_time_layer_switches", "Временной слой меняется слишком часто.", switches=switches)
        return {
            "ok": not errors,
            "status": "PASS" if not errors else "BLOCKED",
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "sequences": len(document.sequences),
                "anchors": len(document.anchors),
                "artifacts": sum(1 for a in document.anchors if a.anchor_type is AnchorType.ARTIFACT),
                "claims": len(document.claims),
            },
        }

    def validate_project(self, project: Dict[str, object], story_text: str) -> Dict[str, object]:
        metadata = project.get("analysis_metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        payload = metadata.get("forgotten_cities") if isinstance(metadata, dict) else None
        if not isinstance(payload, dict):
            return {"ok": False, "status": "BLOCKED", "errors": [{
                "code": "missing_forgotten_cities_analysis",
                "message": "Проект не содержит профильного анализа «Забытые города».",
                "details": {},
            }], "warnings": [], "stats": {}}
        document = ForgottenCitiesDocumentIR.from_dict(payload)
        resources = list(project.get("locations") or []) + list(project.get("props") or [])
        for anchor in document.anchors:
            # Analysis metadata is a historical snapshot. Only the current
            # persisted resource can authorize a batch after an unlock/recast.
            anchor.locked = False
            anchor.reference_asset_id = None
            names = {anchor.canonical_name.casefold()}
            if " — " in anchor.canonical_name:
                names.add(anchor.canonical_name.split(" — ", 1)[0].casefold())
            # Prefer the distinct temporal anchor over a generic city resource
            # which may occur earlier in the list and already be locked.
            resource = next((item for item in resources
                             if str(item.get("name", "")).casefold() == anchor.canonical_name.casefold()), None)
            if resource is None:
                resource = next((item for item in resources if str(item.get("name", "")).casefold() in names), None)
            if not resource:
                continue
            refs = resource.get("references_json") or resource.get("references") or {}
            if isinstance(refs, str):
                try:
                    refs = json.loads(refs)
                except ValueError:
                    refs = {}
            if resource.get("locked") and refs:
                anchor.locked = True
                anchor.reference_asset_id = str(resource.get("id") or "project_reference")
        report = self.validate(document, story_text, require_locked_references=True)
        narrative_grounder=None
        if metadata.get('narrative_storyboard'):
            from imagegen.narrative_contract import sequence_grounder
            from imagegen.domain import DomainError
            try:
                narrative_grounder=sequence_grounder(story_text,metadata)
            except (DomainError, ValueError, KeyError, TypeError) as error:
                # One invalid sequence affects its frames collectively. Do not
                # repeat the first whole-plan error for every timed subshot.
                report['errors'].append({'code':'invalid_narrative_sequence','message':str(error),
                    'details':{'scope':'sequence'}})
                report['ok']=False
                report['status']='BLOCKED'
                return report
        from .grounding import bind_source
        for index, entry in enumerate(project.get('scenes') or [], 1):
            scene = entry.get('data', entry)
            if metadata.get('narrative_storyboard'):
                from imagegen.domain import DomainError
                try:
                    narrative_grounder(scene)
                except DomainError as error:
                    report['errors'].append({'code':'invalid_narrative_sequence','message':str(error),
                        'details':{'scene_index':index}})
                # The full reviewed beat resolves a dependent subshot's subject.
                # Checking only its isolated sentence would lose that evidence.
                continue
            binding = bind_source(payload, scene.get('forgotten_event_id') or '',
                scene.get('source_quote') or scene.get('beat', ''), story=story_text,
                source_start=scene.get('source_start'))
            for message in binding['errors']:
                report['errors'].append({'code': 'unresolved_visual_subject', 'message': message,
                                         'details': {'scene_index': index}})
        report['ok'] = not report['errors']
        report['status'] = 'PASS' if report['ok'] else 'BLOCKED'
        return report

"""Generation passports and fail-honest continuity checks for Forgotten Cities.

The deterministic checks in this module verify the generation contract and its
provenance.  They deliberately do not pretend to understand pixels: semantic
architecture, style and period inspection remains ``unavailable`` until a real
visual reviewer is connected, and is therefore surfaced for human review.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional


PASSPORT_VERSION = "forgotten_cities_generation/1.0"
CONTINUITY_VERSION = "forgotten_cities_continuity/1.0"
PROMPT_BLOCKS = (
    "style_contract",
    "city_contract",
    "time_layer_contract",
    "shot_contract",
    "negative_contract",
)


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scene_contract(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    scene = snapshot.get("scene") if isinstance(snapshot, dict) else None
    contract = scene.get("forgotten_cities_prompt_contract") if isinstance(scene, dict) else None
    return dict(contract) if isinstance(contract, dict) else {}


def build_generation_passport(
    generation: Dict[str, Any],
    asset: Dict[str, Any],
    *,
    project: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the immutable approval record for one rendered still."""
    snapshot = generation.get("snapshot") or {}
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    contract = _scene_contract(snapshot)
    scene = snapshot.get("scene") or {}
    project = project or {}
    reference_hashes = dict(contract.get("reference_hashes") or {})
    prompt_blocks = {key: contract.get(key, "") for key in PROMPT_BLOCKS}
    runtime_profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), dict) else {}
    contract_payload = {
        "prompt_blocks": prompt_blocks,
        "compiled_prompt": contract.get("compiled_prompt", ""),
        "anchor_ids": list(contract.get("anchor_ids") or []),
        "reference_hashes": reference_hashes,
        "channel_profile_version_id": snapshot.get("channel_profile_version_id"),
        "scene_revision": generation.get("scene_revision"),
    }
    return {
        "passport_version": PASSPORT_VERSION,
        "project_id": generation.get("project_id"),
        "scene_id": generation.get("scene_id"),
        "scene_revision": generation.get("scene_revision"),
        "generation_id": generation.get("id"),
        "asset_id": asset.get("id"),
        "asset_sha256": asset.get("sha256"),
        "seed": generation.get("seed"),
        "provider_id": generation.get("provider_id"),
        "backend": runtime_profile.get("image_backend"),
        "model": (runtime_profile.get("flow_image_model") or runtime_profile.get("google_model")
                  or runtime_profile.get("unet")),
        "profile_type": "forgotten_cities",
        "profile_version_id": snapshot.get("channel_profile_version_id"),
        "profile_version": (project.get("channel_profile") or {}).get("current_version_number"),
        "channel_reference_hashes": sorted(snapshot.get("channel_reference_hashes") or []),
        "prompt": snapshot.get("prompt", ""),
        "negative_contract": contract.get("negative_contract", ""),
        "prompt_blocks": prompt_blocks,
        "time_layer": scene.get("time_layer") or _contract_value(contract, "time_layer_contract", "Time layer"),
        "evidence_level": scene.get("evidence_level") or _contract_value(contract, "time_layer_contract", "Evidence"),
        "people_policy": scene.get("people_policy") or _contract_value(contract, "shot_contract", "People policy"),
        "city_contract": contract.get("city_contract", ""),
        "anchor_ids": list(contract.get("anchor_ids") or []),
        "reference_hashes": reference_hashes,
        "source_quote": scene.get("source_quote", ""),
        "contract_hash": _hash(contract_payload),
    }


def _contract_value(contract: Dict[str, Any], block: str, label: str) -> str:
    text = str(contract.get(block) or "")
    marker = f"{label}:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split(".", 1)[0].strip()


def evaluate_generation_continuity(
    generation: Dict[str, Any],
    asset: Dict[str, Any],
    *,
    project: Optional[Dict[str, Any]] = None,
    approved_passports: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Return deterministic results plus an honest visual-review capability state."""
    passport = build_generation_passport(generation, asset, project=project)
    snapshot = generation.get("snapshot") or {}
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    contract = _scene_contract(snapshot)
    checks: List[Dict[str, Any]] = []

    def check(code: str, ok: bool, message: str, severity: str = "error") -> None:
        checks.append({
            "code": code,
            "status": "pass" if ok else "fail",
            "severity": severity,
            "message": message,
        })

    check("asset_hash", bool(asset.get("sha256")), "У изображения зафиксирован SHA-256.")
    check("generation_contract", bool(contract), "Сохранён контракт генерации Forgotten Cities.")
    for block in PROMPT_BLOCKS:
        check(f"prompt_{block}", bool(str(contract.get(block) or "").strip()), f"Заполнен блок {block}.")
    compiled = str(contract.get("compiled_prompt") or "")
    check(
        "compiled_five_block_prompt",
        all(label in compiled for label in ("STYLE CONTRACT:", "CITY CONTRACT:", "TIME LAYER:", "SHOT CONTRACT:", "NEGATIVE CONTRACT:")),
        "Скомпилирован полный пятиблочный промпт.",
    )
    anchor_ids = list(contract.get("anchor_ids") or [])
    reference_hashes = dict(contract.get("reference_hashes") or {})
    check(
        "reference_hashes",
        all(anchor_id in reference_hashes for anchor_id in anchor_ids),
        "Все прикреплённые визуальные якоря имеют сохранённые хеши.",
    )
    prior = [item for item in approved_passports if isinstance(item, dict)]
    if prior:
        cities = {str(item.get("city_contract") or "") for item in prior if item.get("city_contract")}
        check(
            "city_identity",
            not cities or passport.get("city_contract") in cities,
            "Контракт города совпадает с ранее утверждёнными кадрами.",
        )

    blocking = [item for item in checks if item["status"] == "fail" and item["severity"] == "error"]
    visual_checks = [
        {"code": "architecture_drift", "status": "unavailable", "message": "Требуется визуальная проверка архитектуры."},
        {"code": "style_drift", "status": "unavailable", "message": "Требуется визуальная проверка стиля и палитры."},
        {"code": "era_drift", "status": "unavailable", "message": "Требуется визуальная проверка эпохи и анахронизмов."},
        {"code": "artifact_geometry", "status": "unavailable", "message": "Требуется визуальная проверка формы ключевых артефактов."},
    ]
    return {
        "continuity_version": CONTINUITY_VERSION,
        "status": "fail" if blocking else "warning",
        "deterministic_status": "fail" if blocking else "pass",
        "visual_status": "unavailable",
        "checks": checks + visual_checks,
        "blocking_errors": blocking,
        "passport": passport,
    }


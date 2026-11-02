"""Bounded, resumable cloud review of Forgotten Cities documentary sequences."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from imagegen.analyzer_v3.models import DocumentIR, SourceSpan


class ForgottenCitiesCloudBatchAnalyzer:
    """Reviews one semantic sequence per request and preserves successful batches."""

    _cache: Dict[str, Dict[str, Any]] = {}

    def __init__(self, max_prompt_chars: int = 24000, max_attempts: int = 2):
        self.max_prompt_chars = max(4000, int(max_prompt_chars))
        self.max_attempts = max(1, min(2, int(max_attempts)))

    def run(self, doc: DocumentIR, story: str, client: Any, progress_callback: Any = None) -> Dict[str, Any]:
        profile = doc.metadata.get("forgotten_cities", {})
        sequences = profile.get("sequences", [])
        city = profile.get("city_bible", {})
        completed, failed = [], []
        cache_hits = 0
        calls_before = self._calls(client)
        if progress_callback:
            progress_callback(0, len(sequences), "Облачная проверка смысловых эпизодов")
        for sequence in sequences:
            cache_key = self._cache_key(story, sequence, getattr(client, "model", "groq"))
            if cache_key in self._cache:
                completed.append(self._cache[cache_key])
                cache_hits += 1
                continue
            result = None
            last_error = ""
            for attempt in range(1, self.max_attempts + 1):
                try:
                    raw = self._request(sequence, city, client, last_error)
                    result = self._validate(raw, sequence, story)
                    result["attempts"] = attempt
                    self._cache[cache_key] = result
                    completed.append(result)
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"[:400]
            if result is None:
                failed.append({
                    "sequence_id": sequence.get("sequence_id"),
                    "error": last_error or "unknown cloud error",
                    "attempts": self.max_attempts,
                })
            if progress_callback:
                progress_callback(len(completed) + len(failed), len(sequences), "Облачная проверка смысловых эпизодов")
        expected = len(sequences)
        covered = len(completed)
        report = {
            "status": "certified" if covered == expected else "certified_degraded",
            "batches_total": expected,
            "batches_completed": covered,
            "batches_failed": len(failed),
            "cache_hits": cache_hits,
            "cloud_calls": max(0, self._calls(client) - calls_before),
            "coverage_percent": round(100.0 * covered / expected, 2) if expected else 100.0,
            "successful_batches": completed,
            "failed_batches": failed,
            "local_event_coverage": len(doc.events),
            "local_audit_status": doc.metadata.get("two_layer_audit", {}).get("status", "not_run"),
        }
        profile["cloud_batch_analysis"] = report
        return report

    def _request(self, sequence, city, client, diagnostic):
        source = sequence.get("source_span", {})
        body = {
            "global_context": {
                "city": city.get("canonical_name"),
                "region": city.get("region"),
                "period": [city.get("period_start"), city.get("period_end")],
                "materials": city.get("material_palette", []),
            },
            "sequence": {
                "sequence_id": sequence.get("sequence_id"),
                "event_ids": sequence.get("event_ids", []),
                "source_span": source,
                "time_layer": sequence.get("time_layer"),
                "evidence_level": sequence.get("evidence_level"),
            },
            "required_output": {
                "sequence_id": sequence.get("sequence_id"),
                "event_ids": sequence.get("event_ids", []),
                "summary": "brief visual summary",
                "source_spans": [{"start": source.get("start"), "end": source.get("end"), "text": source.get("text")}],
                "warnings": [],
            },
        }
        prompt = json.dumps(body, ensure_ascii=False)
        if len(prompt) > self.max_prompt_chars:
            raise ValueError(f"sequence prompt exceeds configured limit ({len(prompt)} > {self.max_prompt_chars})")
        if diagnostic:
            prompt += "\nPrevious response failed local validation: " + diagnostic
        payload = {
            "model": getattr(client, "model", "openai/gpt-oss-120b"),
            "temperature": 0.0,
            "max_completion_tokens": min(4096, max(800, self.max_prompt_chars // 8)),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Review one historical documentary sequence. Return JSON only. Preserve exact source coordinates. Do not invent facts, locations, artifacts or people."},
                {"role": "user", "content": prompt},
            ],
        }
        response = client._post_json(payload)
        choices = response.get("choices") or []
        if not choices:
            raise ValueError("cloud response has no choices")
        parsed = json.loads(choices[0].get("message", {}).get("content", ""))
        if not isinstance(parsed, dict):
            raise ValueError("cloud response root must be an object")
        return parsed

    @staticmethod
    def _validate(raw, sequence, story):
        sequence_id = str(sequence.get("sequence_id") or "")
        if raw.get("sequence_id") != sequence_id:
            raise ValueError("sequence_id mismatch")
        expected_events = [str(item) for item in sequence.get("event_ids", [])]
        if [str(item) for item in raw.get("event_ids", [])] != expected_events:
            raise ValueError("event coverage mismatch")
        seq_span = SourceSpan.from_dict(sequence["source_span"])
        spans = []
        for item in raw.get("source_spans", []):
            span = SourceSpan.from_dict(item)
            if not span.validate_against(story):
                raise ValueError("source span mismatch")
            if not (seq_span.start <= span.start < span.end <= seq_span.end):
                raise ValueError("source span lies outside sequence")
            spans.append(span.to_dict())
        if not spans:
            raise ValueError("cloud response has no exact source spans")
        return {
            "sequence_id": sequence_id,
            "event_ids": expected_events,
            "summary": str(raw.get("summary") or "")[:1000],
            "source_spans": spans,
            "warnings": [str(item)[:500] for item in raw.get("warnings", []) if isinstance(item, (str, int, float))],
        }

    @staticmethod
    def _cache_key(story, sequence, model):
        payload = json.dumps({"story": hashlib.sha256(story.encode("utf-8")).hexdigest(), "sequence": sequence, "model": model}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _calls(client):
        if hasattr(client, "get_usage"):
            return int((client.get_usage() or {}).get("calls", 0))
        return 0

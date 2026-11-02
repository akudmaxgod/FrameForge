"""Prices fixed by the specification; costs are local estimates in USD."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
from .errors import ResponseError

log = logging.getLogger(__name__)
PRICE = {"in_miss": 0.15e-6, "in_hit": 0.003e-6, "out": 0.60e-6}


@dataclass
class Stats:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_hit: bool = False
    result_cache_hit: bool = False
    pass_a_cost: float = 0.0
    pass_b_cost: float = 0.0
    requests: int = 0
    usage_complete: bool = True

    def record(self, usage: object, phase: str, when: datetime) -> None:
        data = usage.model_dump() if hasattr(usage, "model_dump") else usage
        if not isinstance(data, dict):
            self.usage_complete = False
            raise ResponseError("API не вернул usage: стоимость неизвестна.")
        incoming, outgoing = data.get("prompt_tokens"), data.get("completion_tokens")
        if any(type(n) is not int or n < 0 for n in (incoming, outgoing)):
            self.usage_complete = False
            raise ResponseError("API вернул некорректный usage: стоимость неизвестна.")
        hit, miss = data.get("prompt_cache_hit_tokens"), data.get("prompt_cache_miss_tokens")
        if (any(type(n) is not int or n < 0 for n in (hit, miss))
                or hit + miss != incoming):
            hit, miss = 0, incoming
            log.warning("usage_cache_unavailable phase=%s; all input charged as miss", phase)
        utc = when.astimezone(timezone.utc)
        peak = utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
        cost = (Decimal(miss) * Decimal("0.00000015")
                + Decimal(hit) * Decimal("0.000000003")
                + Decimal(outgoing) * Decimal("0.00000060")) * (2 if peak else 1)
        self.tokens_in += incoming
        self.tokens_out += outgoing
        self.cache_hit_tokens += hit
        self.cache_miss_tokens += miss
        self.cache_hit = self.cache_hit_tokens > 0
        self.cost_usd = float(Decimal(str(self.cost_usd)) + cost)
        attr = "pass_a_cost" if phase == "A" else "pass_b_cost"
        setattr(self, attr, float(Decimal(str(getattr(self, attr))) + cost))
        log.info("usage phase=%s tokens_in=%s tokens_out=%s cache_hit_tokens=%s peak=%s cost_usd=%s",
                 phase, incoming, outgoing, hit, peak, cost)
        if phase == "B" and hit == 0:
            log.warning("prompt_cache_no_hit phase=B")

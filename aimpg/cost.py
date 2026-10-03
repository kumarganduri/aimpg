"""API-equivalent dollar cost of requests, from Anthropic's and OpenAI's published price tables.

Prices live in prices.json with their source URL. For subscription users the
number is what the same work would cost on the pay-per-use API, not a bill.
Unknown models are counted, never guessed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Iterable

from aimpg.model import Request, Usage


@lru_cache(maxsize=1)
def load_prices() -> dict:
    return json.loads(resources.files("aimpg").joinpath("prices.json").read_text())


@lru_cache(maxsize=256)
def price_for(model: str) -> dict | None:
    """Longest price-table key the model id starts with (e.g. claude-haiku-4-5-20251001)."""
    keys = [k for k in load_prices()["models"] if model.startswith(k)]
    return load_prices()["models"][max(keys, key=len)] if keys else None


def usage_cost(usage: Usage, model: str) -> float | None:
    p = price_for(model)
    if p is None:
        return None
    one_hour = min(usage.cache_write_1h, usage.cache_write)
    return (
        usage.fresh_in * p["input"]
        + (usage.cache_write - one_hour) * p["cache_write_5m"]
        + one_hour * p["cache_write_1h"]
        + usage.cache_read * p["cache_read"]
        + usage.output * p["output"]
    ) / 1e6


def total_cost(requests: Iterable[Request], weights: Iterable[float] | None = None) -> tuple[float, int]:
    """(USD, number of requests with an unknown model)."""
    total, unknown = 0.0, 0
    reqs = list(requests)
    ws = list(weights) if weights is not None else [1.0] * len(reqs)
    for r, w in zip(reqs, ws):
        c = usage_cost(r.usage, r.model)
        if c is None:
            unknown += 1
        else:
            total += c * w
    return total, unknown

"""The one place energy is computed. Everything else calls this module.

Per request (joules, before overhead):

    prefill   = (fresh_in + cache_write) * E_pre        # a cache write is a full prefill
    cache     =  cache_read * E_kv                       # free if still in HBM, a reload if not
    decode    =  output * (E_dec + E_ctx * ctx_len)      # every output token re-reads the KV cache

    E_pre = 2 * P * J_flop * prefill_inefficiency
    E_dec = (P * weight_bits / batch) * J_bit + 2 * P * J_flop
    E_ctx = kv_bits_per_token * J_bit
    E_kv  = kv_bits_per_token * J_bit * kv_reload_multiplier

    Wh = overhead * joules / 3600

Every input is a [low, high] range from factors.json. The formula is
monotone increasing in each factor (taking batch_size reversed), so the
all-low and all-high corners bound the result.

Ranking (W) removes the model-class scale: W = joules / E_pre. Because the
ratios inside W are estimates, `compare` checks every corner of the factor
ranges and only names a winner when it wins at all of them.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import resources
from typing import Iterable

from aimpg.model import Request, Usage

_RANGE_KEYS = (
    "batch_size",
    "weight_bits",
    "kv_bits_per_token",
    "prefill_inefficiency",
    "kv_reload_multiplier",
)


@dataclass(frozen=True)
class Factors:
    params: float  # active parameters (count, not billions)
    batch_size: float
    weight_bits: float
    kv_bits_per_token: float
    prefill_inefficiency: float
    kv_reload_multiplier: float
    overhead: float
    j_flop: float
    j_bit: float

    @property
    def e_pre(self) -> float:
        return 2 * self.params * self.j_flop * self.prefill_inefficiency

    @property
    def e_dec(self) -> float:
        return (self.params * self.weight_bits / self.batch_size) * self.j_bit + 2 * self.params * self.j_flop

    @property
    def e_ctx(self) -> float:
        return self.kv_bits_per_token * self.j_bit

    @property
    def e_kv(self) -> float:
        return self.kv_bits_per_token * self.j_bit * self.kv_reload_multiplier


@dataclass(frozen=True)
class WhRange:
    low: float
    high: float

    def __add__(self, other: "WhRange") -> "WhRange":
        return WhRange(self.low + other.low, self.high + other.high)

    def __mul__(self, k: float) -> "WhRange":
        return WhRange(self.low * k, self.high * k)

    @property
    def mid(self) -> float:
        return (self.low * self.high) ** 0.5  # geometric: the range spans orders of magnitude


ZERO = WhRange(0.0, 0.0)


@lru_cache(maxsize=1)
def load_factors() -> dict:
    return json.loads(resources.files("aimpg").joinpath("factors.json").read_text())


def model_class(model: str) -> tuple[str, bool]:
    """(class name, assumed) — assumed is True when the name matched no class."""
    data = load_factors()
    name = model.lower()
    for cls, spec in data["classes"].items():
        if any(token in name for token in spec["matches"]):
            return cls, False
    return data["default_class"], True


def corners(cls: str) -> list[Factors]:
    """Every combination of low/high for the uncertain factors, for one class."""
    data = load_factors()
    ranges = data["ranges"]
    consts = data["constants"]
    params = data["classes"][cls]["active_params_billion"]
    out = []
    axes = [("params", params)] + [(k, ranges[k]) for k in _RANGE_KEYS] + [("overhead", ranges["overhead"])]
    for picks in itertools.product(("low", "high"), repeat=len(axes)):
        values = {name: spec[pick] for (name, spec), pick in zip(axes, picks)}
        values["params"] *= 1e9
        out.append(Factors(j_flop=consts["joules_per_flop"]["value"], j_bit=consts["joules_per_hbm_bit"]["value"], **values))
    return out


def _end(cls: str, which: str) -> Factors:
    data = load_factors()
    ranges = data["ranges"]
    consts = data["constants"]
    return Factors(
        params=data["classes"][cls]["active_params_billion"][which] * 1e9,
        overhead=ranges["overhead"][which],
        j_flop=consts["joules_per_flop"]["value"],
        j_bit=consts["joules_per_hbm_bit"]["value"],
        **{k: ranges[k][which] for k in _RANGE_KEYS},
    )


def joules(usage: Usage, f: Factors) -> float:
    """GPU-side joules for one request, before datacenter overhead."""
    prefill = (usage.fresh_in + usage.cache_write) * f.e_pre
    cache = usage.cache_read * f.e_kv
    decode = usage.output * (f.e_dec + f.e_ctx * usage.ctx_len)
    return prefill + cache + decode


def wh(usage: Usage, f: Factors) -> float:
    return f.overhead * joules(usage, f) / 3600.0


def request_wh(request: Request) -> WhRange:
    cls, _ = model_class(request.model)
    return WhRange(wh(request.usage, _end(cls, "low")), wh(request.usage, _end(cls, "high")))


def total_wh(requests: Iterable[Request]) -> WhRange:
    total = ZERO
    for r in requests:
        total = total + request_wh(r)
    return total


def weighted_wh(requests: Iterable[Request], weights: Iterable[float]) -> WhRange:
    total = ZERO
    for r, w in zip(requests, weights):
        total = total + request_wh(r) * w
    return total


def weighted_tokens(usages: Iterable[Usage], f: Factors) -> float:
    """W: joules with the model-class scale (E_pre) divided out."""
    return sum(joules(u, f) for u in usages) / f.e_pre


class Verdict(str, Enum):
    A_WINS = "A uses less"
    B_WINS = "B uses less"
    TOO_CLOSE = "too close to call with current energy data"
    NOT_COMPARABLE = "not directly comparable (different model classes)"


def compare(a: list[Request], b: list[Request]) -> Verdict:
    """Same-model-class comparison that holds at every corner of the factor ranges."""
    classes = {model_class(r.model)[0] for r in a + b}
    if len(classes) != 1:
        return Verdict.NOT_COMPARABLE
    (cls,) = classes
    ua, ub = [r.usage for r in a], [r.usage for r in b]
    signs = {
        (weighted_tokens(ua, f) > weighted_tokens(ub, f)) - (weighted_tokens(ua, f) < weighted_tokens(ub, f))
        for f in corners(cls)
    }
    if signs == {-1}:
        return Verdict.A_WINS
    if signs == {1}:
        return Verdict.B_WINS
    return Verdict.TOO_CLOSE

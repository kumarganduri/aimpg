"""Everyday comparisons for energy ranges ("≈ boiling a kettle 70–590 times").

Factors live in equivalences.json, each with its source. Comparisons are always
shown as ranges, like the energy itself, never as a single falsely precise number.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from aimpg.energy import WhRange


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(resources.files("aimpg").joinpath("equivalences.json").read_text())


def _n(x: float) -> str:
    if x < 1:
        return "<1"
    if x < 10:
        return f"{x:.0f}" if x >= 2 else f"{x:.1f}".rstrip("0").rstrip(".")
    if x < 1000:
        return f"{round(x, -1 if x >= 100 else 0):,.0f}"
    return f"{round(x, -2):,.0f}"


def count_range(r: WhRange, wh_each: float) -> str:
    lo, hi = _n(r.low / wh_each), _n(r.high / wh_each)
    return lo if lo == hi else f"{lo}–{hi}"


def phrase(r: WhRange, key: str) -> str:
    item = load()["items"][key]
    return item["say"].format(n=count_range(r, item["wh"]))


def co2(r: WhRange) -> str:
    g = load()["co2_g_per_kwh"]["value"]
    lo, hi = r.low / 1000 * g, r.high / 1000 * g  # grams
    if hi >= 1000:
        return f"{lo / 1000:.1f}–{hi / 1000:.1f} kg CO₂".replace(".0–", "–").replace(".0 kg", " kg")
    return f"{lo:.0f}–{hi:.0f} g CO₂"


def everyday(r: WhRange, keys: tuple[str, ...] = ("kettle", "ev_km")) -> str:
    """'boiling a litre … 70–590 times · driving an electric car 40–330 km · 3.3–27 kg CO₂'"""
    return " · ".join([phrase(r, k) for k in keys] + [co2(r)])

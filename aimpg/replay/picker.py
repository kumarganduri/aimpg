"""Model picker: the cheapest model that's good enough for YOUR code.

Different models can't be compared on estimated energy alone (their sizes are
secret, so their energy ranges overlap), so the picker ranks on what is exact:
how often each model solved your commits (judged by your own tests) and the
real dollar cost Claude Code reported. Energy is shown as a range per solved
task, with everyday comparisons.

Cost and energy are "per solved task": everything a model spent, including on
failed attempts, divided by what it actually solved. A cheap model that fails
a lot is not cheap.

Recommendation: the cheapest model whose solve rate is within 10 points of the
best model's. With fewer than 20 runs per model it's marked tentative.
"""

from __future__ import annotations

from dataclasses import dataclass

from aimpg import equivalence
from aimpg.energy import ZERO, WhRange, request_wh
from aimpg.model import Request, Usage
from aimpg.replay.run import Record

GOOD_ENOUGH = 0.10
MIN_RUNS = 20


@dataclass
class ModelRow:
    setup: str
    model: str
    runs: int
    solved: int
    usd: float
    wh: WhRange

    @property
    def solve_rate(self) -> float:
        return self.solved / self.runs if self.runs else 0.0

    @property
    def usd_per_solved(self) -> float | None:
        return self.usd / self.solved if self.solved else None

    @property
    def wh_per_solved(self) -> WhRange | None:
        return WhRange(self.wh.low / self.solved, self.wh.high / self.solved) if self.solved else None


def rows(records: list[Record]) -> list[ModelRow]:
    out: dict[str, ModelRow] = {}
    for r in records:
        row = out.setdefault(r.setup, ModelRow(r.setup, r.model, 0, 0, 0.0, ZERO))
        row.runs += 1
        row.solved += r.passed
        row.usd += r.cost_usd
        for u in r.usages:
            row.wh = row.wh + request_wh(Request("x", "s", r.model, 0.0, Usage(*u), "/"))
    return sorted(out.values(), key=lambda x: (x.usd_per_solved is None, x.usd_per_solved or 0))


def recommend(table: list[ModelRow]) -> str:
    solved = [r for r in table if r.solved]
    if not solved:
        return "No model solved any task: nothing to recommend yet."
    best = max(r.solve_rate for r in table)
    good = [r for r in solved if r.solve_rate >= best - GOOD_ENOUGH]
    pick = min(good, key=lambda r: r.usd_per_solved)
    priciest = max(solved, key=lambda r: r.usd_per_solved)
    text = f"Use {pick.setup.split('@')[-1]}: it solved {pick.solve_rate:.0%} of tasks (best: {best:.0%})"
    if priciest is not pick:
        ratio = priciest.usd_per_solved / pick.usd_per_solved
        text += f" at {ratio:.1f}x lower cost per solved task than {priciest.setup.split('@')[-1]}"
    text += "."
    if min(r.runs for r in table) < MIN_RUNS:
        text += f" Tentative: fewer than {MIN_RUNS} runs per model."
    return text


def render(records: list[Record]) -> str:
    table = rows(records)
    lines = [f"{'model':<10}{'solved':>10}{'$ / solved task':>17}  energy per solved task"]
    for r in table:
        name = r.setup.split("@")[-1]
        usd = f"${r.usd_per_solved:.2f}" if r.usd_per_solved is not None else "—"
        wh = r.wh_per_solved
        energy = "—" if wh is None else f"{wh.low:.1f}–{wh.high:.1f} Wh ≈ {equivalence.phrase(wh, 'phone')}"
        lines.append(f"{name:<10}{f'{r.solved}/{r.runs}':>10}{usd:>17}  {energy}")
    lines += ["", recommend(table)]
    return "\n".join(lines)

"""Measure session→commit attribution against your own hand labels.

    uv run python evals/label.py            # label ~30 commits (about 20 minutes)
    uv run python evals/attribution_eval.py # precision / recall per tier

Labels stay on your machine in evals/data/ (gitignored): they describe your
private history.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aimpg.attribution import Attribution, attribute  # noqa: E402
from aimpg.gitkept import DAY  # noqa: E402
from aimpg.logs import DEFAULT_ROOT, iter_log_files, parse_logs  # noqa: E402

LABELS = ROOT / "evals" / "data" / "labels.jsonl"


@dataclass
class Score:
    tier: str
    predicted: int = 0
    correct: int = 0

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else float("nan")


def predicted_sessions(attribution: Attribution) -> dict[tuple[str, str], tuple[str, str]]:
    """(repo, sha) -> (session the tool credits most energy to, tier)."""
    out = {}
    for task in attribution.tasks:
        weight: Counter[str] = Counter()
        for r, w in zip(task.requests, task.weights):
            weight[r.session_id] += w
        if weight:
            out[(task.repo, task.sha)] = (weight.most_common(1)[0][0], task.attribution)
    return out


def score(labels: list[dict], predictions: dict[tuple[str, str], tuple[str, str]]) -> dict:
    """labels: {"repo", "sha", "session": str | None}; None means "not made with AI"."""
    tiers: dict[str, Score] = {}
    ai_labeled = ai_found = false_claims = 0
    for label in labels:
        key = (label["repo"], label["sha"])
        truth = label["session"]
        guess = predictions.get(key)
        if guess is not None:
            s = tiers.setdefault(guess[1], Score(guess[1]))
            s.predicted += 1
            s.correct += int(guess[0] == truth)
            false_claims += int(truth is None)
        if truth is not None:
            ai_labeled += 1
            ai_found += int(guess is not None and guess[0] == truth)
    predicted = sum(s.predicted for s in tiers.values())
    correct = sum(s.correct for s in tiers.values())
    return {
        "labels": len(labels),
        "precision": correct / predicted if predicted else float("nan"),
        "recall": ai_found / ai_labeled if ai_labeled else float("nan"),
        "false_claims_on_hand_commits": false_claims,
        "by_tier": {t: {"predicted": s.predicted, "precision": s.precision} for t, s in sorted(tiers.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if not args.labels.exists():
        print(f"No labels at {args.labels}. Run: uv run python evals/label.py")
        return 1
    labels = [json.loads(line) for line in args.labels.read_text().splitlines() if line.strip()]
    now = time.time()
    attribution = attribute(parse_logs(iter_log_files(DEFAULT_ROOT)), now - args.days * DAY, now)
    result = score(labels, predicted_sessions(attribution))
    print(json.dumps(result, indent=2))
    target = 0.90
    ok = result["precision"] >= target
    print(f"\nprecision {result['precision']:.0%} (target ≥{target:.0%}): {'PASS' if ok else 'BELOW TARGET'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

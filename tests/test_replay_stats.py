import random

from aimpg.model import Usage
from aimpg.replay.stats import Run, commits_needed, compare

MODEL = "claude-sonnet-5"


def usage(scale: float) -> list[Usage]:
    return [Usage(fresh_in=int(2000 * scale), cache_write=int(20_000 * scale), cache_read=int(60_000 * scale), output=int(800 * scale))]


def runs_for(commits: int, setup: str, scale_fn, passed=lambda c, r: True, repeats: int = 2, seed: int = 0) -> list[Run]:
    rng = random.Random(seed)
    out = []
    for c in range(commits):
        for r in range(repeats):
            noise = 1 + rng.uniform(-0.03, 0.03)
            out.append(Run(f"c{c}", setup, r, passed(c, r), usage(scale_fn(c) * noise), MODEL))
    return out


def size(c):  # commits differ a lot in size; relative effects must handle that
    return 1 + c * 0.7


def test_clear_saving_is_detected():
    runs = runs_for(10, "base", size) + runs_for(10, "terse", lambda c: size(c) * 0.7, seed=1)
    v = compare(runs, "base", "terse")
    assert v.verdict == "uses less"
    # 0.7x tokens saves between 30% (linear terms) and 51% (output x context
    # term scales as 0.7 * 0.7), because each output token re-reads the context.
    assert -0.52 < v.effect_mid < -0.29
    assert v.ci_worst[1] < 0


def test_clear_extra_cost_is_detected():
    runs = runs_for(10, "base", size) + runs_for(10, "heavy", lambda c: size(c) * 1.4, seed=1)
    assert compare(runs, "base", "heavy").verdict == "uses more"


def test_tiny_difference_inside_noise_is_not_proven():
    rng = random.Random(3)
    base = runs_for(10, "base", size)
    chal = [Run(r.commit, "ch", r.repeat, True, usage(size(int(r.commit[1:])) * (1 + rng.uniform(-0.2, 0.2))), MODEL) for r in base]
    assert compare(base + chal, "base", "ch").verdict == "not proven"


def test_giving_up_early_cannot_win():
    # The challenger fails most commits cheaply; only shared passes are compared,
    # and too few remain for any verdict.
    base = runs_for(10, "base", size)
    lazy = runs_for(10, "lazy", lambda c: size(c) * 0.1, passed=lambda c, r: c < 3, seed=2)
    v = compare(base + lazy, "base", "lazy")
    assert v.verdict == "not enough passing commits"
    assert v.shared_commits == 3
    assert v.pass_rate["lazy"] == 0.3 and v.pass_rate["base"] == 1.0


def test_failed_runs_do_not_dilute_energy():
    # Same energy on passing runs; the challenger also has failed runs that were
    # cheap. Energy is compared on passing runs only, so no false "uses less".
    base = runs_for(10, "base", size)
    mixed = runs_for(10, "mixed", size, seed=1) + [Run(f"c{c}", "mixed", 9, False, usage(0.05), MODEL) for c in range(10)]
    assert compare(base + mixed, "base", "mixed").verdict != "uses less"


def test_commits_needed_grows_with_noise():
    quiet = runs_for(2, "base", size) + runs_for(2, "ch", size, seed=5)
    rng = random.Random(9)
    loud_base = runs_for(2, "base", size)
    loud_ch = [Run(r.commit, "ch", r.repeat, True, usage(size(int(r.commit[1:])) * (1 + rng.uniform(-0.5, 0.5))), MODEL) for r in loud_base]
    n_quiet = commits_needed(quiet, "base", "ch")
    n_loud = commits_needed(loud_base + loud_ch, "base", "ch")
    assert n_quiet is not None and n_loud is not None
    assert n_loud > n_quiet


def test_commits_needed_without_pairs_is_none():
    assert commits_needed(runs_for(1, "base", size, repeats=1), "base", "ch") is None

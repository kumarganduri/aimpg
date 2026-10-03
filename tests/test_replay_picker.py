from aimpg.replay import picker
from aimpg.replay.run import Record


def rec(setup, model, commit, passed, cost, scale=1.0):
    u = [2, int(3000 * scale), int(20000 * scale), int(500 * scale)]
    return Record(f"{commit}-{setup}", "/r", commit, setup, 0, "passed" if passed else "tests_failed", passed, model, cost, 30.0, [u])


def batch(model_specs, n=10):
    out = []
    for name, model, solve_every, cost in model_specs:
        for c in range(n):
            out.append(rec(f"claude-code@{name}", model, f"c{c}", c % solve_every == 0 if solve_every > 1 else True, cost))
    return out


def test_cheapest_good_enough_model_is_recommended():
    records = batch([("haiku", "claude-haiku-4-5", 1, 0.03), ("sonnet", "claude-sonnet-5-5", 1, 0.14), ("opus", "claude-opus-5-5", 1, 0.30)], n=20)
    text = picker.recommend(picker.rows(records))
    assert text.startswith("Use haiku: it solved 100% of tasks (best: 100%)")
    assert "10.0x lower cost per solved task than opus" in text
    assert "Tentative" not in text


def test_a_cheap_model_that_fails_a_lot_is_not_picked():
    # haiku solves only every 2nd task (50%), sonnet and opus solve all
    records = batch([("haiku", "claude-haiku-4-5", 2, 0.03), ("sonnet", "claude-sonnet-5-5", 1, 0.14), ("opus", "claude-opus-5-5", 1, 0.30)])
    text = picker.recommend(picker.rows(records))
    assert text.startswith("Use sonnet")
    assert "Tentative" in text  # only 10 runs per model


def test_cost_per_solved_task_includes_failed_attempts():
    records = batch([("haiku", "claude-haiku-4-5", 2, 0.10)])
    (row,) = picker.rows(records)
    assert row.solved == 5 and abs(row.usd_per_solved - 0.20) < 1e-9  # $1.00 total / 5 solved


def test_nothing_solved():
    records = [rec("claude-code@haiku", "claude-haiku-4-5", "c1", False, 0.02)]
    assert "No model solved" in picker.recommend(picker.rows(records))
    assert "—" in picker.render(records)


def test_render_shows_everyday_energy():
    out = picker.render(batch([("sonnet", "claude-sonnet-5-5", 1, 0.14)], n=3))
    assert "phone charge" in out and "sonnet" in out and "$0.14" in out

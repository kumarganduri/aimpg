import pytest

from aimpg.energy import (
    Verdict,
    compare,
    corners,
    joules,
    load_factors,
    model_class,
    request_wh,
    total_wh,
    weighted_tokens,
)
from aimpg.model import Request, Usage


def req(model="claude-sonnet-5", **usage) -> Request:
    return Request(id="r", session_id="s", model=model, ts=0.0, usage=Usage(**usage), cwd="/repo")


@pytest.fixture
def f():
    return corners("mid")[0]


def test_cache_write_costs_the_same_as_fresh_input(f):
    assert joules(Usage(fresh_in=1000), f) == joules(Usage(cache_write=1000), f)


def test_output_cost_grows_with_context_length(f):
    short = joules(Usage(output=100, cache_read=1_000), f) - joules(Usage(cache_read=1_000), f)
    long = joules(Usage(output=100, cache_read=100_000), f) - joules(Usage(cache_read=100_000), f)
    assert long > short


def test_cache_read_is_cheaper_than_fresh_prefill():
    for f in corners("mid"):
        assert joules(Usage(cache_read=10_000), f) < joules(Usage(fresh_in=10_000), f)


def test_low_never_exceeds_high_for_every_class():
    for model in ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"):
        r = request_wh(req(model, fresh_in=10, cache_write=40_000, cache_read=50_000, output=300))
        assert 0 < r.low < r.high


def test_low_and_high_bound_every_corner():
    usage = Usage(fresh_in=5, cache_write=20_000, cache_read=80_000, output=500)
    r = request_wh(req(**usage.__dict__))
    from aimpg.energy import wh

    values = [wh(usage, f) for f in corners("mid")]
    assert r.low == pytest.approx(min(values))
    assert r.high == pytest.approx(max(values))


def test_bigger_model_class_uses_more():
    u = dict(fresh_in=10, cache_write=1000, output=100)
    assert request_wh(req("claude-opus-5", **u)).low > request_wh(req("claude-haiku-4-5", **u)).low


def test_total_sums_requests():
    a, b = req(output=100), req(output=200)
    assert total_wh([a, b]).high == pytest.approx(request_wh(a).high + request_wh(b).high)


def test_w_is_proportional_to_joules_within_a_class(f):
    u1, u2 = Usage(fresh_in=1000, output=10), Usage(fresh_in=3000, output=30)
    assert weighted_tokens([u2], f) / weighted_tokens([u1], f) == pytest.approx(joules(u2, f) / joules(u1, f))


class TestModelClass:
    def test_known(self):
        assert model_class("claude-opus-5-5") == ("large", False)
        assert model_class("claude-sonnet-5") == ("mid", False)
        assert model_class("claude-haiku-4-5-20251001") == ("small", False)

    def test_unknown_maps_to_default_and_is_flagged(self):
        assert model_class("some-new-model") == ("mid", True)


class TestCompare:
    def test_clear_winner_holds_at_every_corner(self):
        lean = [req(fresh_in=100, output=100)]
        heavy = [req(fresh_in=10_000, output=1000)]
        assert compare(lean, heavy) is Verdict.A_WINS
        assert compare(heavy, lean) is Verdict.B_WINS

    def test_trading_cache_reads_for_output_is_too_close_to_call(self):
        # A saver that cuts cached context but writes more output can win or
        # lose depending on the unmeasured reload cost, so we refuse to rank it.
        # (100k cached tokens vs 200 extra output tokens flips across corners.)
        cache_heavy = [req(cache_read=100_000, output=1)]
        output_heavy = [req(output=201)]
        assert compare(cache_heavy, output_heavy) is Verdict.TOO_CLOSE

    def test_cross_class_is_not_comparable(self):
        assert compare([req("claude-opus-5")], [req("claude-haiku-4-5")]) is Verdict.NOT_COMPARABLE


def test_every_factor_has_a_citation():
    data = load_factors()
    entries = list(data["constants"].values()) + list(data["ranges"].values()) + list(data["classes"].values())
    assert all(e.get("citation") for e in entries)

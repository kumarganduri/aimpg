import pytest

from aimpg.cost import load_prices, price_for, total_cost, usage_cost
from aimpg.energy import WhRange
from aimpg import equivalence
from aimpg.logs import parse_logs
from aimpg.model import Request, Usage

from conftest import assistant


def test_price_lookup_uses_the_longest_matching_model_key():
    assert price_for("claude-opus-5-5")["cache_read"] == 0.2
    assert price_for("claude-opus-5")["cache_read"] == 0.5  # not confused with 5-5
    assert price_for("claude-haiku-4-5-20251001")["output"] == 5.0
    assert price_for("some-other-model") is None


def test_usage_cost_prices_each_token_kind():
    # Sonnet 5.5: $2 in, $2.50 5m write, $4 1h write, $0.20 read, $10 out (per MTok)
    u = Usage(fresh_in=1_000_000, cache_write=2_000_000, cache_read=1_000_000, output=1_000_000, cache_write_1h=1_000_000)
    assert usage_cost(u, "claude-sonnet-5-5") == pytest.approx(2 + 2.5 + 4 + 0.2 + 10)


def test_total_cost_counts_unknown_models_instead_of_guessing():
    reqs = [Request("a", "s", "claude-sonnet-5-5", 0, Usage(output=1_000_000), "/"), Request("b", "s", "mystery", 0, Usage(output=5), "/")]
    usd, unknown = total_cost(reqs)
    assert usd == pytest.approx(10) and unknown == 1
    assert total_cost(reqs[:1], [0.5])[0] == pytest.approx(5)


def test_every_price_and_comparison_has_a_source():
    assert load_prices()["source"].startswith("https://")
    data = equivalence.load()
    assert all(item["citation"] for item in data["items"].values()) and data["co2_g_per_kwh"]["citation"]


def test_everyday_comparisons_are_ranges():
    r = WhRange(7_000, 59_000)
    assert equivalence.phrase(r, "kettle") == "boiling a litre of water in a kettle 70–590 times"
    assert equivalence.phrase(WhRange(27, 213), "phone") == "1.6–13 full phone charges"
    assert equivalence.co2(r) == "3.2–27 kg CO₂"
    assert equivalence.phrase(WhRange(1, 2), "phone") == "<1 full phone charges"


def test_thinking_tokens_count_as_output_and_1h_cache_writes_are_kept(write_log):
    row = assistant("r1", "2026-09-30T19:41:41.000Z", fresh=2, write=300, read=1000, out=100)
    row["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 128}
    row["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 100}
    u = parse_logs([write_log([row])]).requests[0].usage
    assert u.output == 228 and u.cache_write == 300 and u.cache_write_1h == 200

import pytest

from aimpg.attribution import Attribution
from aimpg.coach import fresh_session_tip, model_size_tip, tips
from aimpg.model import Request, Task, Usage


def req(i, ts, ctx, out=200, model="claude-opus-5-5", session="s"):
    # base context 10k, carried context grows with the session
    return Request(f"r{i}", session, model, ts, Usage(fresh_in=2, cache_write=500, cache_read=ctx, output=out), "/w")


def session_with_commit():
    before = [req(i, 100 + i, 10_000 + i * 5_000) for i in range(10)]  # grows to 55k before the commit
    after = [req(20 + i, 300 + i, 60_000 + i * 2_000) for i in range(5)]  # carries ~50k after it
    commit = Task("/repo", "c1", 200.0, "feat", "exact", "kept")
    for r in before:
        commit.add(r)
    return before + after, Attribution(tasks=[commit])


def test_fresh_session_saving_comes_from_context_carried_past_a_commit():
    requests, attribution = session_with_commit()
    tip = fresh_session_tip(requests, attribution)
    assert tip is not None
    assert 0.1 < tip.share < 0.6
    assert tip.saving_usd > 0 and tip.saving_wh.low > 0
    assert "re-reading conversation from before your last commit" in tip.measured


def test_no_commits_means_no_fresh_session_tip():
    requests, _ = session_with_commit()
    assert fresh_session_tip(requests, Attribution()) is None


def test_model_size_tip_only_for_large_models():
    requests, _ = session_with_commit()
    tip = model_size_tip(requests)
    assert tip is not None and tip.saving_usd > 0
    assert "100% of your AI energy ran on the largest models" in tip.measured
    sonnet = [Request(r.id, r.session_id, "claude-sonnet-5-5", r.ts, r.usage, r.cwd) for r in requests]
    assert model_size_tip(sonnet) is None


def test_tips_are_ranked_by_money_saved():
    requests, attribution = session_with_commit()
    found = tips(requests, attribution)
    assert [t.saving_usd for t in found] == sorted((t.saving_usd for t in found), reverse=True)

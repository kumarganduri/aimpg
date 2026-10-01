# TODOS

## Phase 2 (blocking the Phase 2 eng review)

### Replay test leakage + model memorization
- **What:** Run replays SWE-bench style. Copy the commit's test files into the clone only after the agent finishes, then run them as the judge. Prefer commits newer than the model's training cutoff, or private repos.
- **Why:** At the parent commit, the commit's own tests don't exist yet. Adding them first leaks the answer; leaving them out means nothing judges pass/fail. Public repos (for example Legwork) may be in the model's training data, which inflates pass rates.
- **Pros:** Replay pass rates become trustworthy, and the whole leaderboard rests on them.
- **Cons:** Commits without test changes need another judge (a build check or a judge model), and the cutoff filter shrinks the candidate pool.
- **Context:** Raised by the outside voice in the 2026-10-01 eng review (finding 2). The design doc's Phase 2 step 2 says "run the repo's tests (or the tests touched by the commit)" without specifying when they're applied.
- **Depends on:** Phase 1 shipped; Phase 2 eng review.

### Statistical stability gate
- **What:** Replace "same order across 3 repeats" with a paired per-commit difference (setup A minus setup B) and a bootstrap 95% CI that must exclude 0. Re-cost the replay budget for 10+ commits.
- **Why:** With 2 setups, "same order 3 times" happens by chance 25% of the time, so the Phase 3 gate would pass on noise.
- **Pros:** Public rankings only appear when the difference is real.
- **Cons:** More replays means more token spend; the budget is likely above the current $60-150 estimate.
- **Context:** Outside voice finding 5, 2026-10-01 eng review. It affects Success Criteria (Phase 2) and the Phase 3 gate.
- **Depends on:** Phase 2 harness.

### Network-limit spike for the replay sandbox (macOS: DONE 2026-10-01, see spikes/egress/RESULTS.md)
- **What:** A time-boxed spike to make the replay container reach only the model API. Docker can't restrict outbound traffic by domain on its own, so it needs an allowlisting CONNECT proxy on an internal Docker network. Check what Legwork's sandbox actually does first.
- **Why:** The approved sandbox contract (eng review D7) promises "egress limited to the model API" but had no build plan or estimate.
- **Pros:** Makes the "no GitHub cheating" promise real.
- **Cons:** TLS through a proxy and the CLI's proxy settings can be fiddly per agent CLI.
- **Context:** Outside voice finding 7. Estimate: human ~1-2 days / CC ~2 hrs. It should be the first task of Phase 2.
- **Result:** macOS works without Docker: Seatbelt allows only a local port, and an allowlisting CONNECT proxy reaches only api.anthropic.com. Remaining: the real `claude -p` run inside it, and the Linux (bwrap) variant.
- **Depends on:** Nothing. It can be spiked any time.

# Design: Real-world MPG for AI agents (working name `aimpg`)

Written 2026-10-01; updated through the eng review and the Phase 1 build.
Branch: main (new project, no repo yet)
Repo: github.com/kumarganduri/aimpg
Status: APPROVED
Mode: Builder (open source / climate impact)

## Problem Statement

AI energy use is growing fast. Most of it goes to repeated, oversized, or misdirected work rather than new thinking. Coding agents are the heaviest token burners: they re-read history, dump huge tool outputs, retry, and use frontier models for trivial steps. Nobody can see this waste per useful outcome, so nobody competes on it. Tools claim "50-90% fewer tokens, zero behavior change" with no independent proof.

Root problem chosen: **invisibility**. Of the five kinds of AI waste mapped in the session (repetition, oversizing, translation, misdirection, invisibility), invisibility drives the others. Make "energy per solved task" a number people compare, and tool makers will compete on it.

## What Makes This Cool

- **A fuel receipt for your own AI work.** "Last 30 days: 41 kept commits, ~1.2-3.0 kWh, median 38 Wh per commit; 58% of energy went into the first 10 requests of each session; 22% produced commits you later threw away." Personal, surprising, shareable.
- **Personal SWE-bench.** Replay your own past commits (parent checkout plus the original task) through Claude Code, Codex (and others once they expose token usage), or the same tool with and without RTK, Caveman or routing. Your own tests judge each run. The result is a controlled comparison on real code, which neither lab benchmarks nor passive logs can produce.
- **Repo efficiency badge**, like a coverage badge: "agent setup: 22 Wh per replayed task, 100% pass." It spreads on its own.
- **An open replay leaderboard**, Fuelly-style real-world numbers next to the EPA-sticker lab numbers (Joule Index, AI Energy Score).

## Constraints

- Solo builder, free and open source, climate-impact mission.
- Energy for closed models (Claude, GPT) cannot be measured, only estimated.
- Privacy: code and prompts never leave the machine. Only aggregate numbers, opt-in.
- Replays cost real tokens. Sampling must be small and lean on cheap tiers.

## Premises (agreed)

1. Invisibility is the root problem. Making "energy per solved task" a compared number drives the other fixes.
2. *(Revised after second opinion)* The wedge is **controlled comparisons on real-world code**. Passive logs give each user a receipt. Paired replays from git history decide which tools and savers actually win. The public leaderboard is built from replay results, not passive logs, because passive data confounds tool with user and task.
3. The first data source is Claude Code session logs, local-first.
4. Energy is published as estimates with ranges and an open method. For same-model comparisons, rank on weighted tokens (fresh input, cache read and output weighted separately), since the energy factor is a constant there.
5. "Solved" means observable proxies (tests green, commit kept N days, PR merged), never self-report.

## Landscape (checked 2026-10-01)

- Token savers for coding agents already exist: RTK, Tamp, Caveman, Paritok, context-mode, dynamic context pruning. We do not build another one. They become entries on the board.
- Lab energy benchmarks: Joule Index (preview, 3 bugs, joules + $ per coding task), Hugging Face AI Energy Score and ML.ENERGY (per model). The SWE-bench energy study finds about 60% of energy in the first 10 agent steps. HAL tracks cost against accuracy.
- Reusable pieces: **ccusage** (parses `~/.claude/projects/*.jsonl` into input, output, cache-create and cache-read tokens per session and model) and **EcoLogits** (per-call energy/GHG estimates from parameter-count classes, with ranges and an open method). CodeCarbon is not a fit: it measures local hardware, not remote inference.
- Ideas explored and parked for later, as future leaderboard entries: JIT-compiling agent hot paths into code or tiny models; latent (KV) agent communication (KVCOMM, LatentMAS); a shared KV-cache CDN (blocked on the bandwidth vs recompute math); a get-it-right-first-time clarifier; local-first cascades; carbon-aware scheduling.

## Approaches Considered

- **A: Fuel receipt only.** Ships fastest, but it can't rank tools or prove savers are lossless. Kept as phase 1 of B.
- **C: Efficiency auto-tuner** (overnight replay A/B tests that recommend a config). Strongest personal payoff, weaker as a public standard. Deferred as an add-on once B's replay engine exists.

## Recommended Approach: B, receipt → personal SWE-bench → open replay leaderboard

### Phase 1: `aimpg report` (receipt). Human ~1-2 weeks / CC ~1-2 days
1. **Language and parser:** Python (PyPI, `uvx aimpg`). Write a small JSONL parser in Python, using ccusage's TypeScript source as the reference for field semantics only. No runtime dependency on ccusage or the network. Energy factors are bundled in the package.
**Data flow (eng review, D3-D10):**

```
~/.claude/projects/**/*.jsonl
        │  logs.py: tolerant parse, dedup by requestId (keep max output row),
        │           drop <synthetic>, sidechain → parent session, coverage counters
        ▼
  [Request] ──────────────┐  model.py: Request → Session → Task (shared with Phase 2)
        │                 │
        ▼                 ▼
  attribution.py      energy.py + factors.json (versioned, cited)
  tier 1 exact:  Bash `git commit` call ↔ commit in that repo ±30s
  tier 2 fuzzy:  window + file overlap (Edit/Write + Bash paths + subagent edits)
  else:          unattributed (with reason)
        │                 │
        ▼                 │
  gitkept.py (batched per repo: one git log --numstat, git cherry)
  kept / pending / discarded / unknown(stale ref)
        │                 │
        └──────► receipt.py ◄┘ ──► cli.py (`aimpg report`)
```

2. **Parser (D8, 6A):**
   - Read `~/.claude/projects/**/*.jsonl`, including subagent/sidechain files (sidechain requests roll up to the parent session).
   - **Dedup is mandatory.** Measured on the author's logs: 11,560 usage rows but only 4,648 unique requestIds, because each content block row repeats the request's usage, so naive sums overcount about 2.5x. Keep the row with the max `output_tokens` per requestId (55 groups disagreed, likely partial streaming rows). 227 requestIds span files (resume/fork).
   - Drop `model == "<synthetic>"`. Rows without a requestId use a `(sessionId, message.id)` key.
   - Unknown row types and corrupt lines are counted, never fatal. The receipt always prints a coverage line (parsed %, skipped by reason) and the Claude Code versions seen.
   - Several models in one session are handled per request. A cwd outside any git repo, or a repo that has been moved or deleted, is `unattributed` with a reason.
   - Performance: a full parse of 190MB measured 0.7s, so 500MB is projected at about 2s. No parse cache is needed.
3. **Session ↔ commit attribution (D3, 1A, tiered):**
   - **Tier 1, exact (D20, measured):** for each Bash tool call running `git commit` (229 in the author's logs), match the commit in that call's repo whose committer time falls within `[tool_use ts − 2s, tool_result ts + 2s]`. On T0 this matched 8/8 recent commits, including one whose hooks made it land 147s after the call started (a ±30s rule would have missed it). It doesn't depend on output formats.
   - **Always use the row's own `cwd`, never the log folder name.** Claude Code files logs under the folder a session started in: in the author's logs, all work on one project (4,947 rows) lived under a different project's log folder.
   - **Tier 2, fuzzy:** resolve cwd to the repo toplevel (handles subdirectories and worktrees). The window runs from the first message to the last message + 2h. A candidate commit must touch at least one file in the session's touched set: Edit/Write/MultiEdit/NotebookEdit paths, plus paths parsed from Bash (`sed -i`, `perl -pi`, `>`/`>>` redirects, `mv`/`cp`/`rm`), plus subagent edits. Bash edits are about 30% of all edits in the author's logs. Commits matched only in the +2h grace period are labeled `grace`. **Fuzzy matches must be authored by the repo's `user.email`** (build finding: without this, 11 of 11 fuzzy matches on real data were teammates' merged PRs pulled into the window). No configured email means no fuzzy matching.
   - **Energy split (D21): time segments.** Within a session, the requests between commit k−1 and commit k belong to commit k, **in any repo** (build finding 2026-10-01: grouping by session+cwd repo stranded 52% of energy, because sessions often run from one folder and commit into another via absolute paths or `cd x && git commit`; per-session segmentation raised exact-match coverage from 48% to 83%). Requests after the last commit are `unattributed (no commit yet)`. One session can span days and many repos (one session made 86 commits to a single repo). Lines-changed splitting is used only for tier-2 fuzzy matches where no commit call exists. A commit matched by several sessions sums their segments.
   - **Rewrites (D15):** tier 1 also scans `git reflog`. An orphaned SHA (amend, rebase) maps to its rewritten successor by patch-id, so the work keeps its attribution.
   - **Build order (D13, staged):** build tier 1 + the labeled accuracy eval first, then measure what share of attributed AI Wh lands on exact matches. If it's under 70%, build tier 2 next, still inside Phase 1 and before launch. Tier 2 is sequenced, not cut.
   - Every task carries `attribution: exact | fuzzy | grace`. The receipt shows the exact share. Sessions with no match go into the "unattributed Wh" bucket, which is always reported.
4. **Kept status (D6, 4A):**
   - The reference ref is the newer of local `<default>` and `origin/<default>` (default branch read from `origin/HEAD`, falling back to `main`/`master`). Use built-in `git cherry <ref> <commit>` (patch-id equivalence, survives rebases) instead of hand-rolled patch-id code.
   - States: `pending` (under 7 days old, shown but left out of medians), `kept`, `discarded` (energy still counted), `unknown` (the ref's last update is older than commit + 7 days, so the receipt says "pull to update"), `unknown (possible squash)`.
   - **Offline squash check (D11):** if `git cherry` finds no equivalent, test whether the commit's added lines appear in some default-branch commit that touches the same files within 30 days (subset match). A match counts as `kept (squash)`. An inconclusive result is `unknown (possible squash)`, never `discarded`. The receipt's discarded % covers only commits where the squash check ran conclusively.
   - `--fetch` (opt-in network) runs `git fetch` and enables the squash fallback via `gh` (PR → merge commit). Without `--fetch` there are no network calls.
   - Batched per repo (D10, 8A): one `git log --all --numstat --format=...` over the window, plus `git cherry` per ref, held in memory. No per-commit subprocesses.
5. **Energy model, one formula (D4, 2A):**
   - Per request:
     `Wh = E_pre[c]·(fresh_in + cache_write) + E_kv[c]·cache_read + output·(E_dec[c] + E_ctx[c]·ctx_len)`
     where `ctx_len = fresh_in + cache_write + cache_read`.
   - A cache write is a full prefill, so it costs the same as fresh input. The 1h/5m cache split is billing only and is ignored.
   - `E_*` are low/high ranges per model class in a versioned `factors.json`, with a citation per coefficient (prefill/decode from ML.ENERGY measurements of comparable open models, cross-checked against EcoLogits classes). The factors are vendored with a sync script and EcoLogits is not a runtime dependency. Unknown models map to the nearest class and are flagged in the receipt.
6. **Ranking metric:** `W = Wh / E_pre[c]`, the same formula with the model-class scale removed, so W and Wh can never disagree. Same-model comparisons rank on W, and the receipt displays Wh ranges.
   - **Sensitivity check (D12):** the ratios inside W (E_kv/E_pre, E_dec/E_pre, E_ctx/E_pre) are estimates. Every comparison computes W at the low and high ends of all ratios. "A beats B" is reported only when it holds at both ends. Otherwise the result is "too close to call with current energy data". The raw token breakdown is always shown next to W.
   - **Same-model only (D14):** v1 rankings compare setups on the same model. Cross-vendor results show raw tokens and Wh ranges labeled "not directly comparable".
6b. **Shared core model (D5, 3A):** `Request(id, session_id, model, ts, usage, ctx_len, is_sidechain, repo, files_touched)` → `Session` → `Task(commit, attribution, status)`. Energy is computed only from `Request`s in `energy.py`. Phase 2 plug-ins return the same `Request` shape.
7. **Receipt:**
   - Wh per kept commit over 30 days (range, median of kept only).
   - Kept / pending / discarded / unattributed totals.
   - Top 3 most and least efficient tasks.
   - Share of energy in the first 10 requests of each session.
   - **v1 has no content-level breakdown** (tool output vs context re-reads). That requires tokenizing content blocks per turn and is deferred to v2.
8. Local only. No network calls.

### Phase 2: `aimpg replay` (personal SWE-bench). Human ~4-6 weeks / CC ~1-2 weeks
1. Select N kept commits with a test signal (default 5-10, sampled to cap token spend).
2. For each one: make a fresh shallow clone at the parent commit (see step 4) and derive the task prompt from the commit message, linked issue or PR description. Run setup X headless in a sandbox, run the repo's tests (or the tests touched by the commit) and record weighted tokens, Wh range, wall time and pass/fail.
3. **Plug-in contract:** each setup (`claude-code`, `claude-code+rtk`, `claude-code+caveman`, `codex`, ...) declares:
   - `run(cmd, prompt, workdir)`: its headless invocation;
   - `tokens(log_path) -> list[Request]`: its log parser, returning the shared core model (D5) so the Phase 1 energy code is reused unchanged;
   - `image`: the container image the setup runs in (D7);
   - `usage_map`: how the vendor's usage fields map onto `fresh_in / cache_write / cache_read / output` (`cache_write` is nullable, e.g. for OpenAI) (D14);
   - `token_source`: `exact`, `estimated` or `none`.
   Setups with `token_source: none` (possibly Cursor) are excluded from rankings until they expose usage. v1 ships only `claude-code` and `claude-code+rtk`.
4. **No answer leakage + safety (D7, 5A, contract; details in the Phase 2 eng review):**
   - The replay workdir is a fresh shallow clone at the parent commit, with no future refs, tags or reflog.
   - Each run happens in a Docker container. Only that clone is mounted (read-write). Nothing from `$HOME` is mounted, so the agent can't read the real repo.
   - Network egress is limited to the model API, so the agent can't search GitHub for the merged commit.
   - The API key comes in through an env var. The user's agent config directory is never mounted.
5. Report paired results with an explicit "lossless?" verdict: pass rate is the same or better at lower weighted tokens.
6. **Guard rails:**
   - a hard API spend cap, plus a dry-run cost estimate before anything runs;
   - API-key billing only, since subscription usage can't be attributed per run;
   - an `--allow-net` style sandbox permission.

### Phase 3: Open leaderboard + badge. Human ~4 weeks / CC ~1 week
**Gate:** do not start Phase 3 until Phase 2 produces a stable ranking (same order across 3 repeats) on at least 2 repos.
1. Opt-in upload of replay summaries only: setup id, model class, repo language and size bucket, task size bucket, weighted tokens, Wh range, pass/fail. No code, prompts, paths or repo names. The repo id is `HMAC(random per-install salt, remote URL)`, used only for dedup. A plain hash of a public repo name could be reversed with a dictionary attack (D15).
2. Static leaderboard site that shows rankings only where paired replays support them, with confidence intervals.
3. Badge endpoint plus README snippet. The **badge number comes from replays** (median weighted tokens and Wh per passing replayed task for the repo's chosen setup), not from passive logs, so it is consistent with premise 2.

## Open Questions

- **Prompt fidelity in replays:** commit messages are a weak stand-in for the original task. Should we use the PR description, the linked issue, or the first user message from the original session log when available? The last is best but only exists for Claude Code commits.
- **Test coverage:** many commits touch code with no tests. Do we skip them (biased sample) or use a weaker proxy such as builds plus a judge model?
- **Determinism:** agent runs vary. How many repeats per setup make a ranking stable? Start with 3 and measure the variance.
- **Energy factors:** EcoLogits ranges for current frontier models are wide. Should we publish weighted-token rankings as primary and Wh as secondary?
- **Name:** "Fuelly" is an existing brand and must not be used. The working name `aimpg` is a placeholder.
- **Partnering:** should the open method and replay data be contributed to the Joule Index / AI Energy Score once it's credible?

## Test Plan (eng review D9, 7A)

pytest. Every planned path ships with its tests.

- `tests/test_logs.py`:
  - dedup keeps the max-output row
  - `<synthetic>` is dropped
  - a missing requestId falls back to the (session, message.id) key
  - sidechain rows roll up to the parent session
  - unknown row types and corrupt lines are counted, not fatal
  - the coverage line math is correct
  - Fixtures are scrubbed snippets of real logs.
- `tests/test_attribution.py`:
  - tier 1 call-interval match, including a slow-hook commit (152s call) and a miss just outside the interval (D20)
  - time-segment energy split across many commits in one multi-day session; cwd changes between repos mid-session (D21)
  - logs filed under another project's folder are attributed by row cwd
  - tier 2 window + overlap
  - Bash path extraction (`sed -i`, `perl -pi`, redirects, `mv`/`cp`/`rm`)
  - subagent edits roll up
  - many-to-many split by lines changed
  - subdirectory/worktree cwd resolves to the repo toplevel
  - deleted repo → unattributed with a reason
  - `grace` label
- `tests/test_gitkept.py` (temp git repos built in the test):
  - kept via `git cherry`
  - a rebased commit (new sha, same patch) stays kept
  - discarded
  - pending (under 7 days)
  - stale ref → unknown
  - `--fetch` squash path (with `gh` mocked)
  - offline squash: subset match → `kept (squash)`, inconclusive → `unknown (possible squash)`, never `discarded` (D11)
  - amend and interactive rebase within a session → reflog successor keeps attribution (D15)
- `tests/test_energy.py`:
  - `cache_write` costs the same as `fresh_in`
  - output cost increases with `ctx_len`
  - invariant: W ∝ Wh within a class
  - low ≤ high for every range
  - unknown model → nearest class + flag
  - `factors.json` schema: every coefficient has a citation
  - sensitivity: a constructed pair whose order flips across ratio bounds reports "too close to call"; a robust pair reports a winner (D12)
  - cross-vendor comparison is labeled "not directly comparable" (D14)
- `tests/test_e2e_report.py` [E2E]: a generated fixture repo plus scripted logs with known answers. `aimpg report` output matches the expected tasks, states and Wh ranges. Also covers no `~/.claude/projects`, no git repos, and all-pending.
- `tests/test_perf.py`: a 2,000-commit fixture repo + 500MB of synthetic logs must finish in under 10s (marked slow).
- **Accuracy eval [EVAL]** (`evals/attribution_eval.py`, data kept local and uncommitted): about 30 hand-labeled commits from the author's repos, with precision and recall per tier. Target: ≥90% precision for exact+fuzzy.

## Success Criteria

- Phase 1: the receipt runs on 3 of the author's own repos in under 10 seconds and surfaces at least one non-obvious finding worth a launch post.
- Phase 2: a paired replay of 5+ commits on one repo gives a stable ranking (same order across 3 repeats) between at least 2 setups. Budget: 5 commits × 2 setups × 3 repeats = 30 runs, roughly $60-150 on API billing with Sonnet-class models. Use a Haiku-class model first to validate the harness for under $15.
- Phase 3: 25+ external repos contribute replay summaries within 2 months of launch, and at least one tool maker responds publicly to its ranking.

## Distribution Plan

- PyPI package with `uvx aimpg report` zero-install. Python is decided, with a self-contained parser.
- GitHub Releases + a GitHub Actions publish pipeline on tag.
- Leaderboard as a static site (GitHub Pages or Fly) plus a tiny ingest endpoint.

## Dependencies

- ccusage (reference for log field semantics only), EcoLogits (energy method and factors, bundled), git, headless modes of each agent CLI.
- Phase 2 needs a sandbox: reuse what the author learned building [Legwork](https://github.com/kumarganduri/legwork)'s install sandbox.

## Next Steps

1. Spike (2 hours): run ccusage on your own logs, then hand-join one week of sessions to `git log`. Is the session-to-commit join clean enough?
2. Build `aimpg report` (Phase 1) and run it on the author's repos.
3. Write the launch post around the most surprising finding.
4. Prototype replay on a project with 5 commits × 2 setups (`claude-code` vs `claude-code+rtk`).
5. Run `/plan-eng-review` on Phase 2 before building the harness.

## Eng Review Outputs (2026-10-01)

### What already exists
- **ccusage**: reference for log field meanings only. We write our own small Python parser and add no runtime dependency on it.
- **EcoLogits / ML.ENERGY**: sources for the energy coefficients. They are vendored into `factors.json` with citations, and EcoLogits is not a runtime dependency.
- **`git cherry`** (patch-id equivalence): replaces hand-written patch-id matching for kept status.
- **`git log --numstat`, `git reflog`**: batched per repo; they provide commit times, files, line counts and rewrite successors.
- **The Claude Code logs themselves**: Bash `git commit` tool calls give exact attribution (229 in the author's logs). Nothing needs inferring for those.

### NOT in scope (this review)
- Phase 2 replay internals (selection, prompt derivation, repeats): get their own eng review. Only the contracts are locked here (D2).
- Phase 3 leaderboard and badge: gated on Phase 2 passing the statistical gate (TODOS.md).
- The v2 content-level breakdown (tool output vs context re-reads): needs per-turn tokenization.
- Cross-vendor rankings: v1 is same-model only (D14).
- A parse cache: the full parse measured 0.7s for 190MB, so it isn't needed.

### Failure modes
| Codepath | Realistic failure | Test | Handling | User sees |
|---|---|---|---|---|
| logs.py | New Claude Code version adds or renames a field | fixtures + unknown-type counter | counted, not fatal | coverage line shows skipped % |
| logs.py | Double counting from repeated content-block rows | dedup test | max-output row per requestId | correct totals |
| attribution tier 1 | Amend or rebase orphans the SHA | reflog test | reflog successor by patch-id | stays attributed |
| attribution tier 2 | Bash edit path not parsed | path extraction tests + eval | falls back to unattributed | unattributed bucket grows, never hidden |
| gitkept | Local default branch is stale | stale-ref test | `unknown (pull to update)` | clear label |
| gitkept | Squash merge | offline squash tests | subset match or `unknown (possible squash)` | never mislabeled as discarded |
| energy | Unknown model name | nearest-class test | nearest class + flag | flagged row |
| energy | Ratio uncertainty flips a ranking | sensitivity test | "too close to call" | honest result |
| repo discovery | Repo moved or deleted | E2E test | unattributed with reason | reason shown |

Critical gaps (no test, no handling, and silent): **0**.

### Parallelization
| Step | Modules touched | Depends on |
|---|---|---|
| Core model + parser | aimpg/model, aimpg/logs | none |
| Energy + factors | aimpg/energy, factors.json | core model |
| Git layer (log, cherry, squash, reflog) | aimpg/gitkept | none |
| Attribution tier 1 + eval harness | aimpg/attribution, evals/ | core model, git layer |
| Receipt + CLI | aimpg/receipt, aimpg/cli | all of the above |

- Lane A: core model → parser → energy (sequential, shared model).
- Lane B: git layer (independent).
- Then lane C: attribution tier 1 + eval. Then receipt + CLI. Tier 2 only if the D13 gate fires.
- Launch A and B in parallel worktrees, merge both, then C.

### Implementation Tasks
- [x] **T0 (P1)**: Hand-match check done 2026-10-01: 8/8 recent commits matched exactly via call interval (D20); split rule changed to time segments (D21).
- [x] **T1 (P1, human: ~1 day / CC: ~45min)**: logs: tolerant parser with requestId dedup, synthetic drop, sidechain rollup and coverage line (D8). Files: aimpg/logs.py, aimpg/model.py, tests/test_logs.py. Verify: `pytest tests/test_logs.py`
- [x] **T2 (P1, human: ~1 day / CC: ~1hr)**: energy: one physics formula, W derived from it, sensitivity check, versioned cited factors (D4, D12). Files: aimpg/energy.py, aimpg/factors.json, tests/test_energy.py
- [x] **T3 (P1, human: ~1 day / CC: ~1hr)**: gitkept: batched git, `git cherry`, stale-ref and offline squash states (D6, D10, D11). Files: aimpg/gitkept.py, tests/test_gitkept.py
- [x] **T4 (P1, human: ~1 day / CC: ~1hr)**: attribution tier 1 (git commit ±30s + reflog successors) and the labeled accuracy eval (D3, D9, D15). Files: aimpg/attribution.py, evals/attribution_eval.py, tests/test_attribution.py
- [x] **T5 (P1, human: ~1 day / CC: ~45min)**: receipt + CLI + E2E fixture + perf test. Files: aimpg/receipt.py, aimpg/cli.py, tests/test_e2e_report.py, tests/test_perf.py
- [x] **T6**: tier 2 built (Bash paths, author rule, grace, lines split). After the per-session fix, exact matches cover 83% of in-repo energy (above the 70% gate) and tier 2 adds 0 on the author's data, so it stays as a conservative fallback.
- [ ] **T7 (P2, human: ~0.5 day / CC: ~20min)**: PyPI packaging + GitHub Actions trusted publishing on tag


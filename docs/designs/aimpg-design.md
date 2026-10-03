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
   - **Energy split (D21): time segments.** Within a session, the requests between commit k−1 and commit k belong to commit k, **in any repo** (build finding 2026-10-01: grouping by session+cwd repo stranded 52% of energy, because sessions often run from one folder and commit into another via absolute paths or `cd x && git commit`; per-session segmentation raised exact-match coverage from 48% to 83%). Requests after the last commit are `unattributed (no commit yet)`. One session can span days and many repos (one session made 86 commits to a single repo). Lines-changed splitting is used only for tier-2 fuzzy matches where no commit call exists.
   - **Long sessions (build decision):** a 2h+ pause starts a new work burst. A commit's direct energy is its final burst. Earlier bursts since the previous commit are its *lead-up*: reported, never dropped, and excluded from medians and rankings. On real data, one commit had absorbed 6 days of a session, and only 32% of that came after the last break. A commit matched by several sessions sums their segments.
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

### Phase 2: `aimpg replay` (personal SWE-bench). Eng-reviewed 2026-10-01 (decisions R1–R21)

**v1 scope (R1, widened by R20–R21):** macOS sandbox only. **Python (pytest) and JS/TS (vitest/jest) repos.** **Three setups.** The rigor parts all stay: allowlist proxy, hermetic agent runs, hidden tests, and the statistics gate. Linux (bwrap + socket relay) comes later (TODOS.md).

```
select (free)              Phase A: prepare     Phase B: agent             Phase C: judge
────────────────           ────────────────     ───────────────────        ─────────────────
your commits that          fresh shallow clone  setup runs in sandbox      copy the commit's
change code + tests        at parent commit     proxy: api.anthropic.com   test files in,
 ├ message ≥ 6 words       uv sync in sandbox   only; --bare; throwaway    run them, no network
 ├ tests FAIL on parent    proxy: PyPI only     config dir; budget cap     → pass / fail
 └ tests PASS on commit    (once per commit,    tests are NOT present
   (run twice: not flaky)   shared read-only)
```

1. **Selection (R4, free).** v1 pool before the fail→pass filter:
   - Python: Legwork (47 code+test commits) and Mynah (16).
   - JS/TS: humearth (7) and chrome-dino (6).
   - Ghost isn't a root-level package.
   - **JS/TS (R21):** detect vitest or jest from `package.json`. Phase A runs `npm ci` through a proxy that allows only `registry.npmjs.org`. Phase C runs only the commit's test files. The per-run APFS clone covers `node_modules`, which has no editable-path problem.
   - Your own commits that change both code and tests.
   - Commit message of at least 6 words.
   - Committed after the model's training cutoff, so the model can't have memorized it. This resolves the memorization TODO.
   - **Fail→pass pre-check:** the commit's test files applied to the parent code must fail, and on the commit itself must pass, twice each. Flaky tests are dropped.
2. **Task (R3, revised after paid calibration):** commit subject plus body, presented as an issue, **plus an interface hint**: the new names the hidden tests import, file by file (e.g. `legwork/llm_client.py: RATE_LIMIT_RETRY_ATTEMPTS`). Names only, never test code.
   - Why: in the first calibration every run failed before a single test ran, because hidden tests import names the commit invented and a commit message never says what things are called.
   - Fairness check (free, in selection): stub code that only creates the hinted names with empty values must still **fail** the tests. Otherwise the commit is dropped, because the hint would give the answer away.
   - Both setups get identical text.
   - **First full run (2026-10-03, 80 runs, $13.19):** with hints, pass rates were 12% (plain) vs 10% (RTK). Only 1 commit was solved by both, so no verdict. Hidden tests check details a commit message never states (argument order, exact wording, new script files). Descriptive only: RTK's median energy was ~8% *higher*, and its cost $6.72 vs $6.46.
   - **`--task-mode tests`:** the commit's tests are shown ("make these pass without editing them"). Originals are always restored before judging, so editing tests can't help (a fake cheating agent proves it). Tasks become easier than real work, but both setups face the same task.
3. **Setups (R2 + R20):** all on the same model, so every comparison is fair:
   - `claude-code` (baseline);
   - `claude-code+terse`: adds a short "answer tersely, no recaps" `--append-system-prompt`, which is Caveman's core idea;
   - `claude-code+rtk`: RTK's command-output compression hook, configured inside the run's config folder. RTK is a third-party binary, so building this setup starts with an explicit install approval (name, source, size).

   Each challenger is compared with the baseline separately. Each CI is 97.5%, so the pair of claims together stays at 95%.
4. **Plug-in contract:** each setup declares
   - `name`
   - `argv(task, workdir) -> list[str]`
   - `env`
   - `token_source`
   - `usage_map`
   - `tokens(config_dir) -> list[Request]`

   For Claude Code, `tokens` is the **Phase 1 parser run on the run's throwaway config folder** (R9). Claude Code's reported `total_cost_usd` is used only for budget enforcement, and as a cross-check that alerts if tokens disagree by more than 2%.
5. **Sandbox (R5, R6; spike-proven):**
   - Seatbelt profile copied from Legwork's hardened rules, with a header citing the source commit. Reads under `$HOME` are limited to the workdir, and local sockets (SSH agent etc.) are denied.
   - Network allows only the per-phase proxy port.
   - The allowlisting CONNECT proxy permits PyPI hosts in Phase A, `api.anthropic.com` in Phase B, and nothing in Phase C.
   - **Spike-verified 2026-10-02 (`spikes/agent/RESULTS.md`, free, fake key):** Claude Code starts in the hardened profile with read-only access to its install folder, honors `HTTPS_PROXY`, and writes its transcript to `CLAUDE_CONFIG_DIR` under `--bare`. Its telemetry attempt was blocked; set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. A rejected key makes it retry, so the harness fails fast on 401 (R14).
   - **Run isolation (R18):** every run lives under one replay root. Each agent profile denies that whole root except its own workdir and config dir, `TMPDIR` points inside the workdir (no shared system temp), and hidden tests are staged only after the agent exits.
   - **Hermetic config (build change):** no `--bare`, because RTK works through a hook and `--bare` skips hooks, and every setup must be configured the same way. Instead each run gets a **fresh, empty config folder** as `HOME` and `CLAUDE_CONFIG_DIR`, so your hooks, plugins, memory and personal `CLAUDE.md` don't exist there. MCP is off (`--strict-mcp-config`), and the sandbox blocks the keychain, so only the API key can bill. The repo's own `CLAUDE.md` at the parent commit still loads, like real use. RTK's hook (`rtk init -g --auto-patch`) is written only into that throwaway folder; a test proves the user's settings stay unchanged.
   - `CLAUDE_CONFIG_DIR` points to a throwaway folder, and the API key comes from your shell's env var only. It is never written to disk or logs.
6. **Outcomes (R10):** every run is recorded as one of `passed`, `tests_failed`, `timeout` (30 min), `budget_hit`, `agent_error`, `sandbox_denied` or `harness_error`.
   - All but `harness_error` count as not passed, and their tokens still count.
   - A `harness_error` (our bug) is retried once. If it fails again, that commit is excluded for **both** setups.
   - Results are appended to a JSONL file after every run, so a crash loses nothing.
7. **Statistics (R7, revised R15–R16):**
   - **Energy is compared only on shared passes:** commits where both setups passed at least once, paired per commit.
   - The challenger wins only if the bootstrap 95% CI of the paired W difference excludes 0 at every energy-factor corner (D12).
   - Pass rates are reported separately, each with its own CI.
   - `timeout` and `budget_hit` count at the cap (worst case), never as cheap.
   - Fewer than 6 shared-pass commits gives "not enough passing commits".
   - Otherwise the result is a winner or **"not proven"**. Pure Python, no dependency.
   - **Calibration (R16):** 2 commits × 2 setups × 2 repeats = 8 runs. aimpg then shows cost per run, the measured noise, the commits needed to detect a 10% energy difference, and the total cost. You approve that size, or stop.
8. **Budget (R8):** Sonnet-class model.
   - Calibration first (R16, 8 runs), each with `--max-budget-usd`. Calibration also confirms the last unverified point: usage rows in a real transcript.
   - aimpg then proposes a run size and total cost, and asks for a total cap. It stops as soon as the sum of reported costs reaches the cap.
   - **Integrity (R14):** transcript tokens are cross-checked against `total_cost_usd` from the harness-captured stdout. The agent can't reach that stdout, though it could touch its own transcript. An empty transcript, or a mismatch over 2%, is a `harness_error`, never zero tokens.
   - API-key billing only. The user creates the key in the Anthropic Console.
9. **Speed (R11, corrected by R14):** up to 3 runs in parallel, on different commits.
   - Phase A runs once per commit. **Each run gets an APFS clone (`cp -c`) of the prepared workdir plus `uv sync --offline`** (0.5s, no network). The spike showed a shared env imports the original folder, so the agent's edits would be invisible.
   - **Dependencies (R17):** Phase A installs packages from the *commit's* lockfile (package list only, no code), so commits that add a dependency stay replayable. The report counts them.
   - Wall time is recorded as informational, not as a ranking metric.
10. **Tests (R12):**
    - A fake-agent plug-in (modes: `solve`, `nothing`, `crash`, `timeout`, `escape`) drives free end-to-end tests on a fixture repo.
    - Sandbox escape tests run only in macOS CI.
    - The single paid run is the calibration you confirm.

### Phase 3: Open leaderboard + badge. Human ~4 weeks / CC ~1 week
**Gate:** do not start Phase 3 until Phase 2's statistics gate (R7) declares a result, winner or "not proven", on at least 2 repos.
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
- Phase 2: on one repo, 10 commits × 2 setups × 2 repeats give a verdict under R7 (winner or "not proven") within the total cap you confirm after calibration, with zero sandbox escapes in the fake-agent `escape` tests.
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


## Phase 2 Eng Review Outputs (2026-10-02)

### What already exists (reused, not rebuilt)
- **Phase 1 parser + energy model:** replay token counts come from the same code as `aimpg report` (R9).
- **Legwork's Seatbelt rules:** copied with attribution into `aimpg/replay/sandbox.py` (R6).
- **Claude Code built-ins:** `--bare` (hermetic: no hooks/plugins/CLAUDE.md/auto-memory), `--max-budget-usd` (per-run cap), `--append-system-prompt` (terse setup), `CLAUDE_CONFIG_DIR` (throwaway config), and `HTTPS_PROXY` (verified).
- **macOS `cp -c` (APFS clone) + `uv sync --offline`:** per-run copies in about 1s.
- **SWE-bench's fail→pass validation idea:** used for selection (R4).

### NOT in scope (v1)
- Linux sandbox: TODOS.md.
- Leaderboard upload and badge: Phase 3, gated on R7 verdicts on 2 repos.
- Languages other than Python and JS/TS.
- Cross-vendor setups such as Codex: same-model comparisons only (D14).

### Failure modes
| Codepath | Failure | Test | Handling | Visible? |
|---|---|---|---|---|
| sandbox | agent reads the real repo or a sibling run | fake-agent `escape` | deny-all replay root (R18) | test fails loudly |
| proxy | agent reaches GitHub | escape test + proxy log | only api.anthropic.com in Phase B | BLOCK logged |
| transcript | empty, or edited by the agent | cross-check test | `harness_error` (R14) | yes |
| env | edits invisible because of a shared venv | e2e fake `solve` must pass | per-run clone + offline sync | e2e fails |
| deps | commit adds a package | fixture commit adding a dep | commit's lockfile in Phase A (R17) | counted in report |
| key | rejected or expired key, retry storm | 401 fixture | fail fast | clear error |
| budget | runaway cost | budget test with fake costs | per-run `--max-budget-usd` + total stop | yes |
| stats | setup "wins" by failing | stats unit tests | shared-pass comparison (R15) | verdict text |

Critical gaps (no test, no handling, and silent): **0**.

### Parallelization
- Lane A: `replay/sandbox.py` + `replay/proxy.py`. Independent.
- Lane B: `replay/select.py` (pytest + JS/TS pre-check). Independent.
- Lane C: `replay/stats.py`. Pure functions, independent.
- Then: `replay/run.py` + setups + CLI (depends on A and B), the fake-agent e2e, and finally the paid calibration.

### Implementation Tasks (Phase 2)
- [ ] **P2-T1 (P1, CC ~1h)** `replay/sandbox.py` + `replay/proxy.py`: hardened profile, per-phase allowlists, deny-all root, `TMPDIR` in the workdir. Escape tests on macOS.
- [ ] **P2-T2 (P1, CC ~1h)** `replay/select.py`: code+test commits, message ≥6 words, post-cutoff, fail→pass ×2 (pytest, vitest/jest).
- [ ] **P2-T3 (P1, CC ~1h)** `replay/run.py`: phases A/B/C, per-run clone + offline sync, commit-lockfile deps, 7 outcomes, JSONL results, fast 401 fail, telemetry off.
- [ ] **P2-T4 (P1, CC ~45m)** setups: baseline, terse, rtk (RTK install needs Kumar's approval). Tokens come from the Phase 1 parser, cross-checked with `total_cost_usd`.
- [ ] **P2-T5 (P1, CC ~45m)** `replay/stats.py`: shared-pass paired bootstrap at all energy corners, 97.5% per pair, sample-size estimate.
- [ ] **P2-T6 (P1, CC ~1h)** fake agent (solve/nothing/crash/timeout/escape) + end-to-end tests + `aimpg replay` CLI with dry-run cost estimate and total cap.
- [ ] **P2-T7 (P1, paid, Kumar confirms)** calibration: 2 commits × 3 setups × 2 repeats. Confirms usage rows and noise, then proposes the full run size and cost.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | Claude subagent (Codex not installed), auto in eng review | Independent 2nd opinion | 2 | unavailable (native fallback ran) | Phase 2: 8 findings, all folded in (R13–R18) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | CLEAR (PLAN) | Phase 2: 11 issues + 8 outside findings, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex / plan-review / unavailable (CLI not installed). A native Claude subagent fallback completed; it does not count as outside coverage.
- **VERDICT:** ENG CLEARED for Phase 2 v1 (macOS, Python + JS/TS, 3 setups). Build P2-T1…T6, then the paid calibration (P2-T7) with Kumar's confirmation.

NO UNRESOLVED DECISIONS

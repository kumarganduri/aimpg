# Public scoreboard: design brief (for review, 2026-10-05)

## What exists
aimpg (github.com/kumarganduri/aimpg, PyPI `aimpg` 0.4.1) is a free, open-source "miles per gallon for AI coding" tool. Its strategy (docs/STRATEGY.md) is to be **the neutral meter for AI-written code**: cost and carbon per working change.

`aimpg verify` (aimpg/replay/verify.py) reruns a user's own past commits in a sandbox (macOS Seatbelt or Linux bubblewrap, network allowlisted to the model API) with a baseline and a challenger setup. Examples: plain Claude Code vs RTK, a prompt, hooks, another model, Codex, or any agent CLI. The user's own tests judge the result. It writes a **record** (`*.record.json`):

- `aimpg_record: 1`, `kind: "verify"`, `created` (UTC timestamp), `claim` (free text), `public` (bool), `task_mode`
- `repo`: a salted hash (salt not stored) unless `--public`, then the git remote URL
- `versions`: aimpg, factors, prices, `claude --version`, `codex --version`
- `baseline` / `challenger`: setup name and spec (agent, model, appended prompt text, CLAUDE.md text, settings.json hash, command hash, or full text if `--public`)
- `runs[]`: per run: commit (salted hash, or sha if public), setup, repeat, outcome, passed, model, cost_usd, cost_known, wall_s, usages (per-request token counts: fresh, cache write, cache read, output), tokens_check, agent. No code, diffs, commit messages, notes or paths.
- `verdict`: SUPPORTED / NOT SUPPORTED / NOT PROVEN, with per-setup solve rate, $ per solved task, Wh range per solved task, and an energy test for same-model comparisons
- `aimpg verify --check record.json` recomputes the verdict from the runs (math only); `--rerun` replays a public record's commits on another machine.

## What the scoreboard should do (Phase 2, docs/STRATEGY.md)
People opt in to uploading records. A public page aggregates **$ and kWh per solved task** by model, setup and kind of task across many real repos. Examples: "Sonnet 5.5: $0.14 per solved task across 37 repos" and "RTK: no saving shown in 12 of 12 repos". It's the launch story's referee: token-saver and router claims are checked on real code. Funding stance: core free forever; there's no budget for servers beyond the free tiers.

## The three open decisions
1. **Hosting:** a static GitHub Pages site with records submitted by pull request into a repo (CI validates and rebuilds), vs a small upload service (API + DB), vs something else.
2. **Privacy defaults:** what's published per record vs only in aggregate; the minimum number of distinct repos before a number is shown; whether token counts or timestamps can fingerprint a private repo or a person; consent wording.
3. **Moderation / fake records:** `--check` proves only that the verdict follows from the runs, not that the runs happened. How do we resist doctored or fabricated records, vendor astroturfing (a token-saver vendor flooding wins), and duplicates?

Constraints: one maintainer; free; no prompts, code or diffs ever leave the user's machine; must stay credible as a neutral referee.

## Decisions (2026-10-05, after reviews by plan-eng-review + plan-devex-review, cso, and codex + cso)

1. **Hosting:** a separate public repo, `aimpg-scoreboard`. `aimpg submit` opens a PR through `gh`. CI (`pull_request` only, JSON parsing only, never executing record content) validates: schema, version, `verify --check`, integrity, privacy lint and dedupe. A merge rebuilds a static GitHub Pages site. No server.
2. **Privacy:** an upload view (record v2) separate from the local record.
   - **Kept:** model, setup, pass/fail, outcome; cost to $0.01; wall time to 10 s; token sums to 2 significant figures; Wh range per run; request-count bucket; week of run.
   - **Dropped:** per-request usages, prompt/CLAUDE.md/settings/command text, hosts, key env, commit ids (replaced by run-local indexes), free-text claim (a fixed list instead).
   - **repo_id:** HMAC(local secret, root commit sha), used for counting only.
   - **Minimum to show a number:** ≥5 distinct repos AND ≥3 distinct submitters AND no submitter >50% of the runs.
   - **Withdrawal:** by PR.
3. **Trust:**
   - **Tiers:** Reproduced (an independent `--rerun` agrees), Registered (plan published before running), Self-reported (listed, not in the headline), Disputed (shown separately).
   - **Headline:** median per repo, never means; ≤3 repos per GitHub account per challenger.
   - **Vendors:** self-claims are excluded unless reproduced.
   - **Cherry-picking:** registered-but-unreported plans are shown.
   - **Footer:** "Self-reported numbers are not audited; `--check` confirms the arithmetic, not that runs happened."

**Fixes before the scoreboard (0.4.2), found by the reviews:**
- (a) Private records still store prompt/CLAUDE.md text, hosts and key env.
- (b) `--rerun` executes a record's command/hooks without showing them.
- (c) Records drop superseded attempts and excluded commits.
- (d) `check` has no integrity checks (passed vs outcome, non-negative ints, full grid).

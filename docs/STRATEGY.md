# aimpg strategy

*Decided 2026-10-03 after five independent reviews: Codex (outside model), and Claude reviewers in the roles of an engineering VP, an everyday developer, a climate researcher and a market strategist.*

## North star

**The neutral meter for AI-written code.** One number, **cost and carbon per working change**, that developers see in the moment, teams set policy by, and the industry is measured against.

"Working change" (also called "durable change") is a commit or PR that was merged and is still alive 30 days later: not reverted, and no follow-up fix touching the same lines.

## Why this, why now

- AI coding spend has exploded, and leaders can't answer "what are we getting for it?" Token counts and "AI-assisted commits" don't show value per shipped change.
- Every router, model vendor and token-saver sells savings. None of them can referee their own claims.
- aimpg already has what others lack:
  - exact request-to-commit attribution;
  - replays on a team's *own* code judged by its *own* tests, which showed the cheapest model per token was the most expensive per task, and that a popular token-saver saved nothing.
- The carbon methods are landing now (GSF SCI for AI, Dec 2025; EU AI energy-label consultation, 2026).

## Three layers

### 1. Developers: in the moment
A monthly receipt is read once. Behavior changes at the moment of decision.
- **Live status line** in Claude Code, for example `this task $3.10 (your usual $0.90) · fresh session saves ~40%`
- **Post-commit line**, for example "this commit cost $2.40; starting fresh now saves ~$1.10 on the next task"
- **Before a task:** model advice backed by your own replay data.
- **Subscribers** see rate-limit headroom, not dollars they don't pay.
- **Never:** slowing the agent, extra prompts, or guilt-tripping climate copy.

### 2. Teams: policy, not just reports
- **Cost per durable change** and a **waste ratio** (AI spend on work that never shipped or was reverted), by team, repo and tool.
- **Replay-proven defaults** written as Claude Code settings, for example the model per repo and fresh-session rules. One platform-team change reaches thousands of developers.
- **Privacy is non-negotiable:**
  - metadata only, never prompts, code or diffs;
  - minimum group size of 5;
  - no per-person leaderboards;
  - an auditable open-source collector;
  - dollar totals reconciled to invoices.

### 3. Industry: the referee
- **`aimpg verify`:** reproducible checks of efficiency claims (token-savers, routers, model choices).
- **Public scoreboard:** opt-in, anonymized $ and kWh per *solved* task, by model, setup and kind of task, across many real repos.
- **Standards:**
  - build on git-ai's Git Notes record of AI-written lines, adding cost and carbon rather than competing;
  - a reference implementation of GSF SCI for AI for coding agents.
- **Hum** shows the aggregated, real numbers to the public.

## Phases

| Phase | Build | Prove |
|---|---|---|
| **1** (~2 weeks) | Live status line + post-commit hook; durable-change metric; Codex/Cursor log readers; anchor energy to disclosed figures | 2–3 team pilots get a weekly "cost per working change" report plus one recommendation. **Success: at least 2 teams change a model or workflow policy because of it.** |
| **2** | `aimpg verify` + public opt-in scoreboard; Linux sandbox so replays run in CI | Launch story: "RTK saved nothing" and "cheapest per token was most expensive per task"; contributors from outside |
| **3** | Team defaults as settings; GitHub Action; SCI-for-AI export; team rollups | Teams adopting defaults; first standards/reporting citations |

## Funding stance

The core stays **free and open source forever**: everything for individuals, the collector, `verify` and the public scoreboard. A hosted or self-hosted **team layer** (rollups, invoice reconciliation, policy recommendations, reporting exports) may become paid later to fund the work. Don't design it out; decide later.

## Known weaknesses to fix

- Claude Code only (Phase 1 adds Codex and Cursor).
- Energy ranges are about 8x wide (anchor them to disclosed figures such as Google's median prompt energy).
- Small samples (n=10 on one repo); the scoreboard fixes this over time.
- ~~Replays are macOS-only~~ Linux (bubblewrap) added 2026-10-05, tested in CI on Ubuntu 24.04.
- Rebound: cheaper tasks can lead to more tasks. Report totals as well as per-change figures.

# aimpg

**Miles per gallon for AI coding.** See what your AI coding agent really costs in energy, money and CO₂, find out what would cut it, and test whether tools and models live up to their claims on *your* code.

```bash
uvx aimpg report
```

```
YOUR AI CODING
  Energy   14.46 kWh – 130.59 kWh
           ≈ boiling a litre of water in a kettle 140–1,300 times · driving an electric car 85–770 km · 6.6–59.8 kg CO₂
  Money    $1,284 API-equivalent (Anthropic's published prices)
  Output   353 commits made with AI help

  A typical kept commit:  36.6 Wh – 313.0 Wh ≈ 2–18 full phone charges · $2.05

WHAT WOULD HAVE SAVED THE MOST (measured on your logs; upper bounds that overlap)
  1. Start a fresh session after each commit: up to 45% less energy, $397
     45% of your AI energy went to re-reading conversation from before your last commit.
     → After committing, start a new session (or /clear) for the next task.
  2. Use a mid-size model for routine work: up to 16% less energy, $333
     94% of your AI energy ran on the largest models (Opus/Fable class).
     → Check a cheaper model is good enough on your own commits: `aimpg replay models`.

WORKING CHANGES (commits that shipped and lasted 30 days)
  Available on 2026-10-22: aimpg is keeping your history so it can tell.
```

*(Real output from the author's last 30 days.)*

## What you can do with it

### 0. See it live while you work
```bash
uv tool install aimpg && aimpg live install     # shows the change, asks y/N, keeps a backup
```
```
⚡ task $1.96 · usual $2.05 · ctx 160k (92% from before your last commit) · /clear ≈ -92% per request · 5h 63%
```
After the agent commits, one line for you only (never sent to the model): *"that commit cost $2.40 over 31 AI requests. 92% of the context is from earlier work: /clear before the next task saves ~92% per request."* Undo with `aimpg live uninstall`.

### Using Cursor?
Cursor keeps its token counts online. Download them at **cursor.com/dashboard → Usage → Export CSV**, then:
```bash
aimpg report --cursor-usage ~/Downloads/usage-events.csv --cursor-repo ~/code/myproject
```
Team export? Add `--cursor-user "Your Name"` to count only your rows.

### 1. See your AI footprint in terms you can picture
`aimpg report` reads the Claude Code (and Codex CLI) logs already on your machine and ties every AI request to the git commit it produced. Energy is shown as an honest range and translated into kettles, phone charges, EV kilometres and CO₂. Money is the API-equivalent cost at Anthropic's published prices (for subscribers, what the same work would cost on the API).

### 2. Get tips measured on your own habits, not generic advice
The receipt replays your own logs under "what if" rules and tells you what would have saved the most, in Wh and dollars. For example: how much energy went into re-reading old conversation in long sessions, or into running the biggest model for routine fixes.

### 3. Find the cheapest model that's good enough for your code
```bash
aimpg replay select ~/my-repo                      # free: finds commits your tests can judge
aimpg replay models ~/my-repo --cap 15 --task-mode tests
```
```
model         solved  $ / solved task  energy per solved task
sonnet         10/10            $0.12  0.5–6.3 Wh ≈ <1 full phone charges
opus           10/10            $0.33  2.0–21.9 Wh ≈ <1–1.3 full phone charges
haiku           8/10            $0.38  3.3–28.0 Wh ≈ <1–1.6 full phone charges

Use sonnet: it solved 100% of tasks (best: 100%) at 3.1x lower cost per solved task than haiku.
```
*(Real result on the author's Legwork repo, 10 commits per model, $7.56 total.)* **The cheapest model per token was the most expensive per task:** Haiku needed about 38 requests and 2.5 minutes per task, where Sonnet needed 7 requests and 25 seconds. Sonnet was cheapest on 9 of the 10 commits.

It re-does your real past commits with each model in a locked sandbox, and **your own tests decide** what counts as solved.

### 4. Verify any efficiency claim on your own code
Token savers, prompts, hooks, CLAUDE.md files, models, even other agents: one command reruns your real commits both ways in a locked sandbox, and your own tests judge them.
```bash
aimpg verify --challenger rtk --claim "rtk saves tokens"          # a token saver
aimpg verify --append-prompt "Be terse."                          # your own prompt (or --claude-md / --settings)
aimpg verify --challenger haiku                                   # a cheaper model
aimpg verify --challenger codex:gpt-6.1-sol                       # another agent (needs an OpenAI API key)
aimpg verify --challenger "cmd:aider --yes --message {task}" --hosts api.openai.com --key-env OPENAI_API_KEY
```
It shows the plan and the worst-case cost, and spends nothing until you type `y` (default: 10 commits × 2 repeats, $15 cap). The answer is always to one question: **does the challenger solve as much, for less?**
```
CLAIM: rtk saves tokens
VERDICT: NOT PROVEN
  · energy difference not proven

  setup                     solved      $ per solved   energy per solved task
  claude-code                36/40             $0.17   0.9–11.8 Wh
  claude-code+rtk            32/40             $0.21   1.1–14.2 Wh

  Energy, same tasks: +10% (interval -11%…+35%, 17 shared solved commits)
```
That's the real result on the author's Legwork repo: no saving shown, far from the advertised 60–90%. Your repo may differ, and now you can check.

Every run writes a **record** (`~/.aimpg/verify/*.record.json`) holding versions, both setups, each run's tokens, $ and outcome, and the verdict, but never code, diffs or commit messages. Repo and commits are hashed unless you add `--public`. Anyone can recheck the math and the record's consistency for free with `aimpg verify --check record.json` (it can't prove the runs happened), and rerun a public record on their own machine with `aimpg verify --rerun record.json --repo <clone>`, which first shows every command, hook and prompt the record would run and asks before running them. Private records keep only fingerprints (SHA-256) of your prompts, CLAUDE.md, settings and commands, never the text. Records also list every redone attempt and excluded commit.

**Share it:** `aimpg submit <record>` publishes a stripped-down copy on the [public scoreboard](https://kumarganduri.github.io/aimpg-scoreboard) (no code, prompts, commit ids or repo name). It shows the exact file first and asks, then opens a pull request. Results only count once enough repos and people back them, and only independent reruns make the headline.

Same model: the energy test decides (it must hold at every corner of the energy ranges). Different models or agents: cost per solved task decides, because model sizes are secret. A challenger that solves more than 10 points fewer tasks is never "supported". Codex runs are measured from its own logs and priced from OpenAI's published prices. Codex logs in with an OpenAI **API key** (a ChatGPT subscription login can't be used inside the sandbox); the key is written to the run's throwaway folder and deleted when the run ends. Other commands are judged on solve rate and time only.

### 5. Show the cost of each pull request
```bash
aimpg pr --base main            # markdown summary of this branch's AI energy and cost
aimpg pr --base main --post     # add it as a comment on the open PR (uses gh)
```
> ### ⚡ AI energy for this pull request
> **73.4 Wh – 564.0 Wh** ≈ 4–33 full phone charges · 34–258 g CO₂ · **$5.76** API-equivalent

### 6. Report for a team or a sustainability report
```bash
aimpg export --csv ai-energy.csv --no-subjects --no-authors
```
One row per AI-assisted commit: date, repo, energy range, CO₂ range and cost. Teams can combine everyone's files. The privacy switches drop commit messages and author emails.

## Privacy

`report`, `pr` and `export` run entirely on your machine with no network calls (except `--fetch` for `git fetch`, and `pr --post`, which talks to GitHub). Your code and prompts are never uploaded. Replays send only the agent's own work to the model API, from a sandbox that can't read your home folder.

## How it works

**Matching requests to commits:** when the agent runs `git commit`, the commit lands inside that tool call, so the match is exact. Within a session, requests since the previous commit belong to the next one. Work before a 2h+ break is shown separately as *lead-up*. Hand-made commits are matched only if you authored them and they touch files the session edited. On the author's history, a hand-labeled check matched 20/20 commits correctly (`evals/`).

**Working changes:** aimpg keeps a small history of what each commit cost (never prompts or code), because Claude Code deletes its own logs after 30 days. Once a commit is 30 days old, it is judged: still on the main branch and not reverted or largely rewritten counts as a *working change*. The receipt then shows **cost per working change** and a **waste ratio** (AI spend on work that didn't last).

**Energy:** a physical formula, not a price proxy. Prefill compute for new input, one KV-cache re-read per output token (so long contexts cost more), plus datacenter overhead. Model sizes aren't public, so every number is a range. Overheads follow Google's full-stack measurement of a median Gemini prompt (chips, host CPU and memory, idle capacity, cooling ≈ 1.7× the chips alone), and a chat-sized prompt on a mid-size model comes out at 0.017–0.34 Wh, bracketing Google's disclosed 0.24 Wh ([arXiv 2508.15734](https://arxiv.org/pdf/2508.15734)). Sources for every factor are in [`aimpg/factors.json`](aimpg/factors.json), [`aimpg/prices.json`](aimpg/prices.json) and [`aimpg/equivalences.json`](aimpg/equivalences.json).

**Replays:** each run starts from the code just before your commit, in a fresh, history-free copy, inside a sandbox (Seatbelt on macOS, bubblewrap on Linux). The only network allowed is the model API, through an allowlisting proxy. Your own tests judge the result, and edited tests are always restored before judging. Every token count is cross-checked against Claude Code's own totals. Details are in [docs/designs/aimpg-design.md](docs/designs/aimpg-design.md).

## Limits (honest list)

- Claude Code and Codex CLI logs are read automatically. Cursor keeps token usage on its servers, so it needs its usage export (below), and that export doesn't say which folder the work was in: give `--cursor-repo` and requests are matched to your next own commit there by time alone (shown as "by time only"). Replays and `verify` need macOS or Linux and an API key (about $0.10–0.30 per run). On Linux install bubblewrap (`sudo apt install bubblewrap`); Ubuntu 24.04+ also needs a one-time AppArmor rule for bwrap, which aimpg prints if it's missing.
- Energy is an estimate with a wide range; dollars are close (within about 7% of Claude Code's own session totals on the author's logs).
- Tips are upper bounds and overlap; they can't be added together.
- Replays from commit messages alone are hard (12% solved on the author's repo); `--task-mode tests` shows the agent the tests, which makes tasks easier than real work but keeps comparisons fair.

## License

MIT

# aimpg

**Miles per gallon for AI coding.** See what your AI coding agent really costs in energy, money and CO₂, find out what would cut it, and test whether tools and models live up to their claims on *your* code.

```bash
uvx aimpg report
```

```
YOUR AI CODING
  Energy   11.34 kWh – 92.22 kWh
           ≈ boiling a litre of water in a kettle 110–920 times · driving an electric car 67–540 km · 5.2–42.2 kg CO₂
  Money    $1,210 API-equivalent (Anthropic's published prices)
  Output   328 commits made with AI help

  A typical kept commit:  37.4 Wh – 283.5 Wh ≈ 2–17 full phone charges · $3.62

WHAT WOULD HAVE SAVED THE MOST (measured on your logs; upper bounds that overlap)
  1. Start a fresh session after each commit: up to 44% less energy, $367
     44% of your AI energy went to re-reading conversation from before your last commit.
     → After committing, start a new session (or /clear) for the next task.
  2. Use a mid-size model for routine work: up to 16% less energy, $316
     94% of your AI energy ran on the largest models (Opus/Fable class).
     → Check a cheaper model is good enough on your own commits: `aimpg replay models`.
```

*(Real output from the author's last 30 days.)*

## What you can do with it

### 1. See your AI footprint in terms you can picture
`aimpg report` reads the Claude Code logs already on your machine and ties every AI request to the git commit it produced. Energy is shown as an honest range and translated into kettles, phone charges, EV kilometres and CO₂. Money is the API-equivalent cost at Anthropic's published prices (for subscribers, what the same work would cost on the API).

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

### 4. Check whether a "token saver" really saves anything
```bash
aimpg replay run ~/my-repo --setups claude-code,claude-code+rtk --commits 20 --cap 25 --task-mode tests
```
On the author's Legwork repo (20 commits, 80 runs), RTK **did not** save energy: about 10% more on average (range −9% to +36%), and it solved 80% of tasks vs 90% without it. That's far from the advertised 60–90%. Your repo may differ, and now you can check.

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

**Energy:** a physical formula, not a price proxy. Prefill compute for new input, one KV-cache re-read per output token (so long contexts cost more), plus datacenter overhead. Model sizes aren't public, so every number is a range. Sources for every factor are in [`aimpg/factors.json`](aimpg/factors.json), [`aimpg/prices.json`](aimpg/prices.json) and [`aimpg/equivalences.json`](aimpg/equivalences.json).

**Replays:** each run starts from the code just before your commit, in a fresh, history-free copy, inside a macOS sandbox. The only network allowed is the model API, through an allowlisting proxy. Your own tests judge the result, and edited tests are always restored before judging. Every token count is cross-checked against Claude Code's own totals. Details are in [docs/designs/aimpg-design.md](docs/designs/aimpg-design.md).

## Limits (honest list)

- Claude Code logs only, for now. Replays need macOS and an Anthropic API key (about $0.10–0.30 per run).
- Energy is an estimate with a wide range; dollars are close (within about 7% of Claude Code's own session totals on the author's logs).
- Tips are upper bounds and overlap; they can't be added together.
- Replays from commit messages alone are hard (12% solved on the author's repo); `--task-mode tests` shows the agent the tests, which makes tasks easier than real work but keeps comparisons fair.

## License

MIT

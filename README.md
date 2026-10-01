# aimpg

**Miles per gallon for AI coding.** Find out how much energy your AI coding agent used, and which of your commits it went into.

```bash
uvx aimpg report
```

Example output:

```
AI energy in window .......................... 6.99 kWh – 57.39 kWh  (4,744 requests)
  matched to commits ......................... 5.87 kWh – 47.96 kWh  (223 commits: 223 exact, 0 fuzzy, 0 grace)
  exact-match share of in-repo energy: 84%, any match: 84%
  no commit from this session yet ............ 1.11 kWh – 9.38 kWh  (609 requests)

Median energy per kept commit: 28.4 Wh – 216.8 Wh

Most energy-hungry commits:
       213.2 Wh – 1.71 kWh  my-app 2ef4297 fix: date parsing for day-first locales
```

## What it does

`aimpg` reads the Claude Code session logs already on your machine (`~/.claude/projects`). It works out which AI requests produced which of your git commits, and prints an energy receipt:

- **Energy per kept commit**, as a low–high range in Wh
- **Discarded work**: energy that went into commits that never reached your main branch
- **The most and least energy-hungry commits**
- **Unmatched energy**: work that produced no commit (yet), shown openly rather than hidden

## Privacy

Everything runs locally. `aimpg report` makes no network calls (unless you pass `--fetch`, which runs `git fetch`). Nothing is uploaded, and your code and prompts never leave your machine.

## How it matches requests to commits

1. **Exact:** when the agent runs `git commit`, the commit lands while that tool call is running. We match commits to those call windows, including slow commits where pre-commit hooks run for minutes.
2. **Time segments:** within a session, the requests made since the previous commit belong to the next one, in whatever repo it lands.
3. **Fuzzy (fallback):** commits you make by hand are matched only if you authored them (your `user.email`), within 2 hours of the session, and only if they touch files the session edited. Teammates' commits are never claimed.

On the author's own history, a hand-labeled check of 20 commits matched 20/20 to the right session (`evals/`).

## How energy is estimated

Model sizes for Claude aren't public, so every number is a **range**, never a single figure. The formula is physical, not price-based:

- **Prefill:** compute energy for every fresh or cache-written input token.
- **Decode:** each output token re-reads the model weights and the whole KV cache, so long contexts make every output token more expensive.
- **Cache reads:** free when the cache is still in GPU memory, a reload when it isn't.
- **Overhead:** server and datacenter overhead (PUE) on top.

Per-operation energy comes from [From Tokens to Watt-hours](https://arxiv.org/html/2607.26571v1). Server overhead and PUE come from [EcoLogits](https://ecologits.ai/latest/methodology/llm_inference/). Model-size classes are labeled assumptions. Every coefficient and its source is in [`aimpg/factors.json`](aimpg/factors.json).

## Options

```
aimpg report [--days 30] [--logs ~/.claude/projects] [--fetch]
```

## Limits (honest list)

- Claude Code logs only, for now.
- A long session's requests all go to its next commit, so a commit at the end of days of planning can look expensive.
- Commits less than 7 days old show as `pending` until we can tell whether they were kept.

## Roadmap

Next is `aimpg replay`: rerun your own past commits through different setups (for example Claude Code with and without a token saver) to prove which one saves energy without breaking your tests. See [docs/designs/aimpg-design.md](docs/designs/aimpg-design.md).

## License

MIT

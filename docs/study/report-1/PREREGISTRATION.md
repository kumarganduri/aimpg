# AI Coding MPG Report No. 1: pre-registration (FROZEN 2026-10-18)

Written and committed **before any paid run** (git history shows the date). Nothing below changes after the first study run. Deviations, if any, are reported as deviations.

## Question
On real commits from popular open-source repos, judged by each repo's own tests, which agent setups solve as much for less?

## Setups (each run starts from a fresh, empty config in aimpg's sandbox)
- **Baseline:** plain Claude Code on Claude Sonnet 5.5 (`plain`).
- **Challengers:**
  - Claude Code on Claude Haiku 5.5 (`haiku`);
  - Claude Code on Claude Opus 5.5 (`opus`);
  - OpenAI Codex CLI on gpt-6.1-sol (`codex:gpt-6.1-sol`);
  - Claude Code on Sonnet 5.5 with RTK (`rtk`), installed with `rtk init --global --auto-patch --no-trust-filters`, latest RTK release at run time.
- **RTK setup review:** RTK's maintainers were asked to review this setup on 2026-10-11 (https://github.com/rtk-ai/rtk/discussions/4511). Any change they ask for before the main run is adopted and reported. If they haven't replied by 2026-10-18, the setup above is used.
- **Versions recorded per run:** the versions of every tool and model.

## Repos and commits (chosen by rule, not by hand)
- **Repo rule:**
  - Python with `uv.lock` and pytest, or JS/TS with `package-lock.json` and vitest/jest;
  - at least 1,000 stars; a permissive license; tests that run offline;
  - no vendor of a tested setup.
- **Repos:** the 8 in `plan.json`.
- **Replacement:** a repo with fewer than 5 qualifying commits after the free pre-check is replaced by the next reserve, in the listed order.
- **Commit rule:**
  - authored on or after **2026-07-01**, after every tested model's training cutoff (Claude models: June 2026; gpt-6.1-sol: April 2026);
  - single parent;
  - changes both tests and code;
  - message of at least 6 words;
  - its tests **fail on the parent and pass on the commit, twice each**.
- **Selection:** the **first 7** qualifying commits per repo, oldest first, by any author. Each repo's full candidate list and its SHA-256 are published.
- **Task:** the commit message, plus the names the hidden tests import (`hint` mode). The commit's tests stay hidden and decide the outcome. Agents' edits to tests are undone before judging.
- **Repeats:** 3 per commit and setup.

## Primary comparisons (5, Holm-corrected at α = 0.05)
1. Haiku 5.5 vs Sonnet 5.5: cost per solved task.
2. Opus 5.5 vs Sonnet 5.5: cost per solved task.
3. Codex (gpt-6.1-sol) vs Claude Code (Sonnet 5.5): cost per solved task.
4. RTK vs plain, both on Sonnet 5.5: **tokens per attempted task**, the unit RTK advertises.
5. Fresh session vs continued session for the next task: cost per attempted task.
   - Pairs: 2 per repo, the 1st and 2nd and then the 3rd and 4th selected commits.
   - Task 1 is run once.
   - Task 2 is run in the same session after the code is reset to its starting point, and in a new session told only task 1's subject.

## Metrics reported for every comparison
- **Solve rate,** with 95% Wilson intervals, overall and per repo.
- **Cost per attempted task:** every run counts, failures included.
- **Cost per solved task,** and its challenger/baseline ratio, with a 95% interval from a bootstrap over repos and then commits.
- **Tokens per attempted task,** and its ratio.
- **The number of repos where the challenger came out cheaper.**
- **Dollars:** list prices (Anthropic and OpenAI pricing pages), applied to the tokens each run used.
- **Energy:** a secondary estimate with ranges, never a headline.

## Headline rule
A finding is stated as a result only if both:
- its Holm-adjusted p is below 0.05, **and**
- its direction holds in at least 5 of 6 or more repos.

Everything else is reported as "not shown", with its interval.

## Exclusions and stopping
- **Harness errors:** a run that fails for aimpg's own reasons is retried once. If it fails again, that commit is excluded for **every** setup and listed.
- **Account errors** (credit or key problems) are redone and never counted against a setup.
- **RTK runs** where RTK never fired (by RTK's own count) are included in the results, and their number is reported.
- **Budget:** the total cap is **$250**. If it's reached, everything finished is reported and marked incomplete. No result is rerun because of its outcome.

## Pilot
1 repo × 2 commits × 1 repeat, all setups, to check the harness and measure cost and variance. Pilot runs are **not** part of the results and are published separately.

## Disclosure
- aimpg is built by one developer using Claude Code (Anthropic).
- The author pays for all runs, has no affiliation with any vendor tested, and receives nothing from them.
- Every raw record, the selection lists and the analysis code are published, so anyone can rerun any commit with `aimpg verify --rerun`.

## Changes before the freeze (no paid run has happened yet)
- **2026-10-11, harness:**
  - **Dependency groups:** installs use only dependency groups named like dev/test/testing, because installing every group pulled in docs tooling.
  - **Git dependencies:** the install phase may also reach github.com and codeload.github.com, for dependencies a lockfile pins to a commit. The agent phase is unchanged.
  - **macOS cache file:** the sandbox allows the xcrun cache file macOS's git and clang need.
  - **Early give-up:** a repo whose first 5 candidates all fail to install is given up early.
  - **Localhost:** sandboxed code may use localhost networking, because test plugins and tests start local servers (litestar's pytest-rerunfailures binds a socket; axios runs local HTTP servers). Without this, agents couldn't run those tests either. The internet stays blocked.
  - **Git-derived versions:** build tools that read the version from git history get a placeholder (`SETUPTOOLS_SCM_PRETEND_VERSION` and `UV_DYNAMIC_VERSIONING_BYPASS`, both `0.0.0`), because the workspace is a history-free export. pyinfra needs this; it's the same for every setup.
  - **Coverage thresholds:** judging runs only the commit's test files with coverage thresholds off (lint-staged requires 100% coverage, which any subset run fails).
  - **Folder imports:** placeholder stubs handle imports from a folder's index file (recharts).
  - **Selection restart:** the commit selection was restarted from scratch after these changes.
- **2026-10-11, repos:** pydantic now builds its Rust core from source inside the repo, so its commits can't be installed offline. By the replacement rule it is replaced by the first reserve, litestar.

- **2026-10-13, after the pilot ($1.81, not part of the results):**
  - **Pricing from tokens:** every run is priced from its own tokens at the published price tables. Claude Code reported Haiku 5.5 runs at about 27× the published prices ($0.68 vs $0.025, all prompts under the 100k tier) while matching them exactly for Sonnet. Claude Code's own figure is kept in each record.
  - **Spending cap:** Claude Code's per-run spending cap is set to $50, so it can't stop Haiku runs early at its inflated prices. The study's own cap, counted at the published prices, still applies.
  - **Resumed sessions:** a resumed session's token check subtracts the earlier step, because Claude Code's totals include it.
  - **Headline rule:** the rule as coded allowed a headline from fewer than 6 repos. It now requires at least 6 repos with the direction holding in 5, as written above.

## Selection result (free pre-check finished 2026-10-13)

In the study: litestar (replacing pydantic), starlette, click, pyinfra, axios, lint-staged, recharts and homebridge. The reserves strawberry and uvicorn are not used. For each repo, `selection/<repo>.manifest.json` lists every candidate checked, in order, and which qualified. The study uses each repo's **first 7** qualifying commits:

| repo | candidates | qualifying | candidate-list SHA-256 | first 7 (used) |
|---|---|---|---|---|
| axios | 34 checked | 14 | `5d57cc9ac8c8` | 02c2c4f8, 3d253b4f, a85da291, e435384f, 3ebc7624, ff60b432, a93b1c90 |
| click | 17 checked | 14 | `e2f906138961` | 93ba0075, 9f9b149e, 07c909f2, 3495fba1, 7a0a3447, 051725fa, fbdd4340 |
| homebridge | 14 checked | 14 | `088cf27e1aa7` | c58eff4c, a7a1d487, dc0ef09d, 47382933, d934a479, adc95e2e, 8f00f8e6 |
| lint-staged | 26 checked | 14 | `69e2091a4edf` | 86600d3b, 113cc549, bd4dcbf0, d43432b0, 9b98b7f0, 26112a19, 37e8e1ad |
| litestar | 16 checked | 14 | `b724dae99415` | bd70de14, 4a433223, 6d07038f, 2453f456, 143fae39, fe47b846, 19a0ae48 |
| pydantic | 85 checked | 0 | `bac35ca3334b` |  |
| pyinfra | 16 checked | 14 | `6cbe70ca1d88` | fb6b4731, 556c0beb, 676799ba, 36a02ddc, 92650d3e, 61052231, 2b1d16c6 |
| recharts | 20 checked | 14 | `4d5e2e5d3175` | 6972c412, 52a2a896, 2fa048ec, 82d2bec9, f2786e83, 553e77b3, f2c3ecb9 |
| starlette | 14 checked | 14 | `8429f3a58da1` | 99b7cc62, f1dc9b63, b07b8c61, d438d397, 78ae82ca, 9c500db1, 38f8999a |

## Frozen 2026-10-18, before the main run
- **RTK:** no reply by the stated deadline ([discussion #4511](https://github.com/rtk-ai/rtk/discussions/4511), 0 comments on 2026-10-18). RTK runs with the setup stated above. A later reply will be published next to the results.
- **Versions at freeze:**
  - aimpg commit `66d9702`;
  - 2.1.287 (Claude Code);
  - codex-cli 0.159.2;
  - rtk 0.51.0.
- **Nothing above changes from here.** Any deviation during the run (for example a harness error that needs a fix) is reported as a deviation, with the runs before and after it marked.

## Deviations during the main run
- **2026-10-18, Anthropic credit ran out** after 23 runs. The run stopped itself (account error) and resumed after a top-up. 3 interrupted runs were redone, as pre-registered.
- **2026-10-18, OpenAI quota ran out during pyinfra.** Codex records this error only in its session log, so aimpg misread 5 Codex runs as harness errors. Under the "harness error twice" rule, that wrongly excluded 2 pyinfra commits (676799ba, 36a02ddc) for every setup.
  - **Fix:** aimpg now reads Codex's session log, and a quota error counts as an account error that stops the run.
  - **Repair:** the 5 quota-caused Codex records and the 2 exclusions were removed (the original file is kept as `pyinfra.jsonl.before-quota-repair`). Those runs are redone after the top-up.
  - **Results:** no completed result of any setup was changed.

## Found after the analysis (2026-10-25, pre-launch review)
Added after the results were known. Nothing above has been edited.
- **Commit dates: 10 of the 56 commits were authored before 2026-07-01.** The commit rule above says "authored on or after 2026-07-01", but aimpg's selection compared the cutoff with the *committer* date. Those 10 commits were committed (merged or rebased) after the cutoff but written earlier:
  - homebridge: all 7 (c58eff4c on 2026-06-04; a7a1d487, dc0ef09d, 47382933, d934a479, adc95e2e, 8f00f8e6 on 2026-05-08; all committed 2026-07-12);
  - click: 93ba0075 (2026-06-30), 9f9b149e (2026-05-29), fbdd4340 (2026-05-23).

  Code written before a model's training cutoff may have been public (for example in an open pull request) while the model was trained. **Sensitivity check:** without these 10 commits (7 repos, 46 commits), every result keeps its direction and its finding status:
  - Haiku 5.5: −82% per solved task (−86% to −79%), 7 of 7 repos.
  - Opus 5.5: +176% (+93% to +249%), 0 of 7 repos.
  - Codex: +17% (−24% to +51%), not shown.
  - RTK: −3% tokens (−10% to +4%), not shown.
  - Fresh session: +11% per attempt (+2% to +28%), 0 of 7 repos.

  The results above remain the pre-registered analysis; the report page shows both. aimpg's selection now checks the author date as well.
- **Pilot:** the pilot records (litestar, 13 runs) are now published with the data. Priced from tokens at the final price tables they add up to $1.79; the $1.81 above was computed on 2026-10-13.
- **Published file name:** the file before the quota repair is published as `pyinfra.jsonl.before-quota-repair`, as named above, so that tools reading `*.jsonl` don't include it.
- **Reruns:** the study's runs are not `aimpg verify` records, so `aimpg verify --rerun` can't take them directly. To recompute every number from the published data: `aimpg study report plan.json --runs data/`. To rerun a comparison, use the public Haiku-vs-Sonnet records on the scoreboard (click, starlette, axios), or rerun the whole study with `aimpg study run plan.json`.

## Found after publication (2026-11-14, results by task difficulty)
Added after the report was published. Nothing above has been edited. This analysis was **not pre-registered**, and its groups were chosen after seeing the data.
- **Why:** a reader (u/bisonbear2 on Reddit) pointed out that cost per solved task can flatter a cheap model that only solves the tasks every model solves.
- **How:** each of the 56 tasks is graded by its passing runs across four setups (Haiku 5.5, Sonnet 5.5, Opus 5.5 and Codex, 3 runs each, so 0 to 12). RTK is left out because it runs Sonnet again. Groups: easy (10 to 12), in between (3 to 9), hard (0 to 2). The groups are made from the same runs they describe.
- **Passing runs by group:**

  | Tasks | How many | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 | Codex |
  |---|---|---|---|---|---|
  | Easy | 27 | 76 of 81 | 80 of 81 | 81 of 81 | 79 of 81 |
  | In between | 7 | 7 of 21 | 5 of 21 | 14 of 21 | 13 of 21 |
  | Hard | 22 | 0 of 66 | 0 of 66 | 8 of 66 | 4 of 66 |

- **Haiku against Sonnet stands.** Sonnet solved no task that Haiku never solved; Haiku solved 1 that Sonnet never did. Haiku's failed runs were not short: 13 requests and 120 seconds on average, against 8 and 60 for Sonnet's.
- **The Opus result needs this caveat.** "2.6× the cost per solved task" averages over tasks that Haiku and Sonnet never solved in any run. Opus solved 8 of 66 runs on those.
- **The task set separates the models poorly:** 27 tasks are easy for every setup and 22 too hard for most. The counts on hard tasks are small; they are a pattern to test, not a measured rate.
- **"Hard" partly means thinly described (added later on 2026-11-14).** The task an agent gets is the commit message. The hard tasks' messages have a median of 7.5 words; the easy ones 93. Passing runs by description length:

  | Description | Tasks | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 | Codex |
  |---|---|---|---|---|---|
  | 50 words or more | 19 | 47 of 57 | 51 of 57 | 53 of 57 | 54 of 57 |
  | 15 to 49 words | 5 | 3 of 15 | 1 of 15 | 3 of 15 | 2 of 15 |
  | under 15 words | 32 | 33 of 96 | 33 of 96 | 47 of 96 | 40 of 96 |

  So the hard tasks partly test working out what is wanted from very little, and Opus's lead comes mostly from there. On tasks whose original commit changed more than 30 lines of code, every setup solved few runs (Haiku 15 of 60, Sonnet 14, Opus 17, Codex 22). Each task's description length and size are published as `tasks.json` with the data.
- **No pre-registered result changes.** The report page shows this as a section of its own, computed from the published records by `difficulty()` in `make_page.py`.

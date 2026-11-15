# AI Coding MPG Report No. 2: pre-registration (DRAFT, not frozen)

Draft of 2026-11-15 for review. Nothing here is fixed until it is committed as frozen, before any paid run of the main study. What is still undecided is listed at the end.

## Question
Can a strong model that only plans, with a cheap model doing the work, solve as much as the strong model alone for less? And does it solve what the cheap model alone can't?

## What Report No. 1 showed about its own tasks (the reason for this design)
Looked at after the results were known, so these are patterns to test, not findings.
- **The description mattered most.** The task given to the agent is the commit message. Tasks with a message of 50 words or more were solved in 82% to 95% of runs by every setup. Tasks with under 15 words were solved in 34% of runs by Haiku 5.5 and Sonnet 5.5, and in 49% by Opus 5.5.
- **Opus's lead came from thinly described small tasks.** On tasks with under 15 words and 30 or fewer changed lines of code, Opus solved 36 of 54 runs, Haiku 24 and Sonnet 25.
- **Large changes were hard for every setup.** On tasks whose original commit changed more than 30 lines of code, Haiku solved 15 of 60 runs, Sonnet 14, Opus 17 and Codex 22.
- **The number of files changed told little.** 35 of the 56 tasks already changed two or more files.

So an Opus planner is most likely to help where the description is thin: it has to work out what is wanted and write a brief the worker can follow.

## Setups (each run starts from a fresh, empty config in aimpg's sandbox)
- **Sonnet alone:** plain Claude Code on Claude Sonnet 5.5 (`plain`).
- **Haiku alone:** Claude Code on Claude Haiku 5.5 (`haiku`).
- **Opus alone:** Claude Code on Claude Opus 5.5 (`opus`).
- **Planner with worker:** Claude Code on Opus 5.5 with an added planner prompt, and a `worker` sub-agent on Haiku 5.5 with its own prompt (`planner:opus+haiku`). The two prompts are the ones GitHub user pettykng runs daily, posted for this study on 2026-11-14 and used unchanged. They are published with the plan (`prompts/planner.md`, `prompts/worker.md`), under MIT, with the author's agreement. In short: the planner works out what the task means, writes acceptance tests first, briefs the worker, checks the result itself, and sends repairs back to the same worker.
- **Prompt review:** the prompts stay open for comments in aimpg discussion #2 until **2026-11-21**.
- **Versions recorded per run:** the versions of every tool and model.

## Repos and commits (chosen by rule, not by hand)
- **Repos:** the 8 repos of Report No. 1, with the same replacement rule and reserves.
- **Commit rule:** as in Report No. 1, with two changes:
  - the cutoff date (2026-07-01) is checked against the **author** date and the committer date;
  - commits used in Report No. 1 are left out, so no task's result is known in advance.
- **Selection:** the first 7 qualifying commits per repo that were not used in Report No. 1, oldest first, by any author. The 56 commits left out are listed in the plan. Each repo's full candidate list and its SHA-256 are published.
- **Task:** the commit message, plus the names the hidden tests import, plus the status question below. The commit's tests stay hidden and decide the outcome.
- **Status question:** every task ends by asking for a line `STATUS: DONE` or `STATUS: NOT DONE`. It is the same for every setup.
- **Repeats:** 3 per commit and setup. 8 repos x 7 commits x 3 repeats x 4 setups = 672 runs.
- **Selection result (2026-11-15, before any agent run):** all 8 repos have at least 7 qualifying commits, so none is replaced. Of the 56 tasks, 35 have a description under 15 words and 21 have 15 or more; 27 change more than 30 lines of code and 29 change 30 or fewer. Short descriptions per repo: starlette 7, click 6, pyinfra 7, axios 7, lint-staged 6, recharts 1, homebridge 1, litestar 0.

## Task groups (fixed here, before any run)
Each task is put in two groups, using only the original commit:
- **Description:** under 15 words, or 15 words or more (subject and body, without trailers).
- **Size:** 30 or fewer changed lines in non-test files (added plus removed), or more than 30.

Description length is the main grouping; size is secondary. A short description often leaves the task underspecified: the hidden tests check behaviour the message never asks for. Every result below is reported overall and for each group. Groups made from the results themselves (for example by how many runs passed) are reported too, labelled exploratory.

## Primary comparisons (3, Holm-corrected at alpha = 0.05)
1. Planner with worker vs Sonnet alone: cost per solved task.
2. Planner with worker vs Haiku alone: solve rate.
3. Planner with worker vs Opus alone: cost per solved task.

The cost of a planner run is every request priced by its own model, the orchestrator's included.

## Planner vs Opus on short descriptions (rule fixed before any result)
This comparison (cost per solved task, tasks under 15 words only) is where an Opus planner should help most. It can be a headline only if at least 6 repos have at least 3 short-description tasks each. The selection gives 5 such repos, so it is **reported without a headline**, with its interval and its per-repo directions.

## Also reported (not primary)
- Haiku alone and Opus alone vs Sonnet alone, as a repeat of Report No. 1 on new tasks.
- Each primary comparison within each task group.
- **How often "done" is true:** per setup, the share of runs that said `STATUS: DONE` while the hidden tests failed, and the share with no status line.
- **What the planner really did:** the share of runs in which the orchestrator handed work to the worker, the share in which it edited files itself, and the cost split between the two models. Runs are never dropped for not delegating.
- **Changed tests:** before and after every hand-off to a worker, every test file in the workspace is hashed. A run in which a worker changed or deleted a test file that existed when its step began is counted, per setup. "Solved" keeps one meaning for every setup (the hidden tests pass); the planner's result is also shown with those runs counted as failures.
- **Time:** seconds per run.
- **False "done", counted twice for the planner setup:** how often the worker's last report said completed while the hidden tests failed, and in how many of those the planner's final message still said done.
- Solve rate with Wilson intervals, cost per attempted task, tokens per attempted task, and the number of repos in the same direction, as in Report No. 1.

## Headline rule
As in Report No. 1: a finding is stated only if its Holm-adjusted p is below 0.05 and its direction holds in at least 5 of 6 or more repos. Everything else is reported as "not shown", with its interval. Results within a task group are never headlines.

## Exclusions and stopping
- As in Report No. 1: a harness error is retried once and then excludes the commit for every setup; account errors are redone.
- **Budget:** the total cap is set after the pilot, from the planner's measured cost, and written here before the freeze. If it's reached, everything finished is reported and marked incomplete.

## Pilot
1 repo x 2 commits x 1 repeat, all 4 setups (8 runs, about $2). It runs on starlette's 8th and 9th qualifying commits, which the main study does not use. It checks that the worker really runs on Haiku 5.5 (from the cost split by model), that the orchestrator delegates, and what a planner run costs. It also checks that the test-file hashing runs. The worker's model is read from the model named in each API response, not from anything the worker says. Pilot runs are not part of the results and are published separately. Any harness change the pilot leads to is listed here before the freeze.

## Cost estimate
From Report No. 1's real cost per attempt: Sonnet $0.10, Haiku $0.02, Opus $0.31. With 168 runs per setup that is about $17, $3 and $52. The planner setup is unknown until the pilot; between Sonnet's and 1.5 times Opus's cost per run it would be $17 to $80. Total: roughly $90 to $150.

## Not in this report
- **Repair by the same worker or a fresh one** (suggested in discussion #2). It needs a failure message from tests the agent may see, and that source isn't settled. It is planned for Report No. 3 unless it is settled before the freeze.
- **Codex and RTK.** This report is about one question on Claude Code. Report No. 1's results for them stand.
- **Quality beyond tests.** "Solved" still means the hidden tests pass.

## Disclosure
As in Report No. 1: aimpg is built by one developer using Claude Code; the author pays for all runs and has no affiliation with any vendor tested; every raw record, selection list and the analysis code are published.

## Decided on 2026-11-14
- **Minimum description length:** 6 words, as in Report No. 1. Results are reported separately for short and long descriptions.
- **The repair question:** Report No. 3.
- **Prompts:** pettykng's planner and worker prompts (posted 2026-11-14) are used. Comments stay open until 2026-11-21.
- **Budget cap:** set after the pilot.

## Harness (built and tested, no paid run made)
- The planner-with-worker setup, the status question and the "said done" field.
- Each task's description length and changed lines, recorded per run, with results by those groups.
- Solve rate as a primary measure, and comparisons between any two setups.
- A selection that leaves out Report No. 1's commits and keeps its own files.

Still to confirm in the pilot: that Claude Code runs the worker on Haiku 5.5 (each run's cost split by model will show it).

## Undecided until the pilot shows what a planner run costs
Each is one more setup of about 168 runs:
- a planner with no review step (what the brief is worth without the check); this one comes first if there is budget for only one;
- the repair test, same worker against a fresh one, using the planner's own acceptance tests as the failure message;
- an Opus planner with a Sonnet worker, only if Haiku fails on short descriptions even with a good brief.


# Pilot 2 (2026-11-15)

8 runs, $1.64, with the planner prompt as changed after pilot 1. Two spare commits that the main study does not use, each run once per setup:
- starlette 8feafc64: a larger change (38 changed lines) with a 6-word description;
- recharts a5d7737d: a small change (12 changed lines) with a 406-word description.

What it showed:
- The planner handed the implementation to the worker in both runs, and edited no code itself.
- The worker ran on claude-haiku-5-5 (the model named in the API's responses).
- The test-file check ran around both hand-offs. In the recharts run the worker updated one of the project's existing test files; it did not touch the planner's acceptance test.
- Every setup solved the recharts task and no setup solved the starlette task, whose hidden tests expect a `meter_provider` argument that the 6-word description doesn't mention.
- Cost per run: Sonnet $0.06 and $0.12, Haiku $0.02 and $0.02, Opus $0.19 and $0.42, planner $0.34 and $0.48. In the planner runs, Opus was 93% and 98% of the cost.
- Seconds per run: Sonnet 49 and 46, Haiku 107 and 59, Opus 78 and 96, planner 458 and 212.
- Status lines: on the unsolved starlette task the planner and Sonnet ended with `STATUS: NOT DONE`; Haiku and Opus said `DONE`.

One harness change came from this pilot: the test-file check now tells the planner's own acceptance tests apart from the project's existing tests. The planner records here carry that field, computed again from the same transcripts.

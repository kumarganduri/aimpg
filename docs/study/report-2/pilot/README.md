# Pilot 1 (2026-11-15)

8 runs, $1.51: Sonnet, Haiku, Opus and the planner with a Haiku worker, once each on two spare starlette commits (699dae42, c9176b57) that the main study does not use. Pilot runs are not part of the results.

What it showed:
- No setup solved either task. Both descriptions have 9 words, and the hidden tests expect exact header names and order the description doesn't state.
- The planner handed nothing to the worker in either run. It wrote its acceptance tests and then made the change itself, saying the fix was only a few lines. So the worker's model, the test-file check and the cost of a run with a worker are not confirmed yet.
- Cost per run: Sonnet $0.08, Haiku $0.02, Opus $0.28, planner $0.38.
- 7 of the 8 runs ended with `STATUS: DONE` while the hidden tests failed.

`starlette.jsonl` holds one record per run, in the same format as Report No. 1's data.

# TODOS

## Phase 2

The earlier TODOs (test leakage + memorization, statistics gate, network-limit spike) were resolved by the Phase 2 eng review (R3/R4, R7/R15/R16, spikes/egress + spikes/agent).

### Linux replay sandbox
- **What:** a bwrap version of the replay sandbox. bwrap's isolated network can't reach the host's localhost, so the allowlisting proxy is exposed as a Unix socket bound into the sandbox, with a tiny in-sandbox relay.
- **Why:** v1 replays are macOS-only (R1), so Linux users can't run them.
- **Pros:** lets outside contributors run replays; Linux CI could run the escape tests too.
- **Cons:** about 1 day of work. Ubuntu 24.04+ needs the AppArmor profile Legwork already documents (`BWRAP_APPARMOR_PROFILE` in Legwork's sandbox_runner.py).
- **Context:** Phase 2 eng review, 2026-10-02. The macOS design (Seatbelt + proxy) is in spikes/agent/RESULTS.md, and Legwork's `_bwrap_args` is the starting point.
- **Depends on:** the macOS replay producing its first verdict.

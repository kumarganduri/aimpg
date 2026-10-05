# TODOS

## Phase 2

The earlier TODOs (test leakage + memorization, statistics gate, network-limit spike) were resolved by the Phase 2 eng review (R3/R4, R7/R15/R16, spikes/egress + spikes/agent).

### ~~Linux replay sandbox~~ (done 2026-10-05)
bubblewrap backend in aimpg/replay/sandbox.py; the proxy is relayed into the sandbox through a Unix socket. CI's `linux-sandbox` job runs the escape and end-to-end tests on Ubuntu 24.04.
- **Still untested on Linux:** a real paid Claude Code or Codex run (CI has no API keys); only the stand-in agent has run there.

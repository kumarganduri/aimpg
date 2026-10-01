# Spike: real Claude Code + uv inside the hardened replay sandbox (2026-10-02)

Free: Claude Code ran with `--bare` (no keychain) and an obviously fake API key, so
Anthropic rejected every request and nothing could be billed.

**Profile:** `make_profile.py`. These are Legwork's hardened read rules: everything
outside $HOME is readable, and under $HOME only the workdir, the run's config dir, and
read-only tool folders (`~/.local/share/claude`, `~/.local/bin`,
`~/.local/share/uv/python`). Network goes only to the local allowlisting proxy.

| Check | Result |
|---|---|
| `claude --version` starts in the strict sandbox | yes (needs read access to its install folder) |
| reading `~/AwesomeAI`, `~/.ssh` | `Operation not permitted` |
| Claude Code honors `HTTPS_PROXY` | yes: proxy logged `ALLOW api.anthropic.com:443` |
| other traffic | telemetry to `http-intake.logs.us5.datadoghq.com` was **blocked** by the proxy |
| transcript written with `--bare` + `CLAUDE_CONFIG_DIR` | yes: `cfg/projects/<cwd>/<session>.jsonl` |
| usage rows in that transcript | not verified (every call was a 401); confirm in the paid calibration run |
| Phase A `uv sync` with proxy allowing only pypi.org + files.pythonhosted.org | yes, 2.7s for Legwork |
| per-run `cp -c` (APFS clone) of the prepared workdir | 0.27s, but **its venv still imports the original folder** (editable install path) |
| `uv sync --offline` inside the copy | 0.5s, no network, imports now point at the copy |

**Consequences for the plan:**
- Phase B needs read-only allowances for the agent's own install folders, and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` so nothing but model calls is attempted.
- Per run: APFS-clone the prepared workdir, then `uv sync --offline`. A shared
  read-only env doesn't work (outside-voice finding confirmed).
- The config dir must be writable by Claude Code, and the agent's own tools run in
  the same sandbox, so they could touch the transcript. Mitigation: cross-check
  transcript tokens against `total_cost_usd` from the harness-captured stdout, and
  treat any mismatch over 2% (or an empty transcript) as `harness_error`.
- A rejected key makes Claude Code retry for a long time: the harness timeout must
  cover that, and a 401 pattern in stderr should fail fast.

# Spike: replay sandbox that can reach only the model API (2026-10-01)

**Question:** can a replay agent run with internet access *only* to the model API,
so it can't search GitHub for the commit it's supposed to reproduce, without Docker?

**Setup (macOS):**
- `replay.sb`: Seatbelt profile. It denies all network except the local port 18123,
  and denies writes outside the work folder.
- `allowlist_proxy.py`: a ~40-line HTTPS CONNECT proxy on 127.0.0.1:18123 that
  only tunnels to `api.anthropic.com:443`. There is no TLS interception; it only
  checks the destination name.

**Results:**

| Inside the sandbox | Result |
|---|---|
| curl github.com directly | blocked (`000`) |
| curl github.com via proxy | refused by proxy (`BLOCK CONNECT github.com:443`) |
| curl api.anthropic.com via proxy | reached (`401`, no key sent) |
| curl a raw IP (1.1.1.1) | blocked |
| write outside work folder | `Operation not permitted` |
| write inside work folder | ok |

**Conclusion:** the D7 contract ("egress only to the model API") works on macOS
without Docker: Seatbelt plus a local allowlisting proxy, reusing Legwork's
filesystem confinement (reads under $HOME limited to the work folder).

**Still open (for the Phase 2 eng review):**
- Run the real `claude -p` inside the sandbox with `HTTPS_PROXY` and an API key,
  and confirm it honors the proxy and what config paths it needs. This costs a
  few cents of tokens and needs Kumar's API key decision.
- Linux: bwrap `--unshare-net` has no host loopback, so the proxy has to be
  exposed as a Unix socket bound into the sandbox (plus a tiny in-sandbox relay).
- This spike profile allows all file reads; the real one must use Legwork's
  hardened read rules.

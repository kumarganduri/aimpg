"""Agent setups compared in replays. All run the same model, so comparisons are fair.

Hermetic without `--bare` (RTK works through a hook, and `--bare` skips hooks;
every setup must be configured the same way): each run gets a fresh, empty
config folder as HOME and CLAUDE_CONFIG_DIR, so none of the user's hooks,
plugins, memory or CLAUDE.md files exist; MCP is off (`--strict-mcp-config`);
and the sandbox blocks the macOS keychain, so only the API key can bill.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TERSE_PROMPT = (
    "Be terse. Do the work with as few tool calls and as little output as possible. "
    "No recaps, no summaries of what you did, no restating the task."
)


class SetupUnavailable(RuntimeError):
    pass


@dataclass
class Setup:
    name: str
    extra_args: list[str] = field(default_factory=list)
    configure: Callable[[Path], None] | None = None  # writes into the run's fresh config dir
    requires: tuple[str, ...] = ("claude",)
    program: list[str] = field(default_factory=lambda: ["claude"])

    def check(self) -> None:
        missing = [b for b in self.requires if shutil.which(b) is None]
        if missing:
            raise SetupUnavailable(f"setup '{self.name}' needs {', '.join(missing)} installed")

    def argv(self, task: str, model: str, budget_usd: float, cfg: Path) -> list[str]:
        return [
            *(part.replace("{cfg}", str(cfg)) for part in self.program),
            "-p",
            task,
            "--output-format",
            "json",
            "--model",
            model,
            "--max-budget-usd",
            f"{budget_usd:.2f}",
            "--dangerously-skip-permissions",  # safe: the sandbox is the permission boundary
            "--strict-mcp-config",
            *self.extra_args,
        ]


def _rtk_configure(cfg: Path) -> None:
    """Install RTK's Claude Code hook into the run's throwaway config (never the user's)."""
    env = {"HOME": str(cfg), "CLAUDE_CONFIG_DIR": str(cfg), "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("rtk") or "").parent)}
    proc = subprocess.run(["rtk", "init", "--global"], env=env, capture_output=True, text=True, input="y\n")
    settings = [p for p in (cfg / "settings.json", cfg / ".claude" / "settings.json") if p.exists()]
    if proc.returncode != 0 or not settings:
        raise SetupUnavailable(f"rtk init did not create a hook config: {(proc.stderr or proc.stdout)[-300:]}")
    if settings[0] != cfg / "settings.json":
        shutil.copy(settings[0], cfg / "settings.json")
    if "hook" not in (cfg / "settings.json").read_text().lower():
        raise SetupUnavailable("rtk settings contain no hook")


BASELINE = Setup("claude-code")
TERSE = Setup("claude-code+terse", extra_args=["--append-system-prompt", TERSE_PROMPT])
RTK = Setup("claude-code+rtk", configure=_rtk_configure, requires=("claude", "rtk"))

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def fake(mode: str) -> Setup:
    """Free stand-in agent for tests: solve | nothing | crash | timeout | budget | escape.

    The sandbox can't read the project's venv or source under $HOME, so the
    script is copied into the run's config dir and run with uv's real Python.
    """

    def configure(cfg: Path) -> None:
        shutil.copy(FAKE_AGENT, cfg / "fake_agent.py")

    python = str(Path(sys.executable).resolve())
    return Setup(f"fake-{mode}", configure=configure, requires=(), program=[python, "{cfg}/fake_agent.py", mode])


SETUPS = {s.name: s for s in (BASELINE, TERSE, RTK)}


def parse_result(stdout: str) -> dict:
    """The last JSON object Claude Code printed (`--output-format json`)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    try:
        return json.loads(stdout)
    except ValueError:
        return {}

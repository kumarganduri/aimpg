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

from aimpg.replay.proxy import ANTHROPIC, OPENAI

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
    model: str | None = None  # overrides the batch's model (model picker)
    # Which agent CLI this is: "claude" | "codex" | "cmd" (any command; no token source)
    agent: str = "claude"
    hosts: frozenset[str] = ANTHROPIC  # what the proxy lets it reach while it works
    key_env: str = "ANTHROPIC_API_KEY"  # env var its API key is passed in ("" = none)
    template: list[str] = field(default_factory=list)  # "cmd": argv with {task} and {model}
    spec: dict = field(default_factory=dict)  # how verify records were made (no secrets)

    def check(self) -> None:
        missing = [b for b in self.requires if shutil.which(b) is None]
        if missing:
            raise SetupUnavailable(f"setup '{self.name}' needs {', '.join(missing)} installed")

    def argv(self, task: str, model: str, budget_usd: float, cfg: Path) -> list[str]:
        if self.agent == "codex":
            # No $ budget flag in Codex: the run timeout and OpenAI's own spend
            # limits bound it. Its own sandbox can't nest inside ours, and ours
            # is the boundary, so it runs with full access inside it.
            return [
                *(part.replace("{cfg}", str(cfg)) for part in self.program),
                "exec", "--json", "--skip-git-repo-check", "-s", "danger-full-access",
                *(["-m", self.model] if self.model else []),
                *self.extra_args,
                task,
            ]
        if self.agent == "cmd":
            return [part.replace("{task}", task).replace("{model}", self.model or model).replace("{cfg}", str(cfg)) for part in self.template]
        return [
            *(part.replace("{cfg}", str(cfg)) for part in self.program),
            "-p",
            task,
            "--output-format",
            "json",
            "--model",
            self.model or model,
            "--max-budget-usd",
            f"{budget_usd:.2f}",
            "--dangerously-skip-permissions",  # safe: the sandbox is the permission boundary
            "--strict-mcp-config",
            *self.extra_args,
        ]


def _rtk_configure(cfg: Path) -> None:
    """Install RTK's Claude Code hook into the run's throwaway config (never the user's)."""
    env = {"HOME": str(cfg), "CLAUDE_CONFIG_DIR": str(cfg), "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("rtk") or "").parent)}
    # Never interactive: from a real terminal rtk asks yes/no questions and
    # waits forever (found in calibration). No stdin, explicit answers, timeout.
    proc = subprocess.run(
        ["rtk", "init", "--global", "--auto-patch", "--no-trust-filters"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    settings = [p for p in (cfg / "settings.json", cfg / ".claude" / "settings.json") if p.exists()]
    if proc.returncode != 0 or not settings:
        raise SetupUnavailable(f"rtk init did not create a hook config: {(proc.stderr or proc.stdout)[-300:]}")
    if settings[0] != cfg / "settings.json":
        shutil.copy(settings[0], cfg / "settings.json")
    if "hook" not in (cfg / "settings.json").read_text().lower():
        raise SetupUnavailable("rtk settings contain no hook")


BASELINE = Setup("claude-code", spec={"agent": "claude"})
TERSE = Setup("claude-code+terse", extra_args=["--append-system-prompt", TERSE_PROMPT], spec={"agent": "claude", "append_prompt": TERSE_PROMPT})
RTK = Setup("claude-code+rtk", configure=_rtk_configure, requires=("claude", "rtk"), spec={"agent": "claude", "rtk": True})

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def fake(mode: str, agent: str = "claude") -> Setup:
    """Free stand-in agent for tests: solve | nothing | cheat | crash | timeout | budget | escape.

    The sandbox can't read the project's venv or source under $HOME, so the
    script is copied into the run's config dir and run with uv's real Python.
    """

    def configure(cfg: Path) -> None:
        shutil.copy(FAKE_AGENT, cfg / "fake_agent.py")

    python = str(Path(sys.executable).resolve())
    if agent == "cmd":
        return Setup(f"fake-cmd-{mode}", configure=configure, requires=(), agent="cmd", key_env="",
                     template=[python, "{cfg}/fake_agent.py", mode, "{task}"])
    return Setup(f"fake-{agent}-{mode}" if agent != "claude" else f"fake-{mode}", configure=configure, requires=(),
                 program=[python, "{cfg}/fake_agent.py", ("codex-" if agent == "codex" else "") + mode], agent=agent,
                 hosts=OPENAI if agent == "codex" else ANTHROPIC, key_env="OPENAI_API_KEY" if agent == "codex" else "ANTHROPIC_API_KEY")


SETUPS = {s.name: s for s in (BASELINE, TERSE, RTK)}

MODELS = {  # short names for the model picker → model ids Claude Code accepts
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}


def with_model(model: str) -> Setup:
    """Plain Claude Code on a specific model: the model picker's setups."""
    model_id = MODELS.get(model, model)
    return Setup(f"claude-code@{model}", model=model_id, spec={"agent": "claude", "model": model_id})


def codex(model: str | None = None) -> Setup:
    """OpenAI's Codex CLI (`codex exec`), billed to OPENAI_API_KEY. Tokens come from its rollout logs."""
    name = "codex" + (f"@{model}" if model else "")
    return Setup(name, requires=("codex",), program=["codex"], model=model, agent="codex",
                 hosts=OPENAI, key_env="OPENAI_API_KEY", spec={"agent": "codex", "model": model})


def command(template: str, hosts: list[str], key_env: str = "", name: str = "cmd") -> Setup:
    """Any agent CLI. `{task}` is replaced by the task text, `{model}` by the model.

    aimpg can't see its tokens, so only solve rate and time are measured.
    """
    import shlex

    argv = shlex.split(template)
    if not argv or not any("{task}" in a for a in argv):
        raise SetupUnavailable("the command must contain {task} where the task text goes")
    return Setup(name, requires=(argv[0],), agent="cmd", template=argv, hosts=frozenset(hosts), key_env=key_env,
                 spec={"agent": "cmd", "command_sha256": _sha(template), "command": template, "hosts": sorted(hosts), "key_env": key_env})


def custom(name: str = "custom", *, model: str | None = None, append_prompt: str | None = None,
           claude_md: str | None = None, settings: str | None = None) -> Setup:
    """Claude Code with your own change: an extra system prompt, a CLAUDE.md, hooks/settings (texts), a model."""
    md, settings_text = claude_md, settings
    if settings_text is not None:
        json.loads(settings_text)  # fail now, for free, not inside a paid run

    def configure(cfg: Path) -> None:
        for base in (cfg, cfg / ".claude"):  # CLAUDE_CONFIG_DIR and HOME both point here
            base.mkdir(parents=True, exist_ok=True)
            if md is not None:
                (base / "CLAUDE.md").write_text(md)
            if settings_text is not None:
                (base / "settings.json").write_text(settings_text)

    spec = {"agent": "claude", "model": MODELS.get(model, model) if model else None, "append_prompt": append_prompt,
            "claude_md": md, "settings_sha256": _sha(settings_text) if settings_text else None, "settings": settings_text}
    return Setup(name, extra_args=["--append-system-prompt", append_prompt] if append_prompt else [],
                 configure=configure if (md is not None or settings_text is not None) else None,
                 model=MODELS.get(model, model) if model else None, spec=spec)


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


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

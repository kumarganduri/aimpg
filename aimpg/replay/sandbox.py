"""macOS Seatbelt sandbox for replay runs.

Read rules adapted from Legwork's hardened profile
(github.com/kumarganduri/legwork, legwork/sandbox_runner.py `_generate_profile`,
MIT, same author): reads are allowed outside $HOME (system libraries) but not
under it; the workdir is re-allowed. Added for replays:

* network only to the local allowlisting proxy port (or none at all);
* a deny-all replay root, re-allowing only this run's own folders, so parallel
  runs can't read each other's hidden tests (verified: later, more specific
  rules win);
* read-only access to the agent's and toolchain's own install folders under
  $HOME (Claude Code, uv, node), found by resolving the binaries.

Linux (bwrap) is a TODO; other platforms raise SandboxUnavailable.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_MACH_SERVICES = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.system.notification_center",
    "com.apple.system.logger",
    "com.apple.logd",
    "com.apple.diagnosticd",
    "com.apple.SystemConfiguration.configd",
    "com.apple.SystemConfiguration.DNSConfiguration",
    "com.apple.dnssd.service",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
)
TOOLS = ("claude", "codex", "uv", "node", "npm", "npx", "git", "rtk")


class SandboxUnavailable(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def tool_dirs(tools: tuple[str, ...] = TOOLS) -> list[Path]:
    """Install folders of the agent/toolchain that live under $HOME (read-only grants).

    For a binary in some `bin/`, its package root (one level up) is granted so
    node/npm can read their libraries, but never $HOME or ~/.local themselves:
    uv sits directly in ~/.local/bin, and ~/.local holds other apps' data.
    """
    home = Path.home().resolve()
    too_broad = {home, home / ".local", home / ".local" / "share"}
    found: set[Path] = set()
    for name in tools:
        which = shutil.which(name)
        if not which:
            continue
        for p in (Path(which).absolute(), Path(which).resolve()):
            d = p.parent
            if d.name in ("bin", "versions") and d.parent not in too_broad:
                d = d.parent
            if str(d).startswith(str(home) + os.sep) and d not in too_broad:
                found.add(d)
    uv_python = home / ".local/share/uv/python"
    if uv_python.is_dir():
        found.add(uv_python.resolve())
    return sorted(found)


def tool_path(tools: tuple[str, ...] = TOOLS) -> str:
    """PATH made of the folders the tools are actually invoked from."""
    dirs: list[str] = []
    for name in tools:
        which = shutil.which(name)
        if which and str(Path(which).parent) not in dirs and str(Path(which).parent) not in ("/usr/bin", "/bin"):
            dirs.append(str(Path(which).parent))
    return ":".join(dirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])


@dataclass
class Profile:
    writable: list[Path]
    readable: list[Path] = field(default_factory=list)
    deny_roots: list[Path] = field(default_factory=list)
    proxy_port: int | None = None  # None: no network at all

    def render(self) -> str:
        home = str(Path.home().resolve())
        writable = [str(Path(p).resolve()) for p in self.writable]
        readable = [str(Path(p).resolve()) for p in self.readable]
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec)",
            "(allow sysctl-read)",
            "(allow mach-lookup " + " ".join(f'(global-name "{m}")' for m in _MACH_SERVICES) + ")",
            "(allow iokit-open)",
            "(allow signal (target same-sandbox))",
            f'(allow file-read* (require-not (subpath "{home}")))',
        ]
        for root in self.deny_roots:
            lines.append(f'(deny file-read* file-write* (subpath "{Path(root).resolve()}"))')
        for d in readable:
            lines.append(f'(allow file-read* (subpath "{d}"))')
        for d in writable:
            lines.append(f'(allow file-read* file-write* (subpath "{d}"))')
        for ancestor in sorted(_ancestors([*writable, *readable], [home, *map(str, self.deny_roots)])):
            lines.append(f'(allow file-read-metadata (literal "{ancestor}"))')
        lines.append('(allow file-write-data (literal "/dev/null") (literal "/dev/zero"))')
        if self.proxy_port is not None:
            lines += ["(allow system-socket)", f'(allow network-outbound (remote ip "localhost:{self.proxy_port}"))']
        return "\n".join(lines) + "\n"


def _ancestors(paths: list[str], roots: list[str]) -> set[str]:
    """Parent dirs between a denied root and an allowed path: metadata only, so realpath() works."""
    out = set()
    for p in paths:
        for root in roots:
            root = str(Path(root).resolve())
            if p.startswith(root + os.sep):
                cur = Path(p).parent
                while str(cur).startswith(root):
                    out.add(str(cur))
                    if str(cur) == root:
                        break
                    cur = cur.parent
    return out


@dataclass
class Result:
    returncode: int | None  # None when timed out
    stdout: str
    stderr: str
    timed_out: bool = False


def run(argv: list[str], *, profile: Profile, profile_path: Path, env: dict[str, str], cwd: Path, timeout: float) -> Result:
    """Run argv inside the sandbox. The whole process group is killed on timeout."""
    if not available():
        raise SandboxUnavailable("replays need macOS (sandbox-exec); Linux support is on the roadmap")
    profile_path.write_text(profile.render())
    proc = subprocess.Popen(
        ["sandbox-exec", "-f", str(profile_path), *argv],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return Result(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return Result(None, out, err, timed_out=True)

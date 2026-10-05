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

Linux: the same policy through bubblewrap (adapted from Legwork's
`_bwrap_args`): the system read-only; home folders, /tmp, /var/tmp and /run
replaced by empty private tmpfs (so other runs, the answer repo and local
sockets such as the SSH agent are gone); only this run's folders bound back
writable, the agent's install folders read-only. Every namespace is
unshared, network included. The allowlisting proxy can't be reached as a
host port from a private network namespace, so it also listens on a Unix
socket that is bound in, and a tiny relay inside the sandbox listens on
127.0.0.1:<the same port> and forwards to it. Ubuntu 24.04+ needs an
AppArmor profile that lets bwrap (only) create user namespaces; the error
message prints it.

Other platforms raise SandboxUnavailable.
"""

from __future__ import annotations

import functools
import json
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
# Where replay data lives: outside $HOME on both systems.
TMP = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
TOOLS = ("claude", "codex", "uv", "node", "npm", "npx", "git", "rtk")


class SandboxUnavailable(RuntimeError):
    pass


def available() -> bool:
    return unavailable_reason() is None


BWRAP_APPARMOR_PROFILE = """abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
"""


def unavailable_reason() -> str | None:
    """Why replays can't be sandboxed here, with the fix; None if they can."""
    if sys.platform == "darwin":
        return None if _launcher("sandbox-exec") else "sandbox-exec is missing (it ships with macOS)"
    if sys.platform.startswith("linux"):
        if not _launcher("bwrap"):
            return "replays on Linux need bubblewrap: sudo apt install bubblewrap (or dnf/pacman)"
        error = _bwrap_probe()
        if error is None:
            return None
        hint = ""
        try:
            if Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1":
                hint = ("\nUbuntu 24.04+ restricts user namespaces with AppArmor; allow them for bwrap only:\n"
                        "  sudo tee /etc/apparmor.d/bwrap <<'EOF'\n" + BWRAP_APPARMOR_PROFILE + "EOF\n"
                        "  sudo apparmor_parser -r /etc/apparmor.d/bwrap")
        except OSError:
            pass
        return f"bubblewrap can't create a sandbox here ({error}){hint}"
    return f"replays need macOS or Linux (not {sys.platform})"


@functools.lru_cache(maxsize=None)
def _launcher(name: str) -> str | None:
    """Absolute path of the sandbox launcher, looked up in system folders only,
    never in the child's PATH (which a sandboxed step could plant a fake in)."""
    found = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin")
    return os.path.realpath(found) if found else None


@functools.lru_cache(maxsize=1)
def _bwrap_probe() -> str | None:
    try:
        r = subprocess.run([_launcher("bwrap"), "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")


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
    proxy_socket: Path | None = None  # Linux: the proxy's Unix socket, relayed to 127.0.0.1:proxy_port inside

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
            # Codex syncs macOS managed preferences at startup and refuses to run
            # without them; cfprefsd shares them read-only through this memory.
            '(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs"))',
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


def bwrap_args(profile: Profile, cwd: Path) -> list[str]:
    """Linux equivalent of Profile.render (see the module docstring)."""
    home = Path.home().resolve()
    hidden = sorted({p for p in (Path("/home"), Path("/root"), home) if p.is_dir()}, key=lambda p: len(p.parts))
    args = [_launcher("bwrap") or "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run"]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        args += ["--tmpfs", "/var/run"]
    for d in hidden:
        args += ["--tmpfs", str(d)]
    for root in profile.deny_roots:  # e.g. the answer repo, wherever it lives
        r = Path(root).resolve()
        if r.is_dir() and not any(r == h or h in r.parents for h in (*hidden, Path("/tmp"), Path("/var/tmp"))):
            args += ["--tmpfs", str(r)]
    for d in profile.readable:
        d = Path(d).resolve()
        if d.exists():
            args += ["--ro-bind", str(d), str(d)]
    if profile.proxy_socket is not None:
        python = Path(sys.base_prefix).resolve()  # the relay runs on this Python's stdlib
        args += ["--ro-bind", str(python), str(python)]
        sock_dir = Path(profile.proxy_socket).parent.resolve()
        args += ["--bind", str(sock_dir), str(sock_dir)]
    for d in profile.writable:
        d = Path(d).resolve()
        args += ["--bind", str(d), str(d)]
    for d in reversed(hidden):  # stand-in homes read-only: writes fail as on macOS
        args += ["--remount-ro", str(d)]
    args += ["--chdir", str(Path(cwd).resolve())]
    return args


# Runs inside the Linux sandbox as `python -c`: 127.0.0.1:PORT -> the proxy's Unix socket.
_RELAY = """
import socket, subprocess, sys, threading
sock_path, port, argv = sys.argv[1], int(sys.argv[2]), sys.argv[4:]
def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
def serve(srv):
    while True:
        client, _ = srv.accept()
        try:
            up = socket.socket(socket.AF_UNIX)
            up.connect(sock_path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, up), daemon=True).start()
        threading.Thread(target=pipe, args=(up, client), daemon=True).start()
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(64)
threading.Thread(target=serve, args=(srv,), daemon=True).start()
code = subprocess.call(argv)
sys.exit(code if code >= 0 else 128 - code)
"""


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
    reason = unavailable_reason()
    if reason:
        raise SandboxUnavailable(reason)
    if sys.platform == "darwin":
        profile_path.write_text(profile.render())
        full = [_launcher("sandbox-exec"), "-f", str(profile_path), *argv]
    else:
        inner = argv
        if profile.proxy_socket is not None:
            inner = [str(Path(sys.executable).resolve()), "-I", "-c", _RELAY, str(profile.proxy_socket), str(profile.proxy_port), "--", *argv]
        full = [*bwrap_args(profile, cwd), "--", *inner]
        profile_path.write_text(json.dumps(full[:-len(argv)] if argv else full, indent=1))  # audit copy
    proc = subprocess.Popen(
        full,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,  # Codex waits for "additional input" on an open stdin
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

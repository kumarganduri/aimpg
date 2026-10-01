"""Escape tests: run real commands inside the replay sandbox (macOS only)."""

import socket
from pathlib import Path

import pytest

from aimpg.replay import sandbox
from aimpg.replay.proxy import NOTHING, AllowlistProxy
from aimpg.replay.sandbox import Profile

pytestmark = pytest.mark.skipif(not sandbox.available(), reason="Seatbelt sandbox is macOS-only")


@pytest.fixture
def layout(tmp_path):
    root = Path("/private/tmp") / f"aimpg-test-{tmp_path.name}"
    run_a, run_b = root / "runA", root / "runB"
    for d in (run_a / "work", run_a / "cfg", run_b / "work"):
        d.mkdir(parents=True)
    (run_b / "work" / "hidden_test.py").write_text("def test_answer(): ...\n")
    yield root, run_a
    import shutil

    shutil.rmtree(root, ignore_errors=True)


def sh(cmd: str, layout, *, port=None) -> sandbox.Result:
    root, run_a = layout
    profile = Profile(writable=[run_a / "work", run_a / "cfg"], deny_roots=[root], proxy_port=port)
    return sandbox.run(
        ["/bin/sh", "-c", cmd],
        profile=profile,
        profile_path=root / "p.sb",
        env={"PATH": "/usr/bin:/bin"},
        cwd=run_a / "work",
        timeout=30,
    )


def test_own_workdir_is_writable(layout):
    r = sh("echo ok > mine.txt && cat mine.txt", layout)
    assert r.returncode == 0 and r.stdout.strip() == "ok"


def test_home_folder_is_hidden(layout):
    r = sh(f"ls {Path.home()}", layout)
    assert r.returncode != 0 and "Operation not permitted" in r.stderr


def test_sibling_run_hidden_tests_are_unreadable(layout):
    root, _ = layout
    r = sh(f"cat {root}/runB/work/hidden_test.py", layout)
    assert r.returncode != 0 and "Operation not permitted" in r.stderr


def test_the_profile_file_itself_is_unreadable(layout):
    root, _ = layout
    assert sh(f"cat {root}/p.sb", layout).returncode != 0


def test_writes_outside_workdir_fail(layout):
    r = sh("echo x > /private/tmp/aimpg-escape-probe.txt", layout)
    assert r.returncode != 0
    assert not Path("/private/tmp/aimpg-escape-probe.txt").exists()


def test_no_network_without_proxy(layout):
    r = sh("/usr/bin/curl -s -m 5 -o /dev/null -w '%{http_code}' https://1.1.1.1", layout)
    assert r.stdout.strip() in ("000", "")


def test_only_proxy_port_reachable(layout):
    with AllowlistProxy(NOTHING) as proxy:
        direct = sh("/usr/bin/curl -s -m 5 -o /dev/null -w '%{http_code}' https://github.com", layout, port=proxy.port)
        via = sh(f"/usr/bin/curl -s -m 5 -o /dev/null -w '%{{http_code}}' -x {proxy.url} https://github.com", layout, port=proxy.port)
    assert direct.stdout.strip() == "000"
    assert via.stdout.strip() == "000"  # proxy answered 403 to CONNECT
    assert proxy.blocked() == ["github.com:443"]


def test_ssh_agent_socket_is_unreachable(layout, monkeypatch):
    import os

    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        pytest.skip("no ssh agent on this machine")
    r = sh(f"/usr/bin/nc -U {sock} </dev/null", layout)
    assert r.returncode != 0


def test_timeout_kills_the_process_group(layout):
    root, run_a = layout
    r = sandbox.run(
        ["/bin/sh", "-c", "sleep 30 & sleep 30"],
        profile=Profile(writable=[run_a / "work"], deny_roots=[root]),
        profile_path=root / "p.sb",
        env={"PATH": "/usr/bin:/bin"},
        cwd=run_a / "work",
        timeout=1,
    )
    assert r.timed_out and r.returncode is None


def test_tool_dirs_include_agent_install_when_under_home():
    dirs = sandbox.tool_dirs(("sh",))  # /bin/sh is outside $HOME: nothing to grant
    assert all(str(d).startswith(str(Path.home().resolve())) for d in dirs)

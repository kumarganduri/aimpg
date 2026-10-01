"""Tiny helper for building throwaway git repos with controlled commit times."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

DAY = 86400


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, when: float | None = None) -> str:
        env = dict(os.environ)
        if when is not None:
            stamp = f"@{int(when)} +0000"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        return subprocess.run(
            ["git", "-C", str(self.path), *args], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files: dict[str, str], msg: str, when: float) -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", msg, when=when)
        return self.git("rev-parse", "HEAD")

    def remove(self, name: str, msg: str, when: float) -> str:
        self.git("rm", "-q", name)
        self.git("commit", "-q", "-m", msg, when=when)
        return self.git("rev-parse", "HEAD")


def days_ago(n: float) -> float:
    return time.time() - n * DAY

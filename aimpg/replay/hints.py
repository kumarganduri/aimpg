"""Interface hints: the new names a commit's hidden tests import (Phase 2, option 1).

Found in the first paid calibration: every run failed before a single test
ran, because the hidden tests import names the commit invented (e.g.
`RATE_LIMIT_RETRY_ATTEMPTS`) and a commit message never says what things are
called. The hint tells the agent *what to call* things, never *how to make
the tests pass*: names and files only, no test code.

    hidden test:  from legwork.llm_client import LLMConfig, RATE_LIMIT_RETRY_ATTEMPTS
    parent code:  legwork/llm_client.py defines LLMConfig, not the constant
    hint:         legwork/llm_client.py: RATE_LIMIT_RETRY_ATTEMPTS

A fairness check (select.precheck) then requires that stub code which only
*creates* those names, with empty values, still FAILS the tests, so a hint
can never be enough to pass on its own.
"""

from __future__ import annotations

import ast
import posixpath
import re
from dataclasses import dataclass, field

from aimpg.replay import workspace


@dataclass
class Hint:
    names: dict[str, list[str]] = field(default_factory=dict)  # file -> new names the tests need

    def text(self) -> str:
        if not self.names:
            return ""
        lines = [f"- {path}: {', '.join(names)}" for path, names in sorted(self.names.items())]
        return "The tests will use these new names (create them with exactly these names):\n" + "\n".join(lines)

    def add(self, path: str, name: str) -> None:
        bucket = self.names.setdefault(path, [])
        if name not in bucket:
            bucket.append(name)


def python_hint(repo: str, parent: str, sha: str, test_files: list[str]) -> Hint:
    hint = Hint()
    for test in test_files:
        source = workspace.file_at(repo, sha, test)
        if source is None or not test.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        aliases: dict[str, str] = {}  # local name -> module file, for `import pkg.mod as m; m.NEW`
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                path = _module_file(repo, sha, node.module)
                if path is None:
                    continue
                for alias in node.names:
                    sub = _module_file(repo, sha, f"{node.module}.{alias.name}")
                    if sub is not None:  # `from pkg import module`
                        aliases[alias.asname or alias.name] = sub
                    elif alias.name != "*" and not _defined(repo, parent, path, alias.name):
                        hint.add(path, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    path = _module_file(repo, sha, alias.name)
                    if path is not None:
                        aliases[alias.asname or alias.name] = path
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                path = aliases[node.value.id]
                if not _defined(repo, parent, path, node.attr):
                    hint.add(path, node.attr)
    return hint


def _module_file(repo: str, sha: str, module: str) -> str | None:
    """Repo-relative file for a project module at `sha`, or None for third-party/stdlib."""
    base = module.replace(".", "/")
    for prefix in ("", "src/"):
        for candidate in (f"{prefix}{base}.py", f"{prefix}{base}/__init__.py"):
            if workspace.file_at(repo, sha, candidate) is not None:
                return candidate
    return None


def _defined(repo: str, parent: str, path: str, name: str) -> bool:
    """Is `name` available from `path` in the parent commit?"""
    source = workspace.file_at(repo, parent, path)
    if source is None:
        return False  # the whole file is new
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True  # can't tell: don't hint
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(t)):
                    return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return True
    return False


_JS_IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"](\.{1,2}/[^'\"]+)['\"]")
_JS_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.js")


def js_hint(repo: str, parent: str, sha: str, test_files: list[str]) -> Hint:
    hint = Hint()
    for test in test_files:
        source = workspace.file_at(repo, sha, test)
        if source is None:
            continue
        text = source.decode(errors="replace")
        for names, rel in _JS_IMPORT.findall(text):
            path = _js_file(repo, sha, posixpath.normpath(posixpath.join(posixpath.dirname(test), rel)))
            if path is None:
                continue
            before = (workspace.file_at(repo, parent, path) or b"").decode(errors="replace")
            for raw in names.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and name != "type" and not _js_exports(before, name):
                    hint.add(path, name)
    return hint


def _js_file(repo: str, sha: str, base: str) -> str | None:
    for ext in _JS_EXTS:
        if workspace.file_at(repo, sha, base + ext) is not None:
            return base + ext
    return None


def _js_exports(source: str, name: str) -> bool:
    n = re.escape(name)
    return bool(
        re.search(rf"export\s+(default\s+)?(async\s+)?(function\*?|const|let|var|class|type|interface|enum)\s+{n}\b", source)
        or re.search(rf"export\s*\{{[^}}]*\b{n}\b[^}}]*\}}", source)
    )


def for_commit(commit: "workspace.Commit") -> Hint:
    if commit.kind == "js":
        return js_hint(commit.repo, commit.parent, commit.sha, commit.test_files)
    return python_hint(commit.repo, commit.parent, commit.sha, commit.test_files)


def write_stubs(hint: Hint, work, kind: str) -> None:
    """Create every hinted name with an empty value: tests must still FAIL on this."""
    for path, names in hint.names.items():
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text() if target.exists() else ""
        if kind == "js":
            stub = "".join(f"\nexport const {n} = undefined;" for n in names)
        else:
            stub = "".join(f"\n{n} = None" for n in names)
        target.write_text(existing + stub + "\n")

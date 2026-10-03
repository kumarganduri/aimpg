from aimpg.replay import hints

from gitrepo import Repo, days_ago


def two_commits(tmp_path, before: dict, after: dict, msg="feat: change"):
    repo = Repo(tmp_path / "r")
    parent = repo.commit(before, "base", days_ago(3))
    sha = repo.commit(after, msg, days_ago(2))
    return str(repo.path), parent, sha


def test_only_names_new_in_the_commit_are_hinted(tmp_path):
    repo, parent, sha = two_commits(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/llm.py": "class Config: ...\nRETRIES = 3\n"},
        {
            "pkg/llm.py": "class Config: ...\nRETRIES = 3\nRATE_LIMIT_RETRY_ATTEMPTS = 5\ndef wait(): ...\n",
            "tests/test_llm.py": "import json\nfrom pkg.llm import Config, RETRIES, RATE_LIMIT_RETRY_ATTEMPTS, wait\n",
        },
    )
    h = hints.python_hint(repo, parent, sha, ["tests/test_llm.py"])
    assert h.names == {"pkg/llm.py": ["RATE_LIMIT_RETRY_ATTEMPTS", "wait"]}
    assert "pkg/llm.py: RATE_LIMIT_RETRY_ATTEMPTS, wait" in h.text()


def test_module_attribute_access_and_new_files(tmp_path):
    repo, parent, sha = two_commits(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/store.py": "def keep(): ...\n"},
        {
            "pkg/store.py": "def keep(): ...\ndef cleanup_targets(): ...\n",
            "pkg/fresh.py": "def brand_new(): ...\n",
            "tests/test_store.py": "from pkg import store\nimport pkg.fresh as fresh\n\ndef test():\n    store.keep(); store.cleanup_targets(); fresh.brand_new()\n",
        },
    )
    h = hints.python_hint(repo, parent, sha, ["tests/test_store.py"])
    assert h.names == {"pkg/store.py": ["cleanup_targets"], "pkg/fresh.py": ["brand_new"]}


def test_third_party_and_stdlib_imports_are_ignored(tmp_path):
    repo, parent, sha = two_commits(
        tmp_path,
        {"app.py": "x = 1\n"},
        {"app.py": "x = 2\n", "tests/test_app.py": "import pytest\nfrom unittest.mock import patch\nfrom app import x\n"},
    )
    assert hints.python_hint(repo, parent, sha, ["tests/test_app.py"]).names == {}


def test_js_named_imports(tmp_path):
    repo, parent, sha = two_commits(
        tmp_path,
        {"src/num.ts": "export function add(a: number, b: number) { return a + b }\n"},
        {
            "src/num.ts": "export function add(a: number, b: number) { return a + b }\nexport const mul = (a: number, b: number) => a * b\n",
            "src/num.test.ts": "import { add, mul } from './num'\nimport { describe } from 'vitest'\n",
        },
    )
    h = hints.js_hint(repo, parent, sha, ["src/num.test.ts"])
    assert h.names == {"src/num.ts": ["mul"]}


def test_stubs_create_every_hinted_name(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "llm.py").write_text("RETRIES = 3\n")
    h = hints.Hint({"pkg/llm.py": ["RATE_LIMIT_RETRY_ATTEMPTS"], "pkg/new.py": ["brand_new"]})
    hints.write_stubs(h, tmp_path, "python")
    assert "RATE_LIMIT_RETRY_ATTEMPTS = None" in (tmp_path / "pkg" / "llm.py").read_text()
    assert (tmp_path / "pkg" / "new.py").read_text().strip() == "brand_new = None"


def test_no_hint_means_empty_text():
    assert hints.Hint().text() == ""

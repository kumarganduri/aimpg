import time

import pytest

from aimpg.gitkept import KeptChecker, Status, default_ref, load_commits, repo_root

from gitrepo import DAY, Repo, days_ago

BODY = "def handler(event):\n    return compute_answer(event)\n"


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"base.py": "print('base line')\n"}, "base", days_ago(40))
    return r


def classify(repo, now=None):
    now = now or time.time()
    checker = KeptChecker(str(repo.path), since=days_ago(60), now=now)
    commits = {c.sha: c for c in load_commits(str(repo.path), days_ago(60))}
    return checker, commits, checker.classify(list(commits.values()))


def test_old_commit_on_main_is_kept(repo):
    sha = repo.commit({"a.py": BODY}, "feat: a", days_ago(20))
    _, _, status = classify(repo)
    assert status[sha] is Status.KEPT


def test_recent_commit_is_pending(repo):
    sha = repo.commit({"a.py": BODY}, "feat: a", days_ago(2))
    _, _, status = classify(repo)
    assert status[sha] is Status.PENDING


def test_rebased_commit_kept_by_patch_id(repo):
    repo.git("checkout", "-q", "-b", "feature")
    feature_sha = repo.commit({"a.py": BODY}, "feat: a", days_ago(20))
    repo.git("checkout", "-q", "main")
    repo.git("cherry-pick", feature_sha, when=days_ago(19))
    _, _, status = classify(repo)
    assert status[feature_sha] is Status.KEPT


def test_squash_merge_detected_offline(repo):
    repo.git("checkout", "-q", "-b", "feature")
    one = repo.commit({"a.py": BODY}, "wip 1", days_ago(20))
    two = repo.commit({"b.py": "def other_function():\n    return 42\n"}, "wip 2", days_ago(19))
    repo.git("checkout", "-q", "main")
    repo.git("merge", "--squash", "-q", "feature")
    repo.git("commit", "-q", "-m", "feat: squashed (#12)", when=days_ago(18))
    _, _, status = classify(repo)
    assert status[one] is Status.KEPT_SQUASH
    assert status[two] is Status.KEPT_SQUASH


def test_abandoned_branch_is_discarded_when_main_is_fresh(repo):
    repo.git("checkout", "-q", "-b", "dead-end")
    sha = repo.commit({"a.py": "def abandoned_idea():\n    pass\n"}, "try thing", days_ago(20))
    repo.git("checkout", "-q", "main")
    repo.commit({"c.py": "print('main moves on')\n"}, "main work", days_ago(5))
    _, _, status = classify(repo)
    assert status[sha] is Status.DISCARDED


def test_stale_default_ref_gives_unknown(repo):
    # The reflog says main last moved at real "now"; pretend we're 30 days later.
    repo.git("checkout", "-q", "-b", "dead-end")
    sha = repo.commit({"a.py": "def abandoned_idea():\n    pass\n"}, "try thing", days_ago(1))
    repo.git("checkout", "-q", "main")
    _, _, status = classify(repo, now=time.time() + 30 * DAY)
    assert status[sha] is Status.UNKNOWN_STALE


def test_deletion_only_commit_is_inconclusive_not_discarded(repo):
    repo.git("checkout", "-q", "-b", "cleanup")
    sha = repo.remove("base.py", "remove base", days_ago(20))
    repo.git("checkout", "-q", "main")
    repo.commit({"c.py": "print('main moves on')\n"}, "main work", days_ago(5))
    _, _, status = classify(repo)
    assert status[sha] is Status.UNKNOWN_SQUASH


def test_amended_commit_still_loaded_via_reflog(repo):
    original = repo.commit({"a.py": BODY}, "feat: a", days_ago(20))
    repo.git("commit", "-q", "--amend", "-m", "feat: a (amended)", when=days_ago(20))
    shas = {c.sha for c in load_commits(str(repo.path), days_ago(60))}
    assert original in shas


def test_numstat_and_lines_changed(repo):
    sha = repo.commit({"a.py": BODY}, "feat: a", days_ago(20))
    commit = next(c for c in load_commits(str(repo.path), days_ago(60)) if c.sha == sha)
    assert commit.files == {"a.py": (2, 0)}
    assert commit.lines_changed == 2
    assert "return compute_answer(event)" in commit.added["a.py"]


def test_default_ref_update_time_comes_from_reflog_not_tip(repo):
    # Reflog entries take the committer date, so the fixture's main moved 40 days ago.
    ref, updated = default_ref(str(repo.path))
    assert ref == "main"
    assert updated == pytest.approx(days_ago(40), abs=60)
    # A fetch writes origin/main's reflog at wall-clock time even though its tip is old.
    repo.git("update-ref", "-m", "fetch", "refs/remotes/origin/main", "HEAD")
    ref, updated = default_ref(str(repo.path))
    assert ref == "origin/main"
    assert updated > days_ago(1)


def test_repo_root(repo, tmp_path):
    sub = repo.path / "pkg"
    sub.mkdir()
    assert repo_root(str(sub)) == str(repo.path.resolve())
    assert repo_root(str(tmp_path / "missing")) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_root(str(plain)) is None

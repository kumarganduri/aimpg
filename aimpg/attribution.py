"""Tie AI requests to the commits they produced.

Tier 1 (exact): a Bash tool call ran `git commit`, and a commit in that
repo has a committer time inside the call's [start, end] interval (±2s
for whole-second git timestamps). Measured 8/8 on real commit history,
including a commit whose hooks made it land 147s after the call started.

Energy split (time segments): within one session, the requests made after
commit k-1 and up to commit k belong to commit k, whatever repo they ran
in. A session's cwd is often not the repo it works on (absolute paths,
`cd x && ...`), so grouping by folder would strand requests.

    session s:   r r r [c1 in A] r r r r [c2 in B] r r
                 └── c1 ───┘  └──── c2 ─────┘  └ no commit yet

Tier 2 (fuzzy): a session's leftover requests (after its last exact
commit) go to commits in any repo it worked in, made between its first leftover
request and 2h after its last, authored by you (repo user.email) that touch a file the session edited
(Edit/Write tools or Bash: sed -i, redirects, mv/cp/rm). Several matches
split the requests by lines changed. Commits after the session's last
request are labeled "grace". Built because the first real run showed exact
matches covered only ~48% of in-repo energy (design gate: 70%).
"""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from aimpg.gitkept import DAY, Commit, GitError, KeptChecker, load_commits, repo_root, user_email
from aimpg.model import CommitCall, ParseResult, Request, Task

PAD = 2.0  # seconds; git timestamps are whole seconds
GRACE = 2 * 3600.0  # hand commits often land a while after the session goes quiet

NOT_IN_REPO = "not in a git repo (or repo moved/deleted)"
NO_COMMIT_YET = "no commit from this session yet"
GIT_FAILED = "git error"


@dataclass
class RepoInfo:
    ref: str = ""
    ref_updated: float = 0.0
    error: str = ""


@dataclass
class Attribution:
    tasks: list[Task] = field(default_factory=list)
    unattributed: dict[str, list[Request]] = field(default_factory=dict)
    repos: dict[str, RepoInfo] = field(default_factory=dict)
    # session_id -> repos it committed in, ran in, or edited files in
    session_repos: dict[str, set[str]] = field(default_factory=dict)


def attribute(
    parsed: ParseResult,
    since: float,
    now: float,
    *,
    refresh: bool = False,
    find_root: Callable[[str], str | None] = repo_root,
) -> Attribution:
    roots: dict[str, str | None] = {}

    def root(path: str) -> str | None:
        if path not in roots:
            roots[path] = find_root(path)
        return roots[path]

    result = Attribution()
    by_session: dict[str, list[Request]] = defaultdict(list)
    for r in parsed.requests:
        if r.ts >= since:
            by_session[r.session_id].append(r)

    calls_by_session: dict[str, list[tuple[str, CommitCall]]] = defaultdict(list)
    for call in parsed.commit_calls:
        end = call.end if call.end is not None else call.start
        repo = root(call.cwd) if end >= since else None
        if repo is not None:
            calls_by_session[call.session_id].append((repo, call))

    # Repos each session may have worked in: where it committed, where its
    # requests ran, and where the files it edited live. A session's cwd is
    # often not the repo it worked on (absolute paths, `cd x && ...`).
    session_repos: dict[str, set[str]] = {}
    for session, group in by_session.items():
        repos = {repo for repo, _ in calls_by_session.get(session, [])}
        repos |= {root(r.cwd) for r in group} - {None}
        for cwd, path in parsed.files_touched.get(session, set()):
            full = path if os.path.isabs(path) else os.path.join(cwd, path)
            repos |= {root(os.path.dirname(os.path.normpath(full)))} - {None}
        session_repos[session] = repos
    result.session_repos = session_repos

    commits_by_repo: dict[str, dict[str, Commit]] = {}
    status_by_repo: dict[str, dict[str, str]] = {}
    all_repos = sorted(set().union(*session_repos.values()) if session_repos else set())
    # git subprocesses release the GIL, so repos load in parallel
    with ThreadPoolExecutor(max_workers=min(8, len(all_repos) or 1)) as pool:
        loaded = list(pool.map(lambda repo: _load_repo(repo, since - DAY, now, refresh), all_repos))
    for repo, (commits, statuses, info) in zip(all_repos, loaded):
        commits_by_repo[repo] = {c.sha: c for c in commits}
        status_by_repo[repo] = statuses
        result.repos[repo] = info

    def task_for(repo: str, commit: Commit, attribution: str) -> Task:
        key = (repo, commit.sha)
        if key not in tasks:
            tasks[key] = Task(
                repo=repo,
                sha=commit.sha,
                ts=commit.ts,
                subject=commit.subject,
                attribution=attribution,
                status=status_by_repo.get(repo, {}).get(commit.sha, GIT_FAILED),
            )
        return tasks[key]

    # Tier 1: time segments between the session's exact commits, in any repo.
    tasks: dict[tuple[str, str], Task] = {}
    leftovers: dict[str, list[Request]] = {}
    for session, group in by_session.items():
        group.sort(key=lambda r: r.ts)
        i = 0
        for repo, commit in _anchors(calls_by_session.get(session, []), commits_by_repo):
            task = task_for(repo, commit, "exact")
            while i < len(group) and group[i].ts <= commit.ts + PAD:
                task.add(group[i])
                i += 1
        if i < len(group):
            leftovers[session] = group[i:]

    # Tier 2 runs after every exact anchor is known, so a Claude-made commit
    # is never also claimed as someone's hand-made commit.
    exact = set(tasks)
    emails: dict[str, str] = {}
    for session, rest in leftovers.items():
        matches: list[tuple[str, Commit]] = []
        for repo in sorted(session_repos.get(session, set())):
            if repo not in emails:
                emails[repo] = user_email(repo)
            touched = _repo_relative(parsed.files_touched.get(session, set()), repo)
            matches += [(repo, c) for c in _fuzzy(rest, touched, commits_by_repo.get(repo, {}), exact, repo, emails[repo])]
        if not matches:
            reason = NO_COMMIT_YET if session_repos.get(session) else NOT_IN_REPO
            result.unattributed.setdefault(reason, []).extend(rest)
            continue
        total_lines = sum(max(c.lines_changed, 1) for _, c in matches)
        for repo, commit in matches:
            task = task_for(repo, commit, "fuzzy" if commit.ts <= rest[-1].ts + PAD else "grace")
            share = max(commit.lines_changed, 1) / total_lines
            for r in rest:
                task.add(r, share)

    result.tasks = sorted(tasks.values(), key=lambda t: t.ts)
    return result


def _load_repo(repo: str, since: float, now: float, refresh: bool) -> tuple[list[Commit], dict[str, str], RepoInfo]:
    info = RepoInfo()
    commits: list[Commit] = []
    statuses: dict[str, str] = {}
    try:
        commits = load_commits(repo, since)
        checker = KeptChecker(repo, since, now, refresh=refresh, commits=commits)
        info.ref, info.ref_updated = checker.ref, checker.ref_updated
        statuses = {sha: s.value for sha, s in checker.classify(commits).items()}
    except GitError as exc:
        # Commits may still have loaded (e.g. no default branch): attribute
        # them, and their status reads "git error" in the receipt.
        info.error = str(exc)
    return commits, statuses, info


def _repo_relative(touched: set[tuple[str, str]], repo: str) -> set[str]:
    out = set()
    for cwd, path in touched:
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        full = os.path.realpath(full) if os.path.exists(full) else full
        rel = os.path.relpath(full, repo)
        if not rel.startswith(".."):
            out.add(rel)
    return out


def _fuzzy(
    rest: list[Request],
    touched: set[str],
    commits: dict[str, Commit],
    exact: set[tuple[str, str]],
    repo: str,
    email: str,
) -> list[Commit]:
    """Your own hand-made commits in the session's window (+2h grace) that touch files it edited.

    Authorship matters: after a pull, teammates' merged commits land inside the
    window and touch the same files. Without a configured user.email we don't guess.
    """
    if not touched or not email:
        return []
    start, end = rest[0].ts - PAD, rest[-1].ts + GRACE
    return sorted(
        (
            c
            for c in commits.values()
            if start <= c.ts <= end
            and c.author_email == email
            and (repo, c.sha) not in exact
            and touched & set(c.files)
        ),
        key=lambda c: c.ts,
    )


def _anchors(calls: list[tuple[str, CommitCall]], commits_by_repo: dict[str, dict[str, Commit]]) -> list[tuple[str, Commit]]:
    """(repo, commit) whose time falls inside one of the session's commit calls, oldest first."""
    found: dict[tuple[str, str], tuple[str, Commit]] = {}
    for repo, call in calls:
        start = call.start - PAD
        end = (call.end if call.end is not None else call.start) + PAD
        for commit in commits_by_repo.get(repo, {}).values():
            if start <= commit.ts <= end:
                found[(repo, commit.sha)] = (repo, commit)
    return sorted(found.values(), key=lambda rc: rc[1].ts)



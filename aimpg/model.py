"""Core data model shared by `aimpg report` (Phase 1) and `aimpg replay` (Phase 2).

    Request ──(cwd, ts)──► Task(commit) ──► Status (kept / pending / ...)
       │
       └─ usage ──► energy.py (the only place energy is computed)

Phase 2 replay plug-ins must return `Request`s in this exact shape so the
energy code is reused unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Usage:
    """Token counts for one model request, mapped onto our four buckets."""

    fresh_in: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def ctx_len(self) -> int:
        """Tokens the model attends over while generating this request's output."""
        return self.fresh_in + self.cache_write + self.cache_read


@dataclass(frozen=True)
class Request:
    """One deduplicated model API request."""

    id: str
    session_id: str
    model: str
    ts: float  # epoch seconds
    usage: Usage
    cwd: str
    is_sidechain: bool = False


@dataclass(frozen=True)
class CommitCall:
    """A Bash tool call that ran `git commit`; the commit lands inside [start, end]."""

    session_id: str
    cwd: str
    start: float
    end: float | None  # None when the tool result never arrived


@dataclass
class Task:
    """One commit and the AI requests that produced it."""

    repo: str
    sha: str
    ts: float
    subject: str
    attribution: str  # "exact" | "fuzzy" | "grace"
    status: str  # a gitkept.Status value
    requests: list[Request] = field(default_factory=list)
    # Share of each request's energy that belongs to this commit (parallel to
    # `requests`). Exact matches own whole requests; fuzzy matches split a
    # session's leftover requests across commits by lines changed.
    weights: list[float] = field(default_factory=list)

    def add(self, request: Request, weight: float = 1.0) -> None:
        self.requests.append(request)
        self.weights.append(weight)


@dataclass
class ParseResult:
    requests: list[Request] = field(default_factory=list)
    commit_calls: list[CommitCall] = field(default_factory=list)
    # session_id -> {(cwd, path)} touched by Edit/Write-style tools
    files_touched: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Share of usage-bearing requests we kept, out of all we saw."""
        seen = self.stats.get("unique_requests", 0) + self.stats.get("skipped_requests", 0)
        return 1.0 if seen == 0 else self.stats.get("unique_requests", 0) / seen

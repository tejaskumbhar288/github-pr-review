"""Repo-aware context: who calls the code this PR changed (roadmap 2).

Whole-file context tells the model what a changed function *is*. It cannot tell
it what the function is *for*, and that is where the expensive bugs live: a
signature that gained a parameter, a return that can now be ``None``, a
condition that flipped. Each is locally defensible and breaks a caller three
files away. A human reviewer answers this by grepping for the name.

The retrieval is deliberately shallow, and the reason is worth stating because
the obvious alternatives are all worse here:

* **Not embeddings.** A call site is found by an exact identifier, which is the
  one query lexical search answers perfectly and vector search answers fuzzily.
  There is no index to build, nothing to keep in sync with the branch, and no
  way for the retrieval to be subtly stale.
* **Not the whole calling file.** Ten callers at 2k lines each would drown the
  diff that the review is actually about. A window around each call site is
  what a reviewer reads anyway.
* **Bounded hard.** Code search is 30 requests/minute for the whole account, so
  an unbounded expansion here would starve the reviews queued behind it. The
  budget is a handful of symbols per review, and running out is logged rather
  than retried.

Every step degrades to "no extra context", because a review with less context
is a worse review and a review that failed is no review at all.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..github.client import GitHubClient, GitHubError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..github.client import PullRequest

log = logging.getLogger(__name__)

PY_SUFFIXES = (".py", ".pyi")

# Names too common to search for. A query for "run" returns the repo, not the
# callers, and spends a search request to do it.
COMMON_NAMES = frozenset(
    {
        "main",
        "run",
        "get",
        "set",
        "add",
        "put",
        "post",
        "list",
        "load",
        "save",
        "read",
        "write",
        "open",
        "close",
        "start",
        "stop",
        "init",
        "setup",
        "build",
        "make",
        "parse",
        "render",
        "handle",
        "process",
        "update",
        "create",
        "delete",
        "test",
        "setUp",
        "tearDown",
        "call",
        "send",
        "next",
        "data",
        "value",
        "name",
        "config",
        "settings",
        "client",
        "server",
        "model",
        "index",
        "check",
        "apply",
        "format",
        "encode",
        "decode",
        "to_dict",
        "from_dict",
        "execute",
        "connect",
    }
)

MIN_NAME_LEN = 4
# Lines of surrounding code shown either side of a call site.
WINDOW = 4
# Search results requested per symbol. Most are filtered out; see _search.
SEARCH_PAGE = 20


@dataclass
class CallSite:
    path: str
    line: int
    excerpt: str


@dataclass
class RepoContext:
    """What the expansion found, and what it cost."""

    symbols: list[str] = field(default_factory=list)
    call_sites: list[CallSite] = field(default_factory=list)
    searches: int = 0
    reason: str = ""
    """Why there is nothing here, when there is nothing here."""

    @property
    def files(self) -> int:
        return len({c.path for c in self.call_sites})

    def render(self) -> str:
        if not self.call_sites:
            return ""
        by_path: dict[str, list[CallSite]] = {}
        for site in self.call_sites:
            by_path.setdefault(site.path, []).append(site)
        blocks = []
        for path, sites in by_path.items():
            body = "\n\n".join(s.excerpt for s in sites)
            blocks.append(f"### {path} (elsewhere in the repo)\n```\n{body}\n```")
        return "\n\n".join(blocks)


def changed_symbols(content: str, added_lines: set[int]) -> list[str]:
    """Names of the functions and classes this diff touched, best first.

    A definition the PR *introduced* ranks above one it merely edited: a brand
    new function has no callers to break, but a brand new *signature* on an
    existing name is exactly the change that breaks them, and both arrive here
    the same way. Ranking by whether the `def` line itself moved is the cheapest
    signal that separates them, and the ordering only decides which symbols get
    searched first when the budget is tight.
    """
    if not added_lines:
        return []
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        # The file at HEAD may legitimately not parse under this interpreter -
        # newer syntax, a template, a Python 2 leftover. Not an error worth
        # surfacing; just nothing to offer.
        return []

    signature_changed: list[str] = []
    body_changed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if not any(node.lineno <= line <= end for line in added_lines):
            continue
        if not _is_searchable(node.name):
            continue
        # The `def` line plus its decorators and arguments; a change anywhere in
        # there can alter the contract callers rely on.
        header_end = max([node.lineno, *(a.lineno for a in _arg_nodes(node))])
        if any(node.lineno <= line <= header_end for line in added_lines):
            signature_changed.append(node.name)
        else:
            body_changed.append(node.name)

    seen: set[str] = set()
    ordered: list[str] = []
    for name in signature_changed + body_changed:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _arg_nodes(node: ast.AST) -> list[ast.arg]:
    args = getattr(node, "args", None)
    if not isinstance(args, ast.arguments):
        return []
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def _is_searchable(name: str) -> bool:
    if name.startswith("_") or len(name) < MIN_NAME_LEN:
        return False
    return name not in COMMON_NAMES and name.lower() not in COMMON_NAMES


class RepoContextBuilder:
    def __init__(self, gh: GitHubClient, settings: Settings):
        self._gh = gh
        self._settings = settings

    async def build(self, pr: PullRequest, file_contents: dict[str, str]) -> RepoContext:
        if not self._settings.repo_context:
            return RepoContext(reason="disabled")

        symbols = self._pick_symbols(pr, file_contents)
        if not symbols:
            return RepoContext(reason="no searchable symbols in the changed definitions")

        changed_paths = {f.path for f in pr.files}
        ctx = RepoContext(symbols=symbols)
        budget = self._settings.repo_context_max_files

        for name in symbols:
            if budget <= 0:
                break
            ctx.searches += 1
            paths = await self._search(pr, name)
            if paths is None:
                ctx.reason = "code search unavailable (no token, or search rate limit)"
                break
            for path in paths:
                if budget <= 0:
                    break
                if path in changed_paths:
                    # The file is already in the review at full length.
                    continue
                sites = await self._call_sites(pr, path, name)
                if not sites:
                    continue
                changed_paths.add(path)
                ctx.call_sites.extend(sites)
                budget -= 1

        if not ctx.call_sites and not ctx.reason:
            ctx.reason = f"no callers found for {', '.join(symbols)}"
        return ctx

    def _pick_symbols(self, pr: PullRequest, file_contents: dict[str, str]) -> list[str]:
        names: list[str] = []
        for f in pr.files:
            if f.is_deleted or not f.path.endswith(PY_SUFFIXES):
                continue
            content = file_contents.get(f.path)
            if not content:
                continue
            for name in changed_symbols(content, f.patch.added_lines):
                if name not in names:
                    names.append(name)
        return names[: self._settings.repo_context_max_symbols]

    async def _search(self, pr: PullRequest, name: str) -> list[str] | None:
        """Ask GitHub which files use this identifier. ``None`` means we could not.

        Two qualifiers, both measured rather than assumed:

        ``language:python`` because symbols are only ever extracted from Python
        files (see ``_pick_symbols``), so a result in another language cannot be
        a caller of one. Without it, a search for ``AsyncClient`` in httpx comes
        back led by four markdown files - documentation ranks high, is discarded
        immediately afterwards, and takes the result slots a real caller needed.

        A wide ``per_page`` for the same reason: most of what comes back is
        filtered out - the PR's own files, export manifests, files with no
        actual call - so asking for the exact budget returns far less than the
        budget once the filtering is done.
        """
        try:
            data = await self._gh.get_json(
                "/search/code",
                q=f'"{name}" repo:{pr.owner}/{pr.repo} language:python',
                per_page=max(SEARCH_PAGE, self._settings.repo_context_max_files * 3),
            )
        except GitHubError as exc:
            # 403 without a token, 422 on an unindexed repo, 503 while the index
            # catches up. None of them is worth failing or retrying a review for.
            log.info("code search for %r unavailable: %s", name, exc)
            return None
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - explicit for older loops
            log.info("code search for %r timed out", name)
            return None
        return [str(item.get("path") or "") for item in (data.get("items") or []) if item]

    async def _call_sites(self, pr: PullRequest, path: str, name: str) -> list[CallSite]:
        if not path or path.endswith((".md", ".rst", ".txt")):
            return []
        # The base commit, not the head: on a fork the head SHA does not exist in
        # the upstream repo, and the callers we care about are the ones on the
        # branch this PR is merging into anyway.
        content = await self._gh.fetch_file(pr.owner, pr.repo, path, pr.base_sha)
        if not content or len(content) > self._settings.context_char_limit:
            return []
        return extract_call_sites(path, content, name, limit=self._settings.repo_context_per_file)


def extract_call_sites(path: str, content: str, name: str, *, limit: int = 3) -> list[CallSite]:
    """Windows of code around each mention of ``name``, with line numbers.

    Skips the definition itself - the model already has that file in full - and
    merges windows that overlap, so two adjacent calls read as one passage
    rather than as the same lines printed twice.
    """
    mention = re.compile(rf"\b{re.escape(name)}\b")
    definition = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b")
    # A use: called, instantiated, subclassed, or reached through an attribute.
    use = re.compile(rf"\b{re.escape(name)}\s*[(.\[]")
    lines = content.splitlines()

    # Only real uses count. Measured on httpx: a search for `AsyncClient`
    # returns `__init__.py` first, where the only match is the string
    # "AsyncClient" in an `__all__` list. That is an export manifest, not a
    # caller - it says nothing about how the changed code is used, while
    # spending a file from a budget that a real call site needed.
    #
    # No fallback to bare mentions on purpose: when nothing calls the symbol,
    # "no callers found" is the true answer, and the builder can spend the
    # budget on the next search result instead of on a comment that names it.
    hits = [
        i
        for i, line in enumerate(lines)
        if use.search(line) and mention.search(line) and not definition.match(line)
    ]
    if not hits:
        return []

    windows: list[tuple[int, int, int]] = []
    for hit in hits:
        start, end = max(0, hit - WINDOW), min(len(lines), hit + WINDOW + 1)
        if windows and start <= windows[-1][1]:
            prev_start, _, prev_hit = windows[-1]
            windows[-1] = (prev_start, end, prev_hit)
            continue
        windows.append((start, end, hit))
        if len(windows) >= limit:
            break

    return [
        CallSite(
            path=path,
            line=hit + 1,
            excerpt="\n".join(f"{n + 1:>5} | {lines[n]}" for n in range(start, end)),
        )
        for start, end, hit in windows
    ]

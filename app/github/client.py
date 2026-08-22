"""Minimal GitHub REST client - only what a review actually needs."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .patch import ParsedPatch, parse_patch

log = logging.getLogger(__name__)

PR_URL_RE = re.compile(
    r"(?:https?://)?(?:[\w.-]+)/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
)

SKIP_SUFFIXES = (
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".jar",
    ".class",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".wasm",
    ".pyc",
    ".pb",
    ".onnx",
    ".snap",
    ".pbxproj",
)
SKIP_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "uv.lock",
    "Gemfile.lock",
    "mix.lock",
    "pubspec.lock",
    "Podfile.lock",
    "flake.lock",
}
SKIP_DIR_PARTS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    "site-packages",
    ".next",
    "target",
}
# Files whose contents are generated; the diff is noise, not signal.
GENERATED_MARKERS = ("/generated/", ".generated.", "_pb2.py", ".pb.go", ".g.dart")

MAX_PAGES = 20


class GitHubError(RuntimeError):
    """A GitHub API call failed in a way the caller should surface."""


@dataclass
class PRFile:
    path: str
    status: str
    additions: int
    deletions: int
    patch: ParsedPatch
    previous_path: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.status == "removed"


@dataclass
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    files: list[PRFile] = field(default_factory=list)
    author: str = ""
    draft: bool = False
    state: str = "open"
    truncated_files: int = 0
    """Files present in the PR but excluded from review (skipped or over budget)."""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def idempotency_key(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}@{self.head_sha}"

    def file(self, path: str) -> PRFile | None:
        return next((f for f in self.files if f.path == path), None)


def parse_pr_url(url: str) -> tuple[str, str, int]:
    match = PR_URL_RE.search(url.strip())
    if not match:
        raise ValueError(
            f"not a GitHub pull request URL: {url!r}\n"
            "Expected something like https://github.com/owner/repo/pull/123"
        )
    repo = match.group("repo")
    return match.group("owner"), repo.removesuffix(".git"), int(match.group("number"))


def should_skip(path: str) -> bool:
    """Filter files whose diff costs tokens and yields nothing."""
    name = path.rsplit("/", 1)[-1]
    if name in SKIP_NAMES or path.endswith(SKIP_SUFFIXES):
        return True
    if any(part in SKIP_DIR_PARTS for part in path.split("/")):
        return True
    return any(marker in path for marker in GENERATED_MARKERS)


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        api: str = "https://api.github.com",
        *,
        timeout: float = 60.0,
        max_retries: int = 4,
        client: httpx.AsyncClient | None = None,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-code-review",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=api.rstrip("/"), headers=headers, timeout=httpx.Timeout(timeout)
        )

    # --- transport --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        delay = 1.0
        last_error = ""
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                last_error = str(exc)
                if attempt == self._max_retries:
                    raise GitHubError(f"cannot reach GitHub: {exc}") from exc
                await asyncio.sleep(random.uniform(0, delay))
                delay = min(delay * 2, 30.0)
                continue

            if resp.status_code in (403, 429) and _is_rate_limited(resp):
                wait = _rate_limit_wait(resp)
                if attempt == self._max_retries:
                    raise GitHubError(
                        "GitHub rate limit hit. Set GITHUB_TOKEN to raise the ceiling "
                        "from 60 to 5,000 requests/hour."
                    )
                log.warning("github rate limited, sleeping %.0fs", wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 500:
                last_error = f"{resp.status_code}"
                if attempt == self._max_retries:
                    raise GitHubError(f"GitHub returned {resp.status_code} repeatedly")
                await asyncio.sleep(random.uniform(0, delay))
                delay = min(delay * 2, 30.0)
                continue

            return resp

        raise GitHubError(f"GitHub request failed: {last_error}")

    async def get_json(self, path: str, **params: Any) -> Any:
        resp = await self._request("GET", path, params=params or None)
        _raise_for_status(resp, path)
        return resp.json()

    async def post_json(self, path: str, payload: Any) -> Any:
        resp = await self._request("POST", path, json=payload)
        _raise_for_status(resp, path)
        return resp.json() if resp.content else {}

    # --- domain calls -----------------------------------------------------

    async def fetch_pr(
        self, owner: str, repo: str, number: int, *, max_files: int, max_patch_lines: int
    ) -> PullRequest:
        meta = await self.get_json(f"/repos/{owner}/{repo}/pulls/{number}")

        files: list[PRFile] = []
        excluded = 0
        page = 1
        while len(files) < max_files and page <= MAX_PAGES:
            batch = await self.get_json(
                f"/repos/{owner}/{repo}/pulls/{number}/files", per_page=100, page=page
            )
            if not batch:
                break
            for item in batch:
                if len(files) >= max_files:
                    excluded += 1
                    continue
                path = item["filename"]
                if should_skip(path):
                    excluded += 1
                    continue
                patch_text = item.get("patch")
                if patch_text and len(patch_text.splitlines()) > max_patch_lines:
                    # Generated or vendored; not worth the tokens.
                    excluded += 1
                    continue
                files.append(
                    PRFile(
                        path=path,
                        status=item.get("status", "modified"),
                        additions=int(item.get("additions") or 0),
                        deletions=int(item.get("deletions") or 0),
                        patch=parse_patch(patch_text, max_lines=max_patch_lines),
                        previous_path=item.get("previous_filename"),
                    )
                )
            if len(batch) < 100:
                break
            page += 1

        return PullRequest(
            owner=owner,
            repo=repo,
            number=number,
            title=meta.get("title") or "",
            body=(meta.get("body") or "")[:4000],
            base_sha=meta["base"]["sha"],
            head_sha=meta["head"]["sha"],
            files=files,
            author=(meta.get("user") or {}).get("login", ""),
            draft=bool(meta.get("draft")),
            state=meta.get("state", "open"),
            truncated_files=excluded,
        )

    async def fetch_file(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        """Full file contents at a ref - the context that makes reviews specific."""
        try:
            resp = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw"},
            )
        except GitHubError:
            return None
        if resp.status_code != 200:
            return None
        # A directory (or a submodule) comes back as JSON, not raw bytes.
        if resp.headers.get("content-type", "").startswith("application/json"):
            return None
        try:
            return resp.text
        except UnicodeDecodeError:
            return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.headers.get("x-ratelimit-remaining") == "0":
        return True
    body = resp.text.lower()
    return "rate limit" in body or "secondary rate" in body or "abuse detection" in body


def _rate_limit_wait(resp: httpx.Response, *, cap: float = 60.0) -> float:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return min(float(raw), cap)
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset")
    if reset:
        try:
            import time

            return min(max(float(reset) - time.time(), 1.0), cap)
        except ValueError:
            pass
    return 5.0


def _raise_for_status(resp: httpx.Response, path: str) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code == 404:
        raise GitHubError(
            f"not found: {path}. Check the URL, and set GITHUB_TOKEN if the repo is private."
        )
    if resp.status_code == 401:
        raise GitHubError("GitHub rejected the credentials (401). Check GITHUB_TOKEN.")
    if resp.status_code == 403:
        raise GitHubError(
            f"GitHub denied the request (403) for {path}. The token likely lacks the "
            "required scope (contents: read, pull_requests: write)."
        )
    detail = resp.text[:500]
    raise GitHubError(f"GitHub {resp.status_code} for {path}: {detail}")

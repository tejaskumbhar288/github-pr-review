"""Repo-aware context (roadmap 2): who calls the code this PR changed."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.github.client import GitHubError, PRFile, PullRequest
from app.github.patch import parse_patch
from app.review.repo_context import (
    RepoContextBuilder,
    changed_symbols,
    extract_call_sites,
)

MODULE = """\
import os


def compute_discount(total, rate):
    return total * rate


class InvoiceBuilder:
    def build(self):
        return None


def _helper():
    return 1


def run():
    return 2
"""

CALLER = """\
from billing import compute_discount

TAX = 0.2


def checkout(cart):
    subtotal = sum(i.price for i in cart)
    discount = compute_discount(subtotal, 0.1)
    return subtotal - discount


def refund(order):
    return compute_discount(order.total, 1.0)
"""


def test_finds_the_definitions_a_diff_touched():
    added = {4, 5}  # inside compute_discount
    assert changed_symbols(MODULE, added) == ["compute_discount"]


def test_a_changed_signature_outranks_a_changed_body():
    """A new function has no callers to break. A new *signature* on an existing
    name is exactly the change that breaks them, so it is searched first."""
    lines = MODULE.splitlines()
    signature_line = lines.index("def compute_discount(total, rate):") + 1
    body_line = lines.index("    def build(self):") + 2
    names = changed_symbols(MODULE, {signature_line, body_line})
    assert names[0] == "compute_discount"
    assert "InvoiceBuilder" in names


@pytest.mark.parametrize("added", [set(), {12, 13}, {16, 17}])
def test_skips_private_and_ubiquitous_names(added):
    """`_helper` is private and `run` returns the whole repo when searched."""
    assert changed_symbols(MODULE, added) == []


def test_an_unparseable_file_yields_nothing_rather_than_raising():
    assert changed_symbols("def broken(:\n", {1}) == []


def test_call_sites_carry_line_numbers():
    """Both calls survive, and every line is numbered.

    They arrive as one window rather than two because the file is short enough
    that the windows overlap - which is the point of merging them, not a gap.
    """
    sites = extract_call_sites("app/checkout.py", CALLER, "compute_discount")
    excerpt = "\n".join(s.excerpt for s in sites)
    assert "compute_discount(subtotal, 0.1)" in excerpt
    assert "compute_discount(order.total, 1.0)" in excerpt
    assert "    8 |" in excerpt, "the model needs real line numbers to reason with"
    assert all(s.path == "app/checkout.py" for s in sites)


def test_the_definition_itself_is_not_reported_as_a_caller():
    sites = extract_call_sites("billing.py", MODULE, "compute_discount")
    assert sites == []


def test_overlapping_call_sites_merge_into_one_window():
    dense = "\n".join(
        ["x = 1"] * 3 + ["compute_discount(1)", "compute_discount(2)"] + ["y = 2"] * 3
    )
    sites = extract_call_sites("a.py", dense, "compute_discount")
    assert len(sites) == 1, "adjacent calls should read as one passage"


def test_the_window_is_bounded_per_file():
    many = "\n\n\n\n\n\n\n\n\n\n".join(f"compute_discount({i})" for i in range(10))
    sites = extract_call_sites("a.py", many, "compute_discount", limit=3)
    assert len(sites) == 3


# --- the builder -----------------------------------------------------------


class SearchingGitHub:
    def __init__(self, results: dict[str, list[str]] | Exception, files: dict[str, str]):
        self.results = results
        self.files = files
        self.queries: list[str] = []
        self.fetched: list[str] = []

    async def get_json(self, path: str, **params: Any):
        if isinstance(self.results, Exception):
            raise self.results
        self.queries.append(params.get("q", ""))
        name = params.get("q", "").split('"')[1]
        return {"items": [{"path": p} for p in self.results.get(name, [])]}

    async def fetch_file(self, owner, repo, path, ref):
        self.fetched.append(f"{path}@{ref}")
        return self.files.get(path)


def _pr() -> PullRequest:
    patch = "@@ -1,3 +4,5 @@\n def compute_discount(total, rate):\n-    return total\n+    return total * rate\n"
    return PullRequest(
        owner="acme",
        repo="billing",
        number=3,
        title="Apply the rate",
        body="",
        base_sha="base123",
        head_sha="head456",
        files=[
            PRFile(
                path="billing.py",
                status="modified",
                additions=1,
                deletions=1,
                patch=parse_patch(patch),
            )
        ],
    )


def _settings(**over) -> Settings:
    return Settings.from_env({"REPO_CONTEXT": "1", "LLM_PROVIDER": "ollama", **over})


async def test_pulls_in_a_caller_from_elsewhere_in_the_repo():
    pr = _pr()
    gh = SearchingGitHub({"compute_discount": ["app/checkout.py"]}, {"app/checkout.py": CALLER})
    ctx = await RepoContextBuilder(gh, _settings()).build(pr, {"billing.py": MODULE})

    assert ctx.symbols == ["compute_discount"]
    assert ctx.files == 1 and ctx.call_sites
    assert "compute_discount" in gh.queries[0] and "repo:acme/billing" in gh.queries[0]
    # Fetched at the base commit: a fork's head SHA does not exist upstream.
    assert gh.fetched == ["app/checkout.py@base123"]
    assert "checkout.py" in ctx.render()


async def test_files_already_in_the_pr_are_not_pulled_in_twice():
    pr = _pr()
    gh = SearchingGitHub({"compute_discount": ["billing.py"]}, {"billing.py": MODULE})
    ctx = await RepoContextBuilder(gh, _settings()).build(pr, {"billing.py": MODULE})

    assert ctx.call_sites == []
    assert gh.fetched == [], "the file is already in the review at full length"
    assert "no callers found" in ctx.reason


async def test_search_being_unavailable_degrades_to_no_extra_context():
    """Code search 403s without a token and 422s on an unindexed repo. Neither
    is worth failing a review over."""
    pr = _pr()
    gh = SearchingGitHub(GitHubError("GitHub denied the request (403)"), {})
    ctx = await RepoContextBuilder(gh, _settings()).build(pr, {"billing.py": MODULE})

    assert ctx.call_sites == []
    assert "code search unavailable" in ctx.reason


async def test_disabled_by_default_and_says_so():
    pr = _pr()
    gh = SearchingGitHub({}, {})
    ctx = await RepoContextBuilder(gh, Settings.from_env({"LLM_PROVIDER": "ollama"})).build(
        pr, {"billing.py": MODULE}
    )
    assert ctx.reason == "disabled" and ctx.searches == 0


async def test_the_search_budget_is_respected():
    pr = _pr()
    gh = SearchingGitHub(
        {"compute_discount": [f"app/c{i}.py" for i in range(10)]},
        {f"app/c{i}.py": CALLER for i in range(10)},
    )
    ctx = await RepoContextBuilder(gh, _settings(REPO_CONTEXT_MAX_FILES="2")).build(
        pr, {"billing.py": MODULE}
    )
    assert ctx.files == 2, "code search is 30/min across the account; it has to be bounded"


EXPORTS = """\
__all__ = [
    "AsyncBaseTransport",
    "compute_discount",
    "Client",
]
"""


def test_an_export_list_is_a_mention_not_a_caller():
    """Measured on httpx: a search for `AsyncClient` returns `__init__.py`
    first, where the only match is a string in `__all__`. That tells the model
    nothing about how the changed code is used, while spending the file budget
    a real call site needed."""
    assert extract_call_sites("pkg/__init__.py", EXPORTS, "compute_discount") == []


def test_real_uses_outrank_bare_mentions_in_the_same_file():
    source = (
        "from billing import compute_discount\n"
        + "filler\n" * 20
        + "total = compute_discount(cart, 0.1)\n"
    )
    sites = extract_call_sites("app/checkout.py", source, "compute_discount", limit=1)
    assert "compute_discount(cart, 0.1)" in sites[0].excerpt
    assert "from billing import" not in sites[0].excerpt


def test_a_file_that_only_names_the_symbol_is_not_a_caller():
    """No fallback to bare mentions: when nothing calls the symbol, "no callers
    found" is the true answer, and the budget is better spent on the next
    search result than on a comment that names it."""
    source = "filler\n" * 5 + "# see compute_discount for the rate rules\n"
    assert extract_call_sites("docs/notes.py", source, "compute_discount") == []

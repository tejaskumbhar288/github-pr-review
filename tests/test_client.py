from __future__ import annotations

import httpx
import pytest
import respx

from app.github.client import GitHubClient, GitHubError, parse_pr_url, should_skip

API = "https://api.github.com"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/psf/requests/pull/6800", ("psf", "requests", 6800)),
        ("http://github.com/o/r/pull/1", ("o", "r", 1)),
        ("github.com/o/r/pull/12/files", ("o", "r", 12)),
        ("https://github.com/o/r.git/pull/3", ("o", "r", 3)),
        ("https://github.enterprise.corp/o/r/pull/9", ("o", "r", 9)),
        ("  https://github.com/o/r/pull/5#discussion_r1  ", ("o", "r", 5)),
    ],
)
def test_pr_urls_are_parsed(url, expected):
    assert parse_pr_url(url) == expected


@pytest.mark.parametrize("bad", ["", "https://github.com/o/r", "https://github.com/o/r/issues/1"])
def test_bad_urls_explain_the_expected_shape(bad):
    with pytest.raises(ValueError, match="pull request URL"):
        parse_pr_url(bad)


@pytest.mark.parametrize(
    "path,skip",
    [
        ("src/app.py", False),
        ("README.md", False),
        ("poetry.lock", True),
        ("web/node_modules/x/index.js", True),
        ("assets/logo.svg", True),
        ("dist/bundle.min.js", True),
        ("proto/service_pb2.py", True),
        ("vendor/lib.go", True),
        ("app/generated/types.ts", True),
        ("data.bin", True),
    ],
)
def test_noise_files_are_skipped(path, skip):
    assert should_skip(path) is skip


@respx.mock
async def test_fetch_pr_parses_files_and_filters_noise():
    respx.get(f"{API}/repos/o/r/pulls/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "t",
                "body": "b",
                "draft": False,
                "state": "open",
                "user": {"login": "dev"},
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            },
        )
    )
    respx.get(f"{API}/repos/o/r/pulls/1/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -1,1 +1,2 @@\n x\n+y",
                },
                {
                    "filename": "yarn.lock",
                    "status": "modified",
                    "additions": 900,
                    "deletions": 0,
                    "patch": "@@ -1,1 +1,2 @@\n x\n+y",
                },
            ],
        )
    )
    async with GitHubClient(None) as gh:
        pr = await gh.fetch_pr("o", "r", 1, max_files=10, max_patch_lines=800)

    assert [f.path for f in pr.files] == ["src/a.py"]
    assert pr.truncated_files == 1
    assert pr.head_sha == "head" and pr.slug == "o/r#1"
    assert pr.idempotency_key == "o/r#1@head"


@respx.mock
async def test_huge_diffs_are_excluded_from_the_token_budget():
    respx.get(f"{API}/repos/o/r/pulls/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "t",
                "body": "",
                "base": {"sha": "b"},
                "head": {"sha": "h"},
            },
        )
    )
    big = "@@ -1,1 +1,900 @@\n" + "\n".join(f"+l{i}" for i in range(900))
    respx.get(f"{API}/repos/o/r/pulls/1/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "gen.py",
                    "status": "added",
                    "additions": 900,
                    "deletions": 0,
                    "patch": big,
                },
            ],
        )
    )
    async with GitHubClient(None) as gh:
        pr = await gh.fetch_pr("o", "r", 1, max_files=10, max_patch_lines=100)
    assert pr.files == [] and pr.truncated_files == 1


@respx.mock
async def test_max_files_is_enforced():
    respx.get(f"{API}/repos/o/r/pulls/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "t",
                "body": "",
                "base": {"sha": "b"},
                "head": {"sha": "h"},
            },
        )
    )
    respx.get(f"{API}/repos/o/r/pulls/1/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": f"f{i}.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -1,1 +1,2 @@\n x\n+y",
                }
                for i in range(10)
            ],
        )
    )
    async with GitHubClient(None) as gh:
        pr = await gh.fetch_pr("o", "r", 1, max_files=3, max_patch_lines=800)
    assert len(pr.files) == 3 and pr.truncated_files == 7


@respx.mock
async def test_404_explains_the_likely_cause():
    respx.get(f"{API}/repos/o/r/pulls/9").mock(return_value=httpx.Response(404, json={}))
    async with GitHubClient(None) as gh:
        with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
            await gh.fetch_pr("o", "r", 9, max_files=1, max_patch_lines=10)


@respx.mock
async def test_rate_limit_message_is_actionable(no_sleep):
    respx.get(f"{API}/repos/o/r/pulls/1").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-remaining": "0"}, text="API rate limit exceeded"
        )
    )
    async with GitHubClient(None, max_retries=2) as gh:
        with pytest.raises(GitHubError, match="5,000 requests/hour"):
            await gh.fetch_pr("o", "r", 1, max_files=1, max_patch_lines=10)


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds(no_sleep):
    route = respx.get(f"{API}/repos/o/r/pulls/1")
    route.side_effect = [
        httpx.Response(502),
        httpx.Response(
            200, json={"title": "t", "body": "", "base": {"sha": "b"}, "head": {"sha": "h"}}
        ),
    ]
    respx.get(f"{API}/repos/o/r/pulls/1/files").mock(return_value=httpx.Response(200, json=[]))
    async with GitHubClient(None, max_retries=3) as gh:
        pr = await gh.fetch_pr("o", "r", 1, max_files=1, max_patch_lines=10)
    assert pr.head_sha == "h"
    assert route.call_count == 2


@respx.mock
async def test_fetch_file_returns_none_instead_of_raising():
    respx.get(f"{API}/repos/o/r/contents/gone.py").mock(return_value=httpx.Response(404))
    async with GitHubClient(None) as gh:
        assert await gh.fetch_file("o", "r", "gone.py", "head") is None


@respx.mock
async def test_fetch_file_ignores_directory_listings():
    respx.get(f"{API}/repos/o/r/contents/pkg").mock(
        return_value=httpx.Response(200, json=[{"name": "a.py"}])
    )
    async with GitHubClient(None) as gh:
        assert await gh.fetch_file("o", "r", "pkg", "head") is None


@respx.mock
async def test_token_is_sent_when_present():
    route = respx.get(f"{API}/repos/o/r/contents/a.py").mock(
        return_value=httpx.Response(200, text="print(1)", headers={"content-type": "text/plain"})
    )
    async with GitHubClient("secret-token") as gh:
        assert await gh.fetch_file("o", "r", "a.py", "head") == "print(1)"
    assert route.calls[0].request.headers["authorization"] == "Bearer secret-token"

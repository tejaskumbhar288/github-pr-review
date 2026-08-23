# Progress — 23 August 2026

Thirteen commits. The theme was not features: it was that almost every bug
found today was a guard that could not fire in the case it was written for,
and each one was invisible because the system degraded quietly instead of
failing loudly.

## Shipped today

### The Gemini client stopped lying about why it failed

Five separate defects, all in the same small area, all found by running the
thing rather than reading it.

| Commit | What was wrong |
|---|---|
| `0bbd7bb` | Every 429 was treated as transient. The free tier caps at **20 requests per day, per model**, so four eval cases at five retries each spent exactly the day's budget on attempts that could never succeed. The retry logic was causing the outage it existed to survive. |
| `5b64f3b` | `_extract_text()` returned the text whenever it was non-empty and checked `finishReason` afterwards, so the `MAX_TOKENS` branch could only fire when the model emitted *nothing*. Real truncation always emits partial text, so users got "model did not return JSON" — true, and pointing at the wrong thing. |
| `cf0ccd8` | `maxOutputTokens` was never sent, so the cap came from each model's default and the same review finished on one model and was cut off on another. Now explicit, and the error breaks the spend down. |
| `df1ce78` | Truncation failed the whole review. It is sampling variance: the prompt that ran to `answer=26383` returned a complete 643-token review moments later. It could not be retried where it was detected — extraction ran *after* `with_backoff` returned — so extraction moved inside the retried call. |
| `3b20493` | `finishReason=RECITATION` killed a fixture mid-run. Tested rather than assumed: the same case scored 2/2 on both immediate retries. Empty output is now retryable for every reason except `SAFETY`, which is a decision about the content and will be reached again. |

One change was **reverted the same day**: a `maxItems` bound on the response
schema. `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` reject the entire
request with a bare 400 when the schema carries it. A response schema has to be
the one thing that works on every model.

### The GitHub App path ran for the first time

It was the only code in the repo that had never executed. It now does, end to
end, against the real API — JWT signed with the private key, accepted by
`GET /app`, exchanged for a `ghs_` installation token, second call served from
cache in 0.01ms, and that token reading a file through the ordinary client.

`4145fbd` turns that into tests that skip unless an App is configured. One
asserts the installation grants `contents:read` and `pull_requests:write` **and
nothing else** — over-permissioning is silent, so nothing else would notice it
eroding.

Two deployment defects surfaced while containerising it:

- `54be46e` → `3c6fdb8`: the mounted key. The image runs as uid 10001, a bind
  mount carries host ownership through unchanged, and a key at mode 600 owned by
  the developer is unreadable to the process that needs it. The worker died on
  `PermissionError`. Loosening a private key's permissions to suit a container
  is the wrong trade, so the inline `GITHUB_PRIVATE_KEY` form is used instead —
  which is also the only shape Fly or Render can accept.
- The PEM header contains spaces, so unquoted in `.env` it breaks
  `set -a; . ./.env` with a bewildering `RSA: command not found`.

### A real review, posted by the bot

`senior-review-bot[bot]` reviewed
[financial-doc-agent#24](https://github.com/tejaskumbhar288/financial-doc-agent/pull/24)
after a genuine GitHub delivery through a tunnel: **81.8s, 2 findings, both
true, zero false positives.**

- **MAJOR** — `seen[description]` overwrites unconditionally, so `$1200 / $500 /
  $1200` never flags the identical first and third lines. It also caught a
  consequence I had explicitly dismissed: `duplicate_value` reads whichever
  amount was stored last, so the reported total is wrong too. The tool was right
  and I was wrong.
- **MINOR** — `ignored_descriptions` is not normalised while item descriptions
  are, so a caller passing `"Shipping & Handling"` silently fails to match. A
  real bug, introduced by accident, that nobody had noticed.

It missed the `confidence=0.25` issue on that run, having caught it in a direct
call minutes earlier. Sampling variance, recorded rather than re-rolled.

Everything downstream was verified in production: signature rejection (401 on a
forged delivery), draft and bot-authored PRs skipped, `ping` answered, and
**idempotency** — reopening the PR logged `duplicate job dropped` at the same
head SHA and correctly spent no model request.

### Evals grew a live suite

`a7a8c34` adds four cases built on real pull requests. Every expectation was
raised by a maintainer of the project in review and then re-verified against the
file at the PR's head SHA, because a review comment's line number goes stale the
moment the author pushes again. **Three otherwise-good candidates were dropped**
for exactly that — the bug had already been revised away.

The PRs are closed-unmerged deliberately: a merged PR usually has its defect
fixed before merge, while a rejected one keeps it forever. One case is clean by
construction, so the suite measures false positives and not only detection.

`c91f571`: eval runs had never reached Langfuse. The harness built its
`ReviewEngine` without a tracer and fell through to the no-op base class, while
the CLI and worker — which go through `Pipeline` — were traced normally. Fixed,
including the `flush()` whose absence looks identical to tracing being off.

`620a61c` and `51c638e`: a diff is only meaningful when both sides are
comparable, and neither failure is visible in the numbers. The tool now says so
when the baseline came from a different model, and when the baseline itself was
recorded from a run that errored.

### Housekeeping

- Pushed to `github-pr-review`; CI runs on every push.
- All `Co-Authored-By: Claude` trailers stripped from history and force-pushed.
- `GITHUB_TOKEN` and `GITHUB_WEBHOOK_SECRET` were exposed in a transcript by a
  careless `docker compose config`; both rotated. The webhook rotation was
  **silently broken** until tested — three copies must agree (`.env`, the running
  container, GitHub's App config) and the container had a stale one, so two
  deliveries were rejected with 401 before it was caught by hashing each copy.
- `fly.toml` written, with scale-to-zero deliberately off.

## Tomorrow

**Blocked only by the daily quota, which resets at 12:30 IST** (midnight
Pacific — not midnight local; this cost real confusion today).

1. **Record a clean baseline.** Today's attempt hit the quota on the last of
   four cases and wrote a baseline describing three. It was restored rather than
   committed. Needs ~4 requests with headroom.
2. **A clean live-eval run.** Two of the four live cases — `httpx#453` and
   `urllib3#3647` — have *never* completed. Their bugs are verified; whether the
   reviewer finds them is still unknown.

**Ready to start, no blockers.**

3. **Deploy to Fly** (~$2/month). Everything it needs is proven. `fly.toml`
   exists; what remains is a Fly account, `flyctl`, a managed Redis URL, and
   `fly secrets set`. Then repoint the App's webhook with the same
   `PATCH /app/hook/config` call used for the tunnel. The current tunnel is
   ephemeral — when that terminal closes the URL dies.
4. **Write up the bugs.** The material is unusually good and costs nothing: a
   retry loop that burned the quota it was meant to survive; a monotonic counter
   subtracted from wall clock, logging `queued -1787342661.8s ago`; a guard that
   could only fire in the case that never happens.

**Known gaps in the reviewer itself.**

5. **Static-analysis noise.** Of 14 findings on PR #24, about ten were
   `S101 Use of assert detected` on test files — `assert` is how pytest works.
   It is a ruff config gap in that repo, but the reviewer should drop
   known-irrelevant static findings for test paths rather than padding a review
   with them.
6. **Retries are expensive against a 20/day cap.** Making truncation and empty
   responses retryable was correct, but a bad draw now costs up to five requests
   instead of one, and the fixture run took 812s. Worth a smaller retry budget
   for sampling failures than for rate limits.
7. **Langfuse names traces `review_metrics`** instead of `pr-review`. Contents
   are correct; this SDK version has no `update_trace`. Cosmetic, documented.

**Optional / untouched.**

8. `pip install semgrep` — do it *before* re-recording the baseline, since it
   shifts detections toward "by linter" and invalidates the old numbers.
9. Roadmap 1–3: incremental review, repo-aware context, comment resolution.

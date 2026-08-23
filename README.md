# AI Code Review Assistant

Reviews a GitHub pull request the way a senior engineer would: reads the diff
*plus* the full contents of the touched files, runs the linters first so it
doesn't waste attention on what tools already caught, reasons about correctness
and edge cases, and posts findings anchored to real, commentable line numbers.

Runs against a hosted model by default, or fully locally with zero egress.

```bash
python -m app.cli https://github.com/OWNER/REPO/pull/123          # print
python -m app.cli https://github.com/OWNER/REPO/pull/123 --post   # publish
docker compose up                                                   # webhook + worker
```

## Why it's built this way

**Provider abstraction, not provider lock-in.** `LLMProvider` exposes exactly
one method: `complete_json`. The review engine has no idea whether Gemini or a
local Ollama model is behind it. Swapping backends is one env var. This isn't
architecture astronautics — a self-hostable review bot is the only version a
company with a code-egress policy can actually adopt.

**Whole-file context, not just the diff.** A diff alone produces generic
review comments, because the model can't see whether the changed function is
called anywhere dangerous. Feeding entire touched files is the single biggest
quality lever, and it's why context window matters more than tokens/sec for
this workload.

**Line numbers are computed, never guessed.** `github/patch.py` walks the
unified diff, annotates every line with its true position in the new file, and
records which lines GitHub will accept a comment on. The model copies numbers
out of the prompt instead of counting. Findings that don't match a real anchor
are dropped before they can be posted — and the dropped list is surfaced, not
swallowed, so hallucination rate is measurable.

**Silence is a valid answer.** The prompt explicitly permits an empty findings
array and forbids style nitpicks. A bot that comments on every PR gets muted by
the second week. One of the four eval cases is a clean refactor whose correct
review is *nothing at all*.

**Output is sanitised, because models degenerate.** Two separate real runs
produced looping suggestions — one repeating a single token ~300 times, one
emitting 80+ words from a 24-word vocabulary. A character cap truncates those
but leaves the mess visible, so findings are checked for both shapes and the
suggestion is cut at the loop while the finding itself survives.

**Every quality claim is measured.** `python -m app.evals` scores the reviewer
against PRs with known bugs and reports detection, noise and drop rate together,
because a prompt change that finds more real bugs *and* invents more is not
obviously an improvement. `--baseline` says which way a change actually moved.

## Setup

```bash
make install          # or: python -m venv .venv && pip install -r requirements-dev.txt
cp .env.example .env
```

Then pick a backend.

### Gemini (default — recommended)

Grab a key from [Google AI Studio](https://aistudio.google.com/apikey), no card
required, and set `GEMINI_API_KEY` in `.env`.

Free-tier quotas have been cut without notice before, so `with_backoff()` in
`llm/base.py` handles 429s with exponential backoff and full jitter, honouring
`Retry-After` in either the seconds or the HTTP-date form, and falling back to
the `RetryInfo` Google puts in the response body when it sends no header.

Not every 429 is worth retrying, though. The free tier caps requests **per day,
per project, per model** — 20/day on `gemini-3.6-flash` at the time of writing —
and a daily cap is terminal: each retry spends another request from a budget
that is already gone, so one eval run can burn the rest of the day. The provider
reads the `QuotaFailure` detail and raises a non-retryable error for a `PerDay`
quota, retrying only the per-minute ones. Google makes this easy to get wrong:
the body advertises a `retryDelay` of ~40s even when the quota will not reset
for hours. Because the cap is per-model, switching `GEMINI_MODEL` gets you a
fresh budget — but a baseline recorded on one model is not comparable to a run
on another. Note that free-tier
prompts may be used to improve Google's models — fine for the public repos this
is demoed on, not fine for proprietary code. That's what the Ollama path is for.

### Running the App in containers

`GITHUB_PRIVATE_KEY_PATH` is the convenient form for running on your own
machine, but it does not survive containerisation: the image runs as an
unprivileged uid, a bind-mounted key keeps its *host* ownership, and a key
readable only by you is unreadable to the process that needs it. Chasing that
with `chmod` trades a real secret's permissions for a deployment convenience.

Use `GITHUB_PRIVATE_KEY` instead, holding the PEM on one line with `\n`
escapes - `load_private_key()` accepts either. That is also the only form a
platform like Fly or Render can give you, since they inject secrets as
environment variables and have no filesystem to mount:

```bash
# Quote it: the PEM header contains spaces, so an unquoted value breaks
# `set -a; . ./.env` and anything else that sources the file.
python -c 'print("GITHUB_PRIVATE_KEY=\"" + open("key.pem").read().strip().replace(chr(10), "\\n") + "\"")' >> .env
```

### Ollama (local, zero egress)

```bash
ollama pull qwen2.5-coder:7b
LLM_PROVIDER=ollama python -m app.cli <pr-url>
```

On ≤4 GB VRAM a 7B model will partially offload to CPU — expect minutes, not
seconds, per review, and noticeably shallower findings. Usable as a privacy
story and a fallback; not the path for iterating on prompt quality.

### GitHub token

Optional for public repos, but unauthenticated requests are capped at 60/hour.
A fine-grained PAT with read-only `contents` raises that to 5,000. Add
`pull_requests: write` if you want `--post` to work.

## Using it

```bash
python -m app.cli <url>                    # print a review
python -m app.cli <url> --post             # publish it to the PR
python -m app.cli <url> --json             # machine-readable, for CI
python -m app.cli <url> --metrics          # include the metrics block
python -m app.cli <url> --provider ollama  # override the backend
python -m app.cli <url> --no-context       # diffs only, for a token-cost comparison
python -m app.cli <url> --fail-on any      # exit 1 on any finding, not just critical
```

Exit codes: `0` clean · `1` findings at the `--fail-on` threshold · `2` bad
config or arguments · `3` GitHub or LLM failure · `4` unexpected.

## Running as a GitHub App

1. Create a GitHub App with **Repository permissions**: `Contents: read`,
   `Pull requests: read & write`, and subscribe to the **Pull request** event.
2. Set the webhook URL to `https://your-host/webhook` and a webhook secret.
3. Put `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_PATH` (or `GITHUB_PRIVATE_KEY`) and
   `GITHUB_WEBHOOK_SECRET` in `.env`.
4. `docker compose up`.

The receiver verifies the HMAC-SHA256 signature in constant time, refuses to
start without a secret, ignores drafts and bot-authored PRs, enqueues, and
returns `202` — all before doing any real work, because GitHub marks a slow
delivery as failed and redelivers it.

The worker holds a reservation on `(pr, head_sha)`, so a redelivery of the same
commit is dropped while a new commit on the same PR is a genuinely new job. Jobs
move through a Redis processing list rather than a bare `BRPOP`, so a worker
that dies mid-review doesn't silently lose it — the next worker to start
requeues the orphan. A failed review *releases* its reservation, so a transient
provider outage doesn't permanently poison a PR — but only `MAX_ATTEMPTS` times,
after which it stops being retried and lands in a dead-letter list.

Without Redis the queue degrades to an in-process one with a loud warning, so
`make worker` works on a laptop with no infrastructure at all.

## Observability

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to trace token spend,
latency and per-review metrics; `drop_rate` is also emitted as a Langfuse score
so it can be charted and alerted on.

Set `LANGFUSE_HOST` to the region the project actually lives in — `cloud`,
`us.cloud` or `jp.cloud` — because a key from one region returns a flat 401 on
the others, with `Invalid credentials` and no hint that the keys are fine and
only the host is wrong.

Tracing is optional in both directions — missing package, missing keys, or a
Langfuse outage all degrade to a no-op, and every metric is still written to the
structured log. The drop-rate number is too useful to make contingent on a SaaS
being reachable.

```
review_complete {"pr": "tejaskumbhar288/financial-doc-agent#23",
  "model": "gemini-3.6-flash", "files_reviewed": 17, "context_chars": 60653,
  "static_findings": 60, "prompt_tokens": 45368, "completion_tokens": 1944,
  "llm_latency_ms": 32353.6, "proposed_findings": 5, "kept_findings": 5,
  "dropped_findings": 0, "drop_rate": 0.0, "critical": 1, ...}
```

`drop_rate` is `dropped / proposed`: the share of findings the model produced
that pointed at a line it invented. It is the closest thing to a direct
hallucination measurement this system can take.

## Evals

```bash
make eval                    # offline, deterministic, ~4 cases
make eval-baseline           # record the current numbers
# ...change the prompt...
make eval-diff               # did it actually get better?
```

The suite ships four hand-written fixtures with planted bugs — an off-by-one
slice, a SQL injection, a resource leak with a swallowed exception, and a clean
refactor that must produce *no* findings. They run with no network access, so
they can gate a prompt change in CI:

```bash
python -m app.evals --fixtures-only --min-detection 0.6 --max-drop-rate 0.2
```

Live cases against real PRs use the same scoring and live in
`evals/cases.live.json`, which is committed so the claims in it can be checked.
A wrong expected line number scores a correct review as a miss and sends you
optimising in the wrong direction, so none of them are guessed:

- Every expectation was raised by a **maintainer of the project** in review, then
  re-verified by reading the file at the PR's head SHA. A reviewer's line number
  goes stale the moment the author pushes again, and three otherwise-good
  candidates were dropped for exactly that reason - the author had already
  revised the code, so the reported bug was no longer in the final diff.
- The PRs are **closed unmerged** on purpose. A merged PR has usually had its bug
  fixed before merge, so the defect is gone from the diff; a rejected one keeps
  it forever, which is what makes the case reproducible. The catch is that GitHub
  cannot produce a diff at all once the contributor deletes their fork, so each
  PR was checked to still return files.
- One case is **clean by construction** - a two-line typo fix in a deprecation
  warning. It expects nothing, so every finding on it is a false positive. Without
  a case like that the suite only measures detection and never noise.

The committed baseline (`evals/baseline.json`, `gemini-3.6-flash`):

```
case                         kind         found  noise    drop
off-by-one-slice             fixture     1/2         0     0%
sql-injection                fixture     1/1         0     0%
resource-leak                fixture     3/3         0     0%
clean-refactor               fixture     0/0         0     0%
detection 83% (5/6; 2 by model, 3 by linter)  noise/case 0.0  drop rate 0.0%
```

Detection is attributed by source, because a bug ruff catches is a bug the
system reports - the prompt tells the model not to duplicate linter output, so
scoring model findings alone marks correct silence as failure and sends you
tuning the prompt to re-derive what a linter produces in 50ms.

The one standing miss is `pagination.py:9`: empty input drives `total_pages` to
0, the page clamps to 0, and the start index goes negative. No linter catches
it - it needs reasoning across two functions, which is exactly what the model is
there for. That is a real signal about prompt quality, and it is legible only
because the three false misses were removed.

Three numbers move together and have to be read together:

| metric | means | good direction |
|---|---|---|
| `detection` | share of known bugs found | up |
| `noise_per_case` | findings matching no known bug | down |
| `drop_rate` | findings that failed anchor validation | down |

`--baseline` explicitly flags the ambiguous case where detection and drop rate
both rise, because that is a prompt that got louder rather than better.

## Layout

```
app/
  config.py             env-driven settings, validated with actionable errors
  cli.py                terminal entrypoint
  pipeline.py           shared wiring for the CLI and the worker
  llm/
    base.py             LLMProvider interface, backoff, JSON coercion
    gemini.py           REST generateContent, native JSON schema mode
    ollama.py           local /api/chat
    factory.py          name -> provider
  github/
    patch.py            unified-diff parser, line anchoring, anchor snapping
    client.py           PR metadata, file diffs, raw contents, retries
    publisher.py        posts the review, degrades instead of losing it
    auth.py             GitHub App JWT -> cached installation tokens
  review/
    prompt.py           system prompt + context assembly
    analysis.py         ruff/semgrep pre-pass -> "known issues"
    engine.py           orchestration, validation, metrics
  server/
    security.py         HMAC-SHA256 webhook verification
    app.py              FastAPI receiver, enqueue-and-return
    queue.py            Redis queue, idempotency, dead-letter list
    worker.py           async review worker
    dlq.py              read the dead-letter list, and drain it
  obs/
    metrics.py          what a review is worth measuring by
    tracing.py          Langfuse, or a no-op that still logs
  evals/
    cases.py            case + expected-bug definitions
    harness.py          scoring, reporting, baseline comparison
evals/
  cases.json            the offline suite
  fixtures/             hand-written diffs with planted bugs
tests/                  226 offline tests + 25 Redis integration tests
```

## Design notes

**Anchor snapping.** Models are reliable about *which* code is wrong and
occasionally off by one about where it starts — typically pointing at a
function's `def` when the bug is in its first statement. `nearest_anchor()`
snaps within three lines, preferring added lines over context, and records
`snapped_from` so the rescue is visible rather than silent. Anything further
away is a real hallucination and stays dropped.

**Degrading instead of failing.** GitHub 422s the *entire* review request if one
inline comment points outside the diff. Validation makes that rare; the
publisher makes it non-fatal by falling back to a summary-only review with the
findings in the body, and says so in the output.

**Never approve, rarely block.** `REVIEW_EVENT=APPROVE` is downgraded to
`COMMENT` whenever findings exist, and `REQUEST_CHANGES` only escalates when
something is actually critical. A bot that blocks a merge on a hallucination
gets uninstalled.

**`--config auto` is a trap.** Semgrep refuses `auto` unless metrics are on,
because auto asks the registry which rules to run. The pre-pass hardcodes
`--metrics off` - phoning home about proprietary code is the exact thing the
local path exists to avoid - so the two cannot both be true. The shipped default
was `auto`, every semgrep run failed with `Cannot create auto config when
metrics are off`, the error was swallowed as a degraded tool, and the pre-pass
quietly returned ruff findings only. The default is now `p/default`, and a test
asserts it is not `auto`. Semgrep itself stays out of `requirements-dev.txt` -
it is a large install for a pre-pass that already works on ruff alone - so
`pip install semgrep` is what turns it on, and the pre-pass picks it up off PATH
without any further configuration.

**A score has to point at something.** Langfuse rejects a score that
references no trace, session, dataset run or observation — the whole ingestion
batch comes back `400`, and because the SDK reports that from its background
thread the review itself carries on looking healthy. `record()` ran after the
review span had closed and passed no trace id, so the `drop_rate` score this
README advertises was never written once. The trace id now rides a `ContextVar`
from the span to the record call: a `ContextVar` and not an attribute because
the worker runs concurrent reviews over one shared tracer, and single-use
because the no-reviewable-files path records without ever opening a span and
would otherwise be scored against the previous review's trace.

One cosmetic thing is unfixed: Langfuse names the trace after the metrics event
rather than the root `pr-review` span, so the trace list reads `review_metrics`.
Write order does not change it and this SDK version exposes no `update_trace`.
The trace contents are correct — span, generation and event correctly nested.

**Giving up visibly.** Releasing the reservation on failure is what keeps a
transient outage from poisoning a PR, and it is also how a permanently broken
job gets retried forever on every redelivery. So failures are counted per
`(pr, head_sha)` — on the key, not the job, because a redelivery arrives as a
fresh job whose counter would reset every time — and the job that exhausts its
budget is dead-lettered instead of handed back. Orphan recovery counts an
attempt too, or a job that hard-crashes its worker never reaches the failure
path at all and loops for as long as the service runs. What died is on
`/readyz` as a `dead` count and in `make dlq`, which can requeue it once the
cause is fixed.

```bash
make dlq                      # what died, why, how long ago
make dlq ARGS=--requeue       # put it all back
```

**Bounded `.env` discovery.** python-dotenv's default walks up until it finds a
`.env`, which can reach `$HOME` and silently load an unrelated file. This looks
in the working directory and the project root, and nowhere else.

## Status

Verified end to end against a real PR, a real model and real infrastructure:

| path | how it was verified |
|---|---|
| CLI review | real PR, 17 files, 60KB context, 5 findings, drop rate 0.0 |
| Publishing | review posted, `COMMENTED`, inline comment anchored on the RIGHT side |
| Idempotency | re-run skipped in 1.9s with no model call and no duplicate review |
| GitHub read path | real pagination, real diffs, 46 anchors parsed from a live patch |
| Webhook -> worker | signed delivery -> 202 -> Redis -> worker -> graceful failure |
| Redis queue | real server: `SET NX` under a 25-way race, orphan recovery |
| Dead-letter queue | real server: fail -> dead-letter -> `make dlq` -> requeue |
| Docker | image builds, container serves, degrades without Redis |
| Eval suite | 83% detection, 0 noise, 0.0 drop rate against `gemini-3.6-flash` |
| Ollama | full eval suite on a local `llama3.2:3b`: 50% detection, drop rate 0.0 |
| semgrep | real binary, real rules: 2 findings on the SQL-injection fixture |
| Langfuse | real project: span + generation + event nested, `drop_rate` scored |

Not yet exercised against reality: the GitHub App token exchange, which needs an
App to exist.

## Testing

```bash
make test                                              # 226 offline tests
REDIS_TEST_URL=redis://localhost:6379/0 make test      # + 25 against real Redis
```

The offline suite needs no network and no services. The Redis suite covers what
only a real server exercises — the `BRPOPLPUSH` handoff, `SET NX` under a
concurrent webhook storm, and orphan recovery after a worker dies mid-review.

CI runs lint, the offline suite on 3.11 and 3.13, the Redis suite against a
service container, and a Docker build. The eval gate needs a model key, which a
fork's PR is never given, so it is guarded rather than left to fail: set
`GEMINI_API_KEY` as a repository secret to turn it on.

## Roadmap

All six planned steps are implemented, plus the dead-letter queue. What's next:

1. **Incremental review.** Review only the commits added since the last review
   of the same PR, instead of the whole diff again on every push.
2. **Repo-aware context.** Pull the callers of a changed function, not just the
   file it lives in. The retrieval question this opens is the interesting part.
3. **Comment resolution.** Track which findings the author addressed, and use
   that as a real-world precision signal to feed back into the evals.

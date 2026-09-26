# Progress — 25 August 2026

The 23 August note left nine open items. Eight are done. The ninth needs a
credit card, not code.

The theme this time was not new bugs of the same family — it was that **closing
three of yesterday's items immediately produced a new instance of yesterday's
pattern**, and the eval suite caught it within the hour.

## Shipped

### The roadmap is finished

All three follow-ups from the README are implemented, tested and documented.

**Incremental review.** A re-push now reviews only the commits added since our
last review of the same PR, found through the SHA in our own review marker. The
narrowing is used *only* when GitHub reports the head is a fast-forward of that
commit: after a rebase the diff between the two mixes new work with rewritten
history, and reviewing that as an increment would report the author's rebase as
changes. No prior review, a failed compare, or a deleted base commit all fall
back to the whole diff, so the worst case is exactly the old behaviour.

The scope is stated in the posted review *above the summary*, because a reader
who does not know a review covered two commits will read "no blocking issues
found" as a verdict on the whole PR. New commits touching nothing reviewable — a
merge from main, a lockfile bump — post nothing and spend no model request.

**Repo-aware context.** `review/repo_context.py` finds the callers of what
changed: it parses the touched files, works out which definitions the diff hit,
and ranks a changed *signature* above a changed body — a new function has no
callers to break, while a new signature on an existing name is exactly the
change that breaks them. Call sites come back as numbered windows, not whole
files.

Lexical search, not embeddings, and that is the interesting part: a call site is
found by an exact identifier, which is the one query lexical search answers
perfectly and vector search answers fuzzily. No index to build, nothing to keep
in sync with the branch, no way for the retrieval to be quietly stale.

It is **off by default**, which is a budget decision and not a quality one:
GitHub code search is 30 requests/minute across the whole account, so a busy
webhook install would starve its own queue. `--repo-context` turns it on per
review.

Verified against the real API on `encode/httpx`, and the first run returned
**zero callers** — which is where the two fixes came from. The search was led by
four markdown files, and the one code file it did return was `__init__.py`,
where the only match is the string `"AsyncClient"` in an `__all__` list. An
export manifest is not a caller: it says nothing about how the changed code is
used, while spending a slot from the budget a real call site needed. Findings
now require a real *use* (`name(`, `name.`, `name[`) rather than a mention, and
the query carries `language:python`, which is exactly correct because symbols
are only ever extracted from Python files. Same PR, after: **10 call sites
across 4 files**, all genuine instantiations.

**Comment resolution.** `python -m app.review.resolution <url>` reports what
happened to every comment the bot posted: `addressed` (the anchored line has
changed since), `acknowledged` (a reply or positive reaction), `disputed` (a 👎),
`open`.

The headline number **ignores `open` entirely** and reports only the ratio among
comments that drew a response, because silence is not rejection — most open
comments are on PRs nobody has revisited. It returns `None`, not 100%, when
nothing has responded yet. A weak signal read honestly beats a strong one read
wrongly.

### A clean baseline, at last

`evals/baseline.json` was re-recorded after installing semgrep — in that order,
because semgrep shifts detections toward "by linter" and invalidates the old
numbers.

**4 cases, 0 errors, detection 100% (6/6 — 3 by model, 3 by linter), noise/case
0.0, drop rate 0.0%.**

It took two attempts, and the first one is the interesting one. See below.

### The live suite completed for the first time

`evals/baseline.live.json`. Both cases that had **never** finished — `httpx#453`
and `urllib3#3647` — ran to completion.

**4 real PRs, 0 errors: detection 75% (3/4), noise/case 0.0, drop rate 0.0%.**

- `httpx#453` — found. Previously unknown whether it could be.
- `httpx#647` — 2/2.
- `urllib3#3647` — completed, and **missed** its bug: the `amt is None` path
  prepends `_decoded_buffer` regardless of `decode_content`, unlike the `else`
  branch which raises. Recorded as a miss rather than re-rolled.
- `urllib3#2838`, clean by construction — correctly silent. Zero false
  positives, which is the number the suite exists to protect.

### Static-analysis noise is gone

Of 14 findings on the first real review this bot posted, ten were `S101 Use of
assert detected` on test files. `assert` is how pytest works.

The pre-pass runs `ruff --isolated` deliberately — the PR's own config is not
available to us — but the consequence was not deliberate: every ignore a project
sets under `tests/` is invisible, so those findings arrive at full volume.

There is now a suppression list scoped to test paths, and the entry criterion
matters more than the list: a rule qualifies only if it is *structurally*
inapplicable to test code, not merely noisy there. Suppressing a rule that could
still catch a real bug in a test trades noise for silence, which is the worse
failure and the harder one to notice. The count is logged rather than swallowed.

On the first live run afterwards it removed **6 findings from one real PR and 2
from another**.

### Retry budgets, split — and then fixed properly

Rate limits and sampling failures now have separate budgets. Waiting out a rate
limit is free, so it keeps the full five attempts; a re-roll spends another
request from a 20/day cap, so it gets three.

**Then the eval suite immediately proved the split was not enough.** A fixture
whose entire file is 400 characters burned two attempts producing `answer=30893`
and then `answer=31638` tokens. That is not truncation — that is a decode stuck
in a loop, and it stayed stuck because **reviews run at temperature 0.2, where
decoding is nearly deterministic**. Re-sending the identical request was the
least likely thing in the system to change the outcome. The retry was
implemented as "try again" when it needed to be "sample again".

Each re-roll now raises the temperature (0.2 → 0.55 → 0.9, capped), leaving the
first attempt — the one that succeeds almost always — where a review wants it.
On the next clean run the same fixture truncated twice more and **succeeded on
the third, highest-temperature attempt**. At the previous budget it would have
failed again.

This is the same bug as the quota-burning retry loop from 23 August, seen from
the other side: retrying cost quota, so the budget was cut — and cutting the
budget without making the retries *different* just meant failing sooner.

The same "extract after the retry loop" shape was still present in the Ollama
provider, where an empty local response failed the whole review with no retry
possible. Fixed to match Gemini.

### Langfuse names its traces correctly

Traces read `review_metrics` instead of `pr-review`, because the metrics event
is written after the review span closes. `update_trace` does not exist in this
SDK line, which is where the last investigation stopped.

The supported route is `propagate_attributes(trace_name=...)`, which writes
`langfuse.trace.name` onto every span opened inside it. Both the review span and
the metrics event are now opened inside one, so whichever the backend reads,
they agree. Verified against the real SDK with an in-memory exporter, not a
fake — the fix depends on the SDK actually setting that attribute, which only
the SDK can confirm. That assertion is now a test.

### Deployment, as far as it can go without an account

The image was built and run: it imports the app, resolves ruff, and answers
`/healthz` with a 200. `scripts/deploy-fly.sh` does everything after
`fly auth login` — including one-lining the PEM (the inline form is the only one
Fly can accept) and the `PATCH /app/hook/config` webhook repoint, which is done
in the script rather than by hand because that secret has three copies that must
agree and a rotation updating two of them fails closed.

One discrepancy is now stated out loud instead of inferred: the image ships ruff
but not semgrep — a heavy dependency for a 512MB VM — so a containerised review
is not identical to a local one. The worker logs which linters it can reach at
startup.

### The bugs are written up

`docs/bugs.md`. Eleven defects, of which nine are the same shape: **a guard that
could not fire in the case it was written for**, hidden because the system
degraded quietly instead of failing loudly. The retry loop that caused the
outage it existed to survive; a monotonic counter subtracted from wall clock; a
`finishReason` check that ran after the text was already accepted; a re-roll
that was not a new sample; and one found by a test written the same hour — an
`if not ours:` on a dictionary that always had keys, so the "no comments here"
explanation never appeared.

## Numbers

| | before | after |
|---|---|---|
| Offline eval | 83% detection, 1 errored case | **100% (6/6), 0 errors** |
| Live eval | 2 of 4 cases had never completed | **4/4 completed, 75% detection** |
| Tests | 250 | **326** offline + 25 Redis |
| Roadmap items open | 3 | **0** |

## Still open

**Blocked on an account, not on code.** Deploying to Fly (~$2/month) needs a Fly
account, `flyctl`, and a managed Redis URL. `scripts/deploy-fly.sh` refuses to
run with an instruction rather than a stack trace when any of the three is
missing. The current tunnel is ephemeral — when that terminal closes the URL
dies.

**Worth doing next.**

1. `urllib3#3647` is a real miss, now reproducible. It is the first live case
   where the reviewer completed and was wrong, which makes it the most useful
   prompt-tuning signal available — and the baseline exists to tell whether a
   change that fixes it costs anything elsewhere.
2. Repo-aware context retrieves correctly against a real repo, but has never
   been *scored*: no eval case yet turns it on, so whether those 10 call sites
   change a finding is unmeasured. That is the run worth doing next, and the
   baseline now exists to say whether it costs anything elsewhere. Note the
   limitation it exposed: code search indexes the default branch as it is
   today, so on an old PR the callers may have been renamed away — which is
   exactly what `httpx#453`'s `HTTP2Connection` had been.
3. Comment resolution has no data yet. It needs the bot to post reviews people
   actually respond to, which needs the deployment above.

# Guards that could not fire

Notes on the bugs found building this reviewer. They are collected because they
turned out to be one bug wearing eleven hats.

Almost every defect here is **a guard that could not fire in the case it was
written for**, and almost every one stayed hidden because the system *degraded
quietly instead of failing loudly*. The code was not missing. The check was
there, someone had thought about the failure, and the check was positioned,
ordered, or scoped so that the case it existed for was exactly the case it could
not see.

That is a nastier class of bug than a missing check, because every review of the
code finds the check and moves on.

---

## 1. The retry loop caused the outage it existed to survive

Gemini's free tier caps requests **per day, per project, per model** — 20/day at
the time of writing. Both the per-minute and the per-day limit arrive as `429
RESOURCE_EXHAUSTED`, and the code treated every 429 as transient.

So a per-day quota — which will not reset for hours — was retried five times
with exponential backoff. Each retry spent another request from a budget that
was already gone. Four eval cases at five retries each is twenty requests: the
retry logic burned the entire day's quota without a single call succeeding.

Google makes this specific mistake easy. The response body advertises a
`RetryInfo` of about 40 seconds even when the quota resets at midnight Pacific.
Believe it and you get a loop that is confident and wrong.

The fix reads `QuotaFailure.violations[].quotaId` and treats `PerDay` as
terminal. The general lesson is sharper than "classify your 429s":

> **A retry is only free when waiting is what the failure asks for.** If the
> failure consumed a finite resource, retrying spends more of it. Backoff
> assumes the cost of an attempt is time; when the cost is quota, backoff is an
> amplifier.

That reframing is what later separated *rate limits* from *sampling failures*
(§8) — the same distinction, found again in a different place.

---

## 2. The guard that could only fire when it was not needed

`_extract_text()` pulled the text out of the response, returned it if it was
non-empty, and *then* checked `finishReason`:

```python
text = "".join(part["text"] for part in parts)
if text:
    return text
if finish == "MAX_TOKENS":
    raise ...          # unreachable in every real truncation
```

A truncated response almost always carries partial text — that is what
truncation *is*. So the `MAX_TOKENS` branch could only run when the model
emitted nothing at all, which is the one case that is not truncation.

Users got `model did not return JSON`. True, and pointing at the wrong thing:
the JSON parser was the first component downstream of the real cause, so it got
the blame. Every minute spent debugging that message was spent in the wrong
file.

> **Check why you stopped before trusting what you got.** When a check runs
> after the value has already been accepted, it can only fire in the cases the
> value was empty — and those are rarely the cases you wrote it for.

---

## 3. A monotonic counter subtracted from wall clock

The worker logged:

```
reviewing acme/widget#7 (queued -1787342661.8s ago)
```

`time.perf_counter()` is a monotonic counter from an **arbitrary origin** — on
Linux, usually boot. `time.time()` is seconds since 1970. Subtracting one from
the other yields a number with no meaning, whose magnitude happens to be roughly
the age of the Unix epoch minus the machine's uptime.

Two lessons, and the second is the one that generalises:

1. Monotonic clocks compare only to themselves.
2. **The bug announced itself and the log line still shipped.** A negative
   duration is impossible; nothing was watching for impossible. The value is now
   clamped at zero *and* computed from the right clock — the clamp because the
   API and the worker need not share a clock at all, and a queue wait is never
   negative.

---

## 4. The default that varied by model

`maxOutputTokens` was never sent, so the cap came from each model's default.
The same review finished on one model and was cut off on another, and nothing in
either response said the cap was the difference.

It is now explicit, and the error breaks the spend down — which mattered more
than expected. On models that think before answering, reasoning is charged to
the same budget, and real measurements looked like `thinking=6745, answer=1432`
and `thinking=7860, answer=317` against an 8192 cap. Sizing that budget to the
length of a *review* is the wrong instinct: it has to cover the model's
reasoning about a diff it has never seen, which scales with the diff.

> **An unset parameter is not "the default" — it is "whatever the vendor
> chose today, per model."** Pin anything a comparison depends on.

---

## 5. The retry that could not reach the error

Truncation was made retryable. It still failed every time.

Extraction ran *after* `with_backoff()` returned, so by the time the truncation
was detected the retry loop had already exited successfully — it had, after all,
received a `200`. The error was raised in a place from which no retry was
reachable.

Extraction moved inside the retried call. Worth doing because truncation really
is sampling variance: the prompt that ran to `answer=26383` returned a complete
643-token review moments later.

> **Retryability is a property of where an error is raised, not of the error
> class.** An exception that cannot reach the retry loop is not retryable, no
> matter what it is called.

---

## 6. Two copies of a secret, and a third nobody counted

`GITHUB_WEBHOOK_SECRET` was rotated. Deliveries started returning 401.

The secret exists in **three** places that must agree: `.env`, the *running
container's* environment, and GitHub's App configuration. `.env` and GitHub had
been updated; the container was still running with the old value baked in at
start. Two deliveries were rejected before anyone noticed, because a signature
failure looks identical to an attack — which is exactly what it is designed to
look like.

It was found by hashing each copy and comparing, which is the only method that
works when you cannot print the values.

> **A rotation is not done until every copy is counted, and a running process
> is a copy.** Security checks fail closed and silently by design; that same
> design means a misconfiguration and an attack are indistinguishable from the
> logs.

---

## 7. A private key that the process could not read

The image runs as uid 10001. A bind mount carries host ownership through
unchanged. A key at mode `600` owned by the developer is therefore unreadable to
the process that needs it, and the worker died on `PermissionError`.

The tempting fix is `chmod 644` on a private key. That trades a real secret's
permissions for a deployment convenience, so the inline `GITHUB_PRIVATE_KEY`
form is used instead — which is also the only shape a platform like Fly or
Render can accept, since they inject secrets as environment variables and have
no filesystem to mount.

A second one fell out of it: a PEM header contains spaces, so unquoted in
`.env` it breaks `set -a; . ./.env` with a magnificently unhelpful
`RSA: command not found`.

---

## 8. The re-roll that was identical to the attempt that failed

*Found while completing the roadmap, and the sharpest of the set.*

Truncated and empty responses were made retryable — correct, and §5 fixed where
that retry happens. But a fixture whose entire file is 400 characters then
burned two attempts producing `answer=30893` and then `answer=31638` tokens,
truncating both times.

That is not truncation. That is a decode stuck in a loop. And it stayed stuck
because **reviews run at temperature 0.2**, where decoding is close to
deterministic — so re-sending the identical request was the *least* likely thing
in the whole system to produce a different outcome. The retry was implemented
as "try again", when what it needed to be was "sample again".

Each re-roll now raises the temperature (0.2 → 0.55 → 0.9, capped), leaving the
first attempt — the one that succeeds almost always — at the low temperature a
review wants. On the very next clean run, the fixture truncated twice more and
**succeeded on the third, highest-temperature attempt**. At the previous budget
of two it would have failed again.

> **"Retry" and "re-sample" are different operations.** If the first attempt was
> deterministic, a retry that changes nothing is a second copy of the failure,
> billed twice.

This is §1 again from the other side: retrying cost quota, so the budget was cut
— and cutting the budget without making the retries *different* just meant
failing sooner.

---

## 9. A lint pre-pass that could not see the project's own config

The static pre-pass runs `ruff --isolated`, deliberately: the PR's configuration
is not available to us, and most repos have none worth reading anyway.

The consequence was not deliberate. Of 14 findings on the first real review this
bot posted, **ten were `S101 Use of assert detected` on test files** — because
`assert` is how pytest works, and every Python project silences that rule under
`tests/`, in the config we cannot see. Ten of fourteen slots spent telling a
maintainer that their tests contain assertions is how a review bot gets muted in
its second week.

The fix is a small suppression list scoped to test paths, and the interesting
part is the criterion for entering it: a rule qualifies only if it is
*structurally inapplicable* to test code, not merely noisy there. Suppressing a
rule that could still catch a real bug in a test trades noise for silence, which
is the worse failure and the harder one to notice. On the first live run
afterwards it removed 6 findings from one real PR and 2 from another.

> **Choosing not to read the config is a choice about defaults, and defaults
> have to be re-derived, not inherited.**

A near-identical instance turned up the same day in the caller retrieval, which
is worth recording because it shows the shape is not specific to linters. Asked
for the callers of `AsyncClient` in `httpx`, code search returned four markdown
files and one `__init__.py` whose only match was the string `"AsyncClient"` in
an `__all__` list. Every result was a *mention*; none was a caller. The
retrieval reported "no callers found", which was true and useless. Requiring a
real use, and letting GitHub filter by language at the API instead of
discarding results afterwards, took the same query from 0 to 10 genuine call
sites. **A filter applied after the budget is spent is not a filter.**

---

## 10. A baseline that measured the wrong thing

Two failures that are invisible in the numbers themselves, because the numbers
look completely normal:

- A baseline recorded on one model, compared against a run on another. Free-tier
  caps are per-model, so *switching models is the normal way to keep working* —
  which makes this the common case, not the exotic one. The diff then reports
  "detection worse" for a prompt that never changed.
- A baseline recorded from a run that **errored**. Errored cases are excluded
  from every metric, so a run where one of four cases crashed records the
  detection rate of the three that survived, as though it described the suite.

Both now print a warning at comparison time. The second nearly shipped: a run
that hit the daily quota on the last of four cases wrote a baseline describing
three, and it was restored by hand rather than committed.

> **A metric that cannot describe its own sample will happily describe a
> different one.**

---

## 11. `if not ours:` on a dictionary that was never empty

*Found by a test written the same hour, which is the only reason it is in this
list rather than in production.*

The resolution tracker fetched our comments and returned them alongside some
bookkeeping:

```python
ours = {"mine": [], "_all": [...]}
if not ours:                       # never true: the dict has keys
    report.note = "no comments from this bot on that PR"
```

The guard was meant to catch "we have never commented on this PR". It tested the
wrapper, not the contents, and the wrapper always had keys. The report came back
empty with no explanation — which reads exactly like a PR where nothing has
happened yet.

Fixed by returning a plain tuple, so there is no container to accidentally test
the truthiness of.

> **Do not test a container for the emptiness of the thing inside it.** The
> intermediate structure existed for the function's convenience and had no
> opinion about the question being asked.

---

## The pattern

Nine of these eleven are the same shape:

| | the check | why it could not fire |
|---|---|---|
| §1 | retry on 429 | the failure was not the kind waiting fixes |
| §2 | `finishReason == MAX_TOKENS` | ran after the text was already accepted |
| §3 | queue-wait logging | nothing rejected an impossible value |
| §5 | retry on truncation | raised outside the retry loop |
| §6 | signature verification | fired correctly, on a copy nobody counted |
| §8 | re-roll on a bad sample | the re-roll was not a new sample |
| §9 | lint suppression | lived in a config file we chose not to read |
| §10 | baseline comparison | compared two things that were not comparable |
| §11 | empty-result note | tested the wrapper, not the contents |

And every one of them was survivable, which is why they lasted. The review still
finished. The worker still logged. The eval still printed a number. Degrading
quietly is a virtue in production and a liability in development, and the only
defence found so far is to **make the degradation say so out loud** — count the
suppressed findings, name the budget that ran out, warn when a baseline is not
comparable, log which scope a review actually covered.

The bugs were not hard to fix. They were hard to *see*, and they were hard to
see because the system was working as designed right up until someone asked it
a question it had quietly stopped being able to answer.

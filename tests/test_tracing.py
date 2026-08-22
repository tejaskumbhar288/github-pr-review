"""Langfuse tracing.

Everything here runs against a fake client: the point is the shape of the calls,
which is what a real Langfuse rejects when it is wrong.
"""

from __future__ import annotations

from app.obs.metrics import ReviewMetrics
from app.obs.tracing import LangfuseTracer, Tracer, build_tracer


class FakeSpan:
    def __init__(self, trace_id="tr-1", span_id="sp-1"):
        self.trace_id = trace_id
        self.id = span_id
        self.ended = False

    def start_observation(self, **kwargs):
        return FakeSpan(self.trace_id, "sp-child")

    def update(self, **kwargs):
        return None

    def end(self, **kwargs):
        self.ended = True


class FakeClient:
    def __init__(self, span=None):
        self.span = span if span is not None else FakeSpan()
        self.events: list[dict] = []
        self.scores: list[dict] = []

    def start_observation(self, **kwargs):
        return self.span

    def create_event(self, **kwargs):
        self.events.append(kwargs)

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        return None

    def shutdown(self):
        return None


def metrics(**kwargs) -> ReviewMetrics:
    m = ReviewMetrics(pr="acme/widget#1", head_sha="abc", provider="fake", model="m")
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def test_the_drop_rate_score_is_attached_to_the_review_trace():
    """Regression: the score was created with no trace_id, and Langfuse rejects
    the whole batch with a 400 - "Provide exactly one of the following: traceId
    (with optional observationId), sessionId ...". The drop_rate score the
    README advertises as chartable was never recorded at all."""
    client = FakeClient()
    tracer = LangfuseTracer(client)

    with tracer.review("pr-review", pr="acme/widget#1"):
        pass
    tracer.record(metrics(proposed_findings=4, dropped_findings=1))

    (score,) = client.scores
    assert score["trace_id"] == "tr-1"
    assert score["name"] == "drop_rate"
    assert score["value"] == 0.25


def test_the_metrics_event_is_filed_under_the_review_span():
    """A bare create_event opens a second, orphan trace instead of joining the
    review it describes."""
    client = FakeClient()
    tracer = LangfuseTracer(client)

    with tracer.review("pr-review"):
        pass
    tracer.record(metrics())

    (event,) = client.events
    assert event["trace_context"] == {"trace_id": "tr-1", "parent_span_id": "sp-1"}


def test_no_trace_means_no_score_rather_than_a_rejected_batch():
    """The no-reviewable-files path records metrics without ever opening a span.
    A score has to reference something, so there is nothing to score."""
    client = FakeClient()
    tracer = LangfuseTracer(client)

    tracer.record(metrics())

    assert client.scores == []
    # The event still goes, because it does not need a trace to be valid.
    assert client.events and client.events[0]["trace_context"] is None


def test_concurrent_reviews_do_not_take_each_other_s_trace_id():
    """The worker runs reviews concurrently over one shared tracer. Each review
    is its own asyncio task with its own context, so the trace id cannot leak
    between them - which an instance attribute would not give us."""
    import asyncio

    client = FakeClient()

    async def review(trace_id: str) -> None:
        tracer = LangfuseTracer(client)
        client.span = FakeSpan(trace_id, f"sp-{trace_id}")
        with tracer.review("pr-review"):
            await asyncio.sleep(0)
        tracer.record(metrics())

    async def main() -> None:
        await asyncio.gather(review("tr-a"), review("tr-b"))

    asyncio.run(main())
    assert {s["trace_id"] for s in client.scores} == {"tr-a", "tr-b"}


def test_a_tracing_failure_never_breaks_a_review():
    class Broken(FakeClient):
        def create_score(self, **kwargs):
            raise RuntimeError("langfuse is down")

        def create_event(self, **kwargs):
            raise RuntimeError("langfuse is down")

    tracer = LangfuseTracer(Broken())
    with tracer.review("pr-review"):
        pass
    tracer.record(metrics())  # must not raise


def test_no_keys_means_the_no_op_tracer(settings):
    tracer = build_tracer(settings.with_overrides(langfuse_public_key=None))
    assert type(tracer) is Tracer and not tracer.enabled


def test_a_stale_trace_id_is_not_reused_by_the_next_review():
    """Within one worker task, review 2 can take the no-reviewable-files path
    and record without ever opening a span. Its metrics must not be filed
    against review 1's trace."""
    client = FakeClient()
    tracer = LangfuseTracer(client)

    with tracer.review("pr-review"):
        pass
    tracer.record(metrics())
    tracer.record(metrics())  # the next review, which opened no span

    assert len(client.scores) == 1, "only the review that had a trace gets scored"
    assert client.events[1]["trace_context"] is None

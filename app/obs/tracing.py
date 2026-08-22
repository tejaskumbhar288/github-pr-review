"""Tracing for reviews (roadmap 4).

Langfuse is optional in both directions: the package may be absent, and the
keys may be unset. Either way reviews still run and metrics are still recorded
to the structured log, because the drop-rate metric is too useful to make
contingent on a SaaS being configured. Tracing failures are never allowed to
fail a review.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from .metrics import ReviewMetrics

log = logging.getLogger("review.metrics")


class Tracer:
    """No-op base. Also the fallback when Langfuse is unavailable."""

    enabled = False

    @contextmanager
    def review(self, name: str, **metadata: Any) -> Iterator[Span]:
        yield Span()

    def record(self, metrics: ReviewMetrics) -> None:
        # Structured, greppable, and free. This is the floor, not the ceiling.
        log.info("review_complete %s", json.dumps(metrics.as_dict(), default=str))

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        self.flush()


class Span:
    """No-op span handle."""

    def generation(self, **kwargs: Any) -> Span:
        return self

    def update(self, **kwargs: Any) -> None:
        return None

    def end(self, **kwargs: Any) -> None:
        return None


class LangfuseTracer(Tracer):
    enabled = True

    def __init__(self, client: Any):
        self._client = client

    @contextmanager
    def review(self, name: str, **metadata: Any) -> Iterator[Span]:
        span: Any = None
        try:
            span = self._client.start_observation(name=name, as_type="span", metadata=metadata)
        except Exception as exc:  # noqa: BLE001 - tracing must never break a review
            log.debug("langfuse span failed: %s", exc)

        handle = _LangfuseSpan(span, self._client) if span is not None else Span()
        try:
            yield handle
        except Exception as exc:
            handle.update(level="ERROR", status_message=str(exc)[:500])
            handle.end()
            raise
        else:
            handle.end()

    def record(self, metrics: ReviewMetrics) -> None:
        super().record(metrics)
        payload = metrics.as_dict()
        try:
            self._client.create_event(name="review_metrics", metadata=payload)
            # Scores make drop-rate chartable and alertable in the Langfuse UI.
            self._client.create_score(
                name="drop_rate",
                value=metrics.drop_rate,
                comment=f"{metrics.dropped_findings}/{metrics.proposed_findings} unanchorable",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse record failed: %s", exc)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse flush failed: %s", exc)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception:  # noqa: BLE001
            self.flush()


class _LangfuseSpan(Span):
    def __init__(self, span: Any, client: Any):
        self._span = span
        self._client = client

    def generation(self, **kwargs: Any) -> Span:
        try:
            child = self._span.start_observation(as_type="generation", **kwargs)
            return _LangfuseSpan(child, self._client)
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse generation failed: %s", exc)
            return Span()

    def update(self, **kwargs: Any) -> None:
        try:
            self._span.update(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse update failed: %s", exc)

    def end(self, **kwargs: Any) -> None:
        try:
            self._span.end(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse end failed: %s", exc)


def build_tracer(settings: Settings) -> Tracer:
    if not settings.tracing_enabled:
        return Tracer()
    try:
        from langfuse import Langfuse
    except ImportError:
        log.info("langfuse keys set but the package is not installed; tracing disabled")
        return Tracer()
    try:
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse init failed, continuing without tracing: %s", exc)
        return Tracer()
    return LangfuseTracer(client)

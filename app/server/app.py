"""GitHub App webhook receiver (roadmap 2).

The contract with GitHub is: verify the signature, decide fast, return 2xx in
seconds. Anything slower gets the delivery marked as failed and redelivered, so
this endpoint does no work beyond validation and an enqueue - the review itself
belongs to the worker.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse

from ..config import ConfigError, Settings, get_settings
from ..obs.logging import setup_logging
from .queue import JobQueue, ReviewJob, build_queue
from .security import SignatureError, verify_signature

log = logging.getLogger(__name__)

# The only pull_request actions that mean "there is new code to look at".
REVIEWABLE_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    setup_logging(settings.log_level)
    app.state.queue = await build_queue(settings)
    log.info("webhook service ready (queue=%s)", getattr(app.state.queue, "name", "?"))
    try:
        yield
    finally:
        await app.state.queue.close()


def create_app(settings: Settings | None = None, queue: JobQueue | None = None) -> FastAPI:
    settings = settings or get_settings()

    # Refuse to boot without a secret. Without this the service starts happily
    # and rejects every delivery as unsigned - fail-safe, but indistinguishable
    # from a broken tunnel or a wrong URL, and it is reached through
    # `uvicorn --factory`, which never runs main()'s validation.
    if not settings.github_webhook_secret:
        raise ConfigError(
            "GITHUB_WEBHOOK_SECRET is required to run the webhook service. Set it to "
            "the same value configured on the GitHub App, or run the CLI instead."
        )

    app = FastAPI(
        title="AI Code Review",
        version="0.2.0",
        lifespan=lifespan if queue is None else _noop_lifespan,
    )
    app.state.settings = settings
    if queue is not None:
        app.state.queue = queue

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        q = getattr(app.state, "queue", None)
        if q is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        try:
            depth = await q.depth()
            # Surfaced here because a dead letter that only exists in a log line
            # is not "visible" in any sense an on-call person can act on.
            dead = await q.dead_depth()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)
        return JSONResponse(
            {"status": "ok", "queue": getattr(q, "name", "?"), "depth": depth, "dead": dead}
        )

    @app.post("/webhook")
    async def webhook(
        request: Request,
        response: Response,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str = Header(default=""),
    ) -> dict[str, Any]:
        body = await request.body()

        try:
            verify_signature(body, x_hub_signature_256, settings.github_webhook_secret)
        except SignatureError as exc:
            # Deliberately terse: don't tell an unauthenticated caller which
            # part of their forgery was wrong.
            log.warning("rejected delivery %s: %s", x_github_delivery, exc)
            return JSONResponse({"detail": "invalid signature"}, status_code=401)

        if x_github_event == "ping":
            return {"status": "pong"}

        if x_github_event != "pull_request":
            return {"status": "ignored", "reason": f"event {x_github_event!r}"}

        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "malformed JSON"}, status_code=400)

        decision = _decide(payload)
        if decision is not None:
            return {"status": "ignored", "reason": decision}

        job = _job_from_payload(payload, delivery_id=x_github_delivery)
        accepted = await app.state.queue.enqueue(job)
        response.status_code = status.HTTP_202_ACCEPTED
        if not accepted:
            return {"status": "duplicate", "key": job.dedupe_key}
        log.info("queued %s (delivery %s)", job.dedupe_key, x_github_delivery or "-")
        return {"status": "queued", "key": job.dedupe_key, "job_id": job.job_id}

    return app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    """Used when a queue is injected (tests); the caller owns its lifecycle."""
    yield


def _decide(payload: dict[str, Any]) -> str | None:
    """Return a reason to ignore this delivery, or None to review it."""
    action = payload.get("action", "")
    if action not in REVIEWABLE_ACTIONS:
        return f"action {action!r}"

    pr = payload.get("pull_request") or {}
    if not pr:
        return "no pull_request in payload"
    # "ready_for_review" is precisely the moment a draft stops being a draft, so
    # the draft flag in that payload is already False - but check anyway.
    if pr.get("draft") and action != "ready_for_review":
        return "draft"
    if (pr.get("user") or {}).get("type") == "Bot":
        return "authored by a bot"
    return None


def _job_from_payload(payload: dict[str, Any], *, delivery_id: str = "") -> ReviewJob:
    pr = payload["pull_request"]
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name") or ""
    owner, _, name = full_name.partition("/")
    return ReviewJob(
        owner=owner or (repo.get("owner") or {}).get("login", ""),
        repo=name or repo.get("name", ""),
        number=int(pr["number"]),
        head_sha=(pr.get("head") or {}).get("sha", ""),
        installation_id=(payload.get("installation") or {}).get("id"),
        delivery_id=delivery_id,
        action=payload.get("action", ""),
    )


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        settings.validate_for_webhook()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)


app_factory = create_app

if __name__ == "__main__":  # pragma: no cover
    main()

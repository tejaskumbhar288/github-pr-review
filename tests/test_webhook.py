from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.server.app import create_app
from app.server.queue import MemoryQueue
from app.server.security import sign

SECRET = "test-secret"


@pytest.fixture
def queue():
    return MemoryQueue()


@pytest.fixture
def client(settings, queue):
    return TestClient(create_app(settings, queue=queue))


def post(client, body: bytes, *, event="pull_request", secret=SECRET, delivery="d1"):
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    if secret is not None:
        headers["X-Hub-Signature-256"] = sign(body, secret)
    return client.post("/webhook", content=body, headers=headers)


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready["status"] == "ok" and ready["queue"] == "memory"


def test_valid_delivery_is_queued_fast(client, webhook_body, queue):
    r = post(client, webhook_body)
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    assert r.json()["key"] == "acme/widget#7@head456"


def test_unsigned_delivery_is_rejected(client, webhook_body):
    r = post(client, webhook_body, secret=None)
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid signature"}


def test_wrong_secret_is_rejected(client, webhook_body):
    r = post(client, webhook_body, secret="not-the-secret")
    assert r.status_code == 401


def test_tampered_body_is_rejected(client, webhook_body):
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": sign(webhook_body, SECRET),
        "Content-Type": "application/json",
    }
    r = client.post("/webhook", content=webhook_body + b" ", headers=headers)
    assert r.status_code == 401


def test_redelivery_of_the_same_commit_is_deduped(client, webhook_body):
    assert post(client, webhook_body).json()["status"] == "queued"
    second = post(client, webhook_body, delivery="d2")
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"


def test_a_new_commit_on_the_same_pr_is_a_new_job(client, webhook_payload):
    first = json.dumps(webhook_payload).encode()
    assert post(client, first).json()["status"] == "queued"

    webhook_payload["action"] = "synchronize"
    webhook_payload["pull_request"]["head"]["sha"] = "newsha"
    second = json.dumps(webhook_payload).encode()
    assert post(client, second).json()["status"] == "queued"


def test_ping_is_answered(client):
    body = json.dumps({"zen": "hi"}).encode()
    r = post(client, body, event="ping")
    assert r.status_code == 200 and r.json() == {"status": "pong"}


def test_unrelated_events_are_ignored(client, webhook_body):
    r = post(client, webhook_body, event="issues")
    assert r.status_code == 200 and r.json()["status"] == "ignored"


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned", "edited"])
def test_uninteresting_actions_are_ignored(client, webhook_payload, action):
    webhook_payload["action"] = action
    r = post(client, json.dumps(webhook_payload).encode())
    assert r.json()["status"] == "ignored"


def test_drafts_are_ignored(client, webhook_payload):
    webhook_payload["pull_request"]["draft"] = True
    r = post(client, json.dumps(webhook_payload).encode())
    assert r.json() == {"status": "ignored", "reason": "draft"}


def test_ready_for_review_on_a_draft_is_still_reviewed(client, webhook_payload):
    webhook_payload["action"] = "ready_for_review"
    webhook_payload["pull_request"]["draft"] = True
    r = post(client, json.dumps(webhook_payload).encode())
    assert r.json()["status"] == "queued"


def test_bot_authored_prs_are_ignored(client, webhook_payload):
    webhook_payload["pull_request"]["user"]["type"] = "Bot"
    r = post(client, json.dumps(webhook_payload).encode())
    assert r.json()["reason"] == "authored by a bot"


def test_malformed_json_with_a_valid_signature_is_a_400(client):
    body = b"{not json"
    r = post(client, body)
    assert r.status_code == 400


async def test_installation_id_is_carried_onto_the_job(client, webhook_body, queue):
    post(client, webhook_body)
    job = await queue.dequeue(timeout=1)
    assert job.installation_id == 42
    assert job.delivery_id == "d1"
    assert job.action == "opened"


def test_service_refuses_to_boot_without_a_secret(settings):
    """A secret-less service starts fine and rejects every delivery as unsigned,
    which is indistinguishable from a broken tunnel. Fail at boot instead."""
    from app.config import ConfigError

    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET"):
        create_app(settings.with_overrides(github_webhook_secret=None))

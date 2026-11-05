import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from conftest import run_worker
from sqlalchemy import select

from app.clock import iso, now
from app.db import Account, AttemptObservation, DailyUsage, Transfer
from app.errors import DomainError
from app.service import create_transfer
from app.worker import Worker


def create(api, body, key):
    response = api.post("/transfers", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["transfer_id"]


def test_5xx_then_lagging_404_never_switches_provider(api, db, config, gateway, transfer_request):
    gateway.submit = lambda provider, reference, *_: (
        gateway.submissions.append((provider, reference)) or httpx.Response(500)
    )
    tid = create(api, transfer_request, "lagging-404")
    config.review_after_sec = 0
    worker = Worker(db, config, gateway)
    assert run_worker(worker, db, tid, ticks=5) == "NEEDS_REVIEW"
    assert {pid for pid, _ in gateway.submissions} == {"prov_alpha"}
    assert len({ref for _, ref in gateway.submissions}) == 1
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "10000.00"
    ref = gateway.submissions[0][1]
    gateway.payouts[ref] = {"payout_id": "po_late", "reference": ref, "status": "COMPLETED"}
    assert run_worker(worker, db, tid) == "COMPLETED"


def test_transient_failed_lookup_is_confirmed_before_fallback(
    api, db, config, gateway, transfer_request
):
    tid = create(api, transfer_request, "transient-failure")
    worker = Worker(db, config, gateway)
    worker.tick()
    ref = gateway.submissions[0][1]
    original_lookup = gateway.lookup
    gateway.lookup = lambda *_: httpx.Response(
        200, json={"reference": ref, "payout_id": "po_" + ref, "status": "FAILED"}
    )
    worker.tick()
    assert api.get(f"/transfers/{tid}").json()["attempts"][0]["result"] == "VERIFYING_FAILURE"
    gateway.lookup = original_lookup
    assert run_worker(worker, db, tid) == "COMPLETED"
    assert len(gateway.submissions) == 1


def test_conflicting_payout_id_preserves_hold(api, db, config, gateway, transfer_request):
    tid = create(api, transfer_request, "wrong-payout")
    worker = Worker(db, config, gateway)
    worker.tick()
    ref = gateway.submissions[0][1]
    gateway.lookup = lambda *_: httpx.Response(
        200, json={"reference": ref, "payout_id": "another-payout", "status": "COMPLETED"}
    )
    assert run_worker(worker, db, tid, ticks=2) == "NEEDS_REVIEW"
    result = api.get(f"/transfers/{tid}").json()
    assert result["review_reason"] == "CONFLICTING_PAYOUT_ID"
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "10000.00"
    assert len(gateway.submissions) == 1


def test_completed_webhook_against_failed_lookup_requires_review(
    api, db, config, gateway, transfer_request
):
    tid = create(api, transfer_request, "conflict")
    worker = Worker(db, config, gateway)
    worker.tick()
    ref = gateway.submissions[0][1]
    event = {
        "event_id": "completed-event",
        "payout_id": "po_" + ref,
        "reference": ref,
        "status": "COMPLETED",
        "occurred_at": iso(now()),
    }
    raw = json.dumps(event).encode()
    signature = hmac.new(config.provider_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    assert (
        api.post(
            "/webhooks/prov_alpha", content=raw, headers={"X-Signature": signature}
        ).status_code
        == 202
    )
    gateway.lookup = lambda *_: httpx.Response(
        200, json={"reference": ref, "payout_id": "po_" + ref, "status": "FAILED"}
    )
    assert run_worker(worker, db, tid, ticks=3) == "NEEDS_REVIEW"
    assert len(gateway.submissions) == 1
    assert api.get(f"/transfers/{tid}").json()["review_reason"] == "CONFLICTING_PROVIDER_STATUS"


def test_observations_survive_worker_recreation(api, db, config, gateway, transfer_request):
    tid = create(api, transfer_request, "observations")
    Worker(db, config, gateway).tick()
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"
    result = api.get(f"/transfers/{tid}").json()
    assert [o["source"] for o in result["attempts"][0]["observations"]] == ["submit", "lookup"]
    with db() as session:
        assert len(session.scalars(select(AttemptObservation)).all()) == 2


def test_old_lease_owner_cannot_submit_or_settle(api, db, config, gateway, transfer_request):
    tid = create(api, transfer_request, "stale-lease")
    worker = Worker(db, config, gateway)
    old = worker.claim()
    with db.begin() as session:
        session.get(Transfer, tid).lease_until = now() - timedelta(seconds=1)
    current = worker.claim()
    worker.process(*old)
    assert not gateway.submissions
    worker.process(*current)
    worker.apply(tid, old[1], gateway.submissions[0][1], "FAILED", "stale")
    assert run_worker(worker, db, tid) == "COMPLETED"
    assert len(gateway.submissions) == 1


def test_failure_releases_usage_from_creation_day(
    api, db, config, gateway, catalog, transfer_request
):
    previous_day = now() - timedelta(days=1)
    with db.begin() as session:
        transfer, _ = create_transfer(
            session, transfer_request, "midnight", catalog, "decline", previous_day
        )
        tid = transfer.id
    assert run_worker(Worker(db, config, gateway), db, tid) == "FAILED"
    with db() as session:
        assert session.get(DailyUsage, ("c_001", previous_day.date())).amount == 0
        assert session.get(DailyUsage, ("c_001", now().date())) is None
        assert session.get(Account, ("c_001", "RUB")).held == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(422, json=["invalid-decline"]),
        httpx.Response(201, json="invalid-payout"),
        httpx.Response(201, text="broken-json"),
    ],
)
def test_malformed_provider_reply_keeps_reserve_and_stable_key(
    api, db, config, gateway, transfer_request, response
):
    tid = create(api, transfer_request, "malformed")
    config.review_after_sec = 0

    def submit(provider, reference, *_):
        gateway.submissions.append((provider, reference))
        return response

    gateway.submit = submit
    assert run_worker(Worker(db, config, gateway), db, tid, ticks=3) == "NEEDS_REVIEW"
    assert len(set(gateway.submissions)) == 1
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "10000.00"


def test_invalid_config_after_hold_is_reviewable_and_recovers(
    api, db, config, gateway, transfer_request, monkeypatch
):
    tid = create(api, transfer_request, "invalid-live-config")

    def invalid(_):
        raise DomainError("CONFIG_INVALID", "Invalid config", 503)

    with monkeypatch.context() as patch:
        patch.setattr("app.worker.Catalog", invalid)
        assert Worker(db, config, gateway).tick()
    result = api.get(f"/transfers/{tid}").json()
    assert result["status"] == "NEEDS_REVIEW"
    assert result["review_reason"] == "CONFIG_INVALID"
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"

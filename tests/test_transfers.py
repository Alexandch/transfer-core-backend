import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from conftest import run_worker
from sqlalchemy import func, select

from app.clock import iso, now
from app.db import Account, Client, Quote, Transfer, WebhookEvent
from app.errors import DomainError
from app.service import create_transfer, seed
from app.worker import Worker


def test_quote_persists(api, quote_request):
    created = api.post("/quotes", json=quote_request)
    assert created.status_code == 201
    assert api.get("/quotes/" + created.json()["quote_id"]).json() == created.json()


@pytest.mark.parametrize("body", [{}, {"amount": 10000}])
def test_validation_contract(api, body):
    response = api.post("/quotes", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_mandatory_quote_errors(api, quote_request, gateway):
    cases = [
        ({"amount": "500.00"}, "AMOUNT_BELOW_MIN"),
        ({"amount": "300000.01"}, "AMOUNT_ABOVE_MAX"),
        ({"payout_method": "CASH"}, "UNSUPPORTED_DIRECTION"),
    ]
    for change, code in cases:
        response = api.post("/quotes", json={**quote_request, **change})
        assert response.json()["error"]["code"] == code
    gateway.rates = lambda: []
    assert api.post("/quotes", json=quote_request).json()["error"]["code"] == "RATE_UNAVAILABLE"
    assert api.get("/quotes/missing").json()["error"]["code"] == "QUOTE_NOT_FOUND"


def test_transfer_errors(api, db, transfer_request):
    assert (
        api.post("/transfers", json=transfer_request).json()["error"]["code"] == "VALIDATION_ERROR"
    )
    headers = {"Idempotency-Key": "errors"}
    missing = {**transfer_request, "quote_id": "missing"}
    assert (
        api.post("/transfers", json=missing, headers=headers).json()["error"]["code"]
        == "QUOTE_NOT_FOUND"
    )
    with db.begin() as session:
        quote = session.get(Quote, transfer_request["quote_id"])
        quote.expires_at = now() - timedelta(seconds=1)
    assert (
        api.post("/transfers", json=transfer_request, headers=headers).json()["error"]["code"]
        == "QUOTE_EXPIRED"
    )


@pytest.mark.parametrize(
    "scenario,expected",
    [("ok", "COMPLETED"), ("decline", "FAILED"), ("error_5xx", "FAILED"), ("slow", "COMPLETED")],
)
def test_scenarios_and_balance_conservation(
    api, db, config, gateway, transfer_request, scenario, expected
):
    response = api.post(
        "/transfers",
        json=transfer_request,
        headers={"Idempotency-Key": "scenario", "X-Scenario": scenario},
    )
    assert response.status_code == 202, response.text
    tid = response.json()["transfer_id"]
    balance = api.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert balance["held"] == "10000.00"
    assert balance["available"] == "490000.00"
    assert run_worker(Worker(db, config, gateway), db, tid) == expected
    balance = api.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert balance["held"] == "0.00"
    assert balance["spent"] == ("10000.00" if expected == "COMPLETED" else "0.00")
    assert sum(int(balance[k].replace(".", "")) for k in ("available", "held", "spent")) == 50000000
    attempts = api.get("/transfers/" + tid).json()["attempts"]
    assert len(attempts) == (1 if expected == "COMPLETED" else 3)


def test_fallback_and_margin_explanation(api, db, config, gateway, transfer_request):
    gateway.fail_first_provider = True
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "fallback"}
    ).json()["transfer_id"]
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"
    attempts = api.get("/transfers/" + tid).json()["attempts"]
    assert [a["provider_id"] for a in attempts] == ["prov_alpha", "prov_beta"]
    assert (
        api.get(f"/transfers/{tid}/routing").json()["candidates"][0]["provider_id"] == "prov_alpha"
    )


def test_idempotency_before_during_after_and_conflict(api, db, config, gateway, transfer_request):
    headers = {"Idempotency-Key": "repeat"}
    original = api.post("/transfers", json=transfer_request, headers=headers).json()
    tid = original["transfer_id"]
    assert api.post("/transfers", json=transfer_request, headers=headers).json() == original
    worker = Worker(db, config, gateway)
    worker.tick()
    assert (
        api.post("/transfers", json=transfer_request, headers=headers).json()["transfer_id"] == tid
    )
    assert run_worker(worker, db, tid) == "COMPLETED"
    with db.begin() as session:
        session.get(Quote, transfer_request["quote_id"]).expires_at = now() - timedelta(days=1)
    repeated = api.post("/transfers", json=transfer_request, headers=headers)
    assert repeated.json()["status"] == "COMPLETED"
    conflict = {
        **transfer_request,
        "recipient": {**transfer_request["recipient"], "account": "other"},
    }
    assert api.post("/transfers", json=conflict, headers=headers).status_code == 409
    assert (
        api.post(
            "/transfers", json=transfer_request, headers={"Idempotency-Key": "another"}
        ).json()["error"]["code"]
        == "QUOTE_ALREADY_USED"
    )


def test_concurrent_same_key_creates_one_hold(db, api, catalog, transfer_request):
    def submit(_):
        with db.begin() as session:
            transfer, _ = create_transfer(
                session, transfer_request, "parallel", catalog, None, now()
            )
            return transfer.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(submit, range(16)))
    assert len(set(ids)) == 1
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Transfer)) == 1
        assert session.get(Account, ("c_001", "RUB")).held == 1000000


@pytest.mark.parametrize(
    "funds,limit,expected",
    [(1500000, 30000000, "INSUFFICIENT_FUNDS"), (50000000, 1500000, "DAILY_LIMIT_EXCEEDED")],
)
def test_parallel_balance_and_daily_limit(
    db, api, catalog, quote_request, transfer_request, funds, limit, expected
):
    second = api.post("/quotes", json=quote_request).json()["quote_id"]
    with db.begin() as session:
        account = session.get(Account, ("c_001", "RUB"))
        account.available = account.initial = funds
        session.get(Client, "c_001").daily_limit = limit
    requests = [transfer_request, {**transfer_request, "quote_id": second}]

    def submit(index):
        try:
            with db.begin() as session:
                create_transfer(session, requests[index], f"parallel-{index}", catalog, None, now())
            return "ok"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == sorted(["ok", expected])


@pytest.mark.parametrize("mode", ["disconnect_before", "disconnect_after", "after_commit_5xx"])
def test_ambiguous_outcome_never_double_pays(api, db, config, gateway, transfer_request, mode):
    gateway.mode = mode
    tid = api.post("/transfers", json=transfer_request, headers={"Idempotency-Key": mode}).json()[
        "transfer_id"
    ]
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"
    assert len(gateway.payouts) == 1
    assert {p for p, _ in gateway.submissions} == {"prov_alpha"}
    assert len({r for _, r in gateway.submissions}) == 1


def test_restart_after_send_before_result(api, db, config, gateway, transfer_request, monkeypatch):
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "crash"}
    ).json()["transfer_id"]
    worker = Worker(db, config, gateway)
    claimed = worker.claim()

    def crash(*_, **__):
        raise RuntimeError("simulated process crash before result commit")

    monkeypatch.setattr(worker, "apply", crash)
    with pytest.raises(RuntimeError):
        worker.process(*claimed)
    assert len(gateway.payouts) == 1
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"
    assert len(gateway.submissions) == 1


def test_review_keeps_hold_and_can_recover(api, db, config, gateway, transfer_request):
    gateway.finish = False
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "review"}
    ).json()["transfer_id"]
    with db.begin() as session:
        session.get(Transfer, tid).created_at = now() - timedelta(minutes=5)
    worker = Worker(db, config, gateway)
    assert run_worker(worker, db, tid, ticks=2) == "NEEDS_REVIEW"
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "10000.00"
    gateway.finish = True
    assert run_worker(worker, db, tid) == "COMPLETED"


def test_webhook_auth_duplicates_unknown_and_reordering(api, db, config, gateway, transfer_request):
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "webhook"}
    ).json()["transfer_id"]
    worker = Worker(db, config, gateway)
    worker.tick()
    attempt = api.get("/transfers/" + tid).json()["attempts"][0]
    event = {
        "event_id": "event-1",
        "payout_id": attempt["payout_id"],
        "reference": attempt["reference"],
        "status": "COMPLETED",
        "occurred_at": iso(now()),
    }

    def send(payload, signature=True):
        raw = json.dumps(payload).encode()
        sig = hmac.new(config.provider_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        return api.post(
            "/webhooks/prov_alpha",
            content=raw,
            headers={"X-Signature": sig if signature else "bad"},
        )

    assert send(event, False).status_code == 401
    assert send(event).status_code == 202
    assert send(event).json()["duplicate"] is True
    assert send({**event, "status": "FAILED"}).status_code == 409
    assert send({**event, "event_id": "unknown", "reference": "unknown"}).json()[
        "unknown_reference"
    ]
    assert run_worker(worker, db, tid) == "COMPLETED"
    assert send({**event, "event_id": "out-of-order", "status": "PROCESSING"}).status_code == 202
    assert api.get("/transfers/" + tid).json()["status"] == "COMPLETED"
    with db() as session:
        assert session.scalar(select(func.count()).select_from(WebhookEvent)) == 3


def test_multiple_workers_claim_different_jobs(
    api, db, config, gateway, quote_request, transfer_request
):
    for index in range(4):
        qid = api.post("/quotes", json=quote_request).json()["quote_id"]
        api.post(
            "/transfers",
            json={**transfer_request, "quote_id": qid},
            headers={"Idempotency-Key": f"workers-{index}"},
        )

    def claim(_):
        return Worker(db, config, gateway).claim()[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(claim, range(4)))
    assert len(set(ids)) == 4
    assert Worker(db, config, gateway).claim() is None


def test_restart_seed_does_not_reset_money(api, db, catalog, transfer_request):
    api.post("/transfers", json=transfer_request, headers={"Idempotency-Key": "seed"})
    with db.begin() as session:
        seed(session, catalog)
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "10000.00"


def test_quote_price_is_frozen(api, db, config, gateway, transfer_request):
    gateway.rates = lambda: []
    response = api.post("/transfers", json=transfer_request, headers={"Idempotency-Key": "frozen"})
    assert response.status_code == 202
    assert response.json()["target_amount"]["amount"] == "4137.00"
    tid = response.json()["transfer_id"]
    assert run_worker(Worker(db, config, gateway), db, tid) == "COMPLETED"

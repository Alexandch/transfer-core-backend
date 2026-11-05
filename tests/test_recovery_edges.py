from copy import deepcopy

import httpx
from conftest import run_worker

from app.catalog import Catalog
from app.db import Attempt, Transfer
from app.worker import Worker


def test_skipped_provider_is_not_retried(api, db, config, gateway, transfer_request, monkeypatch):
    tid = api.post("/transfers", json=transfer_request, headers={"Idempotency-Key": "skip"}).json()[
        "transfer_id"
    ]
    changed = deepcopy(Catalog(config.data_dir))
    changed.providers[0]["enabled"] = False
    monkeypatch.setattr("app.worker.Catalog", lambda _: changed)
    gateway.mode = "decline"
    assert run_worker(Worker(db, config, gateway), db, tid) == "FAILED"
    assert [pid for pid, _ in gateway.submissions] == ["prov_beta", "prov_gamma"]


def test_lookup_5xx_after_timeout_does_not_allow_failover(
    api, db, config, gateway, transfer_request
):
    gateway.mode = "disconnect_before"
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "lookup-error"}
    ).json()["transfer_id"]
    worker = Worker(db, config, gateway)
    assert worker.tick()
    original_lookup = gateway.lookup
    gateway.lookup = lambda *_: httpx.Response(500)
    assert worker.tick()
    gateway.lookup = original_lookup
    assert run_worker(worker, db, tid) == "COMPLETED"
    assert {pid for pid, _ in gateway.submissions} == {"prov_alpha"}
    with db() as session:
        t = session.get(Transfer, tid)
        a = session.get(Attempt, gateway.submissions[0][1])
        assert t.status == "COMPLETED"
        assert a.uncertain


def test_all_providers_disabled_after_reservation(
    api, db, config, gateway, transfer_request, monkeypatch
):
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "disable"}
    ).json()["transfer_id"]
    changed = Catalog(config.data_dir)
    for p in changed.providers:
        p["enabled"] = False
    monkeypatch.setattr("app.worker.Catalog", lambda _: changed)
    assert run_worker(Worker(db, config, gateway), db, tid) == "FAILED"
    assert not gateway.submissions
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["available"] == "500000.00"


def test_no_eligible_provider_does_not_hold_funds(api, db, transfer_request, monkeypatch):
    monkeypatch.setattr(
        "app.service.select_providers", lambda *_: {"candidates": [], "rejected": []}
    )
    response = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "no-provider"}
    )
    assert response.json()["error"]["code"] == "NO_ELIGIBLE_PROVIDER"
    assert api.get("/clients/c_001/balance").json()["balances"]["RUB"]["held"] == "0.00"

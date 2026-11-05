import json
import shutil

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.api import create_app
from app.catalog import Catalog
from app.observability import JsonFormatter
from app.worker import Worker


def test_empty_metrics_have_zero_statuses_and_seeded_balances(api):
    response = api.get("/metrics")
    samples = [
        s for family in text_string_to_metric_families(response.text) for s in family.samples
    ]
    statuses = {s.labels["status"]: s.value for s in samples if s.name == "transfer_core_transfers"}
    assert statuses == {
        s: 0 for s in ("RESERVED", "PROCESSING", "UNKNOWN", "NEEDS_REVIEW", "COMPLETED", "FAILED")
    }
    balances = {
        s.labels["kind"]: s.value
        for s in samples
        if s.name == "transfer_core_balance_minor_units" and s.labels["client_id"] == "c_001"
    }
    assert balances == {"available": 50000000, "held": 0, "spent": 0, "initial": 50000000}


def test_metrics_include_transfer_and_persisted_attempt(api, db, config, gateway, transfer_request):
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "metrics"}
    ).json()["transfer_id"]
    Worker(db, config, gateway).tick()
    response = api.get("/metrics")
    assert response.headers["content-type"].startswith("text/plain; version=")
    samples = [
        s for family in text_string_to_metric_families(response.text) for s in family.samples
    ]
    assert any(
        s.name == "transfer_core_transfer_status"
        and s.labels["transfer_id"] == tid
        and s.labels["status"] == "PROCESSING"
        for s in samples
    )
    assert any(
        s.name == "transfer_core_attempt_sends_total"
        and s.labels["transfer_id"] == tid
        and s.value == 1
        for s in samples
    )
    assert api.get("/stats").json()["transfers"] == {"PROCESSING": 1}


def test_http_and_worker_logs_contain_transfer_id(
    api, db, config, gateway, transfer_request, monkeypatch
):
    import logging

    captured = []
    for name in ("transfer.api", "transfer.worker"):
        monkeypatch.setattr(
            logging.getLogger(name),
            "info",
            lambda message, *args, **kwargs: captured.append(json.loads(message)),
        )
    response = api.post("/transfers", json=transfer_request, headers={"Idempotency-Key": "logging"})
    tid = response.json()["transfer_id"]
    Worker(db, config, gateway).tick()
    assert response.headers["X-Request-ID"]
    assert any(r["event"] == "http_request" and r["transfer_id"] == tid for r in captured)
    assert any(r["event"] == "provider_result" and r["transfer_id"] == tid for r in captured)
    record = logging.LogRecord(
        "transfer.test", logging.INFO, "", 1, '{"transfer_id":"test"}', (), None
    )
    assert json.loads(JsonFormatter().format(record))["transfer_id"] == "test"


def test_provider_config_reload_without_app_restart(db, config, gateway, quote_request, tmp_path):
    shutil.copytree(config.data_dir, tmp_path / "data")
    config.data_dir = tmp_path / "data"
    with TestClient(create_app(config, db, gateway)) as client:
        quote = client.post("/quotes", json=quote_request).json()
        original = Catalog(config.data_dir).version
        path = config.data_dir / "providers.json"
        providers = json.loads(path.read_text())
        providers[0]["enabled"] = False
        path.write_text(json.dumps(providers), encoding="utf-8")
        response = client.post(
            "/transfers",
            json={
                "quote_id": quote["quote_id"],
                "client_id": "c_001",
                "recipient": {"name": "Test", "account": "demo", "bank_code": "DEMO"},
            },
            headers={"Idempotency-Key": "reload"},
        )
        assert response.status_code == 202
        plan = client.get(f"/transfers/{response.json()['transfer_id']}/routing").json()
        assert plan["candidates"][0]["provider_id"] == "prov_beta"
        assert plan["config_version"] != original
        providers[0]["success_rate_24h"] = "1.5"
        path.write_text(json.dumps(providers), encoding="utf-8")
        assert (
            client.post("/quotes", json=quote_request).json()["error"]["code"] == "CONFIG_INVALID"
        )


def test_changed_provider_fee_is_rechecked_before_submission(
    api, db, config, gateway, transfer_request, monkeypatch
):
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "fee-change"}
    ).json()["transfer_id"]
    changed = Catalog(config.data_dir)
    changed.providers[0]["fee"]["fixed"] = "100.00"
    monkeypatch.setattr("app.worker.Catalog", lambda _: changed)
    Worker(db, config, gateway).tick()
    assert gateway.submissions[0][0] == "prov_beta"
    plan = api.get(f"/transfers/{tid}/routing").json()
    assert plan["selections"][0]["chosen"] == "prov_beta"
    assert any(
        p["provider_id"] == "prov_alpha" and p["reason"] == "margin"
        for p in plan["selections"][0]["rejected"]
    )

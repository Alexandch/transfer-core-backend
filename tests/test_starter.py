import hashlib
import importlib.util
import json
from datetime import datetime

import httpx
import pytest
from conftest import ROOT, run_worker
from fastapi.testclient import TestClient

from app.api import create_app
from app.catalog import Catalog, records
from app.clock import now
from app.config import Settings
from app.errors import DomainError
from app.pricing import calculate
from app.provider import ProviderGateway
from app.rates import Rates
from app.worker import Worker

EXAMPLES = [
    json.loads(p.read_text(encoding="utf-8"))
    for p in sorted((ROOT / "examples").glob("[0-9]*.json"))
]


def assert_subset(expected, actual):
    for key, value in expected.items():
        if isinstance(value, dict):
            assert_subset(value, actual[key])
        else:
            assert actual[key] == value, (key, actual[key], value)


def reference_data():
    path = ROOT / "mock-provider/app/reference_data.py"
    spec = importlib.util.spec_from_file_location("starter_reference_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ReferenceData(ROOT / "data")


@pytest.fixture
def starter_api(db):
    data = reference_data()
    config = Settings(
        data_dir=ROOT / "data",
        allow_scenarios=True,
        provider_protocol="starter",
        poll_interval_sec=0,
    )
    gateway = ProviderGateway(
        config,
        httpx.Client(
            base_url="http://starter",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=data.live_rates(now()))
            ),
        ),
    )
    with TestClient(create_app(config, db, gateway)) as client:
        yield client, config, gateway


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["name"] for e in EXAMPLES])
def test_official_quote_calculation(example):
    rows = records(ROOT / "data/rates.json", "rates")
    at = max(datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")) for r in rows)
    response = example["response"]
    try:
        result = calculate(example["request"], Catalog(ROOT / "data"), Rates(rows, at, 3600), at)
    except DomainError as exc:
        assert exc.status == response.get("status", 422)
        assert_subset(response["body"], exc.body())
    else:
        assert response["status"] == 201
        assert_subset(response["body"], result)


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["name"] for e in EXAMPLES])
def test_official_quote_http(example, starter_api):
    client, _, _ = starter_api
    response = client.post("/quotes", json=example["request"])
    expected_status = example["response"].get("status", 422)
    assert response.status_code == expected_status, response.text
    assert_subset(example["response"]["body"], response.json())
    if response.status_code == 201:
        assert client.get("/quotes/" + response.json()["quote_id"]).json() == response.json()


def test_official_sources_unchanged():
    manifest = json.loads((ROOT / "starter-manifest.json").read_text())
    assert len(manifest) == 25
    for name, expected in manifest.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name


def test_live_rates_preserve_staleness():
    data = reference_data()
    at = now()
    rates = Rates(data.live_rates(at), at, 3600)
    assert str(rates.get("RUB", "THB")) == "0.4200"
    # Stale RUB/PHP can be replaced with the fresh RUB/USD/PHP path; AED has no fresh path.
    assert rates.get("RUB", "PHP") == rates.get("RUB", "USD") * rates.get("USD", "PHP")
    with pytest.raises(DomainError, match="No fresh rate"):
        rates.get("RUB", "AED")


def test_starter_first_internal_error_can_fail_over(api, db, config, gateway, transfer_request):
    config.provider_protocol = "starter"
    gateway.submit = lambda provider, reference, *_: (
        gateway.submissions.append((provider, reference))
        or httpx.Response(
            500, json={"code": "INTERNAL_ERROR", "message": "internal provider error"}
        )
    )
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "starter-500"}
    ).json()["transfer_id"]
    assert run_worker(Worker(db, config, gateway), db, tid) == "FAILED"
    assert len({pid for pid, _ in gateway.submissions}) == 3


def test_starter_replayed_internal_error_remains_ambiguous(
    api, db, config, gateway, transfer_request
):
    config.provider_protocol = "starter"
    gateway.mode = "disconnect_before"
    tid = api.post(
        "/transfers", json=transfer_request, headers={"Idempotency-Key": "starter-replay"}
    ).json()["transfer_id"]
    worker = Worker(db, config, gateway)
    worker.tick()
    gateway.submit = lambda provider, reference, *_: (
        gateway.submissions.append((provider, reference))
        or httpx.Response(500, json={"code": "INTERNAL_ERROR", "message": "temporary failure"})
    )
    config.review_after_sec = 0
    assert run_worker(worker, db, tid, ticks=3) == "NEEDS_REVIEW"
    assert {pid for pid, _ in gateway.submissions} == {"prov_alpha"}


def reserve(client, client_id, request, key):
    quote = client.post("/quotes", json=request)
    assert quote.status_code == 201, quote.text
    return client.post(
        "/transfers",
        json={
            "quote_id": quote.json()["quote_id"],
            "client_id": client_id,
            "recipient": {"name": "Starter Test", "account": key, "bank_code": "DEMO"},
        },
        headers={"Idempotency-Key": key},
    )


def test_official_daily_limit_in_another_currency(starter_api, quote_request):
    client, _, _ = starter_api
    request = {**quote_request, "amount": "200000.00"}
    assert reserve(client, "c_003", request, "usd-cap-1").status_code == 202
    assert reserve(client, "c_003", request, "usd-cap-2").status_code == 202
    result = reserve(client, "c_003", request, "usd-cap-3")
    assert result.json()["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
    assert client.get("/clients/c_003/balance").json()["balances"]["RUB"] == {
        "available": "1600000.00",
        "held": "400000.00",
        "spent": "0.00",
        "initial": "2000000.00",
    }


def test_official_multiple_accounts_share_one_daily_limit(starter_api, quote_request, db):
    from app.db import DailyUsage

    client, _, _ = starter_api
    assert reserve(client, "c_006", quote_request, "rub-hold").status_code == 202
    request = {
        **quote_request,
        "source_country": "KW",
        "source_currency": "KWD",
        "amount": "20.000",
    }
    assert reserve(client, "c_006", request, "kwd-hold").status_code == 202
    accounts = client.get("/clients/c_006/balance").json()["balances"]
    assert accounts["RUB"]["held"] == "10000.00"
    assert accounts["KWD"]["held"] == "20.000"
    with db() as session:
        assert session.get(DailyUsage, ("c_006", now().date())).amount == 1540000


def test_official_direction_without_provider_releases_reserve(starter_api, quote_request, db):
    client, config, gateway = starter_api
    request = {
        **quote_request,
        "target_country": "PH",
        "target_currency": "PHP",
        "payout_method": "CASH_PICKUP",
        "amount": "2000.00",
    }
    result = reserve(client, "c_101", request, "no-provider")
    assert result.status_code == 202
    assert run_worker(Worker(db, config, gateway), db, result.json()["transfer_id"]) == "FAILED"
    assert client.get("/clients/c_101/balance").json()["balances"]["RUB"]["held"] == "0.00"

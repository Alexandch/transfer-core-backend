import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api import create_app
from app.catalog import Catalog, records
from app.clock import iso, now
from app.config import Settings
from app.db import Base, Transfer
from app.service import seed

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog():
    return Catalog(ROOT / "data")


@pytest.fixture
def at():
    return datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
def quote_request():
    return {
        "source_country": "RU",
        "source_currency": "RUB",
        "target_country": "TH",
        "target_currency": "THB",
        "payout_method": "BANK_ACCOUNT",
        "amount": "10000.00",
        "amount_type": "SOURCE",
    }


@pytest.fixture(scope="session")
def test_engine():
    url = os.getenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:55439/transfer_test"
    )
    if not make_url(url).database.endswith("_test"):
        pytest.fail("Refusing to truncate a database without the _test suffix")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
    )
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    yield engine
    engine.dispose()


@pytest.fixture
def db(test_engine, catalog):
    with test_engine.begin() as connection:
        names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE TABLE {names} CASCADE"))
    sessions = sessionmaker(test_engine, expire_on_commit=False)
    with sessions.begin() as session:
        seed(session, catalog)
    return sessions


class FakeGateway:
    def __init__(self):
        self.payouts, self.submissions, self.lookups = {}, [], []
        self.mode = "ok"
        self.fail_first_provider = False
        self.finish = True
        self.disconnected = False
        self.client = httpx.Client(
            transport=httpx.MockTransport(self.respond), base_url="http://provider"
        )

    def respond(self, request):
        rows = [
            {**r, "updated_at": iso(now())} for r in records(ROOT / "data" / "rates.json", "rates")
        ]
        return httpx.Response(200, json=rows)

    def rates(self):
        return self.client.get("/rates").json()

    def submit(self, provider_id, reference, quote, recipient, scenario):
        self.submissions.append((provider_id, reference))
        mode = scenario or self.mode
        if self.fail_first_provider and provider_id == "prov_alpha":
            mode = "decline"
        if mode == "decline":
            return httpx.Response(422, json={"code": "RECIPIENT_ACCOUNT_INVALID"})
        if mode == "error_5xx":
            self.payouts.setdefault(
                reference,
                {
                    "reference": reference,
                    "payout_id": "po_" + reference,
                    "status": "FAILED",
                },
            )
            return httpx.Response(500)
        if mode == "disconnect_before" and not self.disconnected:
            self.disconnected = True
            raise httpx.ReadTimeout("connection lost before acceptance")
        self.payouts.setdefault(
            reference,
            {"reference": reference, "payout_id": "po_" + reference, "status": "PROCESSING"},
        )
        if mode == "disconnect_after" and not self.disconnected:
            self.disconnected = True
            raise httpx.ReadTimeout("connection lost after acceptance")
        if mode == "after_commit_5xx":
            return httpx.Response(500)
        return httpx.Response(201, json=self.payouts[reference])

    def lookup(self, provider_id, reference):
        self.lookups.append((provider_id, reference))
        payout = self.payouts.get(reference)
        if payout is None:
            return httpx.Response(404)
        if self.finish and payout["status"] != "FAILED":
            payout["status"] = "COMPLETED"
        return httpx.Response(200, json=payout)


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def config():
    return Settings(data_dir=ROOT / "data", allow_scenarios=True, poll_interval_sec=0)


@pytest.fixture
def api(db, gateway, config):
    with TestClient(create_app(config, db, gateway)) as client:
        yield client


@pytest.fixture
def transfer_request(api, quote_request):
    response = api.post("/quotes", json=quote_request)
    assert response.status_code == 201, response.text
    return {
        "quote_id": response.json()["quote_id"],
        "client_id": "c_001",
        "recipient": {"name": "Somchai Jaidee", "account": "1234567890", "bank_code": "BKKB"},
    }


def run_worker(worker, db, tid, ticks=30):
    for _ in range(ticks):
        with db.begin() as session:
            t = session.get(Transfer, tid)
            if t.status in {"COMPLETED", "FAILED"}:
                return t.status
            t.next_run = now() - timedelta(seconds=1)
            t.lease_until = None
        assert worker.tick()
    with db() as session:
        return session.get(Transfer, tid).status

"""Locally authored emulator, NOT the unavailable official starter emulator."""

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert

from app.catalog import Catalog, records
from app.clock import iso, now
from app.config import settings
from app.db import engine
from app.money import minor
from app.service import digest

metadata = MetaData(schema="mock")
payouts = Table(
    "payouts",
    metadata,
    Column("id", String(150), primary_key=True),
    Column("provider_id", String(100), nullable=False),
    Column("key", String(200), nullable=False),
    Column("reference", String(100), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("notified_at", DateTime(timezone=True)),
    Column("scenario", String(40), nullable=False),
    UniqueConstraint("provider_id", "key"),
    UniqueConstraint("provider_id", "reference"),
)


def refresh(connection) -> None:
    connection.execute(
        payouts.update()
        .where(payouts.c.status == "PROCESSING", payouts.c.due_at <= now())
        .values(status="COMPLETED")
    )


def view(row) -> dict:
    return {
        "payout_id": row["id"],
        "reference": row["reference"],
        "status": row["status"],
        "failure_code": None,
    }


async def callbacks() -> None:
    async with httpx.AsyncClient(timeout=3) as client:
        while True:
            with engine.begin() as connection:
                refresh(connection)
                pending = (
                    connection.execute(
                        select(payouts)
                        .where(
                            payouts.c.status == "COMPLETED",
                            payouts.c.notified_at.is_(None),
                        )
                        .limit(100)
                    )
                    .mappings()
                    .all()
                )
            for row in pending:
                try:
                    event = {
                        "event_id": "ev_" + row["id"],
                        "payout_id": row["id"],
                        "reference": row["reference"],
                        "status": "COMPLETED",
                        "occurred_at": iso(row["due_at"]),
                    }
                    if row["scenario"] != "no_webhook":
                        events = [event]
                        if row["scenario"] == "duplicate":
                            events += [
                                event,
                                {
                                    **event,
                                    "event_id": event["event_id"] + "_old",
                                    "status": "PROCESSING",
                                },
                            ]
                        for item in events:
                            raw = json.dumps(item, separators=(",", ":")).encode()
                            signature = hmac.new(
                                settings().provider_webhook_secret.encode(), raw, hashlib.sha256
                            ).hexdigest()
                            response = await client.post(
                                row["payload"]["callback_url"],
                                content=raw,
                                headers={
                                    "X-Signature": signature,
                                    "Content-Type": "application/json",
                                },
                            )
                            response.raise_for_status()
                    with engine.begin() as connection:
                        connection.execute(
                            payouts.update()
                            .where(payouts.c.id == row["id"])
                            .values(notified_at=now())
                        )
                except httpx.HTTPError:
                    pass  # Retry after API restarts; core also reconciles without callbacks.
            await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS mock"))
        metadata.create_all(connection)
    task = asyncio.create_task(callbacks())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Demo provider emulator", lifespan=lifespan)


class PayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reference: str = Field(min_length=1, max_length=100)
    amount: str
    currency: str
    recipient: dict
    callback_url: str


def provider_exists(provider_id: str) -> None:
    if provider_id not in {p["provider_id"] for p in Catalog(settings().data_dir).providers}:
        raise HTTPException(404)


@app.get("/health")
def health():
    return {"status": "ok", "emulator": "locally-authored-demo"}


@app.get("/rates")
def rates():
    # rates.json is a historical fixture; this emulator publishes its values as a fresh feed.
    return [
        {**r, "updated_at": iso(now())}
        for r in records(settings().data_dir / "rates.json", "rates")
    ]


@app.post("/{provider_id}/payouts")
async def create_payout(
    provider_id: str,
    body: PayoutRequest,
    idempotency_key: str = Header(default=""),
    x_scenario: str | None = Header(default=None),
):
    provider_exists(provider_id)
    if not idempotency_key:
        raise HTTPException(400)
    minor(body.amount, Catalog(settings().data_dir).currencies[body.currency])
    payload, fingerprint = body.model_dump(), digest(body.model_dump())
    scenario = x_scenario or "ok"
    if not x_scenario and os.getenv("MOCK_MODE") == "chaos":
        variants = [
            "ok",
            "decline",
            "error_5xx",
            "after_commit_5xx",
            "slow",
            "duplicate",
            "no_webhook",
            "malformed",
        ]
        hashed = hashlib.sha256((os.getenv("MOCK_SEED", "42") + body.reference).encode()).digest()
        scenario = variants[int.from_bytes(hashed[:2], "big") % len(variants)]
    with engine.begin() as connection:
        existing = (
            connection.execute(
                select(payouts).where(
                    payouts.c.provider_id == provider_id,
                    payouts.c.key == idempotency_key,
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["fingerprint"] != fingerprint:
                return JSONResponse({"code": "IDEMPOTENCY_CONFLICT"}, status_code=409)
            refresh(connection)
            existing = (
                connection.execute(select(payouts).where(payouts.c.id == existing["id"]))
                .mappings()
                .one()
            )
            return JSONResponse(view(existing), status_code=201)
        if scenario == "decline":
            return JSONResponse(
                {"code": "RECIPIENT_ACCOUNT_INVALID", "message": "Demo decline"}, status_code=422
            )
        if scenario == "error_5xx":
            return JSONResponse({"code": "INTERNAL_ERROR"}, status_code=503)
        delay = int(os.getenv("MOCK_SLOW_SEC", "30")) if scenario == "slow" else 1
        connection.execute(
            insert(payouts)
            .values(
                id="po_" + str(uuid4()),
                provider_id=provider_id,
                key=idempotency_key,
                reference=body.reference,
                fingerprint=fingerprint,
                payload=payload,
                status="PROCESSING",
                due_at=now() + timedelta(seconds=delay),
                scenario=scenario,
            )
            .on_conflict_do_nothing()
        )
        row = (
            connection.execute(
                select(payouts).where(
                    payouts.c.provider_id == provider_id,
                    payouts.c.key == idempotency_key,
                )
            )
            .mappings()
            .one()
        )
    if scenario == "slow":
        await asyncio.sleep(delay)
    elif scenario == "after_commit_5xx":
        return JSONResponse({"code": "LOST_RESPONSE"}, status_code=500)
    elif scenario == "malformed":
        return JSONResponse({"unexpected": "response after successful acceptance"}, status_code=201)
    return JSONResponse(view(row), status_code=201)


@app.get("/{provider_id}/payouts")
def lookup(provider_id: str, reference: str):
    provider_exists(provider_id)
    with engine.begin() as connection:
        refresh(connection)
        row = (
            connection.execute(
                select(payouts).where(
                    payouts.c.provider_id == provider_id,
                    payouts.c.reference == reference,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404)
        return view(row)


@app.get("/{provider_id}/payouts/{payout_id}")
def lookup_id(provider_id: str, payout_id: str):
    provider_exists(provider_id)
    with engine.begin() as connection:
        refresh(connection)
        row = (
            connection.execute(
                select(payouts).where(
                    payouts.c.provider_id == provider_id,
                    payouts.c.id == payout_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404)
        return view(row)


@app.get("/{provider_id}/admin/ledger")
def ledger(provider_id: str):
    provider_exists(provider_id)
    with engine.begin() as connection:
        refresh(connection)
        rows = (
            connection.execute(
                select(payouts).where(
                    payouts.c.provider_id == provider_id,
                    payouts.c.status == "COMPLETED",
                )
            )
            .mappings()
            .all()
        )
        return [
            {**view(r), "amount": r["payload"]["amount"], "currency": r["payload"]["currency"]}
            for r in rows
        ]

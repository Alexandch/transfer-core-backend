import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.catalog import Catalog
from app.clock import iso, now
from app.db import (
    Account,
    Attempt,
    AttemptObservation,
    Client,
    DailyUsage,
    Quote,
    Transfer,
    WebhookEvent,
)
from app.errors import DomainError
from app.money import D, fmt, minor, quantize
from app.rates import Rates
from app.routing import select_providers

TERMINAL = {"COMPLETED", "FAILED"}


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def seed(session: Session, catalog: Catalog) -> None:
    """INSERT ON CONFLICT protects balances across restarts and simultaneous startup."""
    for row in catalog.clients:
        limit = row["daily_limit"]
        session.execute(
            insert(Client)
            .values(
                id=row["client_id"],
                daily_limit=minor(limit["amount"], catalog.currencies[limit["currency"]]),
                daily_currency=limit["currency"],
            )
            .on_conflict_do_nothing()
        )
        for currency, amount in row["balances"].items():
            value = minor(amount, catalog.currencies[currency])
            session.execute(
                insert(Account)
                .values(
                    client_id=row["client_id"],
                    currency=currency,
                    initial=value,
                    available=value,
                    held=0,
                    spent=0,
                )
                .on_conflict_do_nothing()
            )


def transition(transfer: Transfer, status: str, actor: str, reason: str) -> None:
    allowed = {
        "RESERVED": {"PROCESSING", "NEEDS_REVIEW", "FAILED"},
        "PROCESSING": {"UNKNOWN", "NEEDS_REVIEW", "COMPLETED", "FAILED"},
        "UNKNOWN": {"PROCESSING", "NEEDS_REVIEW", "COMPLETED", "FAILED"},
        "NEEDS_REVIEW": {"PROCESSING", "UNKNOWN", "COMPLETED", "FAILED"},
    }
    if status == transfer.status:
        return
    if status not in allowed.get(transfer.status, set()):
        raise ValueError(f"Invalid transition {transfer.status} -> {status}")
    transfer.history = [
        *transfer.history,
        {"from": transfer.status, "to": status, "at": iso(now()), "actor": actor, "reason": reason},
    ]
    transfer.status = status


def get_quote(session: Session, quote_id: str) -> Quote:
    quote = session.get(Quote, quote_id)
    if quote is None:
        raise DomainError("QUOTE_NOT_FOUND", "Quote not found", 404)
    return quote


def create_transfer(
    session: Session,
    request: dict,
    key: str,
    catalog: Catalog,
    scenario: str | None,
    at: datetime,
) -> tuple[Transfer, bool]:
    if not key or not key.strip() or len(key) > 200:
        raise DomainError("VALIDATION_ERROR", "Idempotency-Key must contain 1..200 characters")
    fingerprint = digest({**request, "scenario": scenario})
    client = session.scalar(
        select(Client).where(Client.id == request["client_id"]).with_for_update()
    )
    if client is None:
        raise DomainError("CLIENT_NOT_FOUND", "Client not found", 404)
    existing = session.scalar(
        select(Transfer).where(Transfer.client_id == client.id, Transfer.idempotency_key == key)
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "Key was already used with another body", 409)
        return existing, False
    quote = session.scalar(select(Quote).where(Quote.id == request["quote_id"]).with_for_update())
    if quote is None:
        raise DomainError("QUOTE_NOT_FOUND", "Quote not found", 404)
    if session.scalar(select(Transfer.id).where(Transfer.quote_id == quote.id)):
        raise DomainError("QUOTE_ALREADY_USED", "A quote can fund only one transfer", 409)
    if quote.expires_at <= at:
        raise DomainError("QUOTE_EXPIRED", "Quote expired", 409)
    q = quote.payload
    currency = q["source_amount"]["currency"]
    amount = minor(q["source_amount"]["amount"], catalog.currencies[currency])
    account = session.get(Account, (client.id, currency), with_for_update=True)
    if account is None or account.available < amount:
        raise DomainError("INSUFFICIENT_FUNDS", "Available balance is insufficient", 409)
    # FX terms used by the transfer are the quote-time snapshot, including daily-limit FX.
    quoted_at = datetime.fromisoformat(q["priced_at"].replace("Z", "+00:00"))
    rates = Rates(quote.rate_snapshot, quoted_at, q["max_rate_age_sec"])
    daily_amount = quantize(
        D(q["source_amount"]["amount"]) * rates.get(currency, client.daily_currency),
        catalog.currencies[client.daily_currency],
        up=True,
    )
    usage = session.get(DailyUsage, (client.id, at.date()))
    if usage is None:
        usage = DailyUsage(client_id=client.id, day=at.date(), amount=0)
        session.add(usage)
    if usage.amount + daily_amount > client.daily_limit:
        raise DomainError("DAILY_LIMIT_EXCEEDED", "Daily limit exceeded", 409)
    plan = select_providers(q, catalog, rates, at)
    if not plan["candidates"]:
        raise DomainError(
            "NO_ELIGIBLE_PROVIDER",
            "No provider satisfies limits and margin",
            409,
            {"rejected": plan["rejected"]},
        )
    account.available -= amount
    account.held += amount
    usage.amount += daily_amount
    transfer = Transfer(
        id=str(uuid4()),
        client_id=client.id,
        quote_id=quote.id,
        idempotency_key=key,
        request_hash=fingerprint,
        recipient=request["recipient"],
        scenario=scenario,
        status="RESERVED",
        source_currency=currency,
        source_minor=amount,
        daily_minor=daily_amount,
        usage_day=at.date(),
        plan=plan,
        history=[
            {
                "from": None,
                "to": "RESERVED",
                "at": iso(at),
                "actor": "client",
                "reason": "funds_reserved",
            }
        ],
        created_at=at,
        next_run=at,
    )
    session.add(transfer)
    session.flush()
    return transfer, True


def settle(session: Session, transfer: Transfer, status: str, reason: str) -> None:
    if transfer.status in TERMINAL:
        return
    account = session.get(
        Account, (transfer.client_id, transfer.source_currency), with_for_update=True
    )
    account.held -= transfer.source_minor
    if status == "COMPLETED":
        account.spent += transfer.source_minor
    else:
        account.available += transfer.source_minor
        usage = session.get(DailyUsage, (transfer.client_id, transfer.usage_day))
        usage.amount -= transfer.daily_minor
    transition(transfer, status, "worker", reason)
    transfer.lease_owner = None
    transfer.lease_until = None


def transfer_view(session: Session, transfer: Transfer) -> dict:
    quote = session.get(Quote, transfer.quote_id)
    attempts = session.scalars(
        select(Attempt).where(Attempt.transfer_id == transfer.id).order_by(Attempt.number)
    ).all()
    return {
        "transfer_id": transfer.id,
        "quote_id": transfer.quote_id,
        "client_id": transfer.client_id,
        "status": transfer.status,
        "review_reason": transfer.review_reason,
        "source_amount": quote.payload["source_amount"],
        "target_amount": quote.payload["target_amount"],
        "fee": quote.payload["fee"],
        "created_at": iso(transfer.created_at),
        "history": transfer.history,
        "attempts": [
            {
                "attempt_id": a.id,
                "reference": a.id,
                "provider_id": a.provider_id,
                "payout_id": a.payout_id,
                "status": a.status,
                "result": a.result,
                "created_at": iso(a.created_at),
                "updated_at": iso(a.updated_at),
                "sends": a.sends,
                "observations": [
                    {
                        "status": o.status,
                        "result": o.result,
                        "source": o.source,
                        "payout_id": o.payout_id,
                        "at": iso(o.observed_at),
                    }
                    for o in session.scalars(
                        select(AttemptObservation)
                        .where(AttemptObservation.attempt_id == a.id)
                        .order_by(AttemptObservation.id)
                    )
                ],
            }
            for a in attempts
        ],
    }


def balances(session: Session, client_id: str, catalog: Catalog) -> dict:
    if session.get(Client, client_id) is None:
        raise DomainError("CLIENT_NOT_FOUND", "Client not found", 404)
    rows = session.scalars(
        select(Account).where(Account.client_id == client_id).order_by(Account.currency)
    ).all()
    return {
        "client_id": client_id,
        "balances": {
            a.currency: {
                k: fmt(getattr(a, k), catalog.currencies[a.currency])
                for k in ("available", "held", "spent", "initial")
            }
            for a in rows
        },
    }


def record_webhook(session: Session, provider_id: str, payload: dict) -> dict:
    body_hash = digest(payload)
    inserted = session.execute(
        insert(WebhookEvent)
        .values(
            provider_id=provider_id,
            event_id=payload["event_id"],
            body_hash=body_hash,
            payload=payload,
            received_at=now(),
        )
        .on_conflict_do_nothing()
        .returning(WebhookEvent.event_id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = session.get(WebhookEvent, (provider_id, payload["event_id"]))
        if existing.body_hash != body_hash:
            raise DomainError("WEBHOOK_CONFLICT", "Event ID has conflicting payloads", 409)
        return {"accepted": True, "duplicate": True}
    attempt = session.get(Attempt, payload["reference"])
    if attempt is None or attempt.provider_id != provider_id:
        return {"accepted": True, "unknown_reference": True}
    transfer = session.get(Transfer, attempt.transfer_id, with_for_update=True)
    if (
        attempt.payout_id
        and attempt.payout_id != payload["payout_id"]
        or attempt.status == "FAILED"
        and payload["status"] == "COMPLETED"
    ):
        transfer.review_reason = "CONFLICTING_WEBHOOK"
        if transfer.status not in TERMINAL:
            attempt.conflict = True
            transition(transfer, "NEEDS_REVIEW", "webhook", "CONFLICTING_WEBHOOK")
    if transfer.status not in TERMINAL:
        # Events are durable wake-up hints. Authoritative GET handles duplicates and reordering.
        transfer.next_run = now()
    return {"accepted": True}

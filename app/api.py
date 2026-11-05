import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.catalog import Catalog
from app.clock import iso, now
from app.config import Settings, settings
from app.db import Quote, SessionLocal, Transfer
from app.errors import DomainError
from app.pricing import calculate
from app.provider import ProviderGateway
from app.rates import Rates
from app.schemas import QuoteRequest, TransferRequest, WebhookRequest
from app.service import (
    balances,
    create_transfer,
    get_quote,
    record_webhook,
    seed,
    transfer_view,
)


def create_app(
    config: Settings | None = None, sessions=SessionLocal, gateway: ProviderGateway | None = None
) -> FastAPI:
    config = config or settings()
    gateway = gateway or ProviderGateway(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with sessions.begin() as session:
            seed(session, Catalog(config.data_dir))
        yield
        gateway.client.close()

    api = FastAPI(title="Transfer Core", version="0.1.0", lifespan=lifespan)

    @api.exception_handler(DomainError)
    async def domain_handler(_: Request, exc: DomainError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @api.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        error = DomainError("VALIDATION_ERROR", "Invalid request", details={"errors": details})
        return JSONResponse(error.body(), status_code=422)

    @api.get("/health")
    def health():
        with sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @api.post("/quotes", status_code=201)
    def post_quote(body: QuoteRequest):
        at, catalog = now(), Catalog(config.data_dir)
        rows = gateway.rates()
        rates = Rates(rows, at, config.max_rate_age_sec)
        payload = calculate(body.model_dump(), catalog, rates, at)
        payload["priced_at"], payload["max_rate_age_sec"] = iso(at), config.max_rate_age_sec
        with sessions.begin() as session:
            session.add(
                Quote(
                    id=payload["quote_id"],
                    payload=payload,
                    rate_snapshot=rows,
                    expires_at=datetime.fromisoformat(payload["expires_at"]),
                )
            )
        return payload

    @api.get("/quotes/{quote_id}")
    def read_quote(quote_id: str):
        with sessions() as session:
            return get_quote(session, quote_id).payload

    @api.post("/transfers", status_code=202)
    def post_transfer(
        body: TransferRequest,
        idempotency_key: str = Header(default=""),
        x_scenario: str | None = Header(default=None),
    ):
        if x_scenario is not None and (
            not config.allow_scenarios or x_scenario not in {"ok", "decline", "error_5xx", "slow"}
        ):
            raise DomainError("VALIDATION_ERROR", "X-Scenario is disabled or unsupported")
        with sessions.begin() as session:
            transfer, _ = create_transfer(
                session,
                body.model_dump(),
                idempotency_key,
                Catalog(config.data_dir),
                x_scenario,
                now(),
            )
            return transfer_view(session, transfer)

    @api.get("/transfers/{transfer_id}")
    def read_transfer(transfer_id: str):
        with sessions() as session:
            transfer = session.get(Transfer, transfer_id)
            if transfer is None:
                raise DomainError("TRANSFER_NOT_FOUND", "Transfer not found", 404)
            return transfer_view(session, transfer)

    @api.get("/transfers/{transfer_id}/routing")
    def read_routing(transfer_id: str):
        with sessions() as session:
            transfer = session.get(Transfer, transfer_id)
            if transfer is None:
                raise DomainError("TRANSFER_NOT_FOUND", "Transfer not found", 404)
            return transfer.plan

    @api.get("/clients/{client_id}/balance")
    def read_balance(client_id: str):
        with sessions() as session:
            return balances(session, client_id, Catalog(config.data_dir))

    @api.post("/webhooks/{provider_id}", status_code=202)
    async def webhook(provider_id: str, request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            raise DomainError("VALIDATION_ERROR", "Webhook body is too large", 413)
        expected = hmac.new(
            config.provider_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Signature", "")):
            raise DomainError("INVALID_SIGNATURE", "Invalid webhook signature", 401)
        if provider_id not in {p["provider_id"] for p in Catalog(config.data_dir).providers}:
            raise DomainError("PROVIDER_NOT_FOUND", "Unknown provider", 404)
        try:
            payload = WebhookRequest.model_validate_json(raw).model_dump()
            datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("VALIDATION_ERROR", "Invalid webhook body") from exc
        with sessions.begin() as session:
            return record_webhook(session, provider_id, payload)

    @api.get("/metrics")
    def metrics():
        with sessions() as session:
            counts = session.execute(
                select(Transfer.status, func.count()).group_by(Transfer.status)
            )
            return {"transfers": dict(counts.all())}

    return api


app = create_app()

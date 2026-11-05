from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    daily_limit: Mapped[int] = mapped_column(BigInteger)
    daily_currency: Mapped[str] = mapped_column(String(12))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("available >= 0 AND held >= 0 AND spent >= 0", name="nonnegative_balance"),
        CheckConstraint("available + held + spent = initial", name="balance_conservation"),
    )
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(12), primary_key=True)
    initial: Mapped[int] = mapped_column(BigInteger)
    available: Mapped[int] = mapped_column(BigInteger)
    held: Mapped[int] = mapped_column(BigInteger, default=0)
    spent: Mapped[int] = mapped_column(BigInteger, default=0)


class DailyUsage(Base):
    __tablename__ = "daily_usage"
    __table_args__ = (CheckConstraint("amount >= 0", name="nonnegative_daily_usage"),)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    rate_snapshot: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_client_idempotency"),
        UniqueConstraint("quote_id", name="uq_consumed_quote"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[dict] = mapped_column(JSON)
    scenario: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_currency: Mapped[str] = mapped_column(String(12))
    source_minor: Mapped[int] = mapped_column(BigInteger)
    daily_minor: Mapped[int] = mapped_column(BigInteger)
    usage_day: Mapped[date] = mapped_column(Date)
    plan: Mapped[dict] = mapped_column(JSON)
    history: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    review_reason: Mapped[str | None] = mapped_column(String(150))


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("transfer_id", "number", name="uq_attempt_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[str] = mapped_column(String(100))
    payout_id: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sends: Mapped[int] = mapped_column(Integer, default=0)
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_confirmations: Mapped[int] = mapped_column(Integer, default=0)
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)


class AttemptObservation(Base):
    __tablename__ = "attempt_observations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[str | None] = mapped_column(String(150))
    source: Mapped[str] = mapped_column(String(20))
    payout_id: Mapped[str | None] = mapped_column(String(150))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    provider_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


engine = create_engine(
    settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

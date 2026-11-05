import json
import logging
import signal
import threading
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import or_, select

from app.catalog import Catalog
from app.clock import iso, now
from app.config import Settings, settings
from app.db import Attempt, AttemptObservation, Client, Quote, SessionLocal, Transfer, WebhookEvent
from app.errors import DomainError
from app.observability import configure_logging
from app.provider import ProviderGateway
from app.rates import Rates
from app.routing import select_providers
from app.service import TERMINAL, settle, transition

log = logging.getLogger("transfer.worker")


class Worker:
    def __init__(
        self,
        sessions=SessionLocal,
        config: Settings | None = None,
        gateway: ProviderGateway | None = None,
    ):
        self.sessions = sessions
        self.config = config or settings()
        self.gateway = gateway or ProviderGateway(self.config)

    def claim(self) -> tuple[str, str] | None:
        at, owner = now(), str(uuid4())
        with self.sessions.begin() as session:
            transfer = session.scalar(
                select(Transfer)
                .where(
                    Transfer.status.not_in(TERMINAL),
                    Transfer.next_run <= at,
                    or_(Transfer.lease_until.is_(None), Transfer.lease_until < at),
                )
                .order_by(Transfer.next_run, Transfer.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if transfer is None:
                return None
            transfer.lease_owner = owner
            transfer.lease_until = at + timedelta(seconds=self.config.lease_sec)
            return transfer.id, owner

    def tick(self) -> bool:
        claimed = self.claim()
        if claimed is None:
            return False
        transfer_id, owner = claimed
        try:
            self.process(transfer_id, owner)
        except DomainError as exc:
            self.apply(transfer_id, owner, None, "REVIEW", exc.code)
        except Exception:
            # The durable attempt survives this process; an expired lease resumes it.
            log.exception(json.dumps({"event": "job_error", "transfer_id": transfer_id}))
            try:
                self.apply(transfer_id, owner, None, "REVIEW", "JOB_ERROR")
            except Exception:
                log.exception(
                    json.dumps({"event": "review_write_error", "transfer_id": transfer_id})
                )
        return True

    def process(self, transfer_id: str, owner: str) -> None:
        with self.sessions.begin() as session:
            transfer = session.get(Transfer, transfer_id, with_for_update=True)
            if transfer.status in TERMINAL or transfer.lease_owner != owner:
                return
            attempts = session.scalars(
                select(Attempt).where(Attempt.transfer_id == transfer.id).order_by(Attempt.number)
            ).all()
            attempt = attempts[-1] if attempts else None
            quote_record = session.get(Quote, transfer.quote_id)
            quote = quote_record.payload
            submit = False
            if attempt is None or attempt.status == "FAILED" and not attempt.conflict:
                quoted_at = datetime.fromisoformat(quote["priced_at"].replace("Z", "+00:00"))
                rates = Rates(quote_record.rate_snapshot, quoted_at, quote["max_rate_age_sec"])
                live_plan = select_providers(quote, Catalog(self.config.data_dir), rates, now())
                tried = {a.provider_id for a in attempts}
                chosen = next(
                    (p for p in live_plan["candidates"] if p["provider_id"] not in tried), None
                )
                selection = {
                    **live_plan,
                    "at": iso(now()),
                    "chosen": chosen["provider_id"] if chosen else None,
                }
                transfer.plan = {
                    **transfer.plan,
                    "selections": [*transfer.plan.get("selections", []), selection],
                }
                if chosen is None:
                    # Finalize in the common lock order (client -> transfer -> account).
                    exhausted = True
                else:
                    exhausted = False
                    attempt = Attempt(
                        id=str(uuid4()),
                        transfer_id=transfer.id,
                        number=len(attempts) + 1,
                        provider_id=chosen["provider_id"],
                        status="SUBMITTING",
                        created_at=now(),
                        updated_at=now(),
                        sends=1,
                    )
                    session.add(attempt)
                    transition(transfer, "PROCESSING", "worker", "attempt_prepared")
                    submit = True
            else:
                exhausted = False
            if not exhausted:
                context = (
                    attempt.id,
                    attempt.provider_id,
                    transfer.recipient,
                    transfer.scenario,
                    quote,
                )
        if exhausted:
            self.apply(transfer_id, owner, None, "EXHAUSTED", None)
            return
        attempt_id, provider_id, recipient, scenario, quote = context
        try:
            if submit:
                response = self.gateway.submit(provider_id, attempt_id, quote, recipient, scenario)
                self.handle_response(
                    transfer_id, owner, attempt_id, response, submitted=True, fresh=True
                )
            else:
                response = self.gateway.lookup(provider_id, attempt_id)
                if response.status_code == 404:
                    # A missing lookup can lag acceptance, even after an HTTP 5xx.
                    # Only replay the same key at the same provider; never fail over on 404.
                    with self.sessions.begin() as session:
                        transfer = session.get(Transfer, transfer_id, with_for_update=True)
                        if transfer.status in TERMINAL or transfer.lease_owner != owner:
                            return
                        previous = session.get(Attempt, attempt_id, with_for_update=True)
                        session.add(
                            AttemptObservation(
                                attempt_id=attempt_id,
                                status="UNKNOWN",
                                result="HTTP_404",
                                source="lookup",
                                payout_id=None,
                                observed_at=now(),
                            )
                        )
                        previous.sends += 1
                    response = self.gateway.submit(
                        provider_id, attempt_id, quote, recipient, scenario
                    )
                    self.handle_response(transfer_id, owner, attempt_id, response, submitted=True)
                else:
                    self.handle_response(transfer_id, owner, attempt_id, response, submitted=False)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.apply(transfer_id, owner, attempt_id, "UNKNOWN", "AMBIGUOUS_RESPONSE")

    def handle_response(
        self,
        tid: str,
        owner: str,
        aid: str,
        response: httpx.Response,
        *,
        submitted: bool,
        fresh: bool = False,
    ) -> None:
        if submitted and response.status_code == 422:
            body = response.json()
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("code"), str)
                or not 1 <= len(body["code"]) <= 150
            ):
                raise ValueError("Invalid decline response")
            self.apply(tid, owner, aid, "FAILED", body["code"], source="submit")
        elif response.status_code >= 500:
            if fresh and self.config.provider_protocol == "starter" and response.status_code == 500:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                # starter main.create_payout returns this error before Store.create for a
                # fresh key. Replays may fail after acceptance and MUST remain ambiguous.
                if (
                    isinstance(body, dict)
                    and body.get("code") == "INTERNAL_ERROR"
                    and body.get("message") == "internal provider error"
                ):
                    self.apply(tid, owner, aid, "FAILED", "STARTER_INTERNAL_ERROR", source="submit")
                    return
            result = "SUBMIT_HTTP_5XX" if submitted else "LOOKUP_HTTP_5XX"
            self.apply(
                tid, owner, aid, "UNKNOWN", result, source="submit" if submitted else "lookup"
            )
        elif response.status_code in {200, 201}:
            body = response.json()
            if isinstance(body, list):
                if len(body) != 1:
                    raise ValueError("Expected exactly one reference match")
                body = body[0]
            if not isinstance(body, dict):
                raise ValueError("Expected payout object")
            if body.get("reference", aid if submitted else None) != aid:
                raise ValueError("Mismatched provider reference")
            status = body["status"]
            if status not in {"PROCESSING", "COMPLETED", "FAILED"}:
                raise ValueError("Unknown provider status")
            if not isinstance(body["payout_id"], str) or not 1 <= len(body["payout_id"]) <= 150:
                raise ValueError("Invalid payout id")
            failure_code = body.get("failure_code")
            if failure_code is not None and (
                not isinstance(failure_code, str) or len(failure_code) > 150
            ):
                raise ValueError("Invalid failure code")
            self.apply(
                tid,
                owner,
                aid,
                status,
                body.get("failure_code"),
                body["payout_id"],
                source="submit" if submitted else "lookup",
            )
        else:
            self.apply(tid, owner, aid, "UNKNOWN", f"HTTP_{response.status_code}")

    def apply(
        self,
        tid: str,
        owner: str,
        aid: str | None,
        status: str,
        result: str | None,
        payout_id: str | None = None,
        source: str = "transport",
    ) -> None:
        with self.sessions.begin() as session:
            client_id = session.get(Transfer, tid).client_id
            session.get(Client, client_id, with_for_update=True)
            transfer = session.get(Transfer, tid, with_for_update=True, populate_existing=True)
            if transfer.status in TERMINAL or transfer.lease_owner != owner:
                return
            if status == "REVIEW":
                transfer.review_reason = result
                transition(transfer, "NEEDS_REVIEW", "worker", result)
            elif status == "EXHAUSTED":
                settle(session, transfer, "FAILED", "providers_exhausted")
            else:
                attempt = session.get(Attempt, aid, with_for_update=True)
                session.add(
                    AttemptObservation(
                        attempt_id=aid,
                        status=status,
                        result=result,
                        source=source,
                        payout_id=payout_id,
                        observed_at=now(),
                    )
                )
                if payout_id and attempt.payout_id and attempt.payout_id != payout_id:
                    status, result = "UNKNOWN", "CONFLICTING_PAYOUT_ID"
                    attempt.conflict = True
                completed_event = (
                    session.scalar(
                        select(WebhookEvent.event_id)
                        .where(
                            WebhookEvent.provider_id == attempt.provider_id,
                            WebhookEvent.payload["reference"].as_string() == aid,
                            WebhookEvent.payload["status"].as_string() == "COMPLETED",
                        )
                        .limit(1)
                    )
                    is not None
                )
                if status == "FAILED" and completed_event:
                    status, result = "UNKNOWN", "CONFLICTING_PROVIDER_STATUS"
                    attempt.conflict = True
                if status == "FAILED" and source == "lookup" and not attempt.conflict:
                    attempt.failure_confirmations += 1
                    if attempt.failure_confirmations < 2:
                        status, result = "PROCESSING", "VERIFYING_FAILURE"
                elif status != "UNKNOWN":
                    attempt.failure_confirmations = 0
                if attempt.conflict and status != "COMPLETED":
                    status, result = "UNKNOWN", result or "CONFLICTING_PROVIDER_STATUS"
                attempt.status, attempt.result, attempt.updated_at = status, result, now()
                if status == "UNKNOWN" and result != "SUBMIT_HTTP_5XX":
                    attempt.uncertain = True
                if payout_id and not attempt.payout_id:
                    attempt.payout_id = payout_id
                if status == "COMPLETED":
                    settle(session, transfer, "COMPLETED", "provider_completed")
                    if not attempt.conflict:
                        transfer.review_reason = None
                elif status == "FAILED":
                    transition(transfer, "PROCESSING", "worker", "provider_failed_try_next")
                    transfer.review_reason = None
                elif attempt.conflict:
                    transfer.review_reason = result
                    transition(transfer, "NEEDS_REVIEW", "worker", result)
                elif (now() - transfer.created_at).total_seconds() >= self.config.review_after_sec:
                    transfer.review_reason = "RECONCILIATION_DEADLINE"
                    transition(transfer, "NEEDS_REVIEW", "worker", "reconciliation_deadline")
                elif status == "UNKNOWN":
                    transition(transfer, "UNKNOWN", "worker", result or "ambiguous")
                else:
                    transition(transfer, "PROCESSING", "worker", "provider_accepted")
            delay = self.config.poll_interval_sec
            if aid and status == "UNKNOWN":
                sends = session.get(Attempt, aid).sends
                delay = min(self.config.max_poll_interval_sec, delay * 2 ** min(sends - 1, 5))
            if transfer.status == "NEEDS_REVIEW":
                delay = max(delay, self.config.max_poll_interval_sec)
            transfer.next_run = now() + timedelta(seconds=delay)
            transfer.lease_until, transfer.lease_owner = None, None
            log.info(
                json.dumps(
                    {
                        "event": "provider_result",
                        "transfer_id": tid,
                        "attempt_id": aid,
                        "status": transfer.status,
                        "result": result,
                        "at": iso(now()),
                    }
                )
            )


def main() -> None:
    configure_logging()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    worker = Worker()
    while not stop.is_set():
        if not worker.tick():
            stop.wait(0.25)


if __name__ == "__main__":
    main()

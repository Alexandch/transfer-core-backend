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
from app.db import Attempt, Client, Quote, SessionLocal, Transfer
from app.provider import ProviderGateway
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
        except Exception:
            # The durable attempt survives this process; an expired lease resumes it.
            log.exception(json.dumps({"event": "job_error", "transfer_id": transfer_id}))
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
            submit = False
            if attempt is None or attempt.status == "FAILED":
                index = attempt.number if attempt else 0
                candidates = transfer.plan["candidates"]
                # Re-check live availability before starting a new provider attempt.
                live = {p["provider_id"]: p for p in Catalog(self.config.data_dir).providers}
                while index < len(candidates):
                    provider = live.get(candidates[index]["provider_id"])
                    if (
                        provider
                        and provider["enabled"]
                        and not any(
                            datetime.fromisoformat(w["from"].replace("Z", "+00:00"))
                            <= now()
                            < datetime.fromisoformat(w["to"].replace("Z", "+00:00"))
                            for w in provider.get("maintenance_windows", [])
                        )
                    ):
                        break
                    index += 1
                if index >= len(candidates):
                    # Finalize in the common lock order (client -> transfer -> account).
                    exhausted = True
                else:
                    exhausted = False
                    attempt = Attempt(
                        id=str(uuid4()),
                        transfer_id=transfer.id,
                        number=index + 1,
                        provider_id=candidates[index]["provider_id"],
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
                quote = session.get(Quote, transfer.quote_id).payload
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
                self.handle_response(transfer_id, owner, attempt_id, response, submitted=True)
            else:
                response = self.gateway.lookup(provider_id, attempt_id)
                if response.status_code == 404:
                    with self.sessions() as session:
                        previous = session.get(Attempt, attempt_id)
                        http_5xx = (
                            previous.result == "SUBMIT_HTTP_5XX"
                            and not previous.uncertain
                            and previous.payout_id is None
                        )
                    if http_5xx:
                        # Assumption: authoritative reference lookup after a completed 5xx.
                        self.apply(transfer_id, owner, attempt_id, "FAILED", "HTTP_5XX_NOT_FOUND")
                    else:
                        # Timeout/crash is ambiguous: never move to another provider on 404.
                        with self.sessions.begin() as session:
                            previous = session.get(Attempt, attempt_id, with_for_update=True)
                            previous.sends += 1
                        response = self.gateway.submit(
                            provider_id, attempt_id, quote, recipient, scenario
                        )
                        self.handle_response(
                            transfer_id, owner, attempt_id, response, submitted=True
                        )
                else:
                    self.handle_response(transfer_id, owner, attempt_id, response, submitted=False)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.apply(transfer_id, owner, attempt_id, "UNKNOWN", "AMBIGUOUS_RESPONSE")

    def handle_response(
        self, tid: str, owner: str, aid: str, response: httpx.Response, *, submitted: bool
    ) -> None:
        if submitted and response.status_code == 422:
            self.apply(tid, owner, aid, "FAILED", response.json().get("code", "DECLINED"))
        elif response.status_code >= 500:
            result = "SUBMIT_HTTP_5XX" if submitted else "LOOKUP_HTTP_5XX"
            self.apply(tid, owner, aid, "UNKNOWN", result)
        elif response.status_code in {200, 201}:
            body = response.json()
            if isinstance(body, list):
                if len(body) != 1:
                    raise ValueError("Expected exactly one reference match")
                body = body[0]
            if body.get("reference", aid) != aid:
                raise ValueError("Mismatched provider reference")
            status = body["status"]
            if status not in {"PROCESSING", "COMPLETED", "FAILED"}:
                raise ValueError("Unknown provider status")
            self.apply(tid, owner, aid, status, body.get("failure_code"), body["payout_id"])
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
    ) -> None:
        with self.sessions.begin() as session:
            client_id = session.get(Transfer, tid).client_id
            session.get(Client, client_id, with_for_update=True)
            transfer = session.get(Transfer, tid, with_for_update=True, populate_existing=True)
            if transfer.status in TERMINAL or transfer.lease_owner != owner:
                return
            if status == "EXHAUSTED":
                settle(session, transfer, "FAILED", "providers_exhausted")
            else:
                attempt = session.get(Attempt, aid, with_for_update=True)
                if payout_id and attempt.payout_id and attempt.payout_id != payout_id:
                    status, result = "UNKNOWN", "CONFLICTING_PAYOUT_ID"
                attempt.status, attempt.result, attempt.updated_at = status, result, now()
                if status == "UNKNOWN" and result != "SUBMIT_HTTP_5XX":
                    attempt.uncertain = True
                if payout_id and not attempt.payout_id:
                    attempt.payout_id = payout_id
                if status == "COMPLETED":
                    settle(session, transfer, "COMPLETED", "provider_completed")
                elif status == "FAILED":
                    transition(transfer, "PROCESSING", "worker", "provider_failed_try_next")
                elif (now() - transfer.created_at).total_seconds() >= self.config.review_after_sec:
                    transition(transfer, "NEEDS_REVIEW", "worker", "reconciliation_deadline")
                elif status == "UNKNOWN":
                    transition(transfer, "UNKNOWN", "worker", result or "ambiguous")
                else:
                    transition(transfer, "PROCESSING", "worker", "provider_accepted")
            transfer.next_run = now() + timedelta(seconds=self.config.poll_interval_sec)
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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    worker = Worker()
    while not stop.is_set():
        if not worker.tick():
            stop.wait(0.25)


if __name__ == "__main__":
    main()

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select, text

from app.clock import now
from app.db import Account, Client, Quote, Transfer


def test_quote_expiring_while_waiting_for_client_lock_is_rejected(
    api, db, transfer_request, monkeypatch
):
    before = now()
    expiry = before + timedelta(minutes=5)
    clock = [before]
    monkeypatch.setattr("app.api.now", lambda: clock[0])
    monkeypatch.setattr("app.service.now", lambda: clock[0])
    with db.begin() as session:
        session.get(Quote, transfer_request["quote_id"]).expires_at = expiry
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.begin() as blocker:
            blocker.get(Client, "c_001", with_for_update=True)
            future = pool.submit(
                api.post,
                "/transfers",
                json=transfer_request,
                headers={"Idempotency-Key": "quote-lock-expiry"},
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with db() as observer:
                    waiting = observer.scalar(
                        text("""
                        SELECT count(*) FROM pg_stat_activity
                        WHERE datname = current_database() AND wait_event_type = 'Lock'
                          AND query LIKE '%clients%' AND query LIKE '%FOR UPDATE%'
                    """)
                    )
                if waiting:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Transfer request did not wait for the client lock")
            clock[0] = expiry + timedelta(seconds=1)
        response = future.result(timeout=5)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "QUOTE_EXPIRED"
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Transfer)) == 0
        assert session.get(Account, ("c_001", "RUB")).held == 0

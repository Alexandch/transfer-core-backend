"""Check the HTTP API against the locally authored provider's actual payout ledger."""

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from app.catalog import Catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfers", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--provider", default="http://localhost:9000")
    parser.add_argument("--client", default="c_load")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data"
    )
    args = parser.parse_args()
    run = str(uuid4())
    with httpx.Client(base_url=args.api, timeout=15) as client:
        starting = client.get(f"/clients/{args.client}/balance").raise_for_status().json()

        def create(index):
            quote = (
                client.post(
                    "/quotes",
                    json={
                        "source_country": "RU",
                        "source_currency": "RUB",
                        "target_country": "TH",
                        "target_currency": "THB",
                        "payout_method": "BANK_ACCOUNT",
                        "amount": "1000.00",
                        "amount_type": "SOURCE",
                    },
                )
                .raise_for_status()
                .json()
            )
            body = {
                "quote_id": quote["quote_id"],
                "client_id": args.client,
                "recipient": {
                    "name": "Demo Recipient",
                    "account": f"demo-{index}",
                    "bank_code": "DEMO",
                },
            }
            key = {"Idempotency-Key": f"chaos-{run}-{index}"}
            result = client.post("/transfers", json=body, headers=key).raise_for_status().json()
            repeated = client.post("/transfers", json=body, headers=key).raise_for_status().json()
            assert result["transfer_id"] == repeated["transfer_id"]
            return result["transfer_id"]

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            ids = list(pool.map(create, range(args.transfers)))
        deadline, pending, results = time.monotonic() + args.timeout, set(ids), {}
        while pending and time.monotonic() < deadline:
            for tid in list(pending):
                result = client.get(f"/transfers/{tid}").raise_for_status().json()
                results[tid] = result
                if result["status"] in {"COMPLETED", "FAILED"}:
                    pending.remove(tid)
            if pending:
                time.sleep(0.5)
        unresolved = [tid for tid in pending if results[tid]["status"] != "NEEDS_REVIEW"]
        assert not unresolved, f"Transfers neither terminal nor reviewable: {len(unresolved)}"
        ledger = []
        with httpx.Client(base_url=args.provider) as provider:
            for definition in Catalog(args.data_dir).providers:
                pid = definition["provider_id"]
                ledger.extend(provider.get(f"/{pid}/admin/ledger").raise_for_status().json())
        counts = Counter(row["reference"] for row in ledger)
        by_reference = {row["reference"]: row for row in ledger}
        for result in results.values():
            attempts = result["attempts"]
            paid = sum(counts[a["reference"]] for a in attempts)
            assert paid <= 1, f"Double payout: {result['transfer_id']}"
            if result["status"] in {"COMPLETED", "FAILED"}:
                assert paid == int(result["status"] == "COMPLETED"), result
            for attempt in attempts:
                row = by_reference.get(attempt["reference"])
                if row:
                    assert row["amount"] == result["target_amount"]["amount"]
                    assert row["currency"] == result["target_amount"]["currency"]
        ending = client.get(f"/clients/{args.client}/balance").raise_for_status().json()
        for currency, account in ending["balances"].items():
            assert sum(Decimal(account[k]) for k in ("available", "held", "spent")) == Decimal(
                account["initial"]
            )
            completed = sum(
                Decimal(r["source_amount"]["amount"])
                for r in results.values()
                if r["status"] == "COMPLETED" and r["source_amount"]["currency"] == currency
            )
            held = sum(
                Decimal(r["source_amount"]["amount"])
                for r in results.values()
                if r["status"] == "NEEDS_REVIEW" and r["source_amount"]["currency"] == currency
            )
            before = starting["balances"][currency]
            assert Decimal(account["spent"]) - Decimal(before["spent"]) == completed
            assert Decimal(account["held"]) - Decimal(before["held"]) == held
        print(
            json.dumps(
                {
                    "transfers": len(ids),
                    "statuses": dict(Counter(r["status"] for r in results.values())),
                    "double_payouts": 0,
                    "unexplained_holds": 0,
                    "emulator": "locally-authored-demo",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

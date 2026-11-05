"""Measure HTTP latency/throughput and verify completion through the demo ledger."""

import argparse
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from app.catalog import Catalog


def percentiles(values):
    values = sorted(values)

    def at(percent):
        return round(values[max(0, math.ceil(len(values) * percent) - 1)], 2)

    return {"p50_ms": at(0.50), "p95_ms": at(0.95), "p99_ms": at(0.99)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfers", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--provider", default="http://localhost:9000")
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data"
    )
    args = parser.parse_args()
    if args.transfers <= 0 or args.concurrency <= 0:
        parser.error("transfers and concurrency must be positive")
    run = str(uuid4())
    with httpx.Client(
        base_url=args.api, timeout=30, limits=httpx.Limits(max_connections=args.concurrency + 2)
    ) as client:
        before = client.get("/clients/c_load/balance").raise_for_status().json()["balances"]["RUB"]
        started = time.monotonic()

        def create(index):
            qstart = time.monotonic()
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
            qlatency = (time.monotonic() - qstart) * 1000
            tstart = time.monotonic()
            result = (
                client.post(
                    "/transfers",
                    json={
                        "quote_id": quote["quote_id"],
                        "client_id": "c_load",
                        "recipient": {
                            "name": "Load Test",
                            "account": f"load-{run}-{index}",
                            "bank_code": "DEMO",
                        },
                    },
                    headers={"Idempotency-Key": f"load-{run}-{index}", "X-Scenario": "ok"},
                )
                .raise_for_status()
                .json()
            )
            return result["transfer_id"], qlatency, (time.monotonic() - tstart) * 1000

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            rows = list(pool.map(create, range(args.transfers)))
        accepted_seconds = time.monotonic() - started
        pending = {tid for tid, _, _ in rows}
        results = {}
        deadline = time.monotonic() + args.timeout
        while pending and time.monotonic() < deadline:
            for tid in list(pending):
                result = client.get(f"/transfers/{tid}").raise_for_status().json()
                if result["status"] in {"COMPLETED", "FAILED"}:
                    results[tid] = result
                    pending.remove(tid)
            if pending:
                time.sleep(0.25)
        assert not pending, f"{len(pending)} transfers did not finish"
        assert all(r["status"] == "COMPLETED" for r in results.values())
        ledger = []
        with httpx.Client(base_url=args.provider, timeout=30) as provider:
            for definition in Catalog(args.data_dir).providers:
                pid = definition["provider_id"]
                ledger += provider.get(f"/{pid}/admin/ledger").raise_for_status().json()
        actual = Counter(row["reference"] for row in ledger)
        for result in results.values():
            assert sum(actual[a["reference"]] for a in result["attempts"]) == 1
        after = client.get("/clients/c_load/balance").raise_for_status().json()["balances"]["RUB"]
        assert (
            Decimal(after["spent"]) - Decimal(before["spent"]) == Decimal("1000") * args.transfers
        )
        assert Decimal(after["held"]) == Decimal(before["held"])
        report = {
            "transfers": args.transfers,
            "concurrency": args.concurrency,
            "accepted_transfers_per_sec": round(args.transfers / accepted_seconds, 2),
            "total_seconds": round(time.monotonic() - started, 2),
            "quotes": percentiles([q for _, q, _ in rows]),
            "transfers_api": percentiles([t for _, _, t in rows]),
            "http_errors": 0,
            "double_payouts": 0,
            "unexplained_holds": 0,
        }
        output = json.dumps(report, indent=2)
        print(output)
        if args.output:
            Path(args.output).write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

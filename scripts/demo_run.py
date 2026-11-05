"""Create real demo transfers through the HTTP API and verify their financial outcomes."""

import argparse
import json
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

from app.catalog import Catalog


def wait_for_scrape(client, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = (
            client.get("/api/v1/query", params={"query": 'up{job="transfer-core"}'})
            .raise_for_status()
            .json()
        )
        values = result["data"]["result"]
        if values and values[0]["value"][1] == "1":
            return
        time.sleep(0.5)
    raise AssertionError("Prometheus has not successfully scraped the API")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--mock", default="http://127.0.0.1:9000")
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--client", default="c_101")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    run = str(uuid4())
    results, requests, headers, seen = {}, {}, {}, {}
    with (
        httpx.Client(base_url=args.api, timeout=15) as api,
        httpx.Client(base_url=args.prometheus, timeout=10) as prometheus,
    ):
        wait_for_scrape(prometheus)
        before = (
            api.get(f"/clients/{args.client}/balance").raise_for_status().json()["balances"]["RUB"]
        )
        assert Decimal(before["held"]) == 0, "Choose a demo client without outstanding holds"
        for scenario in ("ok", "decline", "error_5xx", "slow"):
            request = {
                "source_country": "RU",
                "source_currency": "RUB",
                "target_country": "TH",
                "target_currency": "THB",
                "payout_method": "BANK_ACCOUNT",
                "amount": "4137.00" if scenario == "slow" else "10000.00",
                "amount_type": "TARGET" if scenario == "slow" else "SOURCE",
            }
            quote = api.post("/quotes", json=request).raise_for_status().json()
            requests[scenario] = {
                "quote_id": quote["quote_id"],
                "client_id": args.client,
                "recipient": {
                    "name": "Demo Recipient",
                    "account": f"demo-{run}-{scenario}",
                    "bank_code": "DEMO",
                },
            }
            headers[scenario] = {
                "Idempotency-Key": f"demo-{run}-{scenario}",
                "X-Scenario": scenario,
            }
            created = (
                api.post("/transfers", json=requests[scenario], headers=headers[scenario])
                .raise_for_status()
                .json()
            )
            repeated = (
                api.post("/transfers", json=requests[scenario], headers=headers[scenario])
                .raise_for_status()
                .json()
            )
            assert created["transfer_id"] == repeated["transfer_id"]
            results[scenario] = created
            seen[scenario] = {created["status"]}
        deadline = time.monotonic() + args.timeout
        pending = set(results)
        while pending and time.monotonic() < deadline:
            for scenario in list(pending):
                tid = results[scenario]["transfer_id"]
                result = api.get(f"/transfers/{tid}").raise_for_status().json()
                seen[scenario].add(result["status"])
                results[scenario] = result
                if result["status"] in {"COMPLETED", "FAILED"}:
                    pending.remove(scenario)
            if pending:
                time.sleep(0.5)
        assert not pending, f"Demo did not finish: {sorted(pending)}"
        for scenario, result in results.items():
            assert result["status"] == ("COMPLETED" if scenario in {"ok", "slow"} else "FAILED")
            repeated = (
                api.post("/transfers", json=requests[scenario], headers=headers[scenario])
                .raise_for_status()
                .json()
            )
            assert repeated["transfer_id"] == result["transfer_id"]
        changed = {
            **requests["ok"],
            "recipient": {**requests["ok"]["recipient"], "account": "changed"},
        }
        conflict = api.post("/transfers", json=changed, headers=headers["ok"])
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        after = (
            api.get(f"/clients/{args.client}/balance").raise_for_status().json()["balances"]["RUB"]
        )
        spent = sum(
            Decimal(r["source_amount"]["amount"])
            for r in results.values()
            if r["status"] == "COMPLETED"
        )
        assert Decimal(after["spent"]) - Decimal(before["spent"]) == spent
        assert Decimal(after["held"]) == 0
        assert sum(Decimal(after[k]) for k in ("available", "held", "spent")) == Decimal(
            after["initial"]
        )
        ledger = []
        with httpx.Client(base_url=args.mock, timeout=15) as mock:
            for provider in Catalog(Path(__file__).resolve().parents[1] / "data").providers:
                rows = (
                    mock.get(f"/{provider['provider_id']}/admin/ledger")
                    .raise_for_status()
                    .json()["entries"]
                )
                ledger += [r for r in rows if r["status"] == "COMPLETED"]
        paid = Counter(r["reference"] for r in ledger)
        summary = {}
        for scenario, result in results.items():
            assert sum(paid[a["reference"]] for a in result["attempts"]) == int(
                result["status"] == "COMPLETED"
            )
            tid = result["transfer_id"]
            routing = api.get(f"/transfers/{tid}/routing").raise_for_status().json()
            summary[scenario] = {
                "transfer_id": tid,
                "status": result["status"],
                "observed_statuses": sorted(seen[scenario]),
                "providers": [a["provider_id"] for a in result["attempts"]],
                "routing_config_version": routing["config_version"],
            }
        # Wait for the final financial state to reach the Prometheus TSDB, not just /metrics.
        deadline = time.monotonic() + 20
        query = (
            f'transfer_core_balance_minor_units{{client_id="{args.client}",'
            'currency="RUB",kind="spent"}'
        )
        expected = Decimal(after["spent"]) * 100
        while time.monotonic() < deadline:
            samples = (
                prometheus.get("/api/v1/query", params={"query": query})
                .raise_for_status()
                .json()["data"]["result"]
            )
            if samples and Decimal(samples[0]["value"][1]) == expected:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("Final demo balance has not appeared in Prometheus")
        report = {
            "run_id": run,
            "client_id": args.client,
            "transfers": summary,
            "balance_before": before,
            "balance_after": after,
            "idempotency": "passed",
            "ledger": "passed",
            "prometheus_scrape": "passed",
        }
        output = json.dumps(report, ensure_ascii=False, indent=2)
        print(output)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

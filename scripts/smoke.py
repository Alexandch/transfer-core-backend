import json
import time
from uuid import uuid4

import httpx


def main():
    results = {}
    with httpx.Client(base_url="http://localhost:8080", timeout=15) as client:
        for scenario in ("ok", "decline", "error_5xx", "slow"):
            quote = (
                client.post(
                    "/quotes",
                    json={
                        "source_country": "RU",
                        "source_currency": "RUB",
                        "target_country": "TH",
                        "target_currency": "THB",
                        "payout_method": "BANK_ACCOUNT",
                        "amount": "10000.00",
                        "amount_type": "SOURCE",
                    },
                )
                .raise_for_status()
                .json()
            )
            transfer = (
                client.post(
                    "/transfers",
                    json={
                        "quote_id": quote["quote_id"],
                        "client_id": "c_load",
                        "recipient": {"name": "Smoke Test", "account": "demo", "bank_code": "DEMO"},
                    },
                    headers={"Idempotency-Key": str(uuid4()), "X-Scenario": scenario},
                )
                .raise_for_status()
                .json()
            )
            results[scenario] = transfer
        deadline = time.monotonic() + 90
        pending = set(results)
        while pending and time.monotonic() < deadline:
            for scenario in list(pending):
                tid = results[scenario]["transfer_id"]
                results[scenario] = client.get(f"/transfers/{tid}").raise_for_status().json()
                if results[scenario]["status"] in {"COMPLETED", "FAILED"}:
                    pending.remove(scenario)
            if pending:
                time.sleep(0.5)
        assert not pending, results
        for scenario, result in results.items():
            expected = "COMPLETED" if scenario in {"ok", "slow"} else "FAILED"
            assert result["status"] == expected, result
        print(
            json.dumps(
                {
                    key: {"status": value["status"], "attempts": len(value["attempts"])}
                    for key, value in results.items()
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

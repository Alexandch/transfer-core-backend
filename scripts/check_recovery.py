"""Crash only the isolated audit worker after provider acceptance, then verify recovery."""

import argparse
import json
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.audit.yaml"), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="c_102")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    run = str(uuid4())
    compose("stop", "worker")
    with (
        httpx.Client(base_url="http://127.0.0.1:18080", timeout=10) as api,
        httpx.Client(base_url="http://127.0.0.1:19000", timeout=10) as mock,
    ):
        before = (
            api.get(f"/clients/{args.client}/balance").raise_for_status().json()["balances"]["RUB"]
        )
        assert Decimal(before["held"]) == 0, "Audit client has another outstanding operation"
        quote = (
            api.post(
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
        created = (
            api.post(
                "/transfers",
                json={
                    "quote_id": quote["quote_id"],
                    "client_id": args.client,
                    "recipient": {
                        "name": "Recovery Test",
                        "account": f"recovery-{run}",
                        "bank_code": "DEMO",
                    },
                },
                headers={"Idempotency-Key": f"recovery-{run}", "X-Scenario": "slow"},
            )
            .raise_for_status()
            .json()
        )
        tid = created["transfer_id"]
        compose("up", "-d", "--no-deps", "--scale", "worker=1", "worker")
        deadline = time.monotonic() + 15
        accepted = None
        while time.monotonic() < deadline:
            result = api.get(f"/transfers/{tid}").raise_for_status().json()
            if result["attempts"]:
                attempt = result["attempts"][0]
                response = mock.get(
                    f"/{attempt['provider_id']}/payouts", params={"reference": attempt["reference"]}
                )
                if response.status_code == 200:
                    accepted = response.json()
                    break
            time.sleep(0.02)
        assert accepted is not None, "Provider did not accept the payout"
        assert attempt["status"] == "SUBMITTING" and attempt["payout_id"] is None, result
        compose("kill", "--signal", "SIGKILL", "worker")
        crashed = api.get(f"/transfers/{tid}").raise_for_status().json()["attempts"][0]
        assert crashed["status"] == "SUBMITTING" and crashed["payout_id"] is None
        compose("up", "-d", "--no-deps", "worker")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = api.get(f"/transfers/{tid}").raise_for_status().json()
            if result["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        assert result["status"] == "COMPLETED", result
        assert len(result["attempts"]) == 1 and result["attempts"][0]["sends"] == 1
        rows = (
            mock.get(f"/{attempt['provider_id']}/admin/ledger").raise_for_status().json()["entries"]
        )
        rows = [row for row in rows if row["reference"] == attempt["reference"]]
        assert len(rows) == 1 and rows[0]["status"] == "COMPLETED"
        after = (
            api.get(f"/clients/{args.client}/balance").raise_for_status().json()["balances"]["RUB"]
        )
        assert Decimal(after["spent"]) - Decimal(before["spent"]) == Decimal("10000.00")
        assert Decimal(after["held"]) == 0
        report = {
            "transfer_id": tid,
            "status": result["status"],
            "killed_before_response_recorded": True,
            "attempts": 1,
            "submissions": 1,
            "actual_payouts": 1,
            "spent_delta": "10000.00",
            "held_after": after["held"],
        }
        output = json.dumps(report, indent=2)
        print(output)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

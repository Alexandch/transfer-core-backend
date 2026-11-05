"""Run the unmodified starter quote examples against a live API."""

import argparse
import json
from pathlib import Path

import httpx


def check_subset(expected, actual):
    for key, value in expected.items():
        if isinstance(value, dict):
            check_subset(value, actual[key])
        else:
            assert actual[key] == value, f"{key}: {actual[key]!r} != {value!r}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument(
        "--examples", type=Path, default=Path(__file__).resolve().parents[1] / "examples"
    )
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.api, timeout=30) as client:
        for path in sorted(args.examples.glob("[0-9]*.json")):
            example = json.loads(path.read_text(encoding="utf-8"))
            response = client.post("/quotes", json=example["request"])
            assert response.status_code == example["response"].get("status", 422), response.text
            check_subset(example["response"]["body"], response.json())
            results.append({"example": path.name, "status": response.status_code, "passed": True})
    assert results, "No examples found"
    report = {"passed": len(results), "failed": 0, "examples": results}
    output = json.dumps(report, indent=2)
    print(output)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

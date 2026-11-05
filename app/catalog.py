import json
from pathlib import Path


def records(path: Path, key: str) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        return value
    if key in value and isinstance(value[key], list):
        return value[key]
    return [value]


class Catalog:
    """A fresh snapshot per request/job allows atomic data-file replacement without a redeploy."""

    def __init__(self, root: Path):
        self.currencies = {
            x["code"]: x["minor_units"] for x in records(root / "currencies.json", "currencies")
        }
        self.tariffs = records(root / "tariffs.json", "tariffs")
        self.providers = records(root / "providers.json", "providers")
        self.clients = records(root / "clients.json", "clients")
        for digits in self.currencies.values():
            if not isinstance(digits, int) or not 0 <= digits <= 8:
                raise ValueError("minor_units must be an integer between 0 and 8")

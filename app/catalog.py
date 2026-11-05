import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from app.errors import DomainError
from app.provider_config import ProviderDefinition


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
        try:
            self.providers = [
                ProviderDefinition.model_validate(p).model_dump(mode="json", by_alias=True)
                for p in records(root / "providers.json", "providers")
            ]
            ids = [p["provider_id"] for p in self.providers]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate provider_id")
            if any(p["currency"] not in self.currencies for p in self.providers):
                raise ValueError("Unknown provider currency")
        except (OSError, ValueError, ValidationError) as exc:
            raise DomainError(
                "CONFIG_INVALID", "Invalid provider configuration", 503, {"file": "providers.json"}
            ) from exc
        self.version = hashlib.sha256(
            json.dumps(self.providers, sort_keys=True).encode()
        ).hexdigest()
        self.clients = records(root / "clients.json", "clients")
        for digits in self.currencies.values():
            if not isinstance(digits, int) or not 0 <= digits <= 8:
                raise ValueError("minor_units must be an integer between 0 and 8")

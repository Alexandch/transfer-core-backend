import httpx

from app.config import Settings
from app.errors import DomainError


class ProviderGateway:
    def __init__(self, config: Settings, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.provider_url,
            timeout=config.provider_timeout_sec,
            follow_redirects=False,
        )

    def rates(self) -> list[dict]:
        try:
            response = self.client.get("/rates")
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, list):
                raise ValueError("Expected a rate list")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError("RATE_UNAVAILABLE", "Rate service is unavailable", 503) from exc

    def submit(
        self, provider_id: str, reference: str, quote: dict, recipient: dict, scenario: str | None
    ) -> httpx.Response:
        headers = {"Idempotency-Key": reference}
        if scenario:
            headers["X-Scenario"] = scenario
        return self.client.post(
            f"/{provider_id}/payouts",
            headers=headers,
            json={
                "reference": reference,
                **quote["target_amount"],
                "recipient": recipient,
                "callback_url": f"{self.config.callback_base_url}/webhooks/{provider_id}",
            },
        )

    def lookup(self, provider_id: str, reference: str) -> httpx.Response:
        return self.client.get(f"/{provider_id}/payouts", params={"reference": reference})

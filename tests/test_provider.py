import httpx
import pytest

from app.errors import DomainError
from app.provider import ProviderGateway


def test_gateway_contract(config):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/rates":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"payout_id": "po_test", "status": "PROCESSING"})

    gateway = ProviderGateway(
        config, httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
    )
    assert gateway.rates() == []
    quote = {"target_amount": {"amount": "4137.00", "currency": "THB"}}
    assert gateway.submit("prov_alpha", "reference", quote, {}, "ok").status_code == 201
    assert seen[-1].headers["Idempotency-Key"] == "reference"
    assert seen[-1].headers["X-Scenario"] == "ok"
    assert b'"callback_url":"http://api:8080/webhooks/prov_alpha"' in seen[-1].content
    gateway.lookup("prov_alpha", "reference")
    assert seen[-1].url.params["reference"] == "reference"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, text="bad-json"), httpx.Response(200, json={})],
)
def test_rate_service_errors(config, response):
    gateway = ProviderGateway(
        config,
        httpx.Client(base_url="http://mock", transport=httpx.MockTransport(lambda _: response)),
    )
    with pytest.raises(DomainError) as caught:
        gateway.rates()
    assert caught.value.code == "RATE_UNAVAILABLE"

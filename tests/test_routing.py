from copy import deepcopy
from datetime import timedelta

from app.catalog import records
from app.pricing import calculate
from app.rates import Rates
from app.routing import select_providers


def test_filters_and_deterministic_ranking(catalog, at, config, quote_request):
    rates = Rates(records(config.data_dir / "rates.json", "rates"), at, 3600)
    quote = calculate(quote_request, catalog, rates, at)
    plan = select_providers(quote, catalog, rates, at)
    assert [p["provider_id"] for p in plan["candidates"]] == [
        "prov_alpha",
        "prov_beta",
        "prov_gamma",
    ]
    for reason in ("disabled", "direction", "maintenance", "limits", "margin"):
        changed = deepcopy(catalog)
        provider = changed.providers[0]
        if reason == "disabled":
            provider["enabled"] = False
        elif reason == "direction":
            provider["directions"] = []
        elif reason == "maintenance":
            provider["maintenance_windows"] = [
                {
                    "from": (at - timedelta(hours=1)).isoformat(),
                    "to": (at + timedelta(hours=1)).isoformat(),
                }
            ]
        elif reason == "limits":
            provider["limits"]["max"] = "1.00"
        else:
            provider["fee"]["fixed"] = "100.00"
        result = select_providers(quote, changed, rates, at)
        assert result["rejected"][0]["reason"] == reason
        assert "prov_alpha" not in [p["provider_id"] for p in result["candidates"]]


def test_missing_provider_fx_does_not_exclude_other_candidates(catalog, at, config, quote_request):
    rows = [r for r in records(config.data_dir / "rates.json", "rates") if r["quote"] != "KWD"]
    rates = Rates(rows, at, 3600)
    quote = calculate(quote_request, catalog, rates, at)
    catalog.providers[0]["currency"] = "KWD"
    plan = select_providers(quote, catalog, rates, at)
    assert plan["rejected"] == [{"provider_id": "prov_alpha", "reason": "rate_unavailable"}]
    assert [p["provider_id"] for p in plan["candidates"]] == ["prov_beta", "prov_gamma"]

from datetime import datetime
from decimal import localcontext

from app.catalog import Catalog
from app.money import D, fee, fmt, minor, quantize, units
from app.rates import Rates


def select_providers(quote: dict, catalog: Catalog, rates: Rates, at: datetime) -> dict:
    tariff = next(t for t in catalog.tariffs if t["direction_id"] == quote["direction_id"])
    source = quote["source_amount"]["currency"]
    target = quote["target_amount"]["currency"]
    source_value = D(quote["source_amount"]["amount"])
    fee_value = D(quote["fee"]["amount"]) * rates.get(quote["fee"]["currency"], source)
    eligible, rejected = [], []
    with localcontext() as ctx:
        ctx.prec = 60
        for provider in catalog.providers:
            pid, reason = provider["provider_id"], None
            if not provider["enabled"]:
                reason = "disabled"
            elif quote["direction_id"] not in provider["directions"]:
                reason = "direction"
            elif any(
                datetime.fromisoformat(w["from"].replace("Z", "+00:00"))
                <= at
                < datetime.fromisoformat(w["to"].replace("Z", "+00:00"))
                for w in provider.get("maintenance_windows", [])
            ):
                reason = "maintenance"
            if reason:
                rejected.append({"provider_id": pid, "reason": reason})
                continue
            currency = provider["currency"]
            digits = catalog.currencies[currency]
            principal = D(quote["target_amount"]["amount"]) * rates.get(target, currency)
            if not D(provider["limits"]["min"]) <= principal <= D(provider["limits"]["max"]):
                rejected.append({"provider_id": pid, "reason": "limits"})
                continue
            principal_minor = quantize(principal, digits, up=True)
            cost_minor = fee(principal_minor, digits, provider["fee"])
            cost_source = units(cost_minor, digits) * rates.get(currency, source)
            margin_percent = (fee_value - cost_source) / source_value * 100
            if margin_percent < D(tariff["min_margin_percent"]):
                rejected.append(
                    {"provider_id": pid, "reason": "margin", "margin_percent": str(margin_percent)}
                )
                continue
            # Fixed scales avoid a candidate's score depending on its competitors.
            score = (
                D("0.5") * cost_source / source_value
                + D("0.35") * (1 - D(provider["success_rate_24h"]))
                + D("0.15") * D(provider["p95_latency_ms"]) / 30000
            )
            eligible.append(
                {
                    "provider_id": pid,
                    "score": str(score),
                    "provider_fee": {"amount": fmt(cost_minor, digits), "currency": currency},
                    "margin_percent": str(margin_percent),
                }
            )
    eligible.sort(key=lambda x: (D(x["score"]), x["provider_id"]))
    # Validate the frozen debit here, before committing a hold.
    minor(quote["source_amount"]["amount"], catalog.currencies[source])
    return {
        "candidates": eligible,
        "rejected": rejected,
        "policy": "0.50*cost/source + 0.35*(1-success_rate) + 0.15*latency/30000",
    }

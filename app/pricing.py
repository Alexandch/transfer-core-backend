from datetime import datetime, timedelta
from decimal import localcontext
from uuid import uuid4

from app.catalog import Catalog
from app.clock import iso
from app.errors import DomainError
from app.money import D, fee, fmt, minor, quantize, units
from app.rates import Rates


def calculate(request: dict, catalog: Catalog, rates: Rates, at: datetime) -> dict:
    tariff = next(
        (
            t
            for t in catalog.tariffs
            if (
                t["source"]["country"] == request["source_country"]
                and t["source"]["currency"] == request["source_currency"]
                and t["target"]["country"] == request["target_country"]
                and t["target"]["currency"] == request["target_currency"]
                and t["payout_method"] == request["payout_method"]
            )
        ),
        None,
    )
    if tariff is None:
        raise DomainError("UNSUPPORTED_DIRECTION", "Direction or payout method is unsupported")
    source, target = request["source_currency"], request["target_currency"]
    sd, td = catalog.currencies[source], catalog.currencies[target]
    rate = rates.get(source, target)
    charged = tariff["fee"]["charged_in"]
    if charged not in {"SOURCE", "TARGET"} or not D(0) <= D(tariff["fee"]["percent"]) < 100:
        raise ValueError("Invalid tariff fee configuration")

    def forward(source_minor: int) -> tuple[int, int, int]:
        if charged == "SOURCE":
            cost = fee(source_minor, sd, tariff["fee"])
            convertible = max(0, source_minor - cost)
            return cost, convertible, quantize(units(convertible, sd) * rate, td)
        gross = quantize(units(source_minor, sd) * rate, td)
        cost = fee(gross, td, tariff["fee"])
        return cost, source_minor, max(0, gross - cost)

    requested_digits = sd if request["amount_type"] == "SOURCE" else td
    requested = minor(request["amount"], requested_digits)
    if requested <= 0:
        raise DomainError("VALIDATION_ERROR", "Amount must be positive")
    limits = tariff["amount_limits"]
    ld = catalog.currencies[limits["currency"]]
    limit_rate = rates.get(source, limits["currency"])
    limit_min, limit_max = D(limits["min"]), D(limits["max"])
    with localcontext() as ctx:
        ctx.prec = 60
        if request["amount_type"] == "SOURCE":
            source_minor = requested
        else:
            # Exact monotone inversion of the rounded forward function; no floating point.
            lo, hi = 1, quantize(limit_max / limit_rate, sd)
            if forward(hi)[2] < requested:
                raise DomainError(
                    "AMOUNT_ABOVE_MAX",
                    "Required source exceeds direction maximum",
                    details={"max": fmt(minor(limits["max"], ld), ld)},
                )
            while lo < hi:
                mid = (lo + hi) // 2
                if forward(mid)[2] >= requested:
                    hi = mid
                else:
                    lo = mid + 1
            source_minor = lo
        comparable = units(source_minor, sd) * limit_rate
        if comparable < limit_min:
            raise DomainError(
                "AMOUNT_BELOW_MIN",
                "Amount is below direction minimum",
                details={"min": limits["min"]},
            )
        if comparable > limit_max:
            raise DomainError(
                "AMOUNT_ABOVE_MAX",
                "Amount is above direction maximum",
                details={"max": limits["max"]},
            )
        cost, convertible, attainable = forward(source_minor)
    target_minor = requested if request["amount_type"] == "TARGET" else attainable
    if target_minor <= 0 or convertible <= 0:
        raise DomainError("VALIDATION_ERROR", "Fee consumes the entire transfer")

    def money(value: int, currency: str) -> dict:
        return {"amount": fmt(value, catalog.currencies[currency]), "currency": currency}

    return {
        "quote_id": str(uuid4()),
        "direction_id": tariff["direction_id"],
        "source_amount": money(source_minor, source),
        "fee": money(cost, source if charged == "SOURCE" else target),
        "amount_to_convert": money(convertible, source),
        "rate": str(rate),
        "target_amount": money(target_minor, target),
        "breakdown": [
            {
                "step": "fee",
                "rule": "ceil(max(percent * gross + fixed, minimum))",
                "charged_in": charged,
            },
            {"step": "conversion", "rounding": "FLOOR", "rate": str(rate)},
            {
                "step": "target_rounding_surplus",
                "amount": fmt(attainable - target_minor, td),
                "currency": target,
            },
        ],
        "expires_at": iso(at + timedelta(seconds=tariff["quote_ttl_sec"])),
    }

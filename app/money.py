import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from app.errors import DomainError

D = Decimal
MAX_MINOR = 10**15


def minor(value: str, digits: int) -> int:
    pattern = r"(?:0|[1-9][0-9]*)" + (rf"\.[0-9]{{{digits}}}" if digits else "")
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise DomainError("VALIDATION_ERROR", f"Expected a money string with {digits} decimals")
    with localcontext() as ctx:
        ctx.prec = 60
        result = int(D(value) * 10**digits)
    if result > MAX_MINOR:
        raise DomainError("VALIDATION_ERROR", "Amount is too large")
    return result


def units(value: int, digits: int) -> Decimal:
    return D(value).scaleb(-digits)


def quantize(value: Decimal, digits: int, *, up: bool = False) -> int:
    with localcontext() as ctx:
        ctx.prec = 60
        return int(
            (value * 10**digits).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        )


def fmt(value: int, digits: int) -> str:
    return f"{units(value, digits):.{digits}f}"


def fee(amount: int, digits: int, tariff: dict) -> int:
    with localcontext() as ctx:
        ctx.prec = 60
        computed = units(amount, digits) * D(tariff["percent"]) / 100 + D(tariff["fixed"])
        return quantize(max(computed, D(tariff.get("min", "0"))), digits, up=True)

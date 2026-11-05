from copy import deepcopy
from datetime import timedelta

import pytest

from app.catalog import records
from app.errors import DomainError
from app.money import D, fee, fmt, minor, quantize
from app.pricing import calculate
from app.rates import Rates


@pytest.fixture
def rates(at, config):
    return Rates(records(config.data_dir / "rates.json", "rates"), at, 3600)


@pytest.mark.parametrize(
    "amount,commission,target",
    [
        ("10000.00", "150.00", "4137.00"),
        ("2000.00", "50.00", "819.00"),
        ("1000.00", "50.00", "399.00"),
        ("300000.00", "4500.00", "124110.00"),
        ("10000.01", "150.01", "4137.00"),
    ],
)
def test_source_examples(catalog, rates, at, quote_request, amount, commission, target):
    result = calculate({**quote_request, "amount": amount}, catalog, rates, at)
    assert result["fee"]["amount"] == commission
    assert result["target_amount"]["amount"] == target


def test_target_example(catalog, rates, at, quote_request):
    result = calculate(
        {**quote_request, "amount": "4137.00", "amount_type": "TARGET"}, catalog, rates, at
    )
    assert result["source_amount"]["amount"] == "10000.00"
    assert result["fee"]["amount"] == "150.00"


@pytest.mark.parametrize("method,amount", [("BANK_ACCOUNT", "4137.01"), ("CARD", "4113.00")])
def test_reverse_is_minimal(catalog, rates, at, quote_request, method, amount):
    request = {**quote_request, "payout_method": method}
    reverse = calculate({**request, "amount_type": "TARGET", "amount": amount}, catalog, rates, at)
    source_minor = minor(reverse["source_amount"]["amount"], 2)
    forward = calculate({**request, "amount": fmt(source_minor, 2)}, catalog, rates, at)
    previous = calculate({**request, "amount": fmt(source_minor - 1, 2)}, catalog, rates, at)
    assert D(forward["target_amount"]["amount"]) >= D(amount)
    assert D(previous["target_amount"]["amount"]) < D(amount)
    assert reverse["target_amount"]["amount"] == amount


def test_target_currency_fee(catalog, rates, at, quote_request):
    result = calculate({**quote_request, "payout_method": "CARD"}, catalog, rates, at)
    assert result["fee"] == {"amount": "87.00", "currency": "THB"}
    assert result["target_amount"]["amount"] == "4113.00"
    assert result["amount_to_convert"]["amount"] == "10000.00"


@pytest.mark.parametrize(
    "country,currency,expected", [("JP", "JPY", "14775"), ("KW", "KWD", "29.395")]
)
def test_currency_precision(catalog, rates, at, quote_request, country, currency, expected):
    result = calculate(
        {**quote_request, "target_country": country, "target_currency": currency},
        catalog,
        rates,
        at,
    )
    assert result["target_amount"]["amount"] == expected


@pytest.mark.parametrize(
    "amount,code",
    [
        ("500.00", "AMOUNT_BELOW_MIN"),
        ("300000.01", "AMOUNT_ABOVE_MAX"),
        ("0.00", "VALIDATION_ERROR"),
        ("1e4", "VALIDATION_ERROR"),
        ("1000.001", "VALIDATION_ERROR"),
        ("1000", "VALIDATION_ERROR"),
        ("NaN", "VALIDATION_ERROR"),
        ("-1000.00", "VALIDATION_ERROR"),
    ],
)
def test_amount_errors(catalog, rates, at, quote_request, amount, code):
    with pytest.raises(DomainError) as caught:
        calculate({**quote_request, "amount": amount}, catalog, rates, at)
    assert caught.value.code == code


def test_unsupported(catalog, rates, at, quote_request):
    with pytest.raises(DomainError, match="unsupported"):
        calculate({**quote_request, "payout_method": "CASH"}, catalog, rates, at)


def test_missing_stale_and_future_rates(at):
    for updated in (at - timedelta(hours=2), at + timedelta(hours=1)):
        rates = Rates(
            [{"base": "RUB", "quote": "THB", "rate": "0.42", "updated_at": updated.isoformat()}],
            at,
            3600,
        )
        with pytest.raises(DomainError) as caught:
            rates.get("RUB", "THB")
        assert caught.value.code == "RATE_UNAVAILABLE"


def test_inverse_and_cross_rates(rates):
    assert rates.get("THB", "RUB") * D("0.4200") > D("0.999999999999999")
    assert rates.get("USD", "THB") == D(42)
    assert rates.get("USD", "USD") == D(1)


def test_money_rounding():
    assert quantize(D("1.001"), 2) == 100
    assert quantize(D("1.001"), 2, up=True) == 101
    assert fee(100001, 2, {"percent": "1.5", "fixed": "0.10", "min": "0"}) == 1511


def test_deterministic(catalog, rates, at, quote_request):
    one = calculate(quote_request, catalog, rates, at)
    two = calculate(deepcopy(quote_request), catalog, rates, at)
    one.pop("quote_id")
    two.pop("quote_id")
    assert one == two


def test_target_above_max(catalog, rates, at, quote_request):
    with pytest.raises(DomainError) as caught:
        calculate(
            {**quote_request, "amount": "999999.00", "amount_type": "TARGET"}, catalog, rates, at
        )
    assert caught.value.code == "AMOUNT_ABOVE_MAX"

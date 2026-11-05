import json

import pytest
from conftest import DEMO

from app.catalog import records
from app.errors import DomainError
from app.pricing import calculate
from app.rates import Rates

EXAMPLES = json.loads((DEMO / "quotes.json").read_text())


@pytest.mark.parametrize("example", EXAMPLES)
def test_demo_examples(example, catalog, at, quote_request):
    rates = Rates(records(DEMO / "data" / "rates.json", "rates"), at, 3600)
    request = {**quote_request, **example["request"]}
    if "error" in example:
        with pytest.raises(DomainError) as caught:
            calculate(request, catalog, rates, at)
        assert caught.value.code == example["error"]
    else:
        result = calculate(request, catalog, rates, at)
        for key, fields in example["expected"].items():
            for field, value in fields.items():
                assert result[key][field] == value

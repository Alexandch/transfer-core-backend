from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class QuoteRequest(StrictModel):
    source_country: StrictStr = Field(pattern=r"^[A-Z]{2}$")
    source_currency: StrictStr = Field(pattern=r"^[A-Z]{3,12}$")
    target_country: StrictStr = Field(pattern=r"^[A-Z]{2}$")
    target_currency: StrictStr = Field(pattern=r"^[A-Z]{3,12}$")
    payout_method: StrictStr = Field(min_length=1, max_length=50)
    amount: StrictStr = Field(min_length=1, max_length=32)
    amount_type: Literal["SOURCE", "TARGET"]


class Recipient(StrictModel):
    name: StrictStr = Field(min_length=1, max_length=200)
    account: StrictStr = Field(min_length=1, max_length=200)
    bank_code: StrictStr = Field(min_length=1, max_length=100)


class TransferRequest(StrictModel):
    quote_id: StrictStr = Field(min_length=1, max_length=36)
    client_id: StrictStr = Field(min_length=1, max_length=100)
    recipient: Recipient


class WebhookRequest(StrictModel):
    event_id: StrictStr = Field(min_length=1, max_length=150)
    payout_id: StrictStr = Field(min_length=1, max_length=150)
    reference: StrictStr = Field(min_length=1, max_length=100)
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    occurred_at: StrictStr = Field(min_length=1, max_length=50)

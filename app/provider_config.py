from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

Number = Annotated[StrictStr, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]


class ConfigRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderFee(ConfigRecord):
    percent: Number
    fixed: Number

    @model_validator(mode="after")
    def valid_percent(self):
        if Decimal(self.percent) >= 100:
            raise ValueError("Fee percent must be below 100")
        return self


class ProviderLimits(ConfigRecord):
    min: Number
    max: Number

    @model_validator(mode="after")
    def valid_limits(self):
        if Decimal(self.min) > Decimal(self.max):
            raise ValueError("Limits must be ordered")
        return self


class MaintenanceWindow(ConfigRecord):
    from_: datetime = Field(alias="from")
    to: datetime

    @model_validator(mode="after")
    def valid_window(self):
        if self.from_.tzinfo is None or self.to.tzinfo is None or self.from_ >= self.to:
            raise ValueError("Window must have timezones and from < to")
        return self


class ProviderDefinition(ConfigRecord):
    provider_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    directions: list[StrictStr] = Field(min_length=1)
    currency: StrictStr
    fee: ProviderFee
    limits: ProviderLimits
    success_rate_24h: Number
    p95_latency_ms: Annotated[StrictInt, Field(ge=0)]
    enabled: StrictBool
    maintenance_windows: list[MaintenanceWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_success_rate(self):
        if Decimal(self.success_rate_24h) > 1:
            raise ValueError("success_rate_24h must be between 0 and 1")
        return self

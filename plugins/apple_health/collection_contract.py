"""Portable HealthKit observations; native entitlements remain on the device."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    record_id: str = Field(min_length=1, max_length=256)
    record_type: Literal["steps", "sleep", "workout"]
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    timezone: str = Field(min_length=1, max_length=128)
    value: float = Field(ge=0, le=100000000)
    unit: Literal["count", "seconds"]
    category: str = Field(default="", max_length=128)
    source: str = Field(default="Apple Health", max_length=256)

    @model_validator(mode="after")
    def validate_record(self) -> "HealthRecord":
        if self.end <= self.start:
            raise ValueError("Health record end must follow start")
        if self.unit != ("count" if self.record_type == "steps" else "seconds"):
            raise ValueError("Health record unit does not match its type")
        if self.record_type == "steps" and not self.value.is_integer():
            raise ValueError("Step count must be an integer")
        if self.record_type != "steps" and self.value > self.end - self.start + 1:
            raise ValueError("Recorded duration exceeds its interval")
        return self


def collection_capability() -> dict:
    return dict(capability_id="apple_health.observe", protocol="source.change.v1",
                schema_id="apple_health.record", schema_version=1, direction="observe",
                supports_resources=False, payload_schema=HealthRecord.model_json_schema())

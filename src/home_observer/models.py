from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identity = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(WireModel):
    id: Identity
    device_id: Identity | None = None
    device_name: Name | None = None
    name: Name
    supported: bool
    reason: Name | None = None

    @model_validator(mode="after")
    def unsupported_reason(self):
        if not self.supported and not self.reason:
            raise ValueError("Unsupported entities need a reason")
        return self


class Catalog(WireModel):
    source_id: Literal["demo"]
    entities: list[Entity] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("Duplicate entity identity")
        return self


class SelectionChange(WireModel):
    selected: bool


class Observation(WireModel):
    entity_id: Identity
    selection_revision: int = Field(ge=0)
    source_id: Literal["demo"]
    state: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    availability: Literal["available", "unavailable", "unknown"]
    received_at: datetime
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    last_reported: datetime | None = None
    brightness: int | None = Field(default=None, ge=0, le=255)
    color_temp_kelvin: int | None = Field(default=None, ge=1000, le=10000)
    rgb_color: tuple[int, int, int] | None = None

    @field_validator("received_at", "last_changed", "last_updated", "last_reported")
    @classmethod
    def utc_timestamp(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Timestamp requires a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("rgb_color")
    @classmethod
    def rgb_range(cls, value):
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("RGB channels must be between 0 and 255")
        return value


class Health(WireModel):
    source_id: Literal["demo"]
    status: Literal["healthy", "failed"]
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value):
        return Observation.utc_timestamp(value)

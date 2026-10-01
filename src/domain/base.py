"""Base classes for domain records: immutable, strictly-shaped and JSON round-trippable."""

from decimal import Decimal
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    """Frozen, extra-forbidding Pydantic model. Change one with ``model_copy(update=...)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EventPayload(DomainModel):
    """A record that can be appended to the event store under ``event_type``.

    ``schema_version`` is bumped whenever a payload's stored shape changes incompatibly.
    """

    event_type: ClassVar[str]
    schema_version: ClassVar[int] = 1


# Money and prices that reach the OMS, a broker or the cost model are exact decimals.
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(allow_inf_nan=False)]
NonNegMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

# Market data and features are floats.
PosFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

PosInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]
NonEmptyStr = Annotated[str, Field(min_length=1)]

import asyncio
from typing import override

from pyscheduler.models import types as t
from pyscheduler.protocols import condition as c

from mantis.models.base import SerializableModel
from mantis.utils.time import UTCDatetime, awareutcnow


class Parameters(SerializableModel):
    """Parameters for the at condition."""

    datetime: UTCDatetime
    """Datetime in UTC to wait for."""


class AtCondition(c.Condition):
    """Condition that waits until a specific datetime."""

    @override
    async def wait(self, parameters: dict[str, t.JSON]) -> None:
        params = Parameters.model_validate(parameters)

        now = awareutcnow()
        delta = (params.datetime - now).total_seconds()
        delta = max(delta, 0)

        await asyncio.sleep(delta)

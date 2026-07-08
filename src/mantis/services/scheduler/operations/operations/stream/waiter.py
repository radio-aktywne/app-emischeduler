import asyncio
from datetime import UTC, timedelta

from mantis.services.apis.beaver import models as bm
from mantis.utils.time import awareutcnow


class Waiter:
    """Utility to wait for a time before event start."""

    def __init__(self, instance: bm.InstanceWithEvent) -> None:
        self._instance = instance

    async def wait(self, delta: timedelta) -> None:
        """Wait for a time before event start."""
        start = self._instance.start
        start = start.replace(tzinfo=self._instance.event.timezone).astimezone(UTC)

        target = start - delta
        now = awareutcnow()
        seconds = (target - now).total_seconds()
        seconds = max(seconds, 0)

        await asyncio.sleep(seconds)

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from typing import AsyncGenerator
from uuid import UUID

from pydantic import NaiveDatetime, ValidationError
from pyscheduler.errors import SchedulerError
from pyscheduler.models import enums as e
from pyscheduler.models import transfer as t
from zoneinfo import ZoneInfo

from emischeduler.config.models import StreamSynchronizerConfig, SynchronizerConfig
from emischeduler.emishows import models as sm
from emischeduler.emishows.service import EmishowsService
from emischeduler.scheduling.operations.operations.stream import Parameters
from emischeduler.scheduling.scheduler import Scheduler
from emischeduler.time import naiveutcnow


class StreamSynchronizer:
    """Synchronizes stream tasks."""

    def __init__(
        self,
        config: StreamSynchronizerConfig,
        emishows: EmishowsService,
        scheduler: Scheduler,
    ) -> None:
        self._config = config
        self._emishows = emishows
        self._scheduler = scheduler

    def _get_time_window(self) -> tuple[NaiveDatetime, NaiveDatetime]:
        start = naiveutcnow()
        end = start + self._config.window

        return start, end

    async def _fetch_schedules(
        self, start: NaiveDatetime, end: NaiveDatetime
    ) -> list[sm.EventSchedule]:
        schedules: list[sm.EventSchedule] = []
        offset = 0
        types = {"replay", "prerecorded"}

        while True:
            response = await self._emishows.schedule.list(
                start=start,
                end=end,
                offset=offset,
                where={"OR": [{"type": type} for type in types]},
            )
            schedules = schedules + response.schedules
            offset = offset + len(response.schedules)

            if offset >= response.count:
                break

        return schedules

    def _filter_schedules(
        self,
        schedules: list[sm.EventSchedule],
        start: NaiveDatetime,
        end: NaiveDatetime,
    ) -> list[sm.EventSchedule]:
        out: list[sm.EventSchedule] = []

        for schedule in schedules:
            instances: list[sm.EventInstance] = []
            tz = ZoneInfo(schedule.event.timezone)

            for instance in schedule.instances:
                istart = (
                    instance.start.replace(tzinfo=tz)
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )

                if istart >= start and istart < end:
                    instances = instances + [instance]

            if len(instances) > 0:
                schedule = sm.EventSchedule(event=schedule.event, instances=instances)
                out = out + [schedule]

        return out

    async def _get_schedules(
        self, start: NaiveDatetime, end: NaiveDatetime
    ) -> list[sm.EventSchedule]:
        schedules = await self._fetch_schedules(start, end)
        return self._filter_schedules(schedules, start, end)

    async def _fetch_tasks(self) -> list[t.GenericTask]:
        index = await self._scheduler.tasks.list()
        ids = (
            index.pending
            | index.running
            | index.cancelled
            | index.failed
            | index.completed
        )

        tasks: list[t.GenericTask] = []

        for id in ids:
            task = await self._scheduler.tasks.get(id)
            if task is not None:
                tasks = tasks + [task]

        return tasks

    async def _get_events(self, ids: set[UUID]) -> list[sm.Event]:
        if len(ids) == 0:
            return []

        events: list[sm.Event] = []
        offset = 0

        while True:
            response = await self._emishows.events.list(
                where={
                    "id": {
                        "in": [str(id) for id in ids],
                    },
                },
                offset=offset,
            )
            events = events + response.events
            offset = offset + len(response.events)

            if offset >= response.count:
                break

        return events

    async def _filter_tasks(
        self, tasks: list[t.GenericTask], start: NaiveDatetime, end: NaiveDatetime
    ) -> tuple[list[tuple[t.GenericTask, Parameters]], list[t.GenericTask]]:
        invalid: list[t.GenericTask] = []
        withparams: list[tuple[t.GenericTask, Parameters]] = []

        for task in tasks:
            if task.status not in {e.Status.PENDING, e.Status.RUNNING}:
                continue

            if task.task.operation.type != "stream":
                continue

            try:
                params = Parameters.model_validate(task.task.operation.parameters)
            except ValidationError:
                invalid = invalid + [task]
                continue

            withparams = withparams + [(task, params)]

        ids = {params.id for _, params in withparams}
        events = await self._get_events(ids)
        events = {event.id: event for event in events}

        valid: list[tuple[t.GenericTask, Parameters]] = []

        for task, params in withparams:
            event = events.get(params.id)
            if event is None:
                invalid = invalid + [task]
                continue

            tz = ZoneInfo(event.timezone)
            istart = (
                params.start.replace(tzinfo=tz)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )

            if istart >= start and istart < end:
                valid = valid + [(task, params)]

        return valid, invalid

    async def _get_tasks(
        self, start: NaiveDatetime, end: NaiveDatetime
    ) -> tuple[list[tuple[t.GenericTask, Parameters]], list[t.GenericTask]]:
        tasks = await self._fetch_tasks()
        return await self._filter_tasks(tasks, start, end)

    async def _cancel(self, id: UUID) -> None:
        try:
            request = t.CancelRequest(id=id)
            await self._scheduler.cancel(request)
        except SchedulerError:
            pass

    async def _cancel_extra_tasks(
        self,
        schedules: list[sm.EventSchedule],
        tasks: list[tuple[t.GenericTask, Parameters]],
    ) -> None:
        schedulemap = {schedule.event.id: schedule for schedule in schedules}
        cancel = set[UUID]()

        for task, params in tasks:
            schedule = schedulemap.get(params.id)
            if schedule is None:
                cancel = cancel | {task.task.id}
                continue

            instance = next(
                (
                    instance
                    for instance in schedule.instances
                    if instance.start == params.start
                ),
                None,
            )

            if instance is None:
                cancel = cancel | {task.task.id}
                continue

        for id in cancel:
            await self._cancel(id)

    async def _add(
        self,
        event: sm.Event,
        instance: sm.EventInstance,
    ) -> None:
        tz = ZoneInfo(event.timezone)
        utcstart = (
            instance.start.replace(tzinfo=tz)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        at = utcstart - timedelta(minutes=15)

        try:
            request = t.ScheduleRequest(
                operation=t.Specification(
                    type="stream",
                    parameters={
                        "id": str(event.id),
                        "start": instance.start.isoformat(),
                    },
                ),
                condition=t.Specification(
                    type="at",
                    parameters={
                        "datetime": at.isoformat(),
                    },
                ),
                dependencies={},
            )
            await self._scheduler.schedule(request)
        except SchedulerError:
            pass

    async def _add_new_tasks(
        self,
        schedules: list[sm.EventSchedule],
        tasks: list[tuple[t.GenericTask, Parameters]],
    ) -> None:
        add: list[tuple[sm.Event, sm.EventInstance]] = []

        for schedule in schedules:
            ft = filter(lambda tp: tp[1].id == schedule.event.id, tasks)

            for instance in schedule.instances:
                task = next(
                    (task for task, params in ft if params.start == instance.start),
                    None,
                )

                if task is None:
                    add = add + [(schedule.event, instance)]

        for event, instance in add:
            await self._add(event, instance)

    async def synchronize(self) -> None:
        start, end = self._get_time_window()

        schedules = await self._get_schedules(start, end)
        valid, invalid = await self._get_tasks(start, end)

        for task in invalid:
            await self._cancel(task.task.id)

        await self._cancel_extra_tasks(schedules, valid)
        await self._add_new_tasks(schedules, valid)


class Synchronizer:
    """Synchronizes scheduled tasks with expected ones."""

    def __init__(
        self,
        config: SynchronizerConfig,
        emishows: EmishowsService,
        scheduler: Scheduler,
    ) -> None:
        self._config = config
        self._stream_synchronizer = StreamSynchronizer(
            config.stream, emishows, scheduler
        )

    def _find_next_time(self, dt: NaiveDatetime) -> NaiveDatetime:
        reference = self._config.reference
        interval = self._config.interval

        return reference + math.ceil((dt - reference) / interval) * interval

    async def _wait(self) -> None:
        now = naiveutcnow()
        target = self._find_next_time(now)

        delta = target - now
        delta = delta.total_seconds()
        delta = max(delta, 0)

        await asyncio.sleep(delta)

    async def _synchronize(self) -> None:
        await self._stream_synchronizer.synchronize()

    async def _run(self) -> None:
        try:
            while True:
                await self._wait()
                try:
                    await self._synchronize()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    @asynccontextmanager
    async def run(self) -> AsyncGenerator[None, None]:
        """Run in the context."""

        task = asyncio.create_task(self._run())

        try:
            yield
        finally:
            task.cancel()
            await task

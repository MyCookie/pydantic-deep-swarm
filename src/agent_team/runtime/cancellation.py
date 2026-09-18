"""Unified worker lifecycle, project deadlines, and cancellation enforcement."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .lifecycle import ConcurrencyLimiter, ProcessManager, TaskRegistry, WorkerState


class WorkerTimeoutError(Exception):
    """Worker exceeded its maximum allowed turns."""


@dataclass(frozen=True)
class WorkerTurnUsage:
    """Observable model/tool turn usage for one worker."""

    model_turns: int = 0
    tool_calls: int = 0

    @property
    def total(self) -> int:
        """Return all counted model and tool iterations."""
        return self.model_turns + self.tool_calls


class WorkerTurnBudget:
    """Count every model request and executed tool call against one budget.

    The project deadline callback runs before the local budget check.  This
    ordering makes an expired project produce ``ProjectTimeoutError`` rather
    than being reported as an ordinary worker turn-limit result.
    """

    def __init__(
        self,
        max_turns: int = 10,
        worker_id: str = "worker",
        before_turn: Any | None = None,
        on_turn: Any | None = None,
    ):
        self.max_turns = max(0, int(max_turns))
        self.worker_id = worker_id
        self._before_turn = before_turn
        self._on_turn = on_turn
        self._model_turns = 0
        self._tool_calls = 0

    @property
    def usage(self) -> WorkerTurnUsage:
        """Return a snapshot of the counted iterations."""
        return WorkerTurnUsage(self._model_turns, self._tool_calls)

    async def consume(self, kind: str) -> WorkerTurnUsage:
        """Reserve one model or tool iteration, or raise at the limit."""
        if kind not in {"model", "tool"}:
            raise ValueError(f"unknown worker turn kind: {kind}")

        if self._before_turn is not None:
            result = self._before_turn()
            if inspect.isawaitable(result):
                await result

        usage = self.usage
        if usage.total >= self.max_turns:
            raise WorkerTimeoutError(
                f"Worker {self.worker_id} exceeded turn limit of {self.max_turns} "
                f"(model turns={usage.model_turns}, tool calls={usage.tool_calls})"
            )

        if kind == "model":
            self._model_turns += 1
        else:
            self._tool_calls += 1
        if self._on_turn is not None:
            result = self._on_turn(kind)
            if inspect.isawaitable(result):
                await result
        return self.usage


class WorkerTaskTimeoutError(Exception):
    """Worker task exceeded its individual wall-clock timeout."""


class ProjectTimeoutError(TimeoutError):
    """A project exceeded its one absolute wall-clock deadline."""


@dataclass
class ProjectDeadline:
    """An immutable-start project budget shared by planning and execution."""

    team_id: str
    started_at: float
    expires_at: float
    timeout_seconds: float
    timer_task: asyncio.Task | None = field(default=None, repr=False)
    expired: bool = False

    def remaining_seconds(self, now: float | None = None) -> float:
        """Return the remaining monotonic budget, never below zero."""
        return max(0.0, self.expires_at - (time.monotonic() if now is None else now))


class HardCanceller:
    """Cancel a worker and all registry-owned resources exactly once."""

    def __init__(self, registry: TaskRegistry, process_manager: ProcessManager):
        self.registry = registry
        self.process_manager = process_manager

    async def cancel_worker(self, worker_id: str, hard: bool = True) -> None:
        await self.registry.cancel_worker(worker_id, hard=hard)
        await self.process_manager.terminate_worker_processes(worker_id, hard=hard)

    async def cancel_all_descendants(self, worker_id: str) -> None:
        # Worker nesting is intentionally disabled in the v1 hierarchy.
        return None

    async def cleanup_on_manager_exit(self) -> None:
        workers = self.registry.snapshot_workers()
        for worker_id in list(workers):
            await self.cancel_worker(worker_id, hard=True)
        await self.registry.cleanup(list(workers))


class TimeoutEnforcer:
    """Track per-project absolute deadlines and individual task bounds.

    ``team_timeout`` is the total project budget.  A project starts its clock
    exactly once, before Manager planning, and every later phase uses the same
    deadline.  The individual task timeout remains a lower bound for one worker
    but can never extend the project budget.
    """

    def __init__(
        self,
        registry: TaskRegistry,
        max_turns: int = 10,
        task_timeout_seconds: float = 300,
        team_timeout_seconds: float = 1800,
    ):
        self.registry = registry
        self.max_turns = max(1, int(max_turns))
        self.task_timeout = timedelta(seconds=max(0.001, float(task_timeout_seconds)))
        self.team_timeout = timedelta(seconds=max(0.001, float(team_timeout_seconds)))
        self._task_start_times: dict[str, datetime] = {}
        self._team_deadlines: dict[str, ProjectDeadline] = {}
        self._deadline_tasks: dict[str, asyncio.Task] = {}

    @property
    def active_team_ids(self) -> tuple[str, ...]:
        """Return active project ids in start order."""
        return tuple(self._team_deadlines)

    def start_team(self, team_id: str = "default") -> ProjectDeadline:
        """Start one project deadline, without resetting an active project.

        The method is intentionally synchronous so callers can establish the
        deadline before their first await.  When called inside an event loop it
        also starts a caller-owned monitor task; ``finish_team`` always cancels
        and awaits that task.
        """
        existing = self._team_deadlines.get(team_id)
        if existing is not None:
            return existing

        started_at = time.monotonic()
        deadline = ProjectDeadline(
            team_id=team_id,
            started_at=started_at,
            expires_at=started_at + self.team_timeout.total_seconds(),
            timeout_seconds=self.team_timeout.total_seconds(),
        )
        self._team_deadlines[team_id] = deadline
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            timer = loop.create_task(
                self._watch_project_deadline(deadline),
                name=f"project-deadline:{team_id}",
            )
            deadline.timer_task = timer
            self._deadline_tasks[team_id] = timer
        return deadline

    async def _watch_project_deadline(self, deadline: ProjectDeadline) -> None:
        """Mark expiry without owning or cancelling the project task tree."""
        try:
            await asyncio.sleep(deadline.remaining_seconds())
            deadline.expired = True
        except asyncio.CancelledError:
            raise

    async def finish_team(self, team_id: str = "default") -> None:
        """Release a project deadline and await its monitor task."""
        deadline = self._team_deadlines.pop(team_id, None)
        timer = self._deadline_tasks.pop(team_id, None)
        if deadline is None and timer is None:
            return
        if timer is not None and timer is not asyncio.current_task():
            if not timer.done():
                timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass

    async def close(self) -> None:
        """Finish every active project and reap every deadline monitor."""
        await asyncio.gather(
            *(self.finish_team(team_id) for team_id in list(self._team_deadlines)),
            return_exceptions=False,
        )

    def remaining_team_seconds(self, team_id: str = "default") -> float | None:
        """Return a project's remaining budget, or ``None`` if it is inactive."""
        deadline = self._team_deadlines.get(team_id)
        if deadline is None:
            return None
        remaining = deadline.remaining_seconds()
        if remaining <= 0:
            deadline.expired = True
        return remaining

    def start_task(self, task_id: str) -> None:
        self._task_start_times[task_id] = datetime.utcnow()

    def finish_task(self, task_id: str) -> None:
        self._task_start_times.pop(task_id, None)

    async def check_task_timeout(self, task_id: str) -> None:
        started = self._task_start_times.get(task_id)
        if started and datetime.utcnow() - started > self.task_timeout:
            raise WorkerTaskTimeoutError(f"Task {task_id} exceeded timeout of {self.task_timeout}")

    async def check_team_timeout(self, team_id: str = "default") -> None:
        remaining = self.remaining_team_seconds(team_id)
        if remaining is not None and remaining <= 0:
            raise ProjectTimeoutError(f"Project {team_id} exceeded total timeout of {self.team_timeout}")

    async def run_with_team_deadline(
        self,
        awaitable: Any,
        team_id: str = "default",
        *,
        timeout: float | None = None,
    ) -> Any:
        """Await work without allowing it to outlive the project deadline."""
        remaining = self.remaining_team_seconds(team_id)
        if remaining is None:
            if timeout is None:
                return await awaitable
            return await asyncio.wait_for(awaitable, timeout=max(0.0, float(timeout)))

        effective_timeout = remaining
        project_limited = timeout is None or remaining <= max(0.0, float(timeout))
        if timeout is not None:
            effective_timeout = min(remaining, max(0.0, float(timeout)))
        try:
            return await asyncio.wait_for(awaitable, timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            current_remaining = self.remaining_team_seconds(team_id)
            if project_limited and current_remaining is not None and current_remaining <= 0:
                raise ProjectTimeoutError(
                    f"Project {team_id} exceeded total timeout of {self.team_timeout}"
                ) from exc
            raise

    # Explicit alias for callers that describe the boundary as a project.
    run_with_project_deadline = run_with_team_deadline

    def check_turn_limit(self, worker_id: str) -> None:
        worker = self.registry._workers.get(worker_id)
        if worker:
            limit = min(self.max_turns, max(0, int(worker.max_turns)))
            if worker.turns > limit:
                raise WorkerTimeoutError(f"Worker {worker_id} exceeded turn limit of {limit}")

    def increment_turn(self, worker_id: str, kind: str = "model") -> int:
        if kind not in {"model", "tool"}:
            raise ValueError(f"unknown worker turn kind: {kind}")
        worker = self.registry._workers.get(worker_id)
        if worker is None:
            return 0
        worker.turns += 1
        if kind == "model":
            worker.model_turns += 1
        else:
            worker.tool_calls += 1
        return worker.turns


def _close_unstarted_awaitable(awaitable: Any) -> None:
    """Avoid coroutine warnings when a deadline expires before scheduling work."""
    if asyncio.iscoroutine(awaitable):
        awaitable.close()


async def enforce_worker_lifecycle(
    worker_id: str,
    coro: Any,
    canceller: HardCanceller,
    timeout_enforcer: TimeoutEnforcer,
    concurrency_limiter: ConcurrencyLimiter,
    *,
    task_id: str | None = None,
    team_id: str = "default",
) -> Any:
    """Run one worker coroutine through the canonical lifecycle boundary."""
    actual_task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
    acquired = False
    lease: object | None = None
    task_started = False
    coro_started = False
    try:
        team_remaining = timeout_enforcer.remaining_team_seconds(team_id)
        if team_remaining is not None and team_remaining <= 0:
            _close_unstarted_awaitable(coro)
            await canceller.cancel_worker(worker_id, hard=True)
            raise ProjectTimeoutError(f"Project {team_id} exceeded its deadline")

        if team_remaining is None:
            lease = await concurrency_limiter.acquire()
        else:
            try:
                lease = await asyncio.wait_for(
                    concurrency_limiter.acquire(),
                    timeout=team_remaining,
                )
            except asyncio.TimeoutError as exc:
                _close_unstarted_awaitable(coro)
                await canceller.cancel_worker(worker_id, hard=True)
                raise ProjectTimeoutError(f"Project {team_id} exceeded its deadline") from exc
        acquired = True

        timeout_enforcer.start_task(actual_task_id)
        task_started = True
        try:
            await timeout_enforcer.check_team_timeout(team_id)
        except ProjectTimeoutError:
            _close_unstarted_awaitable(coro)
            await canceller.cancel_worker(worker_id, hard=True)
            raise

        task_timeout = timeout_enforcer.task_timeout.total_seconds()
        team_remaining = timeout_enforcer.remaining_team_seconds(team_id)
        project_limited = team_remaining is not None and team_remaining <= task_timeout
        effective_timeout = min(task_timeout, team_remaining) if team_remaining is not None else task_timeout
        coro_started = True
        try:
            return await asyncio.wait_for(coro, timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            await canceller.cancel_worker(worker_id, hard=True)
            current_remaining = timeout_enforcer.remaining_team_seconds(team_id)
            if project_limited and current_remaining is not None and current_remaining <= 0:
                raise ProjectTimeoutError(f"Project {team_id} exceeded its deadline") from exc
            raise WorkerTaskTimeoutError(f"Worker {worker_id} timed out") from exc
    except asyncio.CancelledError:
        if not coro_started:
            _close_unstarted_awaitable(coro)
        await canceller.cancel_worker(worker_id, hard=True)
        raise
    finally:
        if task_started:
            timeout_enforcer.finish_task(actual_task_id)
        if acquired:
            await concurrency_limiter.release(lease)


async def check_team_timeout_loop(
    timeout_enforcer: TimeoutEnforcer,
    team_id: str = "default",
    interval: float = 5.0,
) -> None:
    """Optional caller-owned monitor; cancellation belongs to the caller."""
    try:
        while True:
            await asyncio.sleep(max(0.0, interval))
            await timeout_enforcer.check_team_timeout(team_id)
    except asyncio.CancelledError:
        return

"""Runtime lifecycle management with runtime-enforced cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
import uuid


class WorkerState(Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


__all__ = [
    "WorkerState", "TaskState", "ProcessHandle", "WorkerRecord", "TaskRecord",
    "TaskRegistry", "ProcessManager", "ConcurrencyLimiter",
]


@dataclass
class ProcessHandle:
    """Track a subprocess spawned by a worker."""

    worker_id: str
    task_id: str
    pid: int
    process_group: int
    start_time: datetime
    command: str
    status: str = "running"
    process: Any | None = field(default=None, repr=False)
    _reap_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _reaped: bool = field(default=False, repr=False, compare=False)

    def terminate(self) -> None:
        try:
            os.killpg(self.process_group, signal.SIGTERM)
            self.status = "terminating"
        except (ProcessLookupError, OSError):
            self.status = "exited"

    def kill(self) -> None:
        try:
            os.killpg(self.process_group, signal.SIGKILL)
            self.status = "killed"
        except (ProcessLookupError, OSError):
            self.status = "exited"

    async def reap(self, hard: bool = True) -> None:
        """Terminate and await the child so cleanup does not leave a zombie."""
        async with self._reap_lock:
            if self._reaped:
                return
            process = self.process
            if process is None:
                self._reaped = True
                return
            was_running = getattr(process, "returncode", None) is None
            if was_running:
                self.kill() if hard else self.terminate()
            try:
                communicate = getattr(process, "communicate", None)
                if callable(communicate):
                    await communicate()
                else:
                    wait = getattr(process, "wait", None)
                    if callable(wait):
                        result = wait()
                        if hasattr(result, "__await__"):
                            await result
            except (ProcessLookupError, OSError):
                pass
            self.status = ("killed" if hard else "terminated") if was_running else "exited"
            self._reaped = True


@dataclass
class WorkerRecord:
    """Track a worker's state and resources."""

    worker_id: str
    role: str
    state: WorkerState = WorkerState.CREATED
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    task_id: str | None = None
    result: Any = None
    error: str | None = None
    async_task: asyncio.Task | None = None
    child_processes: list[ProcessHandle] = field(default_factory=list)
    turns: int = 0
    model_turns: int = 0
    tool_calls: int = 0
    max_turns: int = 10
    project_id: str | None = None


@dataclass
class TaskRecord:
    """Track one worker task and whether it consumed a concurrency slot."""

    task_id: str
    worker_id: str
    state: TaskState = TaskState.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any = None
    error: str | None = None
    slot_accounted: bool = False


class TaskRegistry:
    """Registry for active tasks/workers with idempotent terminal transitions."""

    def __init__(self, max_workers: int = 6, max_concurrent: int = 3):
        self.max_workers = max_workers
        self.max_concurrent = max_concurrent
        self._workers: dict[str, WorkerRecord] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._concurrent_count = 0
        self._lock = asyncio.Lock()

    async def register_worker(
        self,
        role: str,
        max_turns: int = 10,
        project_id: str | None = None,
    ) -> str:
        async with self._lock:
            active = sum(
                1 for worker in self._workers.values()
                if worker.state not in {WorkerState.COMPLETE, WorkerState.FAILED, WorkerState.CANCELLED, WorkerState.TIMED_OUT}
            )
            if active >= self.max_workers:
                raise RuntimeError(f"Max workers ({self.max_workers}) reached")
            worker_id = uuid.uuid4().hex[:8]
            self._workers[worker_id] = WorkerRecord(
                worker_id=worker_id,
                role=role,
                max_turns=max(0, int(max_turns)),
                project_id=project_id,
            )
            return worker_id

    async def attach_async_task(self, worker_id: str, task: asyncio.Task) -> bool:
        """Attach the owning asyncio task so cancellation can await it."""
        async with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or worker.state in {
                WorkerState.COMPLETE,
                WorkerState.FAILED,
                WorkerState.CANCELLED,
                WorkerState.TIMED_OUT,
            }:
                return False
            worker.async_task = task
            return True

    async def register_task(self, worker_id: str) -> str:
        async with self._lock:
            if worker_id not in self._workers:
                raise KeyError(f"Unknown worker: {worker_id}")
            task_id = uuid.uuid4().hex[:8]
            self._tasks[task_id] = TaskRecord(task_id=task_id, worker_id=worker_id)
            self._workers[worker_id].task_id = task_id
            self._workers[worker_id].state = WorkerState.QUEUED
            return task_id

    async def start_task(self, task_id: str) -> bool:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False
            if record.state != TaskState.PENDING:
                return record.state == TaskState.RUNNING
            worker = self._workers.get(record.worker_id)
            if worker is None or worker.state in {
                WorkerState.CANCELLING,
                WorkerState.COMPLETE,
                WorkerState.FAILED,
                WorkerState.CANCELLED,
                WorkerState.TIMED_OUT,
            }:
                return False
            record.state = TaskState.RUNNING
            record.started_at = datetime.utcnow()
            record.slot_accounted = True
            self._concurrent_count += 1
            worker = self._workers.get(record.worker_id)
            if worker:
                worker.state = WorkerState.RUNNING
                worker.started_at = record.started_at
            return True

    async def complete_task(self, task_id: str, result: Any = None) -> bool:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.state in {TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED}:
                return False
            worker = self._workers.get(record.worker_id)
            if worker and worker.state in {WorkerState.CANCELLING, WorkerState.CANCELLED}:
                if record.state == TaskState.RUNNING and record.slot_accounted:
                    self._concurrent_count = max(0, self._concurrent_count - 1)
                    record.slot_accounted = False
                record.state = TaskState.CANCELLED
                record.completed_at = datetime.utcnow()
                record.error = "worker cancelled"
                worker.state = WorkerState.CANCELLED
                worker.completed_at = record.completed_at
                worker.error = record.error
                worker.async_task = None
                return False
            if record.state == TaskState.RUNNING and record.slot_accounted:
                self._concurrent_count = max(0, self._concurrent_count - 1)
                record.slot_accounted = False
            record.state = TaskState.COMPLETE
            record.completed_at = datetime.utcnow()
            record.result = result
            worker = self._workers.get(record.worker_id)
            if worker:
                worker.state = WorkerState.COMPLETE
                worker.completed_at = record.completed_at
                worker.result = result
                worker.async_task = None
            return True

    async def fail_task(self, task_id: str, error: str) -> bool:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.state in {TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED}:
                return False
            worker = self._workers.get(record.worker_id)
            if worker and worker.state in {WorkerState.CANCELLING, WorkerState.CANCELLED}:
                if record.state == TaskState.RUNNING and record.slot_accounted:
                    self._concurrent_count = max(0, self._concurrent_count - 1)
                    record.slot_accounted = False
                record.state = TaskState.CANCELLED
                record.completed_at = datetime.utcnow()
                record.error = "worker cancelled"
                worker.state = WorkerState.CANCELLED
                worker.completed_at = record.completed_at
                worker.error = record.error
                worker.async_task = None
                return False
            if record.state == TaskState.RUNNING and record.slot_accounted:
                self._concurrent_count = max(0, self._concurrent_count - 1)
                record.slot_accounted = False
            record.state = TaskState.FAILED
            record.completed_at = datetime.utcnow()
            record.error = error
            worker = self._workers.get(record.worker_id)
            if worker:
                worker.state = WorkerState.FAILED
                worker.completed_at = record.completed_at
                worker.error = error
                worker.async_task = None
            return True

    async def cancel_task(self, task_id: str, error: str = "cancelled") -> bool:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.state in {TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED}:
                return False
            worker = self._workers.get(record.worker_id)
            if worker and worker.state in {WorkerState.COMPLETE, WorkerState.FAILED, WorkerState.TIMED_OUT}:
                return False
            if record.state == TaskState.RUNNING and record.slot_accounted:
                self._concurrent_count = max(0, self._concurrent_count - 1)
                record.slot_accounted = False
            record.state = TaskState.CANCELLED
            record.completed_at = datetime.utcnow()
            record.error = error
            if worker:
                worker.state = WorkerState.CANCELLED
                worker.completed_at = record.completed_at
                worker.error = error
                worker.async_task = None
            return True

    async def cancel_worker(self, worker_id: str, hard: bool = False) -> None:
        """Cancel a worker without awaiting it while holding the registry lock."""
        async with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return
            terminal = worker.state in {
                WorkerState.COMPLETE,
                WorkerState.FAILED,
                WorkerState.CANCELLED,
                WorkerState.TIMED_OUT,
            }
            if not terminal:
                worker.state = WorkerState.CANCELLING
            async_task = worker.async_task
            processes = list(worker.child_processes)
        if terminal:
            for process in processes:
                try:
                    await process.reap(hard=hard)
                except Exception:
                    pass
            async with self._lock:
                worker = self._workers.get(worker_id)
                if worker:
                    worker.child_processes.clear()
            return
        if async_task and async_task is not asyncio.current_task() and not async_task.done():
            async_task.cancel()
            try:
                await async_task
            except BaseException:
                pass
        for process in processes:
            try:
                await process.reap(hard=hard)
            except Exception:
                pass
        if worker.task_id:
            await self.cancel_task(worker.task_id, error="worker cancelled")
        async with self._lock:
            worker = self._workers.get(worker_id)
            if worker:
                if worker.state not in {
                    WorkerState.COMPLETE,
                    WorkerState.FAILED,
                    WorkerState.CANCELLED,
                    WorkerState.TIMED_OUT,
                }:
                    worker.state = WorkerState.CANCELLED
                    worker.completed_at = worker.completed_at or datetime.utcnow()
                worker.child_processes.clear()

    async def get_concurrent_count(self) -> int:
        async with self._lock:
            return self._concurrent_count

    async def wait_for_slot(self) -> None:
        while True:
            if await self.get_concurrent_count() < self.max_concurrent:
                return
            await asyncio.sleep(0.05)

    def snapshot_workers(self) -> dict[str, WorkerRecord]:
        return dict(self._workers)

    def snapshot_tasks(self) -> dict[str, TaskRecord]:
        """Return a shallow snapshot for diagnostics and shutdown cleanup."""
        return dict(self._tasks)

    async def remove_workers(self, worker_ids: list[str] | tuple[str, ...] | set[str]) -> int:
        """Remove only terminal workers and their terminal task records."""
        requested = set(worker_ids)
        removed = 0
        async with self._lock:
            terminal_workers = {
                WorkerState.COMPLETE,
                WorkerState.FAILED,
                WorkerState.CANCELLED,
                WorkerState.TIMED_OUT,
            }
            terminal_tasks = {
                TaskState.COMPLETE,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
            removable_workers = {
                worker_id
                for worker_id in requested
                if worker_id in self._workers and self._workers[worker_id].state in terminal_workers
            }
            for task_id, task in list(self._tasks.items()):
                if task.worker_id not in removable_workers:
                    continue
                if task.state not in terminal_tasks:
                    if task.state == TaskState.PENDING:
                        task.state = TaskState.CANCELLED
                        task.completed_at = datetime.utcnow()
                        task.error = "worker cleanup"
                    else:
                        continue
                if task.slot_accounted:
                    self._concurrent_count = max(0, self._concurrent_count - 1)
                    task.slot_accounted = False
                self._tasks.pop(task_id, None)
                removed += 1
            for worker_id in removable_workers:
                if self._workers[worker_id].task_id is None or not any(
                    task.worker_id == worker_id for task in self._tasks.values()
                ):
                    self._workers.pop(worker_id, None)
                    removed += 1
            return removed

    async def cleanup(self, worker_ids: list[str] | tuple[str, ...] | set[str] | None = None) -> int:
        """Cancel active records, preserve terminal outcomes, then remove them."""
        ids = list(worker_ids) if worker_ids is not None else list(self._workers)
        for worker_id in ids:
            await self.cancel_worker(worker_id, hard=True)
        return await self.remove_workers(ids)

    async def is_empty(self) -> bool:
        """Return whether all worker/task records and slot accounting are clear."""
        async with self._lock:
            return not self._workers and not self._tasks and self._concurrent_count == 0

    async def prune_terminal(self, max_age_seconds: int = 3600) -> int:
        """Drop old terminal records while retaining recent diagnostics."""
        cutoff = datetime.utcnow().timestamp() - max(0, max_age_seconds)
        async with self._lock:
            terminal = {WorkerState.COMPLETE, WorkerState.FAILED, WorkerState.CANCELLED, WorkerState.TIMED_OUT}
            worker_ids = {
                worker_id for worker_id, worker in self._workers.items()
                if worker.state in terminal and worker.completed_at and worker.completed_at.timestamp() < cutoff
            }
            task_ids = {
                task_id for task_id, task in self._tasks.items()
                if task.worker_id in worker_ids or (
                    task.completed_at and task.completed_at.timestamp() < cutoff and
                    task.state in {TaskState.COMPLETE, TaskState.FAILED, TaskState.CANCELLED}
                )
            }
            for task_id in task_ids:
                self._tasks.pop(task_id, None)
            for worker_id in worker_ids:
                self._workers.pop(worker_id, None)
            return len(task_ids) + len(worker_ids)


class ProcessManager:
    """Manage subprocess ownership and cleanup."""

    def __init__(self, registry: TaskRegistry):
        self.registry = registry
        self._lock = asyncio.Lock()

    async def spawn(self, worker_id: str, task_id: str, command: list[str]) -> ProcessHandle:
        process = await asyncio.create_subprocess_exec(
            *command,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return await self.track_process(worker_id, task_id, process, command)

    async def track_process(
        self, worker_id: str, task_id: str, process: Any, command: list[str]
    ) -> ProcessHandle:
        """Attach an already-created subprocess to a worker."""
        handle = ProcessHandle(
            worker_id=worker_id,
            task_id=task_id,
            pid=process.pid,
            process_group=process.pid,
            start_time=datetime.utcnow(),
            command=" ".join(command),
            process=process,
        )
        async with self._lock:
            worker = self.registry._workers.get(worker_id)
            accepted = worker is not None and worker.state not in {
                WorkerState.CANCELLING,
                WorkerState.COMPLETE,
                WorkerState.FAILED,
                WorkerState.CANCELLED,
                WorkerState.TIMED_OUT,
            }
            if accepted:
                worker.child_processes.append(handle)
        if not accepted:
            await handle.reap(hard=True)
        return handle

    async def release_process(self, handle: ProcessHandle, status: str = "exited") -> None:
        handle.status = status
        if status in {"exited", "timed_out", "cancelled"}:
            handle._reaped = True
        async with self._lock:
            worker = self.registry._workers.get(handle.worker_id)
            if worker:
                worker.child_processes = [item for item in worker.child_processes if item is not handle]

    async def terminate_worker_processes(self, worker_id: str, hard: bool = False) -> None:
        async with self._lock:
            worker = self.registry._workers.get(worker_id)
            if not worker:
                return
            processes = list(worker.child_processes)
            worker.child_processes.clear()
        for process in processes:
            try:
                await process.reap(hard=hard)
            except Exception:
                pass

    async def terminate_all_workers(self, hard: bool = True) -> None:
        """Reap every worker-owned subprocess currently registered."""
        await asyncio.gather(
            *(self.terminate_worker_processes(worker_id, hard=hard)
              for worker_id in self.registry.snapshot_workers()),
            return_exceptions=True,
        )


class ConcurrencyLimiter:
    """Bound worker inference concurrency."""

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max(1, max_concurrent)
        self._current_count = 0
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._owners: dict[asyncio.Task | None, list[object]] = {}
        self._leases: dict[object, asyncio.Task | None] = {}

    async def acquire(self) -> object:
        await self._semaphore.acquire()
        owner = asyncio.current_task()
        lease = object()
        try:
            async with self._lock:
                self._current_count += 1
                self._owners.setdefault(owner, []).append(lease)
                self._leases[lease] = owner
        except BaseException:
            self._semaphore.release()
            raise
        return lease

    async def release(self, lease: object | None = None) -> bool:
        owner = asyncio.current_task()
        async with self._lock:
            if lease is None:
                owner_leases = self._owners.get(owner, [])
                if not owner_leases:
                    return False
                lease = owner_leases[-1]
            if lease not in self._leases:
                return False
            lease_owner = self._leases.pop(lease)
            owner_leases = self._owners.get(lease_owner, [])
            if lease in owner_leases:
                owner_leases.remove(lease)
            if owner_leases:
                self._owners[lease_owner] = owner_leases
            else:
                self._owners.pop(lease_owner, None)
            self._current_count -= 1
        self._semaphore.release()
        return True

    @property
    def current_count(self) -> int:
        return self._current_count

    async def wait_if_needed(self) -> None:
        await self.acquire()

"""In-memory runtime registry for storage migration tasks."""

from __future__ import annotations

import asyncio

_running_migrations: dict[int, tuple[str, asyncio.Task[None]]] = {}
_pause_events: dict[int, asyncio.Event] = {}
_cancel_flags: set[int] = set()


def activate_task(task_id: int) -> asyncio.Event:
    """Ensure the runtime pause gate exists and is open."""
    event = _pause_events.get(task_id)
    if event is None:
        event = asyncio.Event()
        _pause_events[task_id] = event
    event.set()
    return event


def pause_task(task_id: int) -> None:
    """Close the runtime pause gate if it exists."""
    event = _pause_events.get(task_id)
    if event is not None:
        event.clear()


def mark_cancelled(task_id: int) -> None:
    """Mark a task as cancelled and unblock any waiting workers."""
    _cancel_flags.add(task_id)
    event = _pause_events.get(task_id)
    if event is not None:
        event.set()


def clear_cancelled(task_id: int) -> None:
    """Clear the cancellation marker for a task."""
    _cancel_flags.discard(task_id)


def is_cancelled(task_id: int) -> bool:
    """Return whether the task has been cancelled."""
    return task_id in _cancel_flags


def register_background_task(
    task_id: int, run_id: str, task: asyncio.Task[None]
) -> None:
    """Track the exact generation of the local background coroutine."""
    _running_migrations[task_id] = (run_id, task)


def pop_background_task(task_id: int) -> asyncio.Task[None] | None:
    """Remove and return the tracked background task."""
    tracked = _running_migrations.pop(task_id, None)
    return tracked[1] if tracked else None


def get_background_run_id(task_id: int) -> str | None:
    """Return the generation of an active local coroutine."""
    tracked = _running_migrations.get(task_id)
    return tracked[0] if tracked and not tracked[1].done() else None


def get_pause_event(task_id: int) -> asyncio.Event | None:
    """Return the pause gate for a task, if any."""
    return _pause_events.get(task_id)


def clear_runtime(task_id: int) -> None:
    """Drop all runtime state for a task."""
    _running_migrations.pop(task_id, None)
    _pause_events.pop(task_id, None)
    _cancel_flags.discard(task_id)


def clear_current_worker(task_id: int) -> None:
    """Leave another worker's local state intact when a claim is rejected."""
    tracked = _running_migrations.get(task_id)
    if tracked is None or tracked[1] is asyncio.current_task():
        clear_runtime(task_id)

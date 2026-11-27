"""Detect abandoned forward migration ownership without repeating object operations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .migration_forward_attempt import TASK, UNCERTAIN, forward_hold_required

_RECOVERABLE = """
    forward_run_id IS NOT NULL AND (
        (recovery_token = forward_run_id
         AND status IN ('running', 'paused', 'migration_uncertain'))
        OR (status = 'running' AND recovery_token IS NULL
            AND updated_at <= now() - interval '30 seconds')
    )
"""


def _owner_key(task_id: int) -> int:
    identifier = f"storage-migration:forward:{task_id}".encode("ascii")
    return int.from_bytes(
        blake2b(identifier, digest_size=8).digest(), "big", signed=True
    )


@asynccontextmanager
async def hold_forward_owner(engine: AsyncEngine, task_id: int) -> AsyncIterator[bool]:
    """Bind liveness to a dedicated PostgreSQL session, with no open transaction."""
    async with engine.connect() as conn:
        # Never return the session lock to the pool, even on cancellation.
        await conn.run_sync(lambda connection: connection.detach())
        acquired = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _owner_key(task_id)}
        )
        await conn.commit()
        yield acquired is True


async def recover_interrupted_forward_runs(db: AsyncSession) -> dict[str, int]:
    recovered = {"paused": 0, "uncertain": 0}
    task_ids = (
        await db.scalars(
            text(f"SELECT id FROM {TASK} WHERE {_RECOVERABLE} ORDER BY id")
        )
    ).all()
    for task_id in task_ids:
        # Both the worker and scanner take advisory ownership before row locks.
        # This covers a live worker paused or blocked in external I/O.
        claimed = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _owner_key(task_id)}
        )
        if not claimed:
            continue
        status = await db.scalar(
            text(
                f"SELECT status FROM {TASK} WHERE id=:id AND {_RECOVERABLE} FOR UPDATE SKIP LOCKED"
            ),
            {"id": task_id},
        )
        if status is None:
            continue

        uncertain = status == "migration_uncertain" or await forward_hold_required(
            db, task_id
        )
        await db.execute(
            text(
                f"UPDATE {TASK} SET status=:status, updated_at=now(), "
                "error_message=CASE WHEN :uncertain THEN COALESCE(error_message, :reason) "
                "ELSE error_message END, recovery_token=NULL, forward_run_id=NULL WHERE id=:id"
            ),
            {
                "id": task_id,
                "status": "migration_uncertain" if uncertain else "paused",
                "uncertain": uncertain,
                "reason": UNCERTAIN,
            },
        )
        recovered["uncertain" if uncertain else "paused"] += 1
    return recovered

"""Scheduled recovery of interrupted forward storage migrations."""

from app.tasks.async_db import task_async_session

from .services.migration_forward_recovery import recover_interrupted_forward_runs


async def recover_forward_runs() -> dict[str, int]:
    async with task_async_session() as db:
        recovered = await recover_interrupted_forward_runs(db)
        await db.commit()
    return recovered

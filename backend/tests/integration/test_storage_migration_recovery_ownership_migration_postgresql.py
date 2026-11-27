"""Behavioral: actual plugin migration preserves recovery ownership in PostgreSQL.

Use the explicitly selected ordinary-role owned fixture. Actual plugin001/002
create the prior schema, and actual Alembic Operations execute003. No mocked
database, storage/provider I/O, production data, or revision stamping is used.
This is a targeted migration gate, not full Alembic upgrade-head acceptance.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.plugins.module_loader import load_plugin_module
from tests.integration.storage_migration_test_support import (
    owned_storage as _owned_storage,
)

owned_storage = _owned_storage
pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]
_BLOCKED_STATES = (
    "source_cleaning",
    "cleanup_interrupted",
    "rolling_back",
    "rollback_failed",
    "rollback_interrupted",
)


async def _migration(db, name: str, action: str) -> None:
    module = load_plugin_module("storage-migration", "migrations.versions." + name)
    assert callable(getattr(module, action)), "Actual migration entrypoint is required"
    connection = await db.connection()

    def run(bind):
        with Operations.context(MigrationContext.configure(bind)):
            getattr(module, action)()

    await connection.run_sync(run)


async def _prior_schema(env) -> None:
    async with env.factory() as db:
        await _migration(db, "001_init", "upgrade")
        await _migration(db, "002_add_cleanup_and_meta_columns", "upgrade")
        await db.execute(
            text(
                "INSERT INTO px_storage_migration_tasks "
                "(id,source_driver,target_driver,status,scope,total_files,"
                "migrated_files,failed_files,skipped_files,total_bytes,migrated_bytes,"
                "concurrency,created_by,source_config_snapshot,target_config_snapshot) "
                "VALUES (1,'fictional-source','fictional-target','completed',"
                "'tenant:101',1,1,0,0,42,42,1,101,"
                '\'{"fixture":"source"}\', \'{"fixture":"target"}\')'
            )
        )
        await db.execute(
            text(
                "INSERT INTO px_storage_migration_logs "
                "(id,task_id,attachment_id,file_path,file_size,status,old_driver,"
                "old_base_url,old_meta) VALUES "
                "(1,1,201,'tenants/101/fictional.txt',42,'success',"
                "'fictional-source','', '{\"history\":\"preserve\"}')"
            )
        )
        await db.commit()


async def _snapshot(env, *, token_column: bool):
    async with env.factory() as db:
        task = (
            (
                await db.execute(
                    text("SELECT * FROM px_storage_migration_tasks WHERE id=1")
                )
            )
            .mappings()
            .one()
        )
        task = dict(task)
        token = task.pop("recovery_token") if token_column else None
        logs = [
            dict(row)
            for row in (
                await db.execute(
                    text("SELECT * FROM px_storage_migration_logs ORDER BY id")
                )
            ).mappings()
        ]
        connection = await db.connection()
        columns = await connection.run_sync(
            lambda bind: sa.inspect(bind).get_columns("px_storage_migration_tasks")
        )
        return task, token, logs, columns


async def test_upgrade_and_safe_downgrade_preserve_exact_historical_rows(owned_storage):
    env = owned_storage
    await _prior_schema(env)
    before = await _snapshot(env, token_column=False)
    async with env.factory() as db:
        await _migration(db, "003_recovery_ownership", "upgrade")
        await _migration(db, "003_recovery_ownership", "upgrade")
        await db.commit()
    after = await _snapshot(env, token_column=True)
    assert after[:3] == before[:3]
    token = next(column for column in after[3] if column["name"] == "recovery_token")
    assert isinstance(token["type"], sa.String)
    assert (token["type"].length, token["nullable"], token["default"]) == (
        32,
        True,
        None,
    )
    assert len(after[3]) == len(before[3]) + 1
    async with env.factory() as db:
        await _migration(db, "003_recovery_ownership", "downgrade")
        await _migration(db, "003_recovery_ownership", "downgrade")
        await db.commit()
    restored = await _snapshot(env, token_column=False)
    assert restored[:3] == before[:3]
    assert [column["name"] for column in restored[3]] == [
        column["name"] for column in before[3]
    ]


@pytest.mark.parametrize("status", _BLOCKED_STATES)
async def test_downgrade_refuses_every_recovery_hold_without_losing_history(
    owned_storage, status
):
    env = owned_storage
    await _prior_schema(env)
    async with env.factory() as db:
        await _migration(db, "003_recovery_ownership", "upgrade")
        await db.execute(
            text("UPDATE px_storage_migration_tasks SET status=:status WHERE id=1"),
            {"status": status},
        )
        await db.commit()
    before = await _snapshot(env, token_column=True)
    async with env.factory() as db:
        with pytest.raises(
            RuntimeError, match="^storage_recovery_ownership_downgrade_blocked$"
        ):
            await _migration(db, "003_recovery_ownership", "downgrade")
        await db.rollback()
    after = await _snapshot(env, token_column=True)
    assert after[:3] == before[:3]
    assert after[0]["status"] == status
    assert after[1] is None
    assert any(column["name"] == "recovery_token" for column in after[3])


async def test_downgrade_refuses_nonempty_owner_even_with_terminal_status(
    owned_storage,
):
    env = owned_storage
    await _prior_schema(env)
    token = "a1" * 16
    async with env.factory() as db:
        await _migration(db, "003_recovery_ownership", "upgrade")
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET recovery_token=:token WHERE id=1"
            ),
            {"token": token},
        )
        await db.commit()
    before = await _snapshot(env, token_column=True)
    async with env.factory() as db:
        with pytest.raises(
            RuntimeError, match="^storage_recovery_ownership_downgrade_blocked$"
        ):
            await _migration(db, "003_recovery_ownership", "downgrade")
        await db.rollback()
    after = await _snapshot(env, token_column=True)
    assert after[:3] == before[:3]
    assert (after[0]["status"], after[1]) == ("completed", token)
    assert any(column["name"] == "recovery_token" for column in after[3])

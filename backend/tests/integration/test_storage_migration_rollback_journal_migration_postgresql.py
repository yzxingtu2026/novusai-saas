"""Behavioral: actual PostgreSQL rollback-journal DDL preserves recovery history.

Actual plugin001..004 migrations and ordinary-role PostgreSQL constraints run
in the explicitly selected owned fixture. No storage/provider calls or revision
stamping occurs. This targeted gate is not a full upgrade-head acceptance.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.storage_migration_test_support import (
    owned_storage as _owned_storage,
)
from tests.integration.test_storage_migration_recovery_ownership_migration_postgresql import (
    _BLOCKED_STATES,
    _migration,
    _prior_schema,
)

owned_storage = _owned_storage
pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]
_JOURNAL_STATES = ("source_restored", "target_deleting", "target_deleted")
_CONSTRAINT = "ck_px_sm_logs_rollback_journal"
_SNAPSHOT = {"version": 1, "fixture": "frozen-target-proof"}


async def _through003(env):
    await _prior_schema(env)
    async with env.factory() as db:
        await _migration(db, "003_recovery_ownership", "upgrade")
        await db.commit()


async def _upgrade004(env):
    async with env.factory() as db:
        await _migration(db, "004_rollback_journal", "upgrade")
        await db.commit()


async def _snapshot(env, *, journal: bool):
    async with env.factory() as db:
        task = dict(
            (
                await db.execute(
                    text("SELECT * FROM px_storage_migration_tasks WHERE id=1")
                )
            )
            .mappings()
            .one()
        )
        log = dict(
            (
                await db.execute(
                    text("SELECT * FROM px_storage_migration_logs WHERE id=1")
                )
            )
            .mappings()
            .one()
        )
        recovery = (
            (log.pop("rollback_state"), log.pop("rollback_snapshot"))
            if journal
            else None
        )
        connection = await db.connection()
        columns, constraints = await connection.run_sync(
            lambda bind: (
                sa.inspect(bind).get_columns("px_storage_migration_logs"),
                sa.inspect(bind).get_check_constraints("px_storage_migration_logs"),
            )
        )
        return task, log, recovery, columns, constraints


async def _refused_downgrade(env):
    before = await _snapshot(env, journal=True)
    async with env.factory() as db:
        with pytest.raises(
            RuntimeError, match="^storage_rollback_journal_downgrade_blocked$"
        ):
            await _migration(db, "004_rollback_journal", "downgrade")
        await db.rollback()
    after = await _snapshot(env, journal=True)
    assert after[:3] == before[:3]
    assert [column["name"] for column in after[3]] == [
        column["name"] for column in before[3]
    ]
    assert {constraint["name"] for constraint in after[4]} == {
        constraint["name"] for constraint in before[4]
    }


async def test_journal_upgrade_and_safe_downgrade_preserve_exact_history(owned_storage):
    env = owned_storage
    await _through003(env)
    before = await _snapshot(env, journal=False)
    await _upgrade004(env)
    await _upgrade004(env)
    after = await _snapshot(env, journal=True)
    assert after[:2] == before[:2]
    assert after[2] == ("none", None)
    columns = {column["name"]: column for column in after[3]}
    state, snapshot = columns["rollback_state"], columns["rollback_snapshot"]
    assert isinstance(state["type"], sa.String)
    assert (state["type"].length, state["nullable"]) == (20, False)
    assert state["default"] == "'none'::character varying"
    assert isinstance(snapshot["type"], sa.JSON)
    assert (snapshot["nullable"], snapshot["default"]) == (True, None)
    assert {constraint["name"] for constraint in after[4]} == {_CONSTRAINT}
    async with env.factory() as db:
        await _migration(db, "004_rollback_journal", "downgrade")
        await _migration(db, "004_rollback_journal", "downgrade")
        await db.commit()
    restored = await _snapshot(env, journal=False)
    assert restored[:3] == before[:3]
    assert [column["name"] for column in restored[3]] == [
        column["name"] for column in before[3]
    ]
    assert restored[4] == before[4]


@pytest.mark.parametrize("state", _JOURNAL_STATES)
async def test_valid_journal_state_is_durable_and_blocks_downgrade(
    owned_storage, state
):
    env = owned_storage
    await _through003(env)
    await _upgrade004(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET rollback_state=:state, "
                "rollback_snapshot=CAST(:snapshot AS json) WHERE id=1"
            ),
            {"state": state, "snapshot": json.dumps(_SNAPSHOT)},
        )
        await db.commit()
    current = await _snapshot(env, journal=True)
    assert current[2] == (state, _SNAPSHOT)
    await _refused_downgrade(env)


@pytest.mark.parametrize(
    ("state", "snapshot"),
    [
        ("unknown", _SNAPSHOT),
        ("none", _SNAPSHOT),
        ("source_restored", None),
        ("target_deleting", None),
        ("target_deleted", None),
        (None, None),
    ],
)
async def test_database_rejects_invalid_journal_state_and_null_pairs(
    owned_storage, state, snapshot
):
    env = owned_storage
    await _through003(env)
    await _upgrade004(env)
    before = await _snapshot(env, journal=True)
    async with env.factory() as db:
        with pytest.raises(IntegrityError) as raised:
            await db.execute(
                text(
                    "UPDATE px_storage_migration_logs SET rollback_state=:state, "
                    "rollback_snapshot=CAST(:snapshot AS json) WHERE id=1"
                ),
                {
                    "state": state,
                    "snapshot": json.dumps(snapshot) if snapshot is not None else None,
                },
            )
        await db.rollback()
    assert raised.value.orig.sqlstate == ("23502" if state is None else "23514")
    after = await _snapshot(env, journal=True)
    assert after[:3] == before[:3]
    assert after[2] == ("none", None)


async def test_journal_json_null_is_not_physical_sql_null_for_downgrade(owned_storage):
    """Database pairs require presence; runtime owns snapshot content validation."""
    env = owned_storage
    await _through003(env)
    await _upgrade004(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET rollback_state='source_restored', "
                "rollback_snapshot=CAST('null' AS json) WHERE id=1"
            )
        )
        await db.commit()
    async with env.factory() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT rollback_snapshot IS NULL "
                    "FROM px_storage_migration_logs WHERE id=1"
                )
            )
        ) is False
    await _refused_downgrade(env)


@pytest.mark.parametrize("status", _BLOCKED_STATES)
async def test_journal_downgrade_refuses_active_or_held_recovery(owned_storage, status):
    env = owned_storage
    await _through003(env)
    await _upgrade004(env)
    async with env.factory() as db:
        await db.execute(
            text("UPDATE px_storage_migration_tasks SET status=:status WHERE id=1"),
            {"status": status},
        )
        await db.commit()
    await _refused_downgrade(env)


async def test_journal_downgrade_refuses_owner_even_with_unused_journal(owned_storage):
    env = owned_storage
    await _through003(env)
    await _upgrade004(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET recovery_token=:token WHERE id=1"
            ),
            {"token": "b2" * 16},
        )
        await db.commit()
    await _refused_downgrade(env)

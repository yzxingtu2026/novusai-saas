"""Behavioral: actual PostgreSQL forward-attempt DDL retains uncertain receipts.

Actual plugin001..005 migrations run using the explicitly selected owned
ordinary-role PostgreSQL fixture. No mocked SQL, storage/provider calls or
revision stamping occurs. This is a targeted migration gate, not upgrade-head
or forward-runner reconciliation acceptance.
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
)
from tests.integration.test_storage_migration_rollback_journal_migration_postgresql import (
    _through003,
    _upgrade004,
)

owned_storage = _owned_storage
pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]
_CONSTRAINT = "ck_px_sm_logs_forward_attempt"
_TOKEN = "c3" * 16
_RECEIPT = {"version": 1, "fixture": "forward-attempt-receipt"}
_ATTEMPT_TABLE = "px_storage_migration_attempts"


async def _through004(env):
    await _through003(env)
    await _upgrade004(env)


async def _upgrade005(env):
    async with env.factory() as db:
        await _migration(db, "005_forward_attempt", "upgrade")
        await db.commit()


async def _snapshot(env, *, forward: bool):
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
        attempt = (
            (log.pop("forward_token"), log.pop("forward_snapshot")) if forward else None
        )
        connection = await db.connection()
        columns, constraints = await connection.run_sync(
            lambda bind: (
                sa.inspect(bind).get_columns("px_storage_migration_logs"),
                sa.inspect(bind).get_check_constraints("px_storage_migration_logs"),
            )
        )
        return task, log, attempt, columns, constraints


async def _ledger(env):
    async with env.factory() as db:
        connection = await db.connection()

        def inspect(bind):
            inspector = sa.inspect(bind)
            if not inspector.has_table(_ATTEMPT_TABLE):
                return None
            return {
                "columns": {
                    column["name"]: {
                        "type": str(column["type"]),
                        "timezone": getattr(column["type"], "timezone", None),
                        "nullable": column["nullable"],
                        "default": column["default"],
                    }
                    for column in inspector.get_columns(_ATTEMPT_TABLE)
                },
                "primary_key": inspector.get_pk_constraint(_ATTEMPT_TABLE),
                "foreign_keys": inspector.get_foreign_keys(_ATTEMPT_TABLE),
                "indexes": inspector.get_indexes(_ATTEMPT_TABLE),
                "constraints": inspector.get_check_constraints(_ATTEMPT_TABLE),
            }

        result = await connection.run_sync(inspect)
        if result is not None:
            result["rows"] = [
                dict(row)
                for row in (
                    await db.execute(
                        text(
                            "SELECT * FROM px_storage_migration_attempts ORDER BY token"
                        )
                    )
                ).mappings()
            ]
        return result


async def _insert_attempt(env, *, token=_TOKEN, state="uploaded", snapshot=_RECEIPT):
    async with env.factory() as db:
        await db.execute(
            text(
                "INSERT INTO px_storage_migration_attempts "
                "(token,task_id,log_id,state,snapshot) VALUES "
                "(:token,1,1,:state,CAST(:snapshot AS json))"
            ),
            {"token": token, "state": state, "snapshot": json.dumps(snapshot)},
        )
        await db.commit()


async def _refused_downgrade(env):
    before = await _snapshot(env, forward=True)
    ledger_before = await _ledger(env)
    async with env.factory() as db:
        with pytest.raises(
            RuntimeError, match="^storage_forward_attempt_downgrade_blocked$"
        ):
            await _migration(db, "005_forward_attempt", "downgrade")
        await db.rollback()
    after = await _snapshot(env, forward=True)
    assert after[:3] == before[:3]
    assert [column["name"] for column in after[3]] == [
        column["name"] for column in before[3]
    ]
    assert {constraint["name"] for constraint in after[4]} == {
        constraint["name"] for constraint in before[4]
    }
    assert await _ledger(env) == ledger_before


async def test_forward_upgrade_and_safe_downgrade_preserve_exact_history(owned_storage):
    env = owned_storage
    await _through004(env)
    before = await _snapshot(env, forward=False)
    assert await _ledger(env) is None
    await _upgrade005(env)
    await _upgrade005(env)
    after = await _snapshot(env, forward=True)
    assert after[:2] == before[:2]
    assert after[2] == (None, None)
    columns = {column["name"]: column for column in after[3]}
    token, receipt = columns["forward_token"], columns["forward_snapshot"]
    assert isinstance(token["type"], sa.String)
    assert (token["type"].length, token["nullable"], token["default"]) == (
        32,
        True,
        None,
    )
    assert isinstance(receipt["type"], sa.JSON)
    assert (receipt["nullable"], receipt["default"]) == (True, None)
    assert {constraint["name"] for constraint in after[4]} == {
        _CONSTRAINT,
        "ck_px_sm_logs_rollback_journal",
    }
    async with env.factory() as db:
        connection = await db.connection()
        log_keys = await connection.run_sync(
            lambda bind: sa.inspect(bind).get_foreign_keys("px_storage_migration_logs")
        )
    assert not any(
        {"forward_token", "forward_snapshot"}.intersection(key["constrained_columns"])
        for key in log_keys
    )
    ledger = await _ledger(env)
    assert ledger["rows"] == []
    assert ledger["primary_key"]["constrained_columns"] == ["token"]
    assert {
        (
            tuple(key["constrained_columns"]),
            key["referred_table"],
            tuple(key["referred_columns"]),
        )
        for key in ledger["foreign_keys"]
    } == {
        (("task_id",), "px_storage_migration_tasks", ("id",)),
        (("log_id",), "px_storage_migration_logs", ("id",)),
    }
    assert {
        (index["name"], tuple(index["column_names"])) for index in ledger["indexes"]
    } == {("ix_px_sm_attempts_task_log", ("task_id", "log_id"))}
    assert {item["name"] for item in ledger["constraints"]} == {
        "ck_px_sm_attempts_state"
    }
    assert ledger["columns"] == {
        "token": {
            "type": "VARCHAR(32)",
            "timezone": None,
            "nullable": False,
            "default": None,
        },
        "task_id": {
            "type": "BIGINT",
            "timezone": None,
            "nullable": False,
            "default": None,
        },
        "log_id": {
            "type": "BIGINT",
            "timezone": None,
            "nullable": False,
            "default": None,
        },
        "state": {
            "type": "VARCHAR(20)",
            "timezone": None,
            "nullable": False,
            "default": None,
        },
        "snapshot": {
            "type": "JSON",
            "timezone": None,
            "nullable": False,
            "default": None,
        },
        "created_at": {
            "type": "TIMESTAMP",
            "timezone": True,
            "nullable": False,
            "default": "now()",
        },
        "updated_at": {
            "type": "TIMESTAMP",
            "timezone": True,
            "nullable": False,
            "default": "now()",
        },
    }
    async with env.factory() as db:
        await _migration(db, "005_forward_attempt", "downgrade")
        await _migration(db, "005_forward_attempt", "downgrade")
        await db.commit()
    restored = await _snapshot(env, forward=False)
    assert restored[:3] == before[:3]
    assert [column["name"] for column in restored[3]] == [
        column["name"] for column in before[3]
    ]
    assert restored[4] == before[4]
    assert await _ledger(env) is None


@pytest.mark.parametrize(
    "status", ["success", "failed", "transferring", "commit_unknown"]
)
async def test_attempt_receipt_is_allowed_and_preserved_for_every_settlement_status(
    owned_storage, status
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET status=:status, "
                "forward_token=:token,forward_snapshot=CAST(:receipt AS json) WHERE id=1"
            ),
            {"status": status, "token": _TOKEN, "receipt": json.dumps(_RECEIPT)},
        )
        await db.commit()
    current = await _snapshot(env, forward=True)
    assert (current[1]["status"], current[2]) == (status, (_TOKEN, _RECEIPT))
    await _refused_downgrade(env)


@pytest.mark.parametrize("token", [None, _TOKEN])
async def test_database_rejects_unpaired_forward_identity(owned_storage, token):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    before = await _snapshot(env, forward=True)
    async with env.factory() as db:
        with pytest.raises(IntegrityError) as raised:
            await db.execute(
                text(
                    "UPDATE px_storage_migration_logs SET forward_token=:token, "
                    "forward_snapshot=CAST(:receipt AS json) WHERE id=1"
                ),
                {
                    "token": token,
                    "receipt": json.dumps(_RECEIPT) if token is None else None,
                },
            )
        await db.rollback()
    assert raised.value.orig.sqlstate == "23514"
    after = await _snapshot(env, forward=True)
    assert after[:3] == before[:3]
    assert after[2] == (None, None)


@pytest.mark.parametrize("status", ["transferring", "commit_unknown"])
async def test_forward_status_alone_blocks_downgrade_without_receipt(
    owned_storage, status
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text("UPDATE px_storage_migration_logs SET status=:status WHERE id=1"),
            {"status": status},
        )
        await db.commit()
    current = await _snapshot(env, forward=True)
    assert (current[1]["status"], current[2]) == (status, (None, None))
    await _refused_downgrade(env)


@pytest.mark.parametrize("status", [*_BLOCKED_STATES, "migration_uncertain"])
async def test_forward_downgrade_preserves_task_uncertainty_and_recovery_holds(
    owned_storage, status
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text("UPDATE px_storage_migration_tasks SET status=:status WHERE id=1"),
            {"status": status},
        )
        await db.commit()
    await _refused_downgrade(env)


async def test_forward_downgrade_preserves_recovery_owner(owned_storage):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_tasks SET recovery_token=:token WHERE id=1"
            ),
            {"token": "d4" * 16},
        )
        await db.commit()
    await _refused_downgrade(env)


@pytest.mark.parametrize(
    "state", ["source_restored", "target_deleting", "target_deleted"]
)
async def test_forward_downgrade_preserves_existing_rollback_journal(
    owned_storage, state
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET rollback_state=:state, "
                "rollback_snapshot=CAST(:receipt AS json) WHERE id=1"
            ),
            {"state": state, "receipt": json.dumps(_RECEIPT)},
        )
        await db.commit()
    await _refused_downgrade(env)


async def test_forward_json_null_is_present_receipt_and_not_safe_to_drop(owned_storage):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET forward_token=:token, "
                "forward_snapshot=CAST('null' AS json) WHERE id=1"
            ),
            {"token": _TOKEN},
        )
        await db.commit()
    async with env.factory() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT forward_snapshot IS NULL "
                    "FROM px_storage_migration_logs WHERE id=1"
                )
            )
        ) is False
    await _refused_downgrade(env)


@pytest.mark.parametrize(
    "state", ["prepared", "uploaded", "succeeded", "failed", "unknown"]
)
async def test_every_persisted_attempt_refuses_downgrade_even_without_latest_pointer(
    owned_storage, state
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    await _insert_attempt(env, state=state)
    current = await _snapshot(env, forward=True)
    assert (current[0]["status"], current[1]["status"], current[2]) == (
        "completed",
        "success",
        (None, None),
    )
    ledger = await _ledger(env)
    row = ledger["rows"][0]
    assert (
        row["token"],
        row["task_id"],
        row["log_id"],
        row["state"],
        row["snapshot"],
    ) == (_TOKEN, 1, 1, state, _RECEIPT)
    assert row["created_at"].utcoffset() is not None
    assert row["updated_at"] == row["created_at"]
    await _refused_downgrade(env)


async def test_new_latest_attempt_keeps_exact_older_uploaded_receipt(owned_storage):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    business = await _snapshot(env, forward=True)
    old_receipt = {
        "version": 1,
        "source": "fictional-source",
        "target": "fictional-first-target",
    }
    await _insert_attempt(env, snapshot=old_receipt)
    original_ledger = await _ledger(env)
    second_token = "e5" * 16
    new_receipt = {
        "version": 1,
        "source": "fictional-source",
        "target": "fictional-second-target",
    }
    await _insert_attempt(
        env, token=second_token, state="prepared", snapshot=new_receipt
    )
    async with env.factory() as db:
        await db.execute(
            text(
                "UPDATE px_storage_migration_logs SET forward_token=:token, "
                "forward_snapshot=CAST(:receipt AS json) WHERE id=1"
            ),
            {"token": second_token, "receipt": json.dumps(new_receipt)},
        )
        await db.execute(
            text(
                "UPDATE px_storage_migration_attempts SET state='succeeded', "
                "updated_at=now() WHERE token=:token"
            ),
            {"token": second_token},
        )
        await db.commit()
    await _upgrade005(env)
    latest = await _snapshot(env, forward=True)
    ledger = await _ledger(env)
    assert latest[:2] == business[:2]
    assert latest[2] == (second_token, new_receipt)
    assert ledger["rows"][0] == original_ledger["rows"][0]
    assert [
        (row["token"], row["state"], row["snapshot"]) for row in ledger["rows"]
    ] == [
        (_TOKEN, "uploaded", old_receipt),
        (second_token, "succeeded", new_receipt),
    ]
    await _refused_downgrade(env)


@pytest.mark.parametrize(
    ("task", "log", "state", "receipt", "sqlstate"),
    [
        (999, 1, "prepared", _RECEIPT, "23503"),
        (1, 999, "prepared", _RECEIPT, "23503"),
        (1, 1, "invalid", _RECEIPT, "23514"),
        (1, 1, "prepared", None, "23502"),
    ],
)
async def test_attempt_table_enforces_actual_foreign_keys_and_shape(
    owned_storage, task, log, state, receipt, sqlstate
):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    async with env.factory() as db:
        with pytest.raises(IntegrityError) as raised:
            await db.execute(
                text(
                    "INSERT INTO px_storage_migration_attempts "
                    "(token,task_id,log_id,state,snapshot) VALUES "
                    "(:token,:task,:log,:state,CAST(:receipt AS json))"
                ),
                {
                    "token": _TOKEN,
                    "task": task,
                    "log": log,
                    "state": state,
                    "receipt": json.dumps(receipt) if receipt is not None else None,
                },
            )
        await db.rollback()
    assert raised.value.orig.sqlstate == sqlstate
    assert (await _ledger(env))["rows"] == []


async def test_duplicate_attempt_token_cannot_replace_prior_receipt(owned_storage):
    env = owned_storage
    await _through004(env)
    await _upgrade005(env)
    await _insert_attempt(env)
    before = await _ledger(env)
    async with env.factory() as db:
        with pytest.raises(IntegrityError) as raised:
            await db.execute(
                text(
                    "INSERT INTO px_storage_migration_attempts "
                    "(token,task_id,log_id,state,snapshot) VALUES "
                    "(:token,1,1,'failed',CAST(:receipt AS json))"
                ),
                {
                    "token": _TOKEN,
                    "receipt": json.dumps({"fixture": "must-not-replace"}),
                },
            )
        await db.rollback()
    assert raised.value.orig.sqlstate == "23505"
    assert await _ledger(env) == before

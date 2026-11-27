"""Private durable receipts for forward storage transfers.

No receipt or database exception in this module is a public error projection.
Reconciliation reads database truth after the writer connection is invalidated;
it never performs storage I/O or infers a failed COMMIT from client rollback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text

from app.models.tenant.attachment import Attachment

from .migration_object_path import relative_parts

TASK = "px_storage_migration_tasks"
LOG = "px_storage_migration_logs"
ATTEMPT = "px_storage_migration_attempts"
UNCERTAIN = "storage_migration_outcome_unknown"
FAILED = "storage_migration_commit_failed"
_TASK_FIELDS = (
    "id",
    "source_driver",
    "target_driver",
    "scope",
    "source_config_snapshot",
    "target_config_snapshot",
    "started_at",
    "recovery_token",
)
_LOG_FIELDS = (
    "id",
    "task_id",
    "attachment_id",
    "file_path",
    "file_size",
    "old_driver",
    "old_base_url",
    "old_meta",
    "new_driver",
    "new_base_url",
    "migrated_at",
    "rollback_state",
    "rollback_snapshot",
)
_SOURCE_FIELDS = (
    "id",
    "tenant_id",
    "path",
    "size",
    "hash",
    "driver",
    "base_url",
    "meta",
    "visibility",
    "mime_type",
    "status",
    "is_deleted",
)


def _source_fields(version: int) -> tuple[str, ...]:
    if version == 3:
        return (*_SOURCE_FIELDS, "created_at", "public_id", "local_object_identity")
    if version == 2:
        return (*_SOURCE_FIELDS, "created_at")
    return _SOURCE_FIELDS


def _json(value: Any) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("storage_forward_receipt_invalid")

    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"))


def _freeze(value: Any) -> Any:
    return json.loads(_json(value))


def _equal(left: Any, right: Any) -> bool:
    # JSON equality is type-strict: Python's True == 1 is not receipt identity.
    return _json(left) == _json(right)


def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return _freeze({key: row[key] for key in fields})


def config_identity(config, version: int) -> dict[str, Any]:
    """Freeze configuration identity without duplicating credentials / 冻结配置身份，避免复制凭据。"""
    if not isinstance(config, dict):
        config = {
            "driver": config.driver,
            "root_path": config.root_path,
            "base_url": config.base_url,
            "options": config.options,
        }
    if version < 3:
        return config
    return {
        "driver": config["driver"],
        "root_path": config["root_path"],
        "base_url": config["base_url"],
        "options_sha256": hashlib.sha256(
            _json(config.get("options", {})).encode()
        ).hexdigest(),
    }


def _task_snapshot(row, version: int) -> dict[str, Any]:
    task = _project(dict(row), _TASK_FIELDS)
    if version == 3:
        for field in ("source_config_snapshot", "target_config_snapshot"):
            task[field] = config_identity(task[field], version)
    return task


@dataclass
class ForwardAttempt:
    token: str
    task_id: int
    log_id: int
    attachment_id: int
    path: str
    snapshot: dict[str, Any]
    phase: str = "prepared"
    writer_pid: int | None = None


class ForwardCommitUncertain(RuntimeError):
    """Only fixed text is printable; the receipt stays private to the runner."""

    def __init__(self, attempt: ForwardAttempt, phase: str):
        super().__init__(UNCERTAIN)
        self.attempt = attempt
        self.phase = phase
        attempt.phase = phase


def _valid(attempt: ForwardAttempt) -> bool:
    snap = attempt.snapshot
    return (
        type(snap) is dict
        and set(snap)
        == {"version", "token", "task", "log", "source", "target"}
        | ({"target_path"} if snap.get("version") in {2, 3} else set())
        and type(snap["version"]) is int
        and snap["version"] in {1, 2, 3}
        and type(attempt.token) is str
        and len(attempt.token) == 32
        and snap["token"] == attempt.token
        and all(
            type(value) is int and value > 0
            for value in (
                attempt.task_id,
                attempt.log_id,
                attempt.attachment_id,
            )
        )
        and type(snap["task"]) is dict
        and set(snap["task"]) == set(_TASK_FIELDS)
        and snap["task"]["id"] == attempt.task_id
        and type(snap["task"]["recovery_token"]) is str
        and len(snap["task"]["recovery_token"]) == 32
        and type(snap["log"]) is dict
        and set(snap["log"]) == set(_LOG_FIELDS)
        and snap["log"]["id"] == attempt.log_id
        and snap["log"]["task_id"] == attempt.task_id
        and snap["log"]["attachment_id"] == attempt.attachment_id
        and snap["log"]["file_path"] == attempt.path
        and type(snap["source"]) is dict
        and set(snap["source"]) == set(_source_fields(snap["version"]))
        and snap["source"]["id"] == attempt.attachment_id
        and snap["source"]["path"] == attempt.path
        and (
            snap["version"] == 1
            or (type(snap.get("target_path")) is str and bool(snap["target_path"]))
        )
    )


async def _locked(db: Any, attempt: ForwardAttempt):
    if not _valid(attempt):
        raise RuntimeError(UNCERTAIN)
    task = (
        (
            await db.execute(
                text(f"SELECT * FROM {TASK} WHERE id=:id FOR UPDATE"),
                {"id": attempt.task_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        task is None
        or task["status"] not in ("running", "paused", "migration_uncertain")
        or not _equal(
            _task_snapshot(task, attempt.snapshot["version"]), attempt.snapshot["task"]
        )
    ):
        raise RuntimeError(UNCERTAIN)
    log = (
        (
            await db.execute(
                text(f"SELECT * FROM {LOG} WHERE id=:id AND task_id=:task FOR UPDATE"),
                {"id": attempt.log_id, "task": attempt.task_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    attachment = await db.scalar(
        select(Attachment)
        .where(Attachment.id == attempt.attachment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    ledger = (
        (
            await db.execute(
                text(f"SELECT * FROM {ATTEMPT} WHERE token=:token FOR UPDATE"),
                {"token": attempt.token},
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        log is None
        or attachment is None
        or ledger is None
        or log["forward_token"] != attempt.token
        or ledger["task_id"] != attempt.task_id
        or ledger["log_id"] != attempt.log_id
    ):
        raise RuntimeError(UNCERTAIN)
    return dict(log), attachment, dict(ledger)


def _attachment(row: Attachment, version: int) -> dict[str, Any]:
    return _freeze({key: getattr(row, key, None) for key in _source_fields(version)})


def _source_matches(
    attempt: ForwardAttempt, log: dict[str, Any], attachment: Attachment
) -> bool:
    return (
        log["status"] == "transferring"
        and _equal(_project(log, _LOG_FIELDS), attempt.snapshot["log"])
        and _equal(
            _attachment(attachment, attempt.snapshot["version"]),
            attempt.snapshot["source"],
        )
    )


async def prepare_forward_attempt(
    db: Any, log_id: int, attachment_id: int, file_path: str
) -> ForwardAttempt:
    relative_parts(file_path)
    task_id = await db.scalar(
        text(f"SELECT task_id FROM {LOG} WHERE id=:id"), {"id": log_id}
    )
    task = (
        (
            await db.execute(
                text(f"SELECT * FROM {TASK} WHERE id=:id FOR UPDATE"),
                {"id": task_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    log = (
        (
            await db.execute(
                text(f"SELECT * FROM {LOG} WHERE id=:id FOR UPDATE"),
                {"id": log_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    attachment = await db.scalar(
        select(Attachment)
        .where(Attachment.id == attachment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        task is None
        or log is None
        or attachment is None
        or task["status"] != "running"
        or type(task["recovery_token"]) is not str
        or len(task["recovery_token"]) != 32
        or task["recovery_token"] != db.info.get("storage_forward_owner")
        or log["task_id"] != task_id
        or log["status"] != "pending"
        or log["attachment_id"] != attachment_id
        or log["file_path"] != file_path
        or attachment.path != file_path
        or attachment.is_deleted
        or attachment.status != "active"
        or attachment.driver != task["source_driver"]
        or log["old_driver"] != attachment.driver
        or log["old_base_url"] != (attachment.base_url or "")
        or type(log["file_size"]) is not int
        or log["file_size"] != attachment.size
        or not _equal(log["old_meta"], attachment.meta)
        or (
            task["scope"] != "all" and task["scope"] != f"tenant:{attachment.tenant_id}"
        )
    ):
        raise RuntimeError("storage_migration_admission_failed")
    if await db.scalar(
        text(
            f"SELECT EXISTS(SELECT 1 FROM {ATTEMPT} attempt JOIN {LOG} log "
            "ON log.id=attempt.log_id WHERE log.attachment_id=:attachment "
            "AND attempt.state IN ('prepared','uploaded','unknown'))"
        ),
        {"attachment": attachment_id},
    ):
        raise RuntimeError("storage_migration_admission_failed")
    token = uuid4().hex
    snapshot = {
        "version": 3,
        "token": token,
        "task": _task_snapshot(task, 3),
        "log": _project(dict(log), _LOG_FIELDS),
        "source": _attachment(attachment, 3),
        "target": None,
        "target_path": file_path,
    }
    attempt = ForwardAttempt(token, task_id, log_id, attachment_id, file_path, snapshot)
    await db.execute(
        text(
            f"INSERT INTO {ATTEMPT}(token,task_id,log_id,state,snapshot) "
            "VALUES (:token,:task,:log,'prepared',CAST(:snapshot AS json))"
        ),
        {"token": token, "task": task_id, "log": log_id, "snapshot": _json(snapshot)},
    )
    await db.execute(
        text(
            f"UPDATE {LOG} SET status='transferring',forward_token=:token,"
            "forward_snapshot=CAST(:snapshot AS json),error_message=NULL WHERE id=:id"
        ),
        {"token": token, "snapshot": _json(snapshot), "id": log_id},
    )
    attempt.writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
    try:
        await db.commit()
    except Exception:
        raise ForwardCommitUncertain(attempt, "prepared") from None
    return attempt


async def record_uploaded(
    db: Any,
    attempt: ForwardAttempt,
    *,
    updated_meta: dict[str, Any],
    target_driver_name: str,
    target_base_url: str,
    migrated_at: datetime,
    target_path: str | None = None,
    local_identity: dict[str, Any] | None = None,
) -> None:
    log, attachment, ledger = await _locked(db, attempt)
    if (
        not _source_matches(attempt, log, attachment)
        or ledger["state"] != "prepared"
        or not _equal(ledger["snapshot"], attempt.snapshot)
        or not _equal(log["forward_snapshot"], attempt.snapshot)
        or target_driver_name != attempt.snapshot["task"]["target_driver"]
        or (target_path or attempt.path)
        != attempt.snapshot.get("target_path", attempt.path)
    ):
        raise RuntimeError(UNCERTAIN)
    attempt.snapshot = {
        **attempt.snapshot,
        "target": _freeze(
            {
                "driver": target_driver_name,
                "base_url": target_base_url,
                "meta": updated_meta,
                "migrated_at": migrated_at,
                **({"path": target_path} if attempt.snapshot["version"] >= 2 else {}),
                **(
                    {"local_object_identity": local_identity}
                    if attempt.snapshot["version"] == 3
                    else {}
                ),
            }
        ),
    }
    await _write_receipt(db, attempt, "uploaded")
    attempt.writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
    try:
        await db.commit()
    except Exception:
        raise ForwardCommitUncertain(attempt, "uploaded") from None


async def _write_receipt(db: Any, attempt: ForwardAttempt, state: str) -> None:
    params = {
        "token": attempt.token,
        "snapshot": _json(attempt.snapshot),
        "state": state,
        "id": attempt.log_id,
    }
    await db.execute(
        text(
            f"UPDATE {ATTEMPT} SET state=:state,snapshot=CAST(:snapshot AS json),updated_at=now() WHERE token=:token"
        ),
        params,
    )
    await db.execute(
        text(
            f"UPDATE {LOG} SET forward_snapshot=CAST(:snapshot AS json) WHERE id=:id AND forward_token=:token"
        ),
        params,
    )


async def lock_forward_attempt(db: Any, attempt: ForwardAttempt) -> None:
    log, attachment, ledger = await _locked(db, attempt)
    if (
        not _source_matches(attempt, log, attachment)
        or ledger["state"] != "uploaded"
        or attempt.snapshot["target"] is None
        or not _equal(ledger["snapshot"], attempt.snapshot)
        or not _equal(log["forward_snapshot"], attempt.snapshot)
    ):
        raise RuntimeError(UNCERTAIN)
    attempt.writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))


async def mark_forward_succeeded(db: Any, attempt: ForwardAttempt) -> None:
    # Caller retains task/log/attachment/ledger locks through the business COMMIT.
    await _write_receipt(db, attempt, "succeeded")


async def fail_forward_attempt(db: Any, attempt: ForwardAttempt) -> None:
    log, attachment, ledger = await _locked(db, attempt)
    if (
        not _source_matches(attempt, log, attachment)
        or ledger["state"] != "prepared"
        or not _equal(ledger["snapshot"], attempt.snapshot)
        or not _equal(log["forward_snapshot"], attempt.snapshot)
    ):
        raise ForwardCommitUncertain(attempt, "prepared")
    await db.execute(
        text(f"UPDATE {LOG} SET status='failed',error_message=:reason WHERE id=:id"),
        {"id": attempt.log_id, "reason": "storage_migration_transfer_failed"},
    )
    await _write_receipt(db, attempt, "failed")
    attempt.writer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
    try:
        await db.commit()
    except Exception:
        raise ForwardCommitUncertain(attempt, "prepared") from None


async def _bound_reads(db: Any) -> None:
    for setting, limit in (("lock_timeout", 500), ("statement_timeout", 2000)):
        await db.execute(
            text(
                f"SELECT set_config('{setting}', CAST(GREATEST(1,LEAST("
                f"COALESCE(NULLIF(EXTRACT(EPOCH FROM current_setting('{setting}')::interval)*1000,0),"
                "CAST(:ceiling AS integer)),CAST(:ceiling AS integer))) AS integer)::text || 'ms', true)"
            ),
            {"ceiling": limit},
        )


async def forward_hold_required(db: Any, task_id: int) -> bool:
    """Gate task mutations on the existing transfer journal and attempt ledger."""
    return bool(
        await db.scalar(
            text(
                f"SELECT EXISTS(SELECT 1 FROM {LOG} WHERE task_id=:id "
                "AND status IN ('transferring','commit_unknown')) "
                f"OR EXISTS(SELECT 1 FROM {ATTEMPT} WHERE task_id=:id "
                "AND state IN ('prepared','uploaded','unknown'))"
            ),
            {"id": task_id},
        )
    )


async def reconcile_forward_attempt(db: Any, attempt: ForwardAttempt) -> str:
    """Resolve exact durable evidence; any mixed/unreadable state stays held."""
    try:
        await _bound_reads(db)
        log, attachment, ledger = await _locked(db, attempt)
        target = attempt.snapshot["target"]
        exact_receipt = _equal(log["forward_snapshot"], attempt.snapshot) and _equal(
            ledger["snapshot"], attempt.snapshot
        )
        if target is not None and ledger["state"] == "succeeded" and exact_receipt:
            expected_attachment = {
                **attempt.snapshot["source"],
                **{key: target[key] for key in ("driver", "base_url", "meta")},
                "path": attempt.snapshot.get("target_path", attempt.path),
            }
            if attempt.snapshot["version"] == 3 and hasattr(
                Attachment, "local_object_identity"
            ):
                expected_attachment["local_object_identity"] = target[
                    "local_object_identity"
                ]
            expected_log = {
                **attempt.snapshot["log"],
                "new_driver": target["driver"],
                "new_base_url": target["base_url"],
                "migrated_at": target["migrated_at"],
                "rollback_state": "none",
                "rollback_snapshot": None,
            }
            if (
                log["status"] == "success"
                and log["error_message"] is None
                and _equal(_project(log, _LOG_FIELDS), expected_log)
                and _equal(
                    _attachment(attachment, attempt.snapshot["version"]),
                    expected_attachment,
                )
            ):
                await db.rollback()
                return "committed"
        if (
            attempt.phase == "prepared"
            and ledger["state"] == "failed"
            and log["status"] == "failed"
            and log["error_message"] == "storage_migration_transfer_failed"
            and exact_receipt
            and _equal(_project(log, _LOG_FIELDS), attempt.snapshot["log"])
            and _equal(
                _attachment(attachment, attempt.snapshot["version"]),
                attempt.snapshot["source"],
            )
        ):
            await db.rollback()
            return "not_committed"
        if (
            attempt.phase == "prepared"
            and _source_matches(attempt, log, attachment)
            and ledger["state"] == "prepared"
            and exact_receipt
        ):
            await db.execute(
                text(
                    f"UPDATE {LOG} SET status='failed',error_message=:reason WHERE id=:id"
                ),
                {"id": attempt.log_id, "reason": FAILED},
            )
            await _write_receipt(db, attempt, "failed")
            await db.commit()
            return "not_committed"
        await db.rollback()
    except Exception:
        await db.rollback()
    return "unknown"

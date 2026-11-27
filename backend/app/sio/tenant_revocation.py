"""在事务提交后断开旧租户套接字 / Disconnect older tenant sockets after commit."""

from __future__ import annotations

import asyncio

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.logging import LogManager
from app.sio.socket_trace import disconnect_tenant_socket_sessions

_PENDING_KEY = "tenant_socket_revocations"
_tasks: set[asyncio.Task[None]] = set()
logger = LogManager.get_logger(__name__)


async def _disconnect_after_commit(tenant_id: int, epoch: int) -> None:
    """有界重试实时断开 / Retry realtime disconnect with bounded delays."""
    for delay in (0, 1, 3):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await disconnect_tenant_socket_sessions(tenant_id, epoch):
                return
        except Exception as exc:
            logger.warning(
                "Tenant socket disconnect unavailable: tenant={} epoch={} error={}",
                tenant_id,
                epoch,
                type(exc).__name__,
            )
    logger.warning(
        "Tenant socket disconnect incomplete: tenant={} epoch={}", tenant_id, epoch
    )


def _dispatch_committed(session: Session) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info[_PENDING_KEY]
    epochs = pending.pop(transaction, {})
    if transaction is not None and transaction.parent is not None:
        parent_epochs = pending.setdefault(transaction.parent, {})
        for tenant_id, epoch in epochs.items():
            parent_epochs[tenant_id] = max(parent_epochs.get(tenant_id, 0), epoch)
        return

    for tenant_id, epoch in epochs.items():
        task = asyncio.create_task(_disconnect_after_commit(tenant_id, epoch))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


def _clear_finished_transaction(
    session: Session, transaction: SessionTransaction
) -> None:
    session.info[_PENDING_KEY].pop(transaction, None)


def schedule_tenant_socket_revocation(
    db: AsyncSession | Session, tenant_id: int, epoch: int
) -> None:
    """提交前只记录意图，回滚时丢弃 / Record intent until commit and discard it on rollback."""
    session = db.sync_session if isinstance(db, AsyncSession) else db
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("Tenant session revocation requires an open transaction")
    if _PENDING_KEY not in session.info:
        session.info[_PENDING_KEY] = {}
        event.listen(session, "after_commit", _dispatch_committed)
        event.listen(session, "after_transaction_end", _clear_finished_transaction)
    pending = session.info[_PENDING_KEY].setdefault(transaction, {})
    pending[tenant_id] = max(pending.get(tenant_id, 0), epoch)

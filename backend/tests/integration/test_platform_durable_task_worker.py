"""中文: 在临时 PostgreSQL 和 Redis 上验证调度与投递。

EN: Exercise the actual Beat, worker, and delivery catalog on local fixtures.

Test type: behavioral
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from datetime import timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.common.notification_delivery import NotificationDelivery
from app.models.system.task_definition import TaskDefinition
from app.models.system.task_run import TaskRun
from app.models.system.tenant_task_binding import TenantTaskBinding
from app.models.tenant.tenant_plan import TenantPlan
from tests.integration.notification_postgres_support import (
    POSTGRES_DSN,
    notification_database,
    plan_notification,
    seed_notification_fixture,
)

REDIS_URL = os.getenv("PLATFORM_FIXTURE_REDIS_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (POSTGRES_DSN and REDIS_URL),
        reason="Disposable platform_tasks PostgreSQL and Redis fixtures required",
    ),
]


def _upgrade_owned_catalogs(connection) -> None:
    root = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    for name in ("platform_notifications_v1", "platform_task_lease_v1"):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()


async def test_beat_recovers_notification_and_marks_expired_task_unknown(
    monkeypatch,
    tmp_path,
) -> None:
    from app.celery_app import celery_app
    from app.configs.service import ConfigService
    from app.repositories.system import task_run_lease_repository as lease
    from app.services.common import (
        notification_delivery_dispatch,
        notification_task_runtime,
    )
    from app.tasks import base, scheduler, task_scheduling
    from app.tasks.leased_tenant_task import LeasedTenantTask

    for connection_url in (REDIS_URL, celery_app.conf.broker_url):
        target = urlsplit(connection_url)
        assert target.scheme == "redis" and target.hostname == "127.0.0.1"
        assert target.port is not None and target.path == "/3"

    handler = "fixture.platform_durable_task_worker.monitor_only"

    class MonitorOnly(LeasedTenantTask):
        LEASE_HANDLER_PATH = handler

    @base.register_task(name=handler, base=MonitorOnly)
    def fixture_handler(_self, tenant_id):
        raise AssertionError("The task monitor must not dispatch a lost task")

    async def config(_self, key, default=None):
        return {
            "notification_enabled": True,
            "notification_max_per_user": 0,
        }.get(key, default)

    monkeypatch.setattr(ConfigService, "get_platform_config", config)
    try:
        async with notification_database(
            additional_models=(TaskDefinition, TaskRun, TenantPlan, TenantTaskBinding),
        ) as fixture:
            with fixture.sync_sessions() as db, db.begin():
                for table, columns in {
                    "notifications": ("dedupe_key",),
                    "notification_deliveries": (
                        "dedupe_key",
                        "payload_ciphertext",
                        "available_at",
                        "lease_token",
                        "lease_expires_at",
                        "submission_started_at",
                    ),
                    "task_runs": ("lease_token", "lease_expires_at", "monitor_lost_at"),
                }.items():
                    for column in columns:
                        db.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                _upgrade_owned_catalogs(db.connection())

            for module in (base, lease, scheduler, task_scheduling):
                monkeypatch.setattr(
                    module, "sync_session_factory", fixture.sync_sessions
                )

            def worker_engine(url, **kwargs):
                return create_async_engine(
                    url,
                    connect_args={"server_settings": {"search_path": fixture.schema}},
                    **kwargs,
                )

            monkeypatch.setattr(
                notification_task_runtime, "create_async_engine", worker_engine
            )
            await seed_notification_fixture(fixture, channels=["inbox", "ws"])

            with monkeypatch.context() as hint:
                hint.setattr(
                    notification_delivery_dispatch,
                    "publish_notification_delivery",
                    lambda _delivery_id: None,
                )
                delivery_ids = await plan_notification(fixture)
            assert len(delivery_ids) == 2

            claimed = lease.TaskRunLeaseRepository.register_active_run(
                celery_task_id="fixture-lost-run",
                effective_tenant_id=77,
                owner_tenant_id=None,
                task_definition_id=None,
                binding_id=None,
                task_code_snapshot="fixture.lost",
                task_name_snapshot="Monitor fixture",
                handler_path_snapshot=handler,
                trigger_source="scheduler",
                run_kind="tenant_binding",
                queue="scheduled",
                priority=None,
                run_key="fixture-lost-run",
                trigger_slot=None,
                trigger_id=None,
                retry_of_run_id=None,
                retry_of_task_id=None,
                args_summary=None,
                trace_id="fixture-lease-trace",
            )
            with fixture.sync_sessions() as db, db.begin():
                db.execute(
                    update(TaskRun)
                    .where(TaskRun.id == claimed.run_id)
                    .values(
                        lease_expires_at=func.clock_timestamp()
                        - text("interval '1 second'")
                    )
                )

            catalog = scheduler.ReloadingPersistentScheduler(
                app=celery_app,
                schedule_filename=str(tmp_path / "durable-beat"),
            )
            try:
                names = catalog._db_entry_names
                assert {name.rsplit(":", 1)[-1] for name in names} == {
                    "notification.deliveries.recovery",
                    "system.task_run_monitor",
                }
                for name in names:
                    entry = catalog.schedule[name]
                    assert entry.task == "app.tasks.task_scheduling.run_task_definition"
                    entry.last_run_at = celery_app.now() - timedelta(minutes=2)

                with Redis.from_url(REDIS_URL) as redis, redis.pubsub() as events:
                    events.subscribe("socketio")
                    assert events.get_message(timeout=2)["type"] == "subscribe"
                    with start_worker(
                        celery_app,
                        pool="solo",
                        queues=("scheduled", "notification"),
                        perform_ping_check=False,
                        loglevel="WARNING",
                    ):
                        catalog.tick()
                        catalog.tick()
                        deadline = monotonic() + 20
                        observed = None
                        event = None
                        while monotonic() < deadline:
                            if event is None:
                                message = events.get_message(
                                    ignore_subscribe_messages=True
                                )
                                if message:
                                    event = json.loads(message["data"])
                            async with fixture.sessions() as db:
                                deliveries = (
                                    await db.scalars(
                                        select(NotificationDelivery).order_by(
                                            NotificationDelivery.id
                                        )
                                    )
                                ).all()
                                lost = await db.get(TaskRun, claimed.run_id)
                                observed = [
                                    (row.channel, row.status, row.attempt)
                                    for row in deliveries
                                ]
                                if (
                                    event
                                    and observed
                                    == [("inbox", "sent", 1), ("ws", "sent", 1)]
                                    and lost.status == "unknown"
                                ):
                                    break
                            await asyncio.sleep(0.1)
                        assert observed == [("inbox", "sent", 1), ("ws", "sent", 1)]
                        assert (
                            lost.status,
                            lost.finished_at,
                            lost.monitor_lost_at is not None,
                        ) == (
                            "unknown",
                            None,
                            True,
                        )
                        assert (event["event"], event["namespace"], event["room"]) == (
                            "notification",
                            "/admin",
                            "user:42",
                        )
            finally:
                catalog.close()
    finally:
        base.get_task_registry().pop(handler, None)
        celery_app.tasks.pop(handler, None)

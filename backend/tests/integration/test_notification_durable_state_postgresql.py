"""中文: 持久通知的崩溃与隔离回归。 EN: Durable notification crash and isolation regressions.

Test type: behavioral, with one fake-network smoke. Real PostgreSQL transactions;
configuration and external SMTP/broker/adapters are fictional only.
"""

import asyncio
import json
import smtplib

import pytest
from sqlalchemy import func, select, text, update

from app.core.i18n import get_locale, set_locale
from app.core.security import decrypt_data
from app.models.common.notification import Notification
from app.models.common.notification_delivery import NotificationDelivery
from app.models.common.notification_preference import NotificationPreference
from app.models.common.notification_template import NotificationTemplate
from app.models.system.admin import Admin
from app.models.tenant.tenant import Tenant
from app.repositories.common.notification_delivery_repository import (
    NotificationDeliveryClaimRepository,
)
from app.services.common.notification_delivery_service import (
    NotificationDeliveryService,
)
from tests.integration.notification_postgres_support import (
    POSTGRES_DSN,
    deny_unfaked_notification_network,
    fake_notification_configuration,
    notification_database,
    plan_notification,
    seed_notification_fixture,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not POSTGRES_DSN, reason="Disposable loopback PostgreSQL required"
    ),
]


@pytest.fixture(autouse=True)
def forbid_unfaked_network(monkeypatch):
    blocked = deny_unfaked_notification_network(monkeypatch)
    yield
    assert blocked == []


async def delivery_row(fixture, delivery_id):
    async with fixture.sessions() as db:
        return await db.get(NotificationDelivery, delivery_id)


async def expire_claim(fixture, delivery_id):
    async with fixture.sessions() as db, db.begin():
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .values(
                lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'")
            )
        )


def fake_smtp(monkeypatch, submit):
    """中文: 仅替换网络边界，保留真实 SMTP helper 与数据库围栏。 EN: Fake network only; retain real SMTP helper and database fence."""

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def sendmail(self, sender, recipients, message):
            return submit(sender, recipients, message)

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", Server)


async def test_commit_hint_contains_only_id_and_scan_recovers_lost_hint(monkeypatch):
    _values, published = fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        ids = await plan_notification(fixture)
        assert published == ids and all(type(value) is int for value in published)
        row = await delivery_row(fixture, ids[0])
        assert row.status == "pending" and row.attempt == 0
        assert "fictional-token" not in row.payload_ciphertext
        assert "fixture@example.test" not in row.payload_ciphertext
        assert json.loads(decrypt_data(row.payload_ciphertext))["data"] == {
            "token": "fictional-token"
        }
        published.clear()
        assert (
            await NotificationDeliveryClaimRepository(fixture.sessions).due_ids() == ids
        )


async def test_savepoint_and_outer_rollback_drop_only_their_hints(monkeypatch):
    from app.services.common.notification_delivery_dispatch import (
        schedule_notification_delivery_after_commit,
    )

    _values, published = fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture, fixture.sessions() as db:
        await db.begin()
        schedule_notification_delivery_after_commit(db, 1)
        savepoint = await db.begin_nested()
        schedule_notification_delivery_after_commit(db, 2)
        await savepoint.rollback()
        assert published == []
        await db.commit()
        assert published == [1]
        await db.begin()
        savepoint = await db.begin_nested()
        schedule_notification_delivery_after_commit(db, 3)
        await savepoint.commit()
        assert published == [1]
        await db.rollback()
        assert published == [1]
        await db.begin()
        schedule_notification_delivery_after_commit(db, 4)
        await db.commit()
        assert published == [1, 4]


async def test_parallel_claims_and_expiry_fence_the_old_owner(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        claims = NotificationDeliveryClaimRepository(fixture.sessions)
        competing = await asyncio.gather(
            claims.claim(delivery_id), claims.claim(delivery_id)
        )
        owners = [claim for claim in competing if claim is not None]
        assert len(owners) == 1 and owners[0].attempt == 1
        old = owners[0]
        await expire_claim(fixture, delivery_id)
        new = await claims.claim(delivery_id)
        assert new.token != old.token and new.attempt == 2
        with pytest.raises(ValueError, match="notification_lease_lost"):
            await claims.mark_submitting(old, email="fixture@example.test")
        assert await claims.finish(old, "sent") is False
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.lease_token) == ("claimed", new.token)


async def test_expired_submission_is_uncertain_and_exact_late_acceptance_can_reconcile(
    monkeypatch,
):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        claims = NotificationDeliveryClaimRepository(fixture.sessions)
        claim = await claims.claim(delivery_id)
        await claims.mark_submitting(claim, email="fixture@example.test")
        await expire_claim(fixture, delivery_id)
        assert await claims.claim(delivery_id) is None
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.lease_token) == (
            "uncertain",
            1,
            claim.token,
        )
        assert row.submission_started_at is not None
        assert await claims.due_ids() == []
        assert await claims.finish(claim, "failed", retryable=True) is False
        assert await claims.finish(claim, "sent") is True
        assert (await delivery_row(fixture, delivery_id)).status == "sent"


async def test_smtp_observes_committed_fence_and_no_row_lock_during_send(monkeypatch):
    """[smoke] 中文: 虚构 SMTP 的完整本地业务路径。 EN: Complete local business path with fictional SMTP."""
    _values, published = fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=["inbox", "email"])
        ids = await plan_notification(fixture)
        async with fixture.sessions() as db:
            delivery_id = await db.scalar(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.channel == "email"
                )
            )
            assert await db.scalar(select(func.count()).select_from(Notification)) == 1
        submissions = []

        def submit(sender, recipients, message):
            with fixture.sync_sessions() as check:
                row = check.scalar(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update(nowait=True)
                )
                assert (row.status, row.attempt) == ("submitting", 1)
                assert row.submission_started_at is not None
                submissions.append((sender, recipients, "fictional-token" in message))
            return {}

        fake_smtp(monkeypatch, submit)
        worker = NotificationDeliveryService(fixture.sessions)
        assert await worker.dispatch(delivery_id) == "sent"
        assert await worker.dispatch(delivery_id) == "not_claimed"
        assert await worker.recover() == {"processed": 0, "outcomes": {}}
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.last_error) == ("sent", 1, None)
        assert submissions == [
            ("fixture-sender@example.test", ["fixture@example.test"], False)
        ]
        assert published == [delivery_id] and len(ids) == 2


async def test_submission_commit_failure_prevents_sendmail(monkeypatch):
    from sqlalchemy import event

    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        submissions = []
        fake_smtp(monkeypatch, lambda *args: submissions.append(args))
        worker = NotificationDeliveryService(fixture.sessions)
        original = worker.claims.mark_submitting

        async def commit_fails(claim, **kwargs):
            # 中文: 仅目标提交事件失败，真实领取及状态修改仍执行。
            # EN: Fail the actual target commit event; retain the real claim and mutation.
            sync_class = fixture.sessions.class_.sync_session_class

            def before_commit(session):
                if any(
                    isinstance(row, NotificationDelivery) and row.status == "submitting"
                    for row in session.dirty
                ):
                    raise RuntimeError("fictional commit unavailable")

            event.listen(sync_class, "before_commit", before_commit)
            try:
                return await original(claim, **kwargs)
            finally:
                event.remove(sync_class, "before_commit", before_commit)

        monkeypatch.setattr(worker.claims, "mark_submitting", commit_fails)
        assert await worker.dispatch(delivery_id) == "failed"
        assert submissions == []
        row = await delivery_row(fixture, delivery_id)
        assert row.status == "failed" and row.submission_started_at is None


@pytest.mark.parametrize("failure", ["disconnect", "finish_database"])
async def test_ambiguous_acceptance_never_resends(monkeypatch, failure):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        submissions = []

        def submit(*args):
            submissions.append(args)
            if failure == "disconnect":
                raise smtplib.SMTPServerDisconnected("fictional lost DATA response")
            return {}

        fake_smtp(monkeypatch, submit)
        worker = NotificationDeliveryService(fixture.sessions)
        original = worker.claims.finish

        async def fail_first_finish(claim, status, *args, **kwargs):
            if status == "sent":
                raise RuntimeError("fictional finalize unavailable")
            return await original(claim, status, *args, **kwargs)

        if failure == "finish_database":
            monkeypatch.setattr(worker.claims, "finish", fail_first_finish)
        assert await worker.dispatch(delivery_id) == "uncertain"
        assert await worker.dispatch(delivery_id) == "not_claimed"
        assert await worker.recover() == {"processed": 0, "outcomes": {}}
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.last_error) == (
            "uncertain",
            1,
            "external_acceptance_unknown"
            if failure == "finish_database"
            else "smtp_acceptance_unknown",
        )
        assert len(submissions) == 1


async def test_definite_temporary_rejection_retries_only_when_due_and_max_three(
    monkeypatch,
):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        submissions = []

        def rejected(*args):
            submissions.append(args)
            raise smtplib.SMTPDataError(451, b"fictional temporary rejection")

        fake_smtp(monkeypatch, rejected)
        worker = NotificationDeliveryService(fixture.sessions)
        for attempt in range(1, 4):
            assert await worker.dispatch(delivery_id) == (
                "pending" if attempt < 3 else "failed"
            )
            row = await delivery_row(fixture, delivery_id)
            assert row.attempt == attempt
            assert await worker.claims.due_ids() == []
            if attempt < 3:
                assert row.lease_token is None and row.submission_started_at is None
                async with fixture.sessions() as db, db.begin():
                    await db.execute(
                        update(NotificationDelivery)
                        .where(NotificationDelivery.id == delivery_id)
                        .values(available_at=func.clock_timestamp())
                    )
        assert len(submissions) == 3


@pytest.mark.parametrize(
    "change",
    [
        "mailbox",
        "recipient_deleted",
        "tenant_disabled",
        "preference",
        "template_disabled",
        "template_channel",
        "foreign_tenant",
    ],
)
async def test_current_boundaries_can_only_narrow_delivery(monkeypatch, change):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        tenant_id = 77 if change in {"tenant_disabled", "foreign_tenant"} else None
        kind = "tenant_admin" if tenant_id else "admin"
        [delivery_id] = await plan_notification(
            fixture, tenant_id=tenant_id, recipient_type=kind
        )
        async with fixture.sessions() as db, db.begin():
            if change == "mailbox":
                await db.execute(
                    update(Admin)
                    .where(Admin.id == 42)
                    .values(email="new-fixture@example.test")
                )
            elif change == "recipient_deleted":
                await db.execute(
                    update(Admin).where(Admin.id == 42).values(is_deleted=True)
                )
            elif change == "tenant_disabled":
                await db.execute(
                    update(Tenant).where(Tenant.id == 77).values(is_active=False)
                )
            elif change == "preference":
                await db.execute(
                    update(NotificationPreference).values(channel_email=False)
                )
            elif change == "template_disabled":
                await db.execute(update(NotificationTemplate).values(is_enabled=False))
            elif change == "template_channel":
                await db.execute(
                    update(NotificationTemplate)
                    .where(NotificationTemplate.code == "fixture.notice")
                    .values(channels=["inbox"])
                )
            else:
                await db.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .values(tenant_id=88)
                )
        submissions = []
        fake_smtp(monkeypatch, lambda *args: submissions.append(args))
        status = await NotificationDeliveryService(fixture.sessions).dispatch(
            delivery_id
        )
        assert status == ("failed" if change == "foreign_tenant" else "skipped")
        assert submissions == []
        row = await delivery_row(fixture, delivery_id)
        assert row.submission_started_at is None
        if change == "template_channel":
            assert row.last_error == "notification_channel_disabled"


async def test_frozen_content_locale_and_force_survive_worker_context_change(
    monkeypatch,
):
    _values, _published = fake_notification_configuration(monkeypatch)
    initial = get_locale()
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        set_locale("en")
        try:
            [delivery_id] = await plan_notification(fixture, force=True)
            row = await delivery_row(fixture, delivery_id)
            payload = json.loads(decrypt_data(row.payload_ciphertext))
            assert payload["force"] is True and payload["locale"] == "en"
            async with fixture.sessions() as db, db.begin():
                await db.execute(
                    update(NotificationTemplate).values(
                        title_template="CHANGED", body_template="CHANGED"
                    )
                )
                await db.execute(
                    update(NotificationPreference).values(channel_email=False)
                )
            set_locale("zh_CN")
            observed = []
            fake_smtp(monkeypatch, lambda *_args: observed.append(get_locale()) or {})
            assert (
                await NotificationDeliveryService(fixture.sessions).dispatch(
                    delivery_id
                )
                == "sent"
            )
            assert observed == ["en"] and get_locale() == "zh_CN"
            assert (
                json.loads(
                    decrypt_data(
                        (await delivery_row(fixture, delivery_id)).payload_ciphertext
                    )
                )
                == payload
            )
        finally:
            set_locale(initial)


@pytest.mark.parametrize("terminal", ["sent", "uncertain", "failed", "skipped"])
async def test_dedupe_preserves_terminal_payload_and_attempt(monkeypatch, terminal):
    _values, published = fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        row = await delivery_row(fixture, delivery_id)
        ciphertext = row.payload_ciphertext
        async with fixture.sessions() as db, db.begin():
            await db.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .values(status=terminal, attempt=3, last_error="fixture_evidence")
            )
        assert await plan_notification(fixture) == [delivery_id]
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.last_error, row.payload_ciphertext) == (
            terminal,
            3,
            "fixture_evidence",
            ciphertext,
        )
        assert published == [delivery_id]


async def test_tenant_mismatch_and_unknown_channel_fail_closed(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=["email", "custom_unsafe"])
        ids = await plan_notification(
            fixture,
            tenant_id=77,
            recipient_type="tenant_admin",
            recipient_id=84,
            force=True,
        )
        assert len(ids) == 2
        for delivery_id in ids:
            row = await delivery_row(fixture, delivery_id)
            assert row.status == "skipped" and row.payload_ciphertext is None
        ids = await plan_notification(fixture, force=True, dedupe_key="valid-unknown")
        rows = [await delivery_row(fixture, delivery_id) for delivery_id in ids]
        assert [(row.channel, row.status) for row in rows] == [
            ("email", "pending"),
            ("custom_unsafe", "skipped"),
        ]
        assert rows[1].last_error == "channel_unsupported"


@pytest.mark.parametrize("channel", ["ws", "webhook"])
async def test_external_adapters_are_deferred_and_failure_is_uncertain(
    monkeypatch, channel
):
    from app.services.common.channels import get_channel

    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=[channel])
        calls = []

        async def deliver(self, **kwargs):
            calls.append(kwargs)
            row = await delivery_row(fixture, delivery_id)
            assert row.status == "submitting" and kwargs["db"] is None
            return False

        monkeypatch.setattr(type(get_channel(channel)), "deliver", deliver)
        [delivery_id] = await plan_notification(fixture, force=True)
        assert calls == []
        worker = NotificationDeliveryService(fixture.sessions)
        assert await worker.dispatch(delivery_id) == "uncertain"
        assert await worker.dispatch(delivery_id) == "not_claimed"
        assert len(calls) == 1 and calls[0]["_worker_write_only"] is True
        assert (
            calls[0]["template_code"] == "fixture.notice" and calls[0]["user_id"] == 42
        )
        if channel == "webhook":
            assert (
                calls[0]["_prepared_webhook_url"]
                == "https://hooks.example.test/fixture"
            )


@pytest.fixture
def capture_websocket_pushes(monkeypatch):
    from app.services.common.channels import ws_channel

    fake_notification_configuration(monkeypatch)
    pushes = []

    async def emit(payload, user_id, namespace):
        pushes.append((payload, user_id, namespace))

    monkeypatch.setattr(ws_channel, "_emit_worker_notification", emit)
    return pushes


@pytest.mark.parametrize("channels", [["ws", "inbox"], ["ws"]])
async def test_websocket_push_uses_its_own_persisted_inbox_id(
    capture_websocket_pushes, channels
):
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=channels)
        for recipient, tenant in (("admin", None), ("tenant_admin", 77)):
            options = {
                "recipient_type": recipient,
                "tenant_id": tenant,
                "dedupe_key": "shared-event",
                "link": "/fixture/detail",
            }
            await plan_notification(fixture, **options)
            await plan_notification(fixture, **options)

        async with fixture.sessions() as db:
            inbox_ids = {
                row.recipient_type: row.id
                for row in (await db.scalars(select(Notification))).all()
            }
            ws_deliveries = (
                await db.scalars(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.channel == "ws")
                    .order_by(NotificationDelivery.id)
                )
            ).all()

        assert len(ws_deliveries) == 2
        assert len(inbox_ids) == (2 if "inbox" in channels else 0)
        assert {row.recipient_type: row.notification_id for row in ws_deliveries} == {
            recipient: inbox_ids.get(recipient)
            for recipient in ("admin", "tenant_admin")
        }
        assert capture_websocket_pushes == []
        worker = NotificationDeliveryService(fixture.sessions)
        assert await worker.recover() == {"processed": 2, "outcomes": {"sent": 2}}
        assert await worker.recover() == {"processed": 0, "outcomes": {}}
        assert [
            (payload.get("id"), recipient_id, namespace, payload["link"])
            for payload, recipient_id, namespace in capture_websocket_pushes
        ] == [
            (inbox_ids.get("admin"), 42, "/admin", "/fixture/detail"),
            (inbox_ids.get("tenant_admin"), 42, "/tenant", "/fixture/detail"),
        ]


async def test_websocket_push_skips_a_deleted_linked_inbox_notification(
    capture_websocket_pushes,
):
    from app.services.common.notification_service import NotificationService

    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=["ws", "inbox"])
        await plan_notification(fixture)
        async with fixture.sessions() as db, db.begin():
            inbox_id = await db.scalar(select(Notification.id))
            ws_id = await db.scalar(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.channel == "ws"
                )
            )
            assert await NotificationService(db).delete_notification(
                inbox_id, "admin", 42
            )

        assert await NotificationDeliveryService(fixture.sessions).dispatch(ws_id) == (
            "skipped"
        )
        row = await delivery_row(fixture, ws_id)
        assert (row.last_error, row.submission_started_at) == (
            "notification_inbox_unavailable",
            None,
        )
        assert capture_websocket_pushes == []


@pytest.mark.parametrize(
    ("channel", "updated_channels"),
    [("ws", ["inbox"]), ("webhook", [])],
)
async def test_removed_template_channel_stops_forced_delivery(
    monkeypatch, channel, updated_channels
):
    from app.services.common.channels import get_channel

    fake_notification_configuration(monkeypatch)
    submissions = []

    async def deliver(self, **kwargs):
        submissions.append(kwargs["template_code"])
        return True

    monkeypatch.setattr(type(get_channel(channel)), "deliver", deliver)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture, channels=[channel])
        [delivery_id] = await plan_notification(fixture, force=True)
        async with fixture.sessions() as db, db.begin():
            await db.execute(
                update(NotificationTemplate)
                .where(NotificationTemplate.code == "fixture.notice")
                .values(channels=updated_channels)
            )

        worker = NotificationDeliveryService(fixture.sessions)
        assert await worker.recover() == {"processed": 1, "outcomes": {"skipped": 1}}
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.last_error, row.submission_started_at) == (
            "skipped",
            "notification_channel_disabled",
            None,
        )
        assert submissions == []
        assert await worker.claims.due_ids() == []


async def test_legacy_payloadless_rows_are_quarantined_without_replay(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        async with fixture.sessions() as db, db.begin():
            row = NotificationDelivery(
                template_code="fixture.notice",
                channel="email",
                recipient_type="admin",
                recipient_id=42,
                status="queued",
            )
            db.add(row)
            await db.flush()
            delivery_id = row.id
        claims = NotificationDeliveryClaimRepository(fixture.sessions)
        assert await claims.due_ids() == []
        await claims.quarantine_legacy(delivery_id)
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.last_error, row.attempt) == (
            "uncertain",
            "legacy_submission_unknown",
            0,
        )
        assert await claims.due_ids() == []


async def test_celery_entrypoint_repeated_invocations_own_engines_and_event_loops(
    monkeypatch,
):
    from types import SimpleNamespace

    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services.common import notification_task_runtime as runtime
    from app.tasks.notification import dispatch_notification_delivery

    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        created = []
        disposed = []
        loops = []

        def engine_factory(url, **kwargs):
            engine = create_async_engine(url, **kwargs)
            created.append(engine)
            loops.append(asyncio.get_running_loop())

            @event.listens_for(engine.sync_engine, "connect")
            def search_path(connection, _record):
                previous = connection.autocommit
                connection.autocommit = True
                cursor = connection.cursor()
                cursor.execute('SET search_path TO "' + fixture.schema + '", public')
                cursor.close()
                connection.autocommit = previous

            event.listen(
                engine.sync_engine,
                "engine_disposed",
                lambda engine: disposed.append(engine),
            )
            return engine

        monkeypatch.setattr(
            runtime, "settings", SimpleNamespace(DATABASE_URL=fixture.dsn)
        )
        monkeypatch.setattr(runtime, "create_async_engine", engine_factory)
        submitted = []
        fake_smtp(monkeypatch, lambda *args: submitted.append(args) or {})
        for status in ("sent", "not_claimed"):
            assert await asyncio.to_thread(
                dispatch_notification_delivery.run, delivery_id
            ) == {"delivery_id": delivery_id, "status": status}
        assert len(submitted) == 1
        assert len(created) == len(disposed) == 2 and created[0] is not created[1]
        assert all(isinstance(engine.sync_engine.pool, NullPool) for engine in created)
        assert loops[0] is not loops[1] and all(loop.is_closed() for loop in loops)


async def test_dedupe_insert_race_keeps_both_unrelated_outer_writes(monkeypatch):
    from app.services.common.notification_service import NotificationService

    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        original = NotificationService._find_delivery_by_dedupe
        ready = asyncio.Event()
        arrivals = 0

        async def coordinate_missing(self, **kwargs):
            nonlocal arrivals
            result = await original(self, **kwargs)
            if result is None and arrivals < 2:
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            return result

        monkeypatch.setattr(
            NotificationService, "_find_delivery_by_dedupe", coordinate_missing
        )

        async def writer(label):
            async with fixture.sessions() as db, db.begin():
                template = await db.scalar(
                    select(NotificationTemplate).where(
                        NotificationTemplate.code == "fixture.notice"
                    )
                )
                db.add(
                    Notification(
                        recipient_type="admin",
                        recipient_id=42,
                        template_code="fixture.notice",
                        category="system",
                        title=label,
                    )
                )
                return (
                    await NotificationService(db)._record_delivery_status(
                        template=template,
                        channel_code="email",
                        user_type="admin",
                        user_id=42,
                        tenant_id=None,
                        status="skipped",
                        dedupe_key="raced-intent",
                    )
                ).id

        winners = await asyncio.gather(writer("outer-one"), writer("outer-two"))
        assert winners[0] == winners[1]
        async with fixture.sessions() as db:
            assert (
                await db.scalar(select(func.count()).select_from(NotificationDelivery))
                == 1
            )
            assert list(
                (
                    await db.scalars(
                        select(Notification.title).order_by(Notification.title)
                    )
                ).all()
            ) == ["outer-one", "outer-two"]


async def test_lost_publish_hint_leaves_committed_intent_recoverable(monkeypatch):
    from app.services.common import notification_delivery_dispatch as dispatch

    fake_notification_configuration(monkeypatch)

    class StoppedPublisher:
        def submit(self, *_args):
            raise RuntimeError("fictional process stopped after commit")

    monkeypatch.setattr(dispatch, "_publisher", StoppedPublisher())
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        assert (await delivery_row(fixture, delivery_id)).status == "pending"
        assert await NotificationDeliveryClaimRepository(
            fixture.sessions
        ).due_ids() == [delivery_id]


async def test_expired_pre_submit_claims_exhaust_after_three_attempts(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        claims = NotificationDeliveryClaimRepository(fixture.sessions)
        for attempt in range(1, 4):
            claim = await claims.claim(delivery_id)
            assert claim.attempt == attempt
            await expire_claim(fixture, delivery_id)
        assert await claims.claim(delivery_id) is None
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.last_error) == (
            "failed",
            3,
            "notification_attempts_exhausted",
        )
        assert row.submission_started_at is None and await claims.due_ids() == []
        with pytest.raises(ValueError, match="notification_lease_lost"):
            await claims.mark_submitting(claim, email="fixture@example.test")


async def test_direct_email_adapter_does_not_reopen_terminal_delivery(monkeypatch):
    from app.services.common.channels.email_channel import EmailChannel

    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        async with fixture.sessions() as db, db.begin():
            row = NotificationDelivery(
                template_code="fixture.notice",
                channel="email",
                recipient_type="admin",
                recipient_id=42,
                status="failed",
                last_error="fixture_terminal",
                attempt=3,
            )
            db.add(row)
            await db.flush()
            delivery_id = row.id
            assert (
                await EmailChannel().deliver(
                    db=db,
                    user_type="admin",
                    user_id=42,
                    title="Fictional notice",
                    body=None,
                    data=None,
                    link=None,
                    priority="normal",
                    template_code="fixture.notice",
                    delivery_record=row,
                )
                is False
            )
        row = await delivery_row(fixture, delivery_id)
        assert (row.status, row.attempt, row.last_error, row.payload_ciphertext) == (
            "failed",
            3,
            "fixture_terminal",
            None,
        )

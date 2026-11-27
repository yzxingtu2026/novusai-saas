"""Test type: behavioral. Notification authorization and finalization races.

Real isolated PostgreSQL transactions and claims; SMTP/configuration are fictional.
"""

import asyncio
import smtplib
from threading import Event, Thread, Timer

import pytest
from sqlalchemy import func, select, text, update

from app.models.common.notification_delivery import NotificationDelivery
from app.models.common.notification_preference import NotificationPreference
from app.models.common.notification_template import NotificationTemplate
from app.models.tenant.tenant import Tenant
from app.models.tenant.tenant_admin import TenantAdmin
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


def fake_smtp(monkeypatch, *, connect=lambda: None, submit=lambda: {}):
    """中文: 仅替换 SMTP 网络边界。 EN: Replace the SMTP network boundary only."""

    class Server:
        def __init__(self, *_args, **_kwargs):
            connect()

        def sendmail(self, *_args):
            return submit()

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", Server)


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ("preference", "notification_preference_disabled"),
        ("tenant", "notification_recipient_scope_mismatch"),
        ("recipient", "notification_recipient_changed"),
        ("template", "notification_template_unavailable"),
        ("master", "notification_disabled"),
        ("email", "notification_channel_disabled"),
        ("tenant_email", "tenant_email_notification_disabled"),
        ("destination", "notification_destination_changed"),
        ("lease", "notification_lease_lost"),
    ],
)
async def test_smtp_handshake_rechecks_current_authority(
    monkeypatch, change, expected_error
):
    values, _ = fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(
            fixture, tenant_id=77, recipient_type="tenant_admin"
        )
        submissions = []

        def connect():
            with fixture.sync_sessions() as db, db.begin():
                if change == "preference":
                    db.execute(
                        update(NotificationPreference)
                        .where(
                            NotificationPreference.user_type == "tenant_admin",
                            NotificationPreference.user_id == 42,
                            NotificationPreference.tenant_id == 77,
                        )
                        .values(channel_email=False)
                    )
                elif change == "tenant":
                    db.execute(
                        update(Tenant).where(Tenant.id == 77).values(is_active=False)
                    )
                elif change == "recipient":
                    db.execute(
                        update(TenantAdmin)
                        .where(TenantAdmin.id == 42, TenantAdmin.tenant_id == 77)
                        .values(email="changed@example.test")
                    )
                elif change == "template":
                    db.execute(
                        update(NotificationTemplate)
                        .where(NotificationTemplate.code == "fixture.notice")
                        .values(is_enabled=False)
                    )
                elif change == "lease":
                    db.execute(
                        update(NotificationDelivery)
                        .where(NotificationDelivery.id == delivery_id)
                        .values(
                            lease_expires_at=func.clock_timestamp()
                            - text("INTERVAL '1 second'")
                        )
                    )
                else:
                    key, value = {
                        "master": ("notification_enabled", False),
                        "email": ("email_enabled", False),
                        "tenant_email": ("tenant_email_notification", False),
                        "destination": ("email_smtp_host", "changed.example.test"),
                    }[change]
                    values[key] = value

        fake_smtp(
            monkeypatch, connect=connect, submit=lambda: submissions.append("DATA")
        )
        assert (
            await NotificationDeliveryService(fixture.sessions).dispatch(delivery_id)
            == "skipped"
        )
        async with fixture.sessions() as db:
            row = await db.get(NotificationDelivery, delivery_id)
            assert (row.status, row.last_error, row.attempt) == (
                "skipped",
                expected_error,
                1,
            )
            assert row.submission_started_at is None
        assert submissions == []


@pytest.mark.parametrize("change", ["preference", "tenant"])
async def test_force_retains_only_preference_bypass_during_handshake(
    monkeypatch, change
):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(
            fixture, force=True, tenant_id=77, recipient_type="tenant_admin"
        )
        submissions = []

        def connect():
            with fixture.sync_sessions() as db, db.begin():
                if change == "tenant":
                    db.execute(
                        update(Tenant).where(Tenant.id == 77).values(is_active=False)
                    )
                else:
                    db.execute(
                        update(NotificationPreference)
                        .where(NotificationPreference.user_type == "tenant_admin")
                        .values(channel_email=False)
                    )

        fake_smtp(
            monkeypatch, connect=connect, submit=lambda: submissions.append("DATA")
        )
        expected_status = "sent" if change == "preference" else "skipped"
        expected_error = (
            None if change == "preference" else "notification_recipient_scope_mismatch"
        )
        assert (
            await NotificationDeliveryService(fixture.sessions).dispatch(delivery_id)
            == expected_status
        )
        async with fixture.sessions() as db:
            row = await db.get(NotificationDelivery, delivery_id)
            assert (row.status, row.attempt, row.last_error) == (
                expected_status,
                1,
                expected_error,
            )
        assert submissions == (["DATA"] if change == "preference" else [])


async def test_finalization_waits_for_short_row_lock_without_resending(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        locked, release = Event(), Event()
        submissions, errors = [], []

        def hold_lock():
            try:
                with fixture.sync_sessions() as db, db.begin():
                    row = db.scalar(
                        select(NotificationDelivery)
                        .where(NotificationDelivery.id == delivery_id)
                        .with_for_update(nowait=True)
                    )
                    assert row.status == "submitting"
                    locked.set()
                    assert release.wait(5)
            except BaseException as exc:
                errors.append(type(exc).__name__)
                locked.set()

        locker = Thread(target=hold_lock)

        def submit():
            submissions.append("DATA")
            locker.start()
            assert locked.wait(5)
            Timer(0.15, release.set).start()
            return {}

        fake_smtp(monkeypatch, submit=submit)
        worker = NotificationDeliveryService(fixture.sessions)
        try:
            result = await worker.dispatch(delivery_id)
            async with fixture.sessions() as db:
                row = await db.get(NotificationDelivery, delivery_id)
                observed = (row.status, row.attempt, row.last_error)
        finally:
            release.set()
            await asyncio.to_thread(locker.join, 5)
        assert errors == []
        assert (result, observed) == ("sent", ("sent", 1, None))
        assert await worker.dispatch(delivery_id) == "not_claimed"
        assert submissions == ["DATA"]


@pytest.mark.parametrize("transport", ["accepted", "temporary_rejection", "disconnect"])
async def test_finalization_timeout_reports_unrecorded_outcome_and_never_resends(
    monkeypatch,
    transport,
):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        held, submissions = [], []

        def submit():
            db = fixture.sync_sessions()
            db.begin()
            row = db.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .with_for_update(nowait=True)
            )
            assert row.status == "submitting"
            held.append(db)
            submissions.append("DATA")
            if transport == "temporary_rejection":
                raise smtplib.SMTPDataError(451, b"fictional rejection")
            if transport == "disconnect":
                raise smtplib.SMTPServerDisconnected("fictional response unavailable")
            return {}

        fake_smtp(monkeypatch, submit=submit)
        worker = NotificationDeliveryService(fixture.sessions)
        try:
            result = await asyncio.wait_for(worker.dispatch(delivery_id), timeout=8)
            async with fixture.sessions() as db:
                row = await db.get(NotificationDelivery, delivery_id)
                observed = (row.status, row.attempt, row.last_error)
        finally:
            for db in held:
                db.rollback()
                db.close()
        expected = (
            "sent_unrecorded" if transport == "accepted" else "finalization_unconfirmed"
        )
        assert (result, observed) == (expected, ("submitting", 1, None))
        assert await worker.dispatch(delivery_id) == "not_claimed"
        async with fixture.sessions() as db, db.begin():
            await db.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .values(
                    lease_expires_at=func.clock_timestamp()
                    - text("INTERVAL '1 second'")
                )
            )
        assert await worker.recover() == {
            "processed": 1,
            "outcomes": {"not_claimed": 1},
        }
        async with fixture.sessions() as db:
            row = await db.get(NotificationDelivery, delivery_id)
            assert (row.status, row.attempt, row.last_error) == (
                "uncertain",
                1,
                "submission_lease_expired",
            )
        assert submissions == ["DATA"]


async def test_finalization_does_not_claim_success_after_ownership_changes(monkeypatch):
    fake_notification_configuration(monkeypatch)
    async with notification_database() as fixture:
        await seed_notification_fixture(fixture)
        [delivery_id] = await plan_notification(fixture)
        worker = NotificationDeliveryService(fixture.sessions)
        original = await worker.claims.claim(delivery_id)
        async with fixture.sessions() as db, db.begin():
            await db.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .values(
                    lease_expires_at=func.clock_timestamp()
                    - text("INTERVAL '1 second'")
                )
            )
        replacement = await worker.claims.claim(delivery_id)
        assert replacement.token != original.token
        assert (
            await worker._finish(
                original,
                "failed",
                "notification_preparation_unavailable",
                retryable=True,
            )
            == "finalization_unconfirmed"
        )
        async with fixture.sessions() as db:
            row = await db.get(NotificationDelivery, delivery_id)
            assert (row.status, row.lease_token, row.attempt, row.last_error) == (
                "claimed",
                replacement.token,
                2,
                None,
            )

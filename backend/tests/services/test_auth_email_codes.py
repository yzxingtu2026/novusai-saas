"""租户邮箱认证与验证码行为回归 / Tenant email and code regressions.

Test type: behavioral.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import redis as cache
from app.core.base_model import Base
from app.core.i18n import _
from app.core.security import get_password_hash, verify_password
from app.exceptions import AuthenticationException, BusinessException
from app.models import Admin, Tenant, TenantAdmin, TenantDomain, TenantPlan, TenantUser
from app.services.common.auth_service import AuthService

pytestmark = pytest.mark.asyncio
CODE = "012345"
PASSWORD = "FictionalOriginal42!"
SECOND_PASSWORD = "FictionalSecond43!"
AUTH_SCOPES = [
    ("admin", Admin, 11, None),
    ("tenant_admin", TenantAdmin, 21, 22),
    ("tenant_user", TenantUser, 31, 32),
]


class AsyncTestSession:
    """向服务暴露真实 SQLite 会话 / Expose a real SQLite session to the service."""

    def __init__(self, session: Session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()

    async def flush(self):
        self.session.flush()


@pytest.fixture(scope="module")
def original_password_hash():
    return get_password_hash(PASSWORD)


@pytest.fixture(scope="module")
def second_password_hash():
    return get_password_hash(SECOND_PASSWORD)


@pytest.fixture
def accounts(original_password_hash):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    models = (Admin, Tenant, TenantAdmin, TenantUser, TenantDomain, TenantPlan)
    Base.metadata.create_all(engine, tables=[model.__table__ for model in models])
    email = f"Historical-{uuid4().hex[:20]}@fixture.invalid"
    with Session(engine) as db:
        db.add_all(
            [
                Tenant(id=1, name="Fixture One", code="fixture-one"),
                Tenant(id=2, name="Fixture Two", code="fixture-two"),
                Admin(
                    id=11,
                    username="PlatformFixture",
                    email=email,
                    password_hash=original_password_hash,
                ),
            ]
        )
        for model, first_id in ((TenantAdmin, 21), (TenantUser, 31)):
            for offset, tenant_id in enumerate((1, 2)):
                db.add(
                    model(
                        id=first_id + offset,
                        tenant_id=tenant_id,
                        username="TenantFixture",
                        email=email,
                        password_hash=original_password_hash,
                    )
                )
        db.commit()
    try:
        yield engine, email
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def local_redis(monkeypatch):
    port = os.environ.get("AUTH_CODE_TEST_REDIS_PORT")
    if not port:
        pytest.skip("An explicit disposable loopback Redis port is required")
    async with Redis(
        host="127.0.0.1",
        port=int(port),
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    ) as client:
        await client.ping()
        monkeypatch.setattr(cache.RedisManager, "_client", client)
        yield client


def auth_service(db):
    service = AuthService(AsyncTestSession(db))
    service._config_service.get_platform_config = AsyncMock(
        side_effect=lambda _key, default=None: default
    )
    service._config_service.get_tenant_config = AsyncMock(
        side_effect=lambda _tenant_id, key, default=None: {
            "tenant_login_methods": ["password", "email"],
            "tenant_captcha_enabled": False,
            "user_login_captcha_enabled": False,
        }.get(key, default)
    )
    return service


@pytest.mark.parametrize(
    ("scope", "model", "account_id", "other_account_id"),
    AUTH_SCOPES,
)
async def test_mixed_case_email_charges_only_the_selected_account(
    accounts, scope, model, account_id, other_account_id
):
    engine, email = accounts
    with Session(engine) as db:
        service = auth_service(db)
        authenticate = getattr(service, f"authenticate_{scope}")
        kwargs = {"tenant_code": "fixture-one"} if other_account_id else {}
        with pytest.raises(AuthenticationException) as denied:
            await authenticate(email.upper(), "FictionalWrong42!", **kwargs)

        assert denied.value.message == _("auth.credentials_invalid")
        assert db.get(model, account_id).login_fail_count == 1
        if other_account_id:
            assert db.get(model, other_account_id).login_fail_count == 0


@pytest.mark.parametrize(
    ("scope", "model", "account_id", "other_account_id"),
    AUTH_SCOPES,
)
async def test_password_login_prefers_exact_legacy_email_and_tracks_its_failures(
    accounts, second_password_hash, scope, model, account_id, other_account_id
):
    engine, email = accounts
    legacy_id = account_id + 10
    tenant_fields = {"tenant_id": 1} if other_account_id else {}
    login_fields = {"tenant_code": "fixture-one"} if other_account_id else {}
    with Session(engine) as db:
        db.add(
            model(
                id=legacy_id,
                username="LegacySecond",
                email=email.lower(),
                password_hash=second_password_hash,
                **tenant_fields,
            )
        )
        db.commit()

        service = auth_service(db)
        record_tokens = AsyncMock()
        service._record_active_tokens = record_tokens
        authenticate = getattr(service, f"authenticate_{scope}")

        with pytest.raises(AuthenticationException):
            await authenticate(email, SECOND_PASSWORD, **login_fields)
        assert db.get(model, account_id).login_fail_count == 1
        assert db.get(model, legacy_id).login_fail_count == 0

        first = await authenticate(email, PASSWORD, **login_fields)
        second = await authenticate(email.lower(), SECOND_PASSWORD, **login_fields)
        assert first["token_type"] == second["token_type"] == "bearer"
        assert [call.args[:2] for call in record_tokens.await_args_list] == [
            (scope, str(account_id)),
            (scope, str(legacy_id)),
        ]
        assert db.get(model, account_id).login_fail_count == 0

        with pytest.raises(AuthenticationException):
            await authenticate(email.upper(), PASSWORD, **login_fields)
        assert record_tokens.await_count == 2
        assert db.get(model, account_id).login_fail_count == 0
        assert db.get(model, legacy_id).login_fail_count == 0

        db.get(model, account_id).is_deleted = True
        db.commit()
        folded = await authenticate(email, SECOND_PASSWORD, **login_fields)
        assert folded["token_type"] == "bearer"
        assert record_tokens.await_args.args[:2] == (scope, str(legacy_id))


@pytest.mark.parametrize(
    ("scope", "model", "account_id", "other_account_id"),
    AUTH_SCOPES,
)
async def test_password_login_uses_unique_folded_email_and_exact_username(
    accounts, second_password_hash, scope, model, account_id, other_account_id
):
    engine, email = accounts
    legacy_id = account_id + 10
    tenant_fields = {"tenant_id": 1} if other_account_id else {}
    login_fields = {"tenant_code": "fixture-one"} if other_account_id else {}
    identifier = email.upper()
    with Session(engine) as db:
        if other_account_id:
            db.get(model, other_account_id).email = identifier
            db.commit()
        service = auth_service(db)
        record_tokens = AsyncMock()
        service._record_active_tokens = record_tokens
        authenticate = getattr(service, f"authenticate_{scope}")

        folded = await authenticate(identifier, PASSWORD, **login_fields)
        assert folded["token_type"] == "bearer"
        assert record_tokens.await_args.args[:2] == (scope, str(account_id))

        db.add(
            model(
                id=legacy_id,
                username=identifier,
                email="legacy-login@fixture.invalid",
                password_hash=second_password_hash,
                **tenant_fields,
            )
        )
        db.commit()
        exact = await authenticate(identifier, SECOND_PASSWORD, **login_fields)
        assert exact["token_type"] == "bearer"
        assert record_tokens.await_args.args[:2] == (scope, str(legacy_id))


async def test_login_code_issuance_declines_case_distinct_email_rows(
    accounts, local_redis, monkeypatch
):
    engine, email = accounts
    key = f"tenant_user_login_code:email:1:{email.lower()}"
    rate_key = f"tenant_user_login_code_rate:email:1:{email.lower()}"
    send_email = Mock()
    monkeypatch.setattr("app.tasks.email.send_email_task.delay", send_email)
    try:
        with Session(engine) as db:
            db.add(
                TenantUser(
                    id=41, tenant_id=1, username="LegacySecond", email=email.lower()
                )
            )
            db.commit()
            result = await auth_service(db).send_tenant_user_login_code(
                channel="email", email=email, tenant_code="fixture-one"
            )
        assert result == {"message": _("auth.login_code_sent")}
        assert await cache.cache_get(rate_key) is True
        assert await cache.cache_get(key) is None
        send_email.assert_not_called()
    finally:
        await local_redis.delete(key, rate_key)


async def test_simultaneous_login_code_redemption_issues_one_session(
    accounts, local_redis, monkeypatch
):
    engine, email = accounts
    key = f"tenant_user_login_code:email:1:{email.lower()}"
    await cache.cache_set(key, {"code": CODE, "user_id": 31}, ttl=60)
    issue_tokens = AsyncMock(return_value={"access_token": "fixture-session"})
    monkeypatch.setattr(AuthService, "_issue_tenant_user_tokens", issue_tokens)
    original_read = AuthService._cache_get
    both_read = asyncio.Event()
    readers = 0

    async def read_same_code(self, requested_key):
        nonlocal readers
        value = await original_read(self, requested_key)
        if requested_key == key:
            readers += 1
            if readers == 2:
                both_read.set()
            await both_read.wait()
        return value

    monkeypatch.setattr(AuthService, "_cache_get", read_same_code)

    async def redeem():
        with Session(engine, expire_on_commit=False) as db:
            return await auth_service(db).authenticate_tenant_user_by_code(
                channel="email",
                email=email.upper(),
                code=CODE,
                tenant_code="fixture-one",
            )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(redeem(), redeem(), return_exceptions=True), timeout=5
        )
        assert readers == 2
        assert results.count({"access_token": "fixture-session"}) == 1
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(errors) == 1
        assert isinstance(errors[0], AuthenticationException)
        assert errors[0].message == _("auth.login_code_invalid")
        issue_tokens.assert_awaited_once()
        assert issue_tokens.await_args.args[0].id == 31
        assert await local_redis.get(key) is None
    finally:
        await local_redis.delete(key)


async def test_login_code_rejects_an_email_no_longer_on_the_account(
    accounts, local_redis, monkeypatch
):
    engine, email = accounts
    old_email = f"stale-{email.lower()}"
    key = f"tenant_user_login_code:email:1:{old_email}"
    payload = {"code": CODE, "user_id": 31}
    await cache.cache_set(key, payload, ttl=60)
    issue_tokens = AsyncMock()
    monkeypatch.setattr(AuthService, "_issue_tenant_user_tokens", issue_tokens)
    try:
        with Session(engine) as db, pytest.raises(AuthenticationException) as denied:
            await auth_service(db).authenticate_tenant_user_by_code(
                channel="email",
                email=old_email,
                code=CODE,
                tenant_code="fixture-one",
            )
        assert denied.value.message == _("auth.login_code_invalid")
        assert await cache.cache_get(key) == payload
        issue_tokens.assert_not_awaited()
    finally:
        await local_redis.delete(key)


async def test_password_reset_consumes_a_previously_issued_mixed_case_key(
    accounts, local_redis
):
    engine, email = accounts
    key = f"password_reset:1:{email}"
    payload = {"code": CODE, "user_id": 31}
    await cache.cache_set(key, payload, ttl=60)
    try:
        with Session(engine) as db:
            service = auth_service(db)
            with pytest.raises(BusinessException) as denied:
                await service.reset_tenant_user_password(
                    email=email,
                    code="654321",
                    new_password="FictionalChanged43!",
                    tenant_code="fixture-one",
                )
            assert denied.value.message == _("auth.reset_code_invalid")
            assert verify_password(PASSWORD, db.get(TenantUser, 31).password_hash)
            assert await cache.cache_get(key) == payload

            await service.reset_tenant_user_password(
                email=email,
                code=CODE,
                new_password="FictionalChanged43!",
                tenant_code="fixture-one",
            )
            db.commit()
            assert verify_password(
                "FictionalChanged43!", db.get(TenantUser, 31).password_hash
            )
            assert verify_password(PASSWORD, db.get(TenantUser, 32).password_hash)
        assert await local_redis.get(key) is None
    finally:
        await local_redis.delete(key)


async def test_shared_redis_admission_claim_and_counter_expiry(local_redis):
    prefix = f"test:auth-code:{uuid4().hex}"
    claim_key, counter_key = f"{prefix}:claim", f"{prefix}:counter"
    try:
        claims = await asyncio.gather(
            *(
                cache.cache_set_if_absent(claim_key, number, ttl=60)
                for number in range(6)
            )
        )
        assert claims.count(True) == 1
        assert await cache.cache_get(claim_key) == claims.index(True)

        assert await cache.cache_increment(counter_key, ttl=60) == 1
        counts = await asyncio.gather(
            *(cache.cache_increment(counter_key, ttl=120) for _ in range(5))
        )
        assert sorted(counts) == [2, 3, 4, 5, 6]
        assert 0 < await local_redis.pttl(counter_key) <= 60_000
    finally:
        await local_redis.delete(claim_key, counter_key)


async def test_consumption_leaves_a_replacement_code_in_redis(local_redis):
    key = f"test:auth-code:{uuid4().hex}:replacement"
    replacement = {"code": "987654", "user_id": 31}
    await cache.cache_set(key, replacement, ttl=60)
    try:
        assert await cache.cache_getdel_if_matches(key, code=CODE, user_id=31) is None
        assert await cache.cache_get(key) == replacement
        assert (
            await cache.cache_getdel_if_matches(
                key, code=replacement["code"], user_id=32
            )
            is None
        )
        assert await cache.cache_get(key) == replacement
    finally:
        await local_redis.delete(key)

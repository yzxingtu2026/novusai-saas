"""中文: refresh-token 原子轮换与 replay 响应的行为测试。

EN: Behavioral tests for atomic refresh-token rotation and replay response.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.core.base_model import utc_now
from app.core.config import settings
from app.exceptions import AuthenticationException


class _AtomicRedis:
    """中文: 保留原子并发语义的小型 Redis 替身。

    EN: Small Redis double preserving the atomic concurrency contracts.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.reject_completed_write = False

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        async with self._lock:
            if nx and key in self._values:
                return None
            if xx and key not in self._values:
                return None
            if xx and value == "completed" and self.reject_completed_write:
                return None
            self._values[key] = value
            if ex is not None:
                self._ttls[key] = ex
            return True

    async def get(self, key: str) -> str | None:
        async with self._lock:
            return self._values.get(key)

    async def exists(self, key: str) -> int:
        async with self._lock:
            return int(key in self._values)

    async def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        assert ttl_seconds > 0
        async with self._lock:
            self._values[key] = value
            self._ttls[key] = ttl_seconds
            return True

    async def eval(self, script: str, numkeys: int, *args: str) -> str | int:
        keys = args[:numkeys]
        values = args[numkeys:]
        async with self._lock:
            if "-- revoke_refresh_family" in script:
                (family_key,) = keys
                ttl = int(values[0])
                current_ttl = self._ttls.get(family_key, -2)
                if current_ttl != -1 and current_ttl < ttl:
                    self._values[family_key] = "1"
                    self._ttls[family_key] = ttl
                return 1
            if "-- refresh_rotation_claim" in script:
                rotation_key, family_key = keys
                owner_state, source_ttl, completed_state, family_ttl = values
                if family_key in self._values:
                    return "revoked"
                state = self._values.get(rotation_key)
                if state is None:
                    self._values[rotation_key] = owner_state
                    self._ttls[rotation_key] = int(source_ttl)
                    return "claimed"
                if state == completed_state:
                    self._values[family_key] = "1"
                    self._ttls[family_key] = int(family_ttl)
                    return "replayed"
                return "inflight"

            if "-- refresh_rotation_complete" in script:
                rotation_key, family_key, source_blacklist_key = keys
                if family_key in self._values or source_blacklist_key in self._values:
                    return 0
            else:
                (rotation_key,) = keys
            owner_state = values[0]
            if len(values) == 3:
                completed_state, ttl = values[1:]
                if self._values.get(rotation_key) != owner_state:
                    return 0
                if self.reject_completed_write:
                    return 0
                self._values[rotation_key] = completed_state
                self._ttls[rotation_key] = int(ttl)
                return 1
            if self._values.get(rotation_key) != owner_state:
                return 0
            self._values.pop(rotation_key, None)
            self._ttls.pop(rotation_key, None)
            return 1

    async def delete(self, key: str) -> int:
        async with self._lock:
            existed = key in self._values
            self._values.pop(key, None)
            self._ttls.pop(key, None)
            return int(existed)


class _RotationService:
    """中文: 可观测签发与记录边界的轮换服务替身。

    EN: Rotation service double exposing mint and active-record boundaries.
    """

    def __init__(
        self,
        *,
        mint_error: Exception | None = None,
        record_error: Exception | None = None,
        record_gate: asyncio.Event | None = None,
    ) -> None:
        self.mint_error = mint_error
        self.record_error = record_error
        self.record_gate = record_gate
        self.mint_calls = 0
        self.record_calls = 0

    def _create_token_pair(
        self,
        subject: int,
        *,
        scope: str,
        extra_claims: dict[str, Any],
    ) -> dict[str, Any]:
        from app.core.security import create_token_pair

        self.mint_calls += 1
        if self.mint_error is not None:
            error = self.mint_error
            self.mint_error = None
            raise error
        return create_token_pair(
            subject,
            scope=scope,
            extra_claims=extra_claims,
        )

    async def _record_active_tokens(
        self,
        user_type: str,
        user_id: str,
        access_jti: str,
        refresh_jti: str,
        *,
        source_refresh_jti: str | None = None,
    ) -> None:
        del user_type, user_id, access_jti, refresh_jti, source_refresh_jti
        self.record_calls += 1
        if self.record_gate is not None:
            await self.record_gate.wait()
        if self.record_error is not None:
            error = self.record_error
            self.record_error = None
            raise error


def _refresh_payload() -> dict[str, object]:
    return {
        "sub": "7",
        "scope": "tenant_admin",
        "type": "refresh",
        "jti": "refresh-jti-1",
        "refresh_family_id": "refresh-family-1",
        "exp": (utc_now() + timedelta(days=1)).timestamp(),
    }


@pytest.mark.asyncio
async def test_concurrent_refresh_claim_allows_exactly_one_inflight_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 行为测试；原子 claim 串行化同 token 的并发交换。

    EN: Test type: behavioral. Atomic claim serializes concurrent exchange.
    """
    from app.core import security

    redis = _AtomicRedis()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    first, second = await asyncio.gather(
        security.claim_refresh_token_rotation(_refresh_payload()),
        security.claim_refresh_token_rotation(_refresh_payload()),
    )

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].family_id == "refresh-family-1"


@pytest.mark.asyncio
async def test_completed_refresh_replay_revokes_all_descendant_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 行为测试；已完成 token 的 replay 关闭整个 family。

    EN: Test type: behavioral. Replay closes the entire rotation family.
    """
    from app.core import security

    redis = _AtomicRedis()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)
    payload = _refresh_payload()

    claim = await security.claim_refresh_token_rotation(payload)
    assert claim is not None
    assert await security.complete_refresh_token_rotation(claim) is True

    replay_claim = await security.claim_refresh_token_rotation(payload)
    assert replay_claim is None
    assert await security.is_refresh_token_family_revoked("refresh-family-1") is True

    descendant_access, _ = security.create_access_token(
        subject=7,
        scope="tenant_admin",
        extra_claims={"refresh_family_id": "refresh-family-1"},
    )
    descendant_refresh, _ = security.create_refresh_token(
        subject=7,
        scope="tenant_admin",
        extra_claims={"refresh_family_id": "refresh-family-1"},
    )

    assert await security.decode_token(descendant_access) is None
    assert await security.decode_token(descendant_refresh) is None


@pytest.mark.asyncio
async def test_concurrent_full_rotation_mints_exactly_one_descendant_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 同一 refresh 并发交换只允许一个请求进入签发边界。

    EN: Concurrent exchange of one refresh token mints exactly one pair.
    """
    from app.services.common.auth_domains.refresh_rotation import rotate_refresh_token

    redis = _AtomicRedis()
    record_gate = asyncio.Event()
    service = _RotationService(record_gate=record_gate)
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    attempts = [
        asyncio.create_task(
            rotate_refresh_token(
                service,
                _refresh_payload(),
                subject=7,
                scope="tenant_admin",
                user_type="tenant_admin",
            )
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    record_gate.set()
    results = await asyncio.gather(*attempts, return_exceptions=True)

    assert service.mint_calls == 1
    assert service.record_calls == 1
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AuthenticationException) for result in results) == 1


@pytest.mark.asyncio
async def test_mint_failure_releases_owned_claim_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 本地签发失败不应把原 refresh 锁死到自然过期。

    EN: A local mint failure releases its claim so the refresh can be retried.
    """
    from app.services.common.auth_domains.refresh_rotation import rotate_refresh_token

    redis = _AtomicRedis()
    service = _RotationService(mint_error=RuntimeError("mint failed"))
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    with pytest.raises(RuntimeError, match="mint failed"):
        await rotate_refresh_token(
            service,
            _refresh_payload(),
            subject=7,
            scope="tenant_admin",
            user_type="tenant_admin",
        )

    tokens = await rotate_refresh_token(
        service,
        _refresh_payload(),
        subject=7,
        scope="tenant_admin",
        user_type="tenant_admin",
    )
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert service.mint_calls == 2
    assert service.record_calls == 1

    from jose import jwt

    access_payload = jwt.decode(
        tokens["access_token"],
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    refresh_payload = jwt.decode(
        tokens["refresh_token"],
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    assert access_payload["refresh_family_id"] == "refresh-family-1"
    assert refresh_payload["refresh_family_id"] == "refresh-family-1"


@pytest.mark.asyncio
async def test_active_record_failure_revokes_minted_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 活跃会话记录失败后，已签发后代所属 family 必须整体失效。

    EN: Active-session recording failure revokes the family of minted descendants.
    """
    from app.core.security import is_refresh_token_family_revoked
    from app.services.common.auth_domains.refresh_rotation import rotate_refresh_token

    redis = _AtomicRedis()
    service = _RotationService(record_error=RuntimeError("record failed"))
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    with pytest.raises(RuntimeError, match="record failed"):
        await rotate_refresh_token(
            service,
            _refresh_payload(),
            subject=7,
            scope="tenant_admin",
            user_type="tenant_admin",
        )

    assert await is_refresh_token_family_revoked("refresh-family-1") is True


@pytest.mark.asyncio
async def test_completion_failure_revokes_family_for_full_descendant_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: completion 失败的撤销标记必须覆盖新 refresh 的完整寿命。

    EN: Completion failure revokes the family for the new refresh's full lifetime.
    """
    from app.core.security import REFRESH_TOKEN_FAMILY_REVOKED_PREFIX
    from app.services.common.auth_domains.refresh_rotation import rotate_refresh_token

    redis = _AtomicRedis()
    redis.reject_completed_write = True
    service = _RotationService()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    with pytest.raises(AuthenticationException):
        await rotate_refresh_token(
            service,
            _refresh_payload(),
            subject=7,
            scope="tenant_admin",
            user_type="tenant_admin",
        )

    family_key = f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}refresh-family-1"
    minimum_descendant_ttl = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    assert redis._ttls[family_key] >= minimum_descendant_ttl


@pytest.mark.asyncio
async def test_abort_is_owner_scoped_and_preserves_a_newer_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 陈旧请求的 abort 不得删除另一个 owner 的状态。

    EN: A stale request's abort must preserve state owned by another request.
    """
    from app.core.security import (
        REFRESH_TOKEN_ROTATION_PREFIX,
        abort_refresh_token_rotation,
        claim_refresh_token_rotation,
    )

    redis = _AtomicRedis()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)
    claim = await claim_refresh_token_rotation(_refresh_payload())
    assert claim is not None
    rotation_key = f"{REFRESH_TOKEN_ROTATION_PREFIX}{claim.token_jti}"
    await redis.set(rotation_key, "inflight:new-owner", ex=claim.ttl_seconds)

    assert await abort_refresh_token_rotation(claim) is False
    assert await redis.get(rotation_key) == "inflight:new-owner"


@pytest.mark.asyncio
async def test_rotation_claim_fails_closed_before_mint_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: Redis 原子状态不可用时不得进入后代签发边界。

    EN: Redis atomic-state failure denies rotation before descendant minting.
    """
    from app.services.common.auth_domains.refresh_rotation import rotate_refresh_token

    redis = _AtomicRedis()

    async def unavailable_eval(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("redis unavailable")

    redis.eval = unavailable_eval  # type: ignore[method-assign]
    service = _RotationService()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: redis)

    with pytest.raises(AuthenticationException):
        await rotate_refresh_token(
            service,
            _refresh_payload(),
            subject=7,
            scope="tenant_admin",
            user_type="tenant_admin",
        )

    assert service.mint_calls == 0
    assert service.record_calls == 0

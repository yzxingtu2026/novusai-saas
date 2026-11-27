"""中文: 行为测试；已撤销的轮换族不得提交新 token。

EN: Test type: behavioral. A revoked rotation family cannot commit new tokens.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import security
from app.exceptions import AuthenticationException
from app.services.common.auth_domains import refresh_rotation
from tests.core.test_refresh_token_rotation import _AtomicRedis, _RotationService


@pytest.mark.asyncio
async def test_completion_rejects_family_revoked_after_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 行为测试；撤族后的 completion 保留 owner 状态并拒绝提交。

    EN: Test type: behavioral. Revoked-family completion preserves owner state.
    """
    store = _AtomicRedis()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: store)
    pair = security.create_token_pair(7, scope="tenant_admin")
    payload = await security.decode_token(pair["refresh_token"])
    claim = await security.claim_refresh_token_rotation(payload)
    assert claim and claim.family_id == payload["refresh_family_id"]
    assert await security.revoke_refresh_token_family(claim.family_id) is True

    assert await security.complete_refresh_token_rotation(claim) is False

    key = security.REFRESH_TOKEN_ROTATION_PREFIX + pair["refresh_jti"]
    assert await store.get(key) == claim.inflight_state
    independent_pair = security.create_token_pair(7, scope="tenant_admin")
    independent_payload = await security.decode_token(independent_pair["refresh_token"])
    independent_claim = await security.claim_refresh_token_rotation(independent_payload)
    assert independent_claim and independent_claim.family_id != claim.family_id
    assert await security.complete_refresh_token_rotation(independent_claim) is True
    independent_key = (
        security.REFRESH_TOKEN_ROTATION_PREFIX + independent_pair["refresh_jti"]
    )
    assert await store.get(independent_key) == "completed"


@pytest.mark.asyncio
async def test_revocation_between_claim_and_mint_returns_no_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文: 行为测试；claim 后、mint 前撤族时不得返回后代 token。

    EN: Test type: behavioral. Revocation between claim and mint returns no pair.
    """
    store = _AtomicRedis()
    monkeypatch.setattr("app.core.redis.get_redis_client", lambda: store)
    pair = security.create_token_pair(7, scope="tenant_admin")
    payload = await security.decode_token(pair["refresh_token"])
    actual_claim = refresh_rotation.claim_refresh_token_rotation
    reached, release = asyncio.Event(), asyncio.Event()

    async def gated_claim(value: dict[str, object]):
        claim = await actual_claim(value)
        assert claim and claim.family_id == payload["refresh_family_id"]
        reached.set()
        await release.wait()
        return claim

    monkeypatch.setattr(refresh_rotation, "claim_refresh_token_rotation", gated_claim)
    service = _RotationService()
    pending = asyncio.create_task(
        refresh_rotation.rotate_refresh_token(
            service,
            payload,
            subject=7,
            scope="tenant_admin",
            user_type="tenant_admin",
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        assert await security.revoke_refresh_token_family(payload["refresh_family_id"])
    finally:
        release.set()

    with pytest.raises(AuthenticationException):
        await asyncio.wait_for(pending, timeout=2)
    assert service.mint_calls == 1
    assert service.record_calls == 1
    assert await security.is_refresh_token_family_revoked(payload["refresh_family_id"])

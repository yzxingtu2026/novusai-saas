"""中文: 所有账户作用域共享的原子 refresh-token 轮换。

EN: Atomic refresh-token rotation shared by all account scopes.
"""

from __future__ import annotations

from math import ceil
from time import time
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.i18n import _
from app.core.security import (
    REFRESH_TOKEN_FAMILY_CLAIM,
    _decode_token_no_blacklist,
    abort_refresh_token_rotation,
    claim_refresh_token_rotation,
    complete_refresh_token_rotation,
    normalize_positive_subject_id,
    revoke_refresh_token_family,
)
from app.exceptions import AuthenticationException

if TYPE_CHECKING:
    from app.services.common.auth_service import AuthService


async def _revoke_minted_family(family_id: str, tokens: dict[str, Any]) -> None:
    """中文: 未登记后代也可能有较长 access，撤销须覆盖实际签名有效期。

    EN: An unregistered child may have a longer access lifetime; fence its actual signed expiry.
    """
    ttl = max(1, int(settings.REFRESH_TOKEN_EXPIRE_DAYS) * 86400)
    for key in ("access_token", "refresh_token"):
        payload = _decode_token_no_blacklist(tokens[key])
        if payload is not None:
            ttl = max(ttl, ceil(payload["exp"] - time()))
    await revoke_refresh_token_family(family_id, ttl_seconds=ttl)


async def rotate_refresh_token(
    service: AuthService,
    payload: dict[str, Any] | None,
    *,
    subject: int,
    scope: str,
    user_type: str,
    extra_claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """中文: 占有、签发、记录并提交一次 refresh-token 交换。

    EN: Claim, mint, record, and commit one refresh-token exchange.
    """
    if (
        payload is None
        or normalize_positive_subject_id(payload.get("sub")) != subject
        or payload.get("scope") != scope
        or user_type != scope
        or scope not in {"admin", "tenant_admin", "tenant_user"}
    ):
        raise AuthenticationException(message=_("auth.refresh_token_invalid"))
    claim = await claim_refresh_token_rotation(payload)
    if claim is None:
        raise AuthenticationException(message=_("auth.refresh_token_invalid"))

    descendant_claims = dict(extra_claims or {})
    descendant_claims[REFRESH_TOKEN_FAMILY_CLAIM] = claim.family_id
    try:
        tokens = service._create_token_pair(
            subject,
            scope=scope,
            extra_claims=descendant_claims,
        )
    except BaseException:
        # 中文: 本地签发尚无外部后代，按 owner 释放 claim 允许客户端重试。
        # EN: Local mint has no external descendant, so owner-abort permits a retry.
        await abort_refresh_token_rotation(claim)
        raise

    try:
        await service._record_active_tokens(
            user_type,
            str(subject),
            tokens["access_jti"],
            tokens["refresh_jti"],
            source_refresh_jti=claim.token_jti,
        )
    except BaseException:
        # 中文: 记录结果不确定时撤销整族，避免部分成功的后代稍后恢复有效。
        # EN: Revoke the family when recording is ambiguous so partial descendants never revive.
        await _revoke_minted_family(claim.family_id, tokens)
        raise

    if await complete_refresh_token_rotation(claim):
        return tokens

    # 中文: 已构造的后代尚不能返回，整族撤销覆盖它们的实际完整寿命。
    # EN: Minted descendants cannot be returned; family revocation covers their actual remaining lifetimes.
    await _revoke_minted_family(claim.family_id, tokens)
    raise AuthenticationException(message=_("auth.refresh_token_invalid"))


__all__ = ["rotate_refresh_token"]

"""Token session and password-policy domain for AuthService."""

from time import time
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.i18n import _
from app.core.redis import get_redis_client
from app.core.security import (
    ACTIVE_TOKENS_PREFIX,
    REFRESH_TOKEN_FAMILY_CLAIM,
    TOKEN_BLACKLIST_PREFIX,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    _decode_token_no_blacklist,
    refresh_token_family_id,
    revoke_refresh_token_family,
    revoke_token,
    session_retention_seconds,
)
from app.exceptions import AuthenticationException, BusinessException

if TYPE_CHECKING:
    from app.services.common.auth_service import AuthService


# 中文: 来源撤销检查与登记原子提交，强制下线之后的在途轮换不能重建会话。
# EN: Check source revocation and register atomically so an in-flight refresh cannot revive after logout.
_RECORD_ACTIVE_TOKENS_LUA = """-- record_active_tokens
if ARGV[4] ~= "" and redis.call("EXISTS", KEYS[2]) == 1 then
    return 0
end
redis.call("HSET", KEYS[1], ARGV[1], ARGV[2])
local ttl = math.max(tonumber(ARGV[3]), redis.call("TTL", KEYS[1]))
redis.call("EXPIRE", KEYS[1], ttl)
return 1
"""

# 中文: 快照、黑名单与删除在一次脚本中完成，避免并发登记被漏撤或误删。
# EN: Snapshot, blacklist and delete atomically so concurrent registration is neither missed nor erased.
_REVOKE_ACTIVE_TOKENS_LUA = """-- revoke_active_tokens
local pairs = redis.call("HGETALL", KEYS[1])
local ttl = math.max(tonumber(ARGV[2]), redis.call("TTL", KEYS[1]))
local function revoke(jti)
    if jti ~= "" then
        local key = ARGV[1] .. jti
        local current_ttl = redis.call("TTL", key)
        if current_ttl ~= -1 and current_ttl < ttl then
            redis.call("SET", key, "1", "EX", ttl)
        end
    end
end
for i = 1, #pairs, 2 do
    revoke(pairs[i])
    revoke(pairs[i + 1])
end
redis.call("DEL", KEYS[1])
return #pairs / 2
"""


class AuthSessionPasswordDomain:
    """Stable auth sub-domain: token-session lifecycle and password policy."""

    def __init__(self, service: "AuthService") -> None:
        self._service = service

    async def record_active_tokens(
        self,
        user_type: str,
        user_id: str,
        access_jti: str,
        refresh_jti: str,
        *,
        source_refresh_jti: str | None = None,
    ) -> None:
        client = get_redis_client()
        key = f"{ACTIVE_TOKENS_PREFIX}{user_type}:{user_id}"
        ttl_seconds = session_retention_seconds()
        committed = await client.eval(
            _RECORD_ACTIVE_TOKENS_LUA,
            2,
            key,
            f"{TOKEN_BLACKLIST_PREFIX}{source_refresh_jti or ''}",
            access_jti,
            refresh_jti,
            str(ttl_seconds),
            source_refresh_jti or "",
        )
        if not committed:
            if source_refresh_jti:
                raise AuthenticationException(message=_("auth.refresh_token_invalid"))
            raise RuntimeError("active token registration was not committed")

    async def revoke_on_logout(
        self,
        access_token: str,
        user_type: str,
        user_id: str,
    ) -> None:
        payload = _decode_token_no_blacklist(access_token)
        if not payload or not payload.get("jti"):
            self._service._log_auth_warning(
                "auth.logout.skipped",
                user_type=user_type,
                user_id=user_id,
                reason="missing_jti",
            )
            return
        access_jti = payload["jti"]
        exp = payload.get("exp")
        access_ttl = max(1, int(exp - time())) if exp else 86400
        # 中文: 显式 family 的已签名 access 必须关闭所有后代，包含提交中的轮换。
        # EN: A signed access token with an explicit family must close every
        # descendant, including a rotation already waiting in the commit path.
        # 中文: 旧 access 仅允许由服务端配对索引确认 refresh family。
        # EN: Legacy access requires its server-side paired index to identify a family.
        family_id = (
            refresh_token_family_id(payload)
            if payload.get("type") == TOKEN_TYPE_ACCESS
            else None
        )

        try:
            client = get_redis_client()
            key = f"{ACTIVE_TOKENS_PREFIX}{user_type}:{user_id}"
            refresh_jti = await client.hget(key, access_jti)
            if (
                payload.get("type") == TOKEN_TYPE_ACCESS
                and REFRESH_TOKEN_FAMILY_CLAIM not in payload
                and refresh_jti
            ):
                family_id = refresh_token_family_id(
                    {"type": TOKEN_TYPE_REFRESH, "jti": refresh_jti}
                )
            if family_id is not None:
                await revoke_refresh_token_family(
                    family_id, ttl_seconds=access_ttl, raise_on_error=True
                )
            await revoke_token(access_jti, access_ttl, raise_on_error=True)
            if refresh_jti:
                refresh_ttl = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
                await revoke_token(refresh_jti, refresh_ttl, raise_on_error=True)
            await client.hdel(key, access_jti)
            self._service._log_auth_info(
                "auth.logout.success",
                user_type=user_type,
                user_id=user_id,
                revoked_refresh=bool(refresh_jti),
            )
        except Exception as exc:
            self._service._log_auth_warning(
                "auth.logout.failed",
                user_type=user_type,
                user_id=user_id,
                reason="token_revoke_error",
                error_type=type(exc).__name__,
            )
            raise

    async def force_logout(
        self,
        user_type: str,
        user_id: int,
        tenant_id: int | None = None,
    ) -> None:
        from app.core.sio_bridge import emit_force_logout
        from app.sio.presence import PresenceManager

        user_id_str = str(user_id)
        key = f"{ACTIVE_TOKENS_PREFIX}{user_type}:{user_id_str}"
        revoked_sessions = 0
        try:
            client = get_redis_client()
            revoked_sessions = int(
                await client.eval(
                    _REVOKE_ACTIVE_TOKENS_LUA,
                    1,
                    key,
                    TOKEN_BLACKLIST_PREFIX,
                    str(session_retention_seconds()),
                )
            )
        except Exception as exc:
            self._service._log_auth_warning(
                "auth.force_logout.revoke_failed",
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                reason="token_revoke_error",
                error_type=type(exc).__name__,
            )

        try:
            await PresenceManager.set_offline(user_type, user_id, tenant_id=tenant_id)
        except Exception as exc:
            self._service._log_auth_warning(
                "auth.force_logout.presence_cleanup_failed",
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                error_type=type(exc).__name__,
            )

        try:
            await emit_force_logout(user_id, user_type)
        except Exception as exc:
            self._service._log_auth_warning(
                "auth.force_logout.emit_failed",
                user_type=user_type,
                user_id=user_id,
                tenant_id=tenant_id,
                error_type=type(exc).__name__,
            )
            raise

        self._service._log_auth_info(
            "auth.force_logout.success",
            user_type=user_type,
            user_id=user_id,
            tenant_id=tenant_id,
            revoked_sessions=revoked_sessions,
        )

    async def validate_password_policy(
        self,
        password: str,
        tenant_id: int | None = None,
    ) -> None:
        if tenant_id:
            min_length = await self._service._config_service.get_tenant_config(
                tenant_id, "tenant_password_min_length", default=None
            )
            complexity = await self._service._config_service.get_tenant_config(
                tenant_id, "tenant_password_complexity", default=None
            )
        else:
            min_length = None
            complexity = None

        if not min_length:
            min_length = await self._service._config_service.get_platform_config(
                "password_min_length", default=8
            )
        if not complexity:
            complexity = await self._service._config_service.get_platform_config(
                "password_complexity", default="medium"
            )

        if len(password) < min_length:
            raise BusinessException(
                message=_("auth.password_too_short", min_length=min_length)
            )

        if complexity == "low":
            return
        if complexity == "medium":
            has_letter = any(char.isalpha() for char in password)
            has_digit = any(char.isdigit() for char in password)
            if not (has_letter and has_digit):
                raise BusinessException(message=_("auth.password_complexity_medium"))
            return
        if complexity == "high":
            has_letter = any(char.isalpha() for char in password)
            has_digit = any(char.isdigit() for char in password)
            has_special = any(not char.isalnum() for char in password)
            if not (has_letter and has_digit and has_special):
                raise BusinessException(message=_("auth.password_complexity_high"))

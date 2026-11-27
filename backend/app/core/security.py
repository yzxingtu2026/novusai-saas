"""
安全模块 / Security Module

提供 JWT Token 生成/验证、密码哈希、Token 黑名单吊销等安全相关功能
Provides JWT token generation/verification, password hashing, token blacklist revoke.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from jose import ExpiredSignatureError, JWTError, jwt

from app.core.base_model import utc_now
from app.core.config import settings
from app.core.logging import LogManager

logger = LogManager.get_logger("app.core.security")

# Token 类型常量 / Token type constants
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
TOKEN_TYPE_IMPERSONATE = (
    "impersonate"  # 一键登录临时 Token / One-click login temporary token
)

# Token 作用域常量（用户类型） / Token scope constants (user types)
TOKEN_SCOPE_ADMIN = "admin"  # 平台管理员 / Platform admin
TOKEN_SCOPE_TENANT_ADMIN = "tenant_admin"  # 企业管理员 / Tenant admin
TOKEN_SCOPE_TENANT_USER = "tenant_user"  # 企业业务用户 / Tenant business user
TENANT_SESSION_EPOCH_CLAIM = "tenant_session_epoch"

# Impersonate Token 过期时间（秒） / Impersonate token expiry (seconds)
IMPERSONATE_TOKEN_EXPIRE_SECONDS = 60


def normalize_nonnegative_integer(value: object) -> int | None:
    """校验非负整数声明 / Normalize a non-negative integer claim."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            value = int(value, 10)
        except ValueError:
            return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def normalize_positive_subject_id(value: object) -> int | None:
    """校验正整数身份 / Normalize a positive account ID."""
    account_id = normalize_nonnegative_integer(value)
    return account_id if account_id and account_id <= 2**63 - 1 else None


def tenant_session_epoch_matches(payload: dict[str, Any] | None, epoch: object) -> bool:
    """令牌必须携带当前会话代际 / Require an explicit current tenant epoch."""
    current = normalize_nonnegative_integer(epoch)
    if current is None or payload is None:
        return False
    return (
        normalize_nonnegative_integer(payload.get(TENANT_SESSION_EPOCH_CLAIM))
        == current
    )


def create_access_token(
    subject: str | int,
    scope: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """
    创建 Access Token / Create access token

    Args:
        subject: Token 主体（通常是用户 ID） / Token subject (usually user ID)
        scope: Token 作用域（用户类型），必须为 TOKEN_SCOPE_* 常量之一 / Token scope (user type)
        expires_delta: 过期时间增量 / Expiration time delta
        extra_claims: 额外的 claims / Additional claims

    Returns:
        (token, jti) 元组 / Tuple of (token, jti)
    """
    if expires_delta:
        expire = utc_now() + expires_delta
    else:
        expire = utc_now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = str(uuid.uuid4())

    to_encode = {
        "sub": str(subject),
        "scope": scope,
        "exp": expire,
        "iat": utc_now(),
        "type": TOKEN_TYPE_ACCESS,
        "jti": jti,
    }

    if extra_claims:
        to_encode.update(extra_claims)

    token = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return token, jti


def create_refresh_token(
    subject: str | int,
    scope: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """
    创建 Refresh Token / Create refresh token

    Args:
        subject: Token 主体（通常是用户 ID） / Token subject (usually user ID)
        scope: Token 作用域（用户类型），必须为 TOKEN_SCOPE_* 常量之一 / Token scope (user type)
        expires_delta: 过期时间增量 / Expiration time delta
        extra_claims: 额外的 claims / Additional claims

    Returns:
        (token, jti) 元组 / Tuple of (token, jti)
    """
    if expires_delta:
        expire = utc_now() + expires_delta
    else:
        expire = utc_now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    jti = str(uuid.uuid4())

    to_encode = {
        "sub": str(subject),
        "scope": scope,
        "exp": expire,
        "iat": utc_now(),
        "type": TOKEN_TYPE_REFRESH,
        "jti": jti,
    }

    if extra_claims:
        to_encode.update(extra_claims)

    token = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return token, jti


class TokenExpiredError(Exception):
    """Token 已过期（区别于无效 Token） / Token expired (distinct from invalid token)"""


# Redis key prefix for token blacklist / Token 黑名单 Redis key 前缀
TOKEN_BLACKLIST_PREFIX = "token_blacklist:"
ACTIVE_TOKENS_PREFIX = "active_tokens:"
REFRESH_TOKEN_FAMILY_CLAIM = "refresh_family_id"
REFRESH_TOKEN_ROTATION_PREFIX = "refresh_token_rotation:"
REFRESH_TOKEN_FAMILY_REVOKED_PREFIX = "refresh_token_family_revoked:"
_REFRESH_ROTATION_INFLIGHT = "inflight"
_REFRESH_ROTATION_COMPLETED = "completed"

# 中文: claim 将 family 检查、旧 token 状态检查和占有写入放进一次 Redis 原子操作。
# EN: Claim combines the family check, source-token state check, and ownership write atomically.
_REFRESH_ROTATION_CLAIM_LUA = """-- refresh_rotation_claim
if redis.call("EXISTS", KEYS[2]) == 1 then
    return "revoked"
end
local state = redis.call("GET", KEYS[1])
if not state then
    redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
    return "claimed"
end
if state == ARGV[3] then
    redis.call("SET", KEYS[2], "1", "EX", ARGV[4])
    return "replayed"
end
return "inflight"
"""

# 中文: completion 同时检查族、来源 JTI 与 owner，登记后强退也不能提交后代。
# EN: Completion checks family, source JTI and owner so logout after registration cannot commit descendants.
_REFRESH_ROTATION_COMPLETE_LUA = """-- refresh_rotation_complete
if redis.call("EXISTS", KEYS[2]) == 1 or redis.call("EXISTS", KEYS[3]) == 1 then
    return 0
end
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call("SET", KEYS[1], ARGV[2], "EX", ARGV[3])
return 1
"""

# 中文: 并发撤族只延长有效期，不让较短后代覆盖较长会话的撤销期限。
# EN: Concurrent family revocation only extends retention; a shorter child cannot shorten a longer session's fence.
_REFRESH_FAMILY_REVOKE_LUA = """-- revoke_refresh_family
local current_ttl = redis.call("TTL", KEYS[1])
if current_ttl ~= -1 and current_ttl < tonumber(ARGV[1]) then
    redis.call("SET", KEYS[1], "1", "EX", ARGV[1])
end
return 1
"""

# 中文: abort 同样按 owner 做 CAS，签发失败不会删除其他请求的 claim。
# EN: Abort is owner-CAS as well, so mint failure never deletes another request's claim.
_REFRESH_ROTATION_ABORT_LUA = """-- refresh_rotation_abort
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
return redis.call("DEL", KEYS[1])
"""


@dataclass(frozen=True, slots=True)
class RefreshTokenRotationClaim:
    """中文: 一次 refresh 交换的独占凭据。

    EN: Exclusive ownership proof for one refresh-token exchange attempt.
    """

    token_jti: str
    family_id: str
    ttl_seconds: int
    owner_id: str

    @property
    def inflight_state(self) -> str:
        """中文: 返回写入 Redis 的 owner-tagged 状态。

        EN: Return the owner-tagged state persisted in Redis.
        """
        return f"{_REFRESH_ROTATION_INFLIGHT}:{self.owner_id}"


def _normalize_refresh_family_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    if not all(char.isalnum() or char in {"-", "_", "."} for char in value):
        return None
    return value


def _refresh_rotation_ttl(payload: dict[str, Any]) -> int | None:
    exp = payload.get("exp")
    if isinstance(exp, bool):
        return None
    try:
        expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc)
        ttl_seconds = ceil((expires_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return ttl_seconds if ttl_seconds > 0 else None


def session_retention_seconds(minimum_seconds: int = 0) -> int:
    """覆盖令牌寿命及后台可配置的一天会话上限。"""
    return max(
        minimum_seconds,
        86400,
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )


def refresh_token_family_id(payload: dict[str, Any]) -> str | None:
    """中文: 返回显式 family id，旧 refresh 则回退到 JTI。

    EN: Return the explicit family id, falling back to a legacy refresh JTI.
    """
    explicit_family = payload.get(REFRESH_TOKEN_FAMILY_CLAIM)
    if explicit_family is not None:
        return _normalize_refresh_family_id(explicit_family)
    if payload.get("type") != TOKEN_TYPE_REFRESH:
        return None
    return _normalize_refresh_family_id(payload.get("jti"))


def create_refresh_token_family_id() -> str:
    """中文: 创建一个 token 谱系共享的不透明 family id。

    EN: Create a validated opaque family id shared by one token lineage.
    """
    return str(uuid.uuid4())


async def revoke_token(
    jti: str, ttl_seconds: int, *, raise_on_error: bool = False
) -> None:
    """
    将 Token 加入黑名单（吊销）/ Revoke token by adding to blacklist.

    Args:
        jti: JWT ID
        ttl_seconds: 剩余有效时间（秒），用作 Redis TTL / Remaining TTL in seconds for Redis key
    """
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        key = f"{TOKEN_BLACKLIST_PREFIX}{jti}"
        await client.setex(key, ttl_seconds, "1")
    except Exception as exc:
        logger.debug("Token blacklist add failed: {}", exc)
        if raise_on_error:
            raise


def _decode_token_no_blacklist(token: str) -> dict[str, Any] | None:
    """
    解码 Token（不检查黑名单，用于登出时获取 jti/exp） / Decode token without blacklist check (for logout flow).
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except (ExpiredSignatureError, JWTError):
        return None


async def is_token_revoked(jti: str | None) -> bool:
    """
    检查 Token 是否已被吊销 / Check if token is revoked.

    Args:
        jti: JWT ID，None 或空则视为已失效 / None or empty => invalidated

    Returns:
        True 表示已吊销 / True if revoked
    """
    if not jti:
        return True
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        key = f"{TOKEN_BLACKLIST_PREFIX}{jti}"
        return bool(await client.exists(key))
    except Exception as exc:
        logger.warning("Token revocation lookup failed closed: {}", type(exc).__name__)
        return True


async def is_refresh_token_family_revoked(
    family_id: str | None,
    *,
    raise_on_error: bool = False,
) -> bool:
    """中文: 检查 refresh family 的持久 replay 撤销标记。

    EN: Check the durable replay-revocation marker for a refresh family.
    """
    normalized_family_id = _normalize_refresh_family_id(family_id)
    if normalized_family_id is None:
        return True
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        return bool(
            await client.exists(
                f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{normalized_family_id}"
            )
        )
    except Exception as exc:
        if raise_on_error:
            raise
        logger.warning(
            "Refresh token family revocation lookup failed closed: {}",
            type(exc).__name__,
        )
        return True


async def revoke_refresh_token_family(
    family_id: str,
    *,
    ttl_seconds: int | None = None,
    raise_on_error: bool = False,
) -> bool:
    """中文: 同时失效同一 family 的 access 与 refresh 后代。

    EN: Invalidate access and refresh descendants belonging to one family.
    """
    normalized_family_id = _normalize_refresh_family_id(family_id)
    if normalized_family_id is None:
        return False
    effective_ttl = session_retention_seconds(ttl_seconds or 0)
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        committed = await client.eval(
            _REFRESH_FAMILY_REVOKE_LUA,
            1,
            f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{normalized_family_id}",
            str(effective_ttl),
        )
        return bool(committed)
    except Exception as exc:
        logger.warning(
            "Refresh token family revocation write failed: {}",
            type(exc).__name__,
        )
        if raise_on_error:
            raise
        return False


async def claim_refresh_token_rotation(
    payload: dict[str, Any],
) -> RefreshTokenRotationClaim | None:
    """中文: 原子占有一次 refresh 交换。

    EN: A simultaneous duplicate observes ``inflight`` and is denied without
    penalizing the winner. A later replay observes ``completed`` and revokes
    the whole family, including already-issued descendant access tokens. Redis
    failure is fail-closed.
    """
    if payload.get("type") != TOKEN_TYPE_REFRESH:
        return None
    token_jti = _normalize_refresh_family_id(payload.get("jti"))
    family_id = refresh_token_family_id(payload)
    ttl_seconds = _refresh_rotation_ttl(payload)
    if token_jti is None or family_id is None or ttl_seconds is None:
        return None

    rotation_key = f"{REFRESH_TOKEN_ROTATION_PREFIX}{token_jti}"
    family_key = f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{family_id}"
    owner_id = str(uuid.uuid4())
    inflight_state = f"{_REFRESH_ROTATION_INFLIGHT}:{owner_id}"
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        result = await client.eval(
            _REFRESH_ROTATION_CLAIM_LUA,
            2,
            rotation_key,
            family_key,
            inflight_state,
            str(ttl_seconds),
            _REFRESH_ROTATION_COMPLETED,
            str(session_retention_seconds(ttl_seconds)),
        )
        if isinstance(result, bytes):
            result = result.decode("utf-8", errors="replace")
        if result == "claimed":
            return RefreshTokenRotationClaim(
                token_jti=token_jti,
                family_id=family_id,
                ttl_seconds=ttl_seconds,
                owner_id=owner_id,
            )
        return None
    except Exception as exc:
        logger.warning(
            "Refresh token rotation claim failed closed: {}",
            type(exc).__name__,
        )
        return None


async def complete_refresh_token_rotation(
    claim: RefreshTokenRotationClaim,
) -> bool:
    """中文: 仅由当前 owner 消耗 refresh；已撤族时返回 False。

    EN: Mark the refresh spent only for its owner; return False for a revoked family.
    """
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        completed = await client.eval(
            _REFRESH_ROTATION_COMPLETE_LUA,
            3,
            f"{REFRESH_TOKEN_ROTATION_PREFIX}{claim.token_jti}",
            f"{REFRESH_TOKEN_FAMILY_REVOKED_PREFIX}{claim.family_id}",
            f"{TOKEN_BLACKLIST_PREFIX}{claim.token_jti}",
            claim.inflight_state,
            _REFRESH_ROTATION_COMPLETED,
            str(max(1, claim.ttl_seconds)),
        )
        return bool(completed)
    except Exception as exc:
        logger.warning(
            "Refresh token rotation completion failed closed: {}",
            type(exc).__name__,
        )
        return False


async def abort_refresh_token_rotation(
    claim: RefreshTokenRotationClaim,
) -> bool:
    """中文: 签发失败时仅释放调用方仍持有的 claim。

    EN: Release only the claim still owned by the caller after mint failure.
    """
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        aborted = await client.eval(
            _REFRESH_ROTATION_ABORT_LUA,
            1,
            f"{REFRESH_TOKEN_ROTATION_PREFIX}{claim.token_jti}",
            claim.inflight_state,
        )
        return bool(aborted)
    except Exception as exc:
        logger.warning(
            "Refresh token rotation abort failed closed: {}",
            type(exc).__name__,
        )
        return False


async def decode_token(
    token: str, raise_on_expired: bool = False
) -> dict[str, Any] | None:
    """
    解码并验证 Token / Decode and verify token

    Args:
        token: JWT Token 字符串 / JWT token string
        raise_on_expired: 为 True 时，过期 Token 抛出 TokenExpiredError
            而非静默返回 None，允许调用方区分过期和无效。
            When True, raises TokenExpiredError for expired tokens
            instead of silently returning None.

    Returns:
        解码后的 payload，验证失败返回 None / Decoded payload, None on failure

    Raises:
        TokenExpiredError: raise_on_expired=True 且 Token 已过期 / when True and token is expired
    """
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        jti = payload.get("jti")
        if not jti:
            return None
        if await is_token_revoked(jti):
            return None
        if (
            REFRESH_TOKEN_FAMILY_CLAIM in payload
            and await is_refresh_token_family_revoked(refresh_token_family_id(payload))
        ):
            return None
        return payload
    except ExpiredSignatureError as exc:
        if raise_on_expired:
            raise TokenExpiredError() from exc
        return None
    except JWTError:
        return None


async def verify_token(token: str, token_type: str = TOKEN_TYPE_ACCESS) -> str | None:
    """
    验证 Token 并返回 subject / Verify token and return subject

    Args:
        token: JWT Token 字符串 / JWT token string
        token_type: 期望的 Token 类型 / Expected token type

    Returns:
        Token 的 subject（用户 ID），验证失败返回 None / Token subject (user ID), None on failure
    """
    payload = await decode_token(token)
    if payload is None:
        return None

    # 检查 Token 类型 / Check token type
    if payload.get("type") != token_type:
        return None

    return payload.get("sub")


async def verify_token_with_scope(
    token: str,
    expected_scope: str,
    token_type: str = TOKEN_TYPE_ACCESS,
    raise_on_expired: bool = False,
) -> tuple[str | None, str | None]:
    """
    验证 Token 并检查 scope / Verify token and check scope

    Args:
        token: JWT Token 字符串 / JWT token string
        expected_scope: 期望的 scope（用户类型） / Expected scope (user type)
        token_type: 期望的 Token 类型 / Expected token type
        raise_on_expired: 为 True 时，过期 Token 抛出 TokenExpiredError / Raise on expired token

    Returns:
        (subject, scope) 元组，验证失败返回 (None, None) / Tuple, (None, None) on failure

    Raises:
        TokenExpiredError: raise_on_expired=True 且 Token 已过期 / when True and token is expired
    """
    payload = await get_token_payload_with_scope(
        token, expected_scope, token_type, raise_on_expired=raise_on_expired
    )
    if payload is None:
        return None, None
    return payload.get("sub"), expected_scope


async def get_token_payload_with_scope(
    token: str,
    expected_scope: str,
    token_type: str = TOKEN_TYPE_ACCESS,
    *,
    raise_on_expired: bool = False,
) -> dict[str, Any] | None:
    """验证后返回指定类型和范围的声明 / Return verified claims for scope and type."""
    payload = await decode_token(token, raise_on_expired=raise_on_expired)
    if (
        payload is None
        or payload.get("type") != token_type
        or payload.get("scope") != expected_scope
    ):
        return None
    return payload


async def get_token_payload(
    token: str,
    token_type: str = TOKEN_TYPE_ACCESS,
) -> dict[str, Any] | None:
    """
    获取 Token 的完整 payload / Get full token payload

    Args:
        token: JWT Token 字符串 / JWT token string
        token_type: 期望的 Token 类型 / Expected token type

    Returns:
        Token 的 payload，验证失败返回 None / Token payload, None on failure
    """
    payload = await decode_token(token)
    if payload is None:
        return None

    # 检查 Token 类型 / Check token type
    if payload.get("type") != token_type:
        return None

    return payload


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码 / Verify password

    Args:
        plain_password: 明文密码 / Plain text password
        hashed_password: 哈希后的密码 / Hashed password

    Returns:
        密码是否正确 / Whether the password is correct
    """
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def get_password_hash(password: str) -> str:
    """
    获取密码哈希 / Get password hash

    Args:
        password: 明文密码 / Plain text password

    Returns:
        哈希后的密码 / Hashed password
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_token_pair(
    subject: str | int,
    scope: str,
    extra_claims: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    创建 Token 对（access_token + refresh_token） / Create token pair

    Args:
        subject: Token 主体 / Token subject
        scope: Token 作用域（用户类型） / Token scope (user type)
        extra_claims: 附加访问声明，刷新令牌同时携带会话族和租户代际 /
            Extra access claims. Both tokens carry the refresh family and tenant epoch.

    Returns:
        包含 access_token、refresh_token、access_jti、refresh_jti 的字典
        Dict with access_token, refresh_token, access_jti, refresh_jti
    """
    pair_claims = dict(extra_claims or {})
    if REFRESH_TOKEN_FAMILY_CLAIM not in pair_claims:
        pair_claims[REFRESH_TOKEN_FAMILY_CLAIM] = create_refresh_token_family_id()
    access_token, access_jti = create_access_token(
        subject, scope=scope, extra_claims=pair_claims
    )
    refresh_claims = {
        claim: pair_claims[claim]
        for claim in (REFRESH_TOKEN_FAMILY_CLAIM, TENANT_SESSION_EPOCH_CLAIM)
        if claim in pair_claims
    }
    refresh_token, refresh_jti = create_refresh_token(
        subject, scope=scope, extra_claims=refresh_claims
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_jti": access_jti,
        "refresh_jti": refresh_jti,
        "token_type": "bearer",
    }


def create_impersonate_token(
    admin_id: int,
    target_scope: str,
    target_tenant_id: int,
    target_role_id: int | None = None,
    expires_seconds: int = IMPERSONATE_TOKEN_EXPIRE_SECONDS,
) -> str:
    """
    创建一键登录临时 Token / Create one-click login temporary token

    用于平台管理员一键登录企业后台或企业管理员一键登录用户端
    Used for admin one-click login to tenant backend or tenant admin one-click login to user side.

    Args:
        admin_id: 发起者 ID（平台管理员或企业管理员） / Initiator ID (admin or tenant admin)
        target_scope: 目标 scope（tenant_admin 或 tenant_user） / Target scope
        target_tenant_id: 目标企业 ID / Target tenant ID
        target_role_id: 目标角色 ID（可选） / Target role ID (optional)
        expires_seconds: 过期时间（秒），默认 60 秒 / Expiry in seconds, default 60

    Returns:
        编码后的 JWT Token / Encoded JWT token
    """
    expire = utc_now() + timedelta(seconds=expires_seconds)
    jti = str(uuid.uuid4())

    to_encode = {
        "sub": str(admin_id),  # 发起者 ID / Initiator ID
        "type": TOKEN_TYPE_IMPERSONATE,
        "target_scope": target_scope,
        "target_tenant_id": target_tenant_id,
        "exp": expire,
        "iat": utc_now(),
        "jti": jti,
    }

    if target_role_id is not None:
        to_encode["target_role_id"] = target_role_id

    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def verify_impersonate_token(
    token: str,
    expected_target_scope: str,
) -> dict[str, Any] | None:
    """
    验证一键登录 Token / Verify one-click login token

    Args:
        token: JWT Token 字符串 / JWT token string
        expected_target_scope: 期望的目标 scope / Expected target scope

    Returns:
        Token 的 payload，验证失败返回 None / Token payload, None on failure
        payload 包含 / contains: sub, target_scope, target_tenant_id, target_role_id(可选/optional)
    """
    payload = await decode_token(token)
    if payload is None:
        return None

    # 检查 Token 类型 / Check token type
    if payload.get("type") != TOKEN_TYPE_IMPERSONATE:
        return None

    # 检查目标 scope / Check target scope
    if payload.get("target_scope") != expected_target_scope:
        return None

    return payload


def _get_encryption_key() -> bytes:
    """
    获取加密密钥 / Get encryption key

    使用 SECRET_KEY 派生 Fernet 密钥 / Derives Fernet key from SECRET_KEY
    Fernet 需要 32 字节的 base64 编码密钥 / Fernet requires 32-byte base64-encoded key
    """
    # 确保密钥长度符合 Fernet 要求 / Ensure key length meets Fernet requirements
    key = settings.SECRET_KEY.encode()
    # 使用 SHA256 生成固定长度的密钥 / Use SHA256 to generate fixed-length key
    import base64
    import hashlib

    hash_key = hashlib.sha256(key).digest()
    return base64.urlsafe_b64encode(hash_key)


def encrypt_data(plaintext: str) -> str:
    """
    加密数据 / Encrypt data

    Args:
        plaintext: 明文 / Plain text

    Returns:
        加密后的密文（Base64 编码） / Encrypted ciphertext (Base64 encoded)
    """
    f = Fernet(_get_encryption_key())
    encrypted = f.encrypt(plaintext.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_data(ciphertext: str) -> str:
    """
    解密数据 / Decrypt data

    Args:
        ciphertext: 密文（Base64 编码） / Ciphertext (Base64 encoded)

    Returns:
        解密后的明文 / Decrypted plain text
    """
    f = Fernet(_get_encryption_key())
    decrypted = f.decrypt(ciphertext.encode("utf-8"))
    return decrypted.decode("utf-8")


__all__ = [
    "create_access_token",
    "create_refresh_token",
    "revoke_token",
    "is_token_revoked",
    "decode_token",
    "verify_token",
    "verify_token_with_scope",
    "get_token_payload_with_scope",
    "get_token_payload",
    "verify_password",
    "get_password_hash",
    "create_token_pair",
    "create_impersonate_token",
    "verify_impersonate_token",
    "encrypt_data",
    "decrypt_data",
    "TokenExpiredError",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "TOKEN_TYPE_IMPERSONATE",
    "TOKEN_SCOPE_ADMIN",
    "TOKEN_SCOPE_TENANT_ADMIN",
    "TOKEN_SCOPE_TENANT_USER",
    "TENANT_SESSION_EPOCH_CLAIM",
    "tenant_session_epoch_matches",
    "REFRESH_TOKEN_FAMILY_CLAIM",
    "REFRESH_TOKEN_ROTATION_PREFIX",
    "REFRESH_TOKEN_FAMILY_REVOKED_PREFIX",
    "RefreshTokenRotationClaim",
    "refresh_token_family_id",
    "session_retention_seconds",
    "create_refresh_token_family_id",
    "is_refresh_token_family_revoked",
    "revoke_refresh_token_family",
    "claim_refresh_token_rotation",
    "complete_refresh_token_rotation",
    "abort_refresh_token_rotation",
    "IMPERSONATE_TOKEN_EXPIRE_SECONDS",
]

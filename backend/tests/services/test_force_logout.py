"""
强制下线功能单元测试 / Force logout unit tests.

覆盖：AuthService.force_logout 吊销 Token、清除 Presence、发送 Socket.IO 事件；
emit_force_logout 按 user_type 仅向对应 namespace 发送。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("redis", reason="redis package required for auth_service import")


class TestAuthServiceForceLogout:
    """AuthService.force_logout 测试 / AuthService.force_logout tests."""

    @pytest.mark.asyncio
    @pytest.mark.structural
    async def test_force_logout_submits_atomic_revocation_and_emits_event(
        self, mock_db, mock_redis
    ):
        """Test type: structural. Dispatch the atomic revocation for the exact identity."""
        from app.core.config import settings
        from app.services.common.auth_service import AuthService

        mock_redis.eval = AsyncMock(return_value=2)

        with (
            patch(
                "app.services.common.auth_domains.session_password.get_redis_client",
                return_value=mock_redis,
            ),
            patch(
                "app.services.common.auth_domains.session_password.revoke_token",
                new_callable=AsyncMock,
            ) as mock_revoke,
            patch(
                "app.sio.presence.PresenceManager.set_offline", new_callable=AsyncMock
            ) as mock_offline,
            patch(
                "app.core.sio_bridge.emit_force_logout", new_callable=AsyncMock
            ) as mock_emit,
        ):
            service = AuthService(mock_db)
            await service.force_logout("tenant_admin", user_id=5, tenant_id=10)

            script, numkeys, key, prefix, ttl = mock_redis.eval.await_args.args
            assert "-- revoke_active_tokens" in script
            assert (numkeys, key, prefix) == (
                1,
                "active_tokens:tenant_admin:5",
                "token_blacklist:",
            )
            assert int(ttl) == max(
                86400,
                settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
            )
            mock_redis.eval.assert_awaited_once()
            mock_revoke.assert_not_awaited()
            # 应清除 presence
            mock_offline.assert_called_once_with("tenant_admin", 5, tenant_id=10)
            # 应仅向 /tenant namespace 发送（tenant_admin）
            mock_emit.assert_called_once_with(5, "tenant_admin")

    @pytest.mark.asyncio
    async def test_force_logout_emit_only_to_correct_namespace(self):
        """emit_force_logout 按 user_type 仅向对应 namespace 发送，不误踢其他类型用户 / Emit to correct namespace only."""
        from app.core.sio_bridge import NS_MAP, emit_force_logout

        mock_sio = AsyncMock()
        mock_sio.emit = AsyncMock(return_value=None)
        with patch("app.core.socketio_server.sio", mock_sio):
            await emit_force_logout(user_id=3, user_type="admin")
            mock_sio.emit.assert_called_once()
            call_args = mock_sio.emit.call_args
            assert call_args[0][0] == "force_logout"
            assert call_args[1]["namespace"] == NS_MAP["admin"]
            assert call_args[1]["room"] == "user:3"

        mock_sio2 = AsyncMock()
        mock_sio2.emit = AsyncMock(return_value=None)
        with patch("app.core.socketio_server.sio", mock_sio2):
            await emit_force_logout(user_id=3, user_type="tenant_user")
            mock_sio2.emit.assert_called_once()
            assert mock_sio2.emit.call_args[1]["namespace"] == NS_MAP["tenant_user"]

    @pytest.mark.asyncio
    async def test_force_logout_empty_tokens_still_emits_and_clears_presence(
        self, mock_db, mock_redis
    ):
        """无活跃 Token 时仍应清除 presence 并发送事件 / No tokens still clears presence and emits."""
        from app.services.common.auth_service import AuthService

        mock_redis.eval = AsyncMock(return_value=0)
        with (
            patch(
                "app.services.common.auth_domains.session_password.get_redis_client",
                return_value=mock_redis,
            ),
            patch(
                "app.services.common.auth_domains.session_password.revoke_token",
                new_callable=AsyncMock,
            ) as mock_revoke,
            patch(
                "app.sio.presence.PresenceManager.set_offline", new_callable=AsyncMock
            ) as mock_offline,
            patch(
                "app.core.sio_bridge.emit_force_logout", new_callable=AsyncMock
            ) as mock_emit,
        ):
            service = AuthService(mock_db)
            await service.force_logout("admin", user_id=1, tenant_id=None)

            mock_revoke.assert_not_called()
            mock_offline.assert_called_once_with("admin", 1, tenant_id=None)
            mock_emit.assert_called_once_with(1, "admin")

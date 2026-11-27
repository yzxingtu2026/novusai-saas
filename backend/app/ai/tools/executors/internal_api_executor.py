"""
Internal API Executor / 内部 API 执行器

Executes the internal-ops meta-tools (list / describe / invoke). Invocations
are performed as HTTP self-calls against this application carrying a
short-lived AI proxy token, so RBAC, tenant isolation, request validation and
audit logging are all enforced by the existing middleware stack.
执行内部操作元工具（list / describe / invoke）。invoke 通过携带短时效 AI 代理
token 的 HTTP 自调用完成，RBAC、租户隔离、参数校验与审计日志全部复用现有
中间件栈强制执行。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.ai.internal_ops.catalog import (
    USER_ROLE_TO_SCOPE,
    InternalOperation,
    get_operation,
    has_permission,
    search_operations,
)
from app.ai.internal_ops.proxy_token import issue_ai_proxy_token
from app.ai.internal_ops.tools import (
    TOOL_DESCRIBE_OPERATION,
    TOOL_INVOKE_OPERATION,
    TOOL_LIST_OPERATIONS,
)
from app.ai.tools.executors.base import BaseToolExecutor
from app.ai.tools.types import ExecutionContext, ToolDefinition, ToolResult
from app.core.config import settings
from app.core.logging import LogManager
from app.core.security import (
    normalize_nonnegative_integer,
    normalize_positive_subject_id,
)
from app.enums.common import UserRoleEnum
from app.repositories.system.tenant_repository import TenantRepository

logger = LogManager.get_logger("ai.tool.internal_api")

# Pending write confirmations live this long in Redis / 待确认写操作的 Redis TTL（秒）
_PENDING_CONFIRMATION_TTL_SECONDS = 600
_PENDING_KEY_PREFIX = "ai:internal_ops:pending"

# Cap for non-JSON response bodies relayed to the LLM / 非 JSON 响应体转发上限
_MAX_TEXT_BODY_CHARS = 2000
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_PATH_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_MAX_PATH_PARAM_LENGTH = 2048


def _normalize_path_param(name: str, value: object) -> str:
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"Path parameter '{name}' must be a scalar")
    text = str(value)
    if (
        not text
        or len(text) > _MAX_PATH_PARAM_LENGTH
        or text in {".", ".."}
        or any(char in text for char in "/\\?#%")
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        raise ValueError(f"Path parameter '{name}' contains an invalid segment")
    return text


def _json_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _arguments_digest(
    operation_id: str,
    path_params: dict[str, Any],
    query_params: dict[str, Any],
    body: dict[str, Any] | None,
) -> str:
    canonical = json.dumps(
        [operation_id, path_params, query_params, body],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class InternalApiExecutor(BaseToolExecutor):
    """
    Internal API meta-tool executor / 内部 API 元工具执行器

    Security model / 安全模型:
    - The proxy token subject is the conversation user, so the self-call goes
      through PermissionMiddleware and RBAC exactly like a manual request.
      代理 token 主体是对话用户本人，自调用与手工请求一样经过权限中间件与 RBAC。
    - Write operations require a server-armed confirmation: the preview call
      records a Redis entry, and execution only proceeds when the confirmed
      replay matches that entry. An LLM passing confirmed=true on its own
      cannot bypass the preview.
      写操作需要服务端布防的确认：预览时写入 Redis 记录，仅当确认重放命中该
      记录才执行。LLM 自行伪造 confirmed=true 无法绕过预览。
    """

    async def validate(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        # JSON Schema validation already runs in ToolSandbox / 沙箱已做 Schema 校验
        del definition, arguments
        return True

    async def execute(
        self,
        definition: ToolDefinition,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ExecutionContext | None = None,
    ) -> ToolResult:
        name = definition.name
        if context is None or normalize_positive_subject_id(context.user_id) is None:
            return ToolResult.error_result(
                tool_call_id,
                "Internal operations require an authenticated conversation user.",
                name=name,
            )

        scope = USER_ROLE_TO_SCOPE.get(str(context.user_role or "").strip())
        if not scope:
            return ToolResult.error_result(
                tool_call_id,
                f"Unsupported user role for internal operations: {context.user_role}",
                name=name,
            )

        try:
            if name == TOOL_LIST_OPERATIONS:
                return self._list_operations(tool_call_id, arguments, context, scope)
            if name == TOOL_DESCRIBE_OPERATION:
                return self._describe_operation(tool_call_id, arguments, context, scope)
            if name == TOOL_INVOKE_OPERATION:
                return await self._invoke_operation(
                    tool_call_id, arguments, context, scope
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Internal ops tool {} failed: {}", name, exc, exc_info=True)
            return ToolResult.error_result(
                tool_call_id,
                f"Internal operation tool failed: {exc}",
                name=name,
            )

        return ToolResult.error_result(
            tool_call_id,
            f"Unknown internal ops tool: {name}",
            name=name,
        )

    # ------------------------------------------------------------------
    # list / describe
    # ------------------------------------------------------------------

    def _list_operations(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        scope: str,
    ) -> ToolResult:
        page, total = search_operations(
            scope=scope,
            user_permissions=set(context.permissions or set()),
            keyword=str(arguments.get("keyword") or ""),
            module=str(arguments.get("module") or ""),
            method=str(arguments.get("method") or ""),
            offset=int(arguments.get("offset") or 0),
        )
        payload: dict[str, Any] = {
            "total": total,
            "returned": len(page),
            "operations": [op.to_brief() for op in page],
        }
        if total == 0:
            payload["empty_result"] = True
            payload["should_stop_searching"] = True
            payload["recommended_next_action"] = (
                "Stop calling list_internal_operations after one retry; "
                "tell the user no matching operation is available in the "
                "current permission scope."
            )
            payload["possible_reasons"] = [
                "permission_scope_has_no_matching_operation",
                "feature_not_enabled",
                "endpoint_not_registered",
            ]
            payload["hint"] = (
                "No operations matched in the current permission scope. "
                "Retry at most once with a broader keyword, then stop "
                "searching and explain that no matching operation is "
                "available."
            )
        elif total > len(page):
            payload["hint"] = (
                "More results available; refine the keyword or pass a larger "
                "offset to page through."
            )
        return ToolResult(
            tool_call_id=tool_call_id,
            name=TOOL_LIST_OPERATIONS,
            success=True,
            output=_json_output(payload),
            summary=f"{total} operations matched",
        )

    def _describe_operation(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        scope: str,
    ) -> ToolResult:
        operation_id = str(arguments.get("operation_id") or "").strip()
        op = get_operation(operation_id)
        error = self._check_operation_access(op, operation_id, context, scope)
        if error:
            return ToolResult.error_result(
                tool_call_id, error, name=TOOL_DESCRIBE_OPERATION
            )
        assert op is not None
        return ToolResult(
            tool_call_id=tool_call_id,
            name=TOOL_DESCRIBE_OPERATION,
            success=True,
            output=_json_output(op.to_detail()),
            summary=f"{op.method} {op.path}",
        )

    # ------------------------------------------------------------------
    # invoke
    # ------------------------------------------------------------------

    async def _invoke_operation(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        scope: str,
    ) -> ToolResult:
        operation_id = str(arguments.get("operation_id") or "").strip()
        op = get_operation(operation_id)
        error = self._check_operation_access(op, operation_id, context, scope)
        if error:
            return ToolResult.error_result(
                tool_call_id, error, name=TOOL_INVOKE_OPERATION
            )
        assert op is not None

        path_params = arguments.get("path_params")
        if path_params is None:
            path_params = {}
        if not isinstance(path_params, dict) or any(
            not isinstance(key, str) for key in path_params
        ):
            return ToolResult.error_result(
                tool_call_id,
                "Argument 'path_params' must be a JSON object.",
                name=TOOL_INVOKE_OPERATION,
            )
        query_params = dict(arguments.get("query_params") or {})
        body = arguments.get("body")
        if body is not None and not isinstance(body, dict):
            return ToolResult.error_result(
                tool_call_id,
                "Argument 'body' must be a JSON object.",
                name=TOOL_INVOKE_OPERATION,
            )

        # Fill path placeholders / 填充路径占位符
        placeholders = _PATH_PLACEHOLDER_RE.findall(op.path)
        missing = [p for p in placeholders if p not in path_params]
        if missing:
            return ToolResult.error_result(
                tool_call_id,
                f"Missing path params: {missing}. "
                "Call describe_internal_operation for the parameter spec.",
                name=TOOL_INVOKE_OPERATION,
            )
        unknown = sorted(set(path_params) - set(placeholders))
        if unknown:
            return ToolResult.error_result(
                tool_call_id,
                f"Unknown path params: {unknown}. Use describe_internal_operation for the parameter spec.",
                name=TOOL_INVOKE_OPERATION,
            )
        try:
            resolved_path = _PATH_PLACEHOLDER_RE.sub(
                lambda match: quote(
                    _normalize_path_param(match[1], path_params[match[1]]), safe=""
                ),
                op.path,
            )
        except ValueError as exc:
            return ToolResult.error_result(
                tool_call_id, str(exc), name=TOOL_INVOKE_OPERATION
            )

        # Write operations require a server-armed confirmation
        # 写操作需要服务端布防的确认
        if op.is_write:
            digest = _arguments_digest(operation_id, path_params, query_params, body)
            confirmed = arguments.get("confirmed") is True
            gate = await self._write_confirmation_gate(
                context=context,
                op=op,
                digest=digest,
                confirmed=confirmed,
                preview={
                    "operation_id": operation_id,
                    "path_params": path_params,
                    "query_params": query_params,
                    "body": body,
                },
            )
            if gate is not None:
                if "error" in gate:
                    return ToolResult.error_result(
                        tool_call_id, gate["error"], name=TOOL_INVOKE_OPERATION
                    )
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=TOOL_INVOKE_OPERATION,
                    success=True,
                    output=_json_output(gate),
                    summary=f"Awaiting confirmation: {op.method} {resolved_path}",
                )

        return await self._perform_http_call(
            tool_call_id=tool_call_id,
            context=context,
            op=op,
            resolved_path=resolved_path,
            query_params=query_params,
            body=body,
        )

    def _check_operation_access(
        self,
        op: InternalOperation | None,
        operation_id: str,
        context: ExecutionContext,
        scope: str,
    ) -> str | None:
        """Shared lookup + scope + permission pre-check / 查找与权限预检"""
        if op is None:
            return (
                f"Operation '{operation_id}' not found. Use "
                "list_internal_operations to discover valid operation ids."
            )
        if op.scope != scope:
            return (
                f"Operation '{operation_id}' belongs to the '{op.scope}' console "
                f"and is not available for the current '{scope}' user."
            )
        if not has_permission(set(context.permissions or set()), op.permission_code):
            return (
                f"The current user lacks permission '{op.permission_code}' "
                "required by this operation."
            )
        return None

    async def _write_confirmation_gate(
        self,
        *,
        context: ExecutionContext,
        op: InternalOperation,
        digest: str,
        confirmed: bool,
        preview: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a preview or an error. None permits the write."""
        from app.core.redis import get_redis_client

        key = (
            f"{_PENDING_KEY_PREFIX}:{context.conversation_id or 0}"
            f":{context.agent_id}:{context.user_id}:{digest}"
        )
        try:
            client = get_redis_client()
            if confirmed:
                # DEL consumes the sentinel and reports whether it existed.
                if await client.delete(key) == 1:
                    return None
                return {
                    "error": (
                        "Internal API confirmation is missing, expired, or already used. "
                        "Request a new preview."
                    )
                }
            await client.setex(key, _PENDING_CONFIRMATION_TTL_SECONDS, "1")
        except Exception as exc:  # noqa: BLE001
            # Fail closed: never run unverified writes without the Redis gate
            # 故障关闭：Redis 不可用时绝不执行未经验证的写操作
            logger.error("Write confirmation gate unavailable: {}", exc)
            return {
                "error": (
                    "Write confirmation service is unavailable; "
                    "the operation was NOT executed. Try again later."
                )
            }

        # Build user-readable approval presentation / 构建用户可读确认展示
        from app.ai.internal_ops.approval_presentation import (
            build_approval_presentation,
        )

        presentation = await build_approval_presentation(
            db=context.db,
            operation_id=op.operation_id,
            method=op.method,
            path=op.path,
            permission_code=op.permission_code,
            summary=op.summary,
            action=op.action,
            body=preview.get("body"),
            path_params=preview.get("path_params"),
            query_params=preview.get("query_params"),
        )

        return {
            "requires_confirmation": True,
            "action": f"{op.method} {op.path}",
            "operation_id": op.operation_id,
            "summary": op.summary,
            "permission": op.permission_code,
            "preview": preview,
            "approval_presentation": presentation.to_dict(),
            "message": (
                "This is a write operation and it was NOT executed yet. "
                "Present the preview to the user and wait. If the user "
                "explicitly approves in a later message, call "
                "invoke_internal_operation again with the exact same "
                "arguments plus confirmed=true. If the user rejects, do "
                "not retry."
            ),
        }

    async def _perform_http_call(
        self,
        *,
        tool_call_id: str,
        context: ExecutionContext,
        op: InternalOperation,
        resolved_path: str,
        query_params: dict[str, Any],
        body: dict[str, Any] | None,
    ) -> ToolResult:
        user_role = str(context.user_role).strip()
        tenant_id = context.tenant_id
        tenant_session_epoch = None
        if user_role in (
            UserRoleEnum.TENANT_ADMIN.value,
            UserRoleEnum.TENANT_USER.value,
        ):
            tenant_id = normalize_positive_subject_id(tenant_id)
            if context.db is None or tenant_id is None:
                return ToolResult.error_result(
                    tool_call_id,
                    "Internal API call denied: tenant session context unavailable",
                    name=TOOL_INVOKE_OPERATION,
                )
            try:
                tenant_session_epoch = normalize_nonnegative_integer(
                    await TenantRepository(context.db).get_active_session_epoch(
                        tenant_id
                    )
                )
            except Exception as exc:
                logger.warning(
                    "AI proxy token tenant epoch lookup failed: tenant={} error_type={}",
                    tenant_id,
                    type(exc).__name__,
                )
            if tenant_session_epoch is None:
                return ToolResult.error_result(
                    tool_call_id,
                    "Internal API call denied: tenant session validation failed",
                    name=TOOL_INVOKE_OPERATION,
                )

        token = issue_ai_proxy_token(
            user_id=normalize_positive_subject_id(context.user_id) or 0,
            user_role=user_role,
            tenant_id=tenant_id,
            agent_id=context.agent_id,
            conversation_id=context.conversation_id,
            tenant_session_epoch=tenant_session_epoch,
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": (
                f"NovusAI-Copilot/1.0 (agent={context.agent_id}; "
                f"conversation={context.conversation_id or 0})"
            ),
        }

        timeout = 30.0
        if context.tool_timeout_seconds:
            timeout = max(min(float(context.tool_timeout_seconds) - 1.0, 55.0), 5.0)

        response_content = bytearray()
        async with httpx.AsyncClient(
            base_url=settings.APP_INTERNAL_BASE_URL,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            try:
                async with client.stream(
                    op.method,
                    resolved_path,
                    params=query_params or None,
                    json=body if op.method in ("POST", "PUT", "PATCH") else None,
                    headers=headers,
                ) as response:
                    response_status = response.status_code
                    response_encoding = response.encoding or "utf-8"
                    try:
                        content_length = int(response.headers.get("content-length", ""))
                    except ValueError:
                        content_length = 0
                    if content_length > _MAX_RESPONSE_BYTES:
                        return ToolResult.error_result(
                            tool_call_id,
                            "Internal API response too large",
                            name=TOOL_INVOKE_OPERATION,
                        )
                    async for chunk in response.aiter_bytes():
                        if len(response_content) + len(chunk) > _MAX_RESPONSE_BYTES:
                            return ToolResult.error_result(
                                tool_call_id,
                                "Internal API response too large",
                                name=TOOL_INVOKE_OPERATION,
                            )
                        response_content.extend(chunk)
            except httpx.HTTPError as exc:
                return ToolResult.error_result(
                    tool_call_id,
                    f"Internal API call failed: {type(exc).__name__}",
                    name=TOOL_INVOKE_OPERATION,
                )

        try:
            response_body: Any = json.loads(response_content)
        except ValueError:
            text = response_content.decode(response_encoding, errors="replace")
            response_body = text[:_MAX_TEXT_BODY_CHARS]

        payload = {
            "status_code": response_status,
            "ok": 200 <= response_status < 300,
            "operation_id": op.operation_id,
            "body": response_body,
        }
        return ToolResult(
            tool_call_id=tool_call_id,
            name=TOOL_INVOKE_OPERATION,
            success=True,
            output=_json_output(payload),
            summary=f"{op.method} {resolved_path} → {response_status}",
        )


__all__ = ["InternalApiExecutor"]

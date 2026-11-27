"""
Code Execution Tool Executor / 代码执行工具执行器

Python execution requires an isolated runtime.
代码执行需要隔离运行环境。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.ai.tools.executors.base import BaseToolExecutor
from app.ai.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from app.ai.tools.types import ExecutionContext


class CodeExecutionExecutor(BaseToolExecutor):
    """
    Validate code input and enforce the isolation requirement / 校验代码输入并要求隔离运行。
    """

    async def execute(
        self,
        definition: ToolDefinition,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ExecutionContext | None = None,
    ) -> ToolResult:
        """Apply the code execution policy / 应用代码执行策略"""
        _ = context
        start = time.perf_counter()

        code = arguments.get("code", "")
        if not code or not code.strip():
            return ToolResult(
                tool_call_id=tool_call_id,
                name=definition.name,
                success=False,
                error="No code provided",
            )

        # 普通子进程继承服务权限 / A plain subprocess inherits the service's permissions.
        return ToolResult(
            tool_call_id=tool_call_id,
            name=definition.name,
            success=False,
            error="Code execution requires an isolated runtime",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def validate(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        """Validate code execution arguments / 校验代码执行参数"""
        _ = definition
        return bool(arguments.get("code"))


__all__ = ["CodeExecutionExecutor"]

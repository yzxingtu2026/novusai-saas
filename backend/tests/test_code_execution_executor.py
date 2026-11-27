"""行为测试 / Behavioral tests for the code execution policy.

The executor is real. Process launch and database access are guarded.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.tools.executors.code_execution_executor import CodeExecutionExecutor
from app.ai.tools.types import ExecutionContext, ToolDefinition, ToolResult

pytestmark = [pytest.mark.asyncio, pytest.mark.behavioral]


@pytest.fixture(autouse=True)
def host_process(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    start = AsyncMock(side_effect=AssertionError("Unexpected host subprocess launch"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    return start


@pytest.fixture
def audit_db() -> MagicMock:
    db = MagicMock(spec_set=["__bool__"])
    db.__bool__.side_effect = AssertionError(
        "Unexpected executor audit database access"
    )
    return db


@pytest.mark.parametrize(
    "config",
    [
        {},
        {
            "_code_language": "python",
            "_code_timeout": 5,
            "_code_memory_limit_mb": 128,
            "_code_allowed_modules": ["math"],
        },
        {"_code_language": "javascript"},
    ],
    ids=["default-language", "explicit-python-with-execution-config", "other-language"],
)
async def test_nonempty_code_requires_an_isolated_runtime(
    config, host_process, audit_db
) -> None:
    definition = ToolDefinition(
        name="execute_code", tool_type="code_execution", config=config
    )
    context = ExecutionContext(tenant_id=11, agent_id=22, db=audit_db)

    result = await CodeExecutionExecutor().execute(
        definition, "code-call", {"code": "print(2 + 2)"}, context
    )

    host_process.assert_not_awaited()
    assert audit_db.mock_calls == []
    assert result == ToolResult(
        tool_call_id="code-call",
        name="execute_code",
        success=False,
        error="Code execution requires an isolated runtime",
        duration_ms=result.duration_ms,
    )
    assert result.duration_ms >= 0


@pytest.mark.parametrize(
    "arguments",
    [{}, {"code": ""}, {"code": " \t\n"}],
    ids=["missing-code", "empty-code", "blank-code"],
)
async def test_blank_code_keeps_its_public_response(
    arguments, host_process, audit_db
) -> None:
    definition = ToolDefinition(
        name="execute_code",
        tool_type="code_execution",
        config={"_code_language": "javascript"},
    )
    context = ExecutionContext(tenant_id=11, agent_id=22, db=audit_db)

    result = await CodeExecutionExecutor().execute(
        definition, "invalid-call", arguments, context
    )

    host_process.assert_not_awaited()
    assert audit_db.mock_calls == []
    assert result == ToolResult(
        tool_call_id="invalid-call",
        name="execute_code",
        success=False,
        error="No code provided",
    )

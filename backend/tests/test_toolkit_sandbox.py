"""Test type: behavioral.

Tests for Toolkit execution policy, child-process dispatch, and static scanning.
使用合成工具验证执行策略、子进程调用和静态扫描。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.ai.tools.executors import toolkit_executor
from app.ai.tools.executors.toolkit_executor import ToolkitExecutor
from app.ai.tools.executors.toolkit_security import scan_toolkit_security
from app.ai.tools.types import ToolDefinition
from app.core.config import settings

pytestmark = pytest.mark.behavioral


@pytest.fixture(autouse=True)
def _development_error_details(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEBUG", "true")


def _make_definition(
    toolkit_content: str,
    method_name: str = "hello",
    trusted: bool = True,
) -> ToolDefinition:
    """Helper to create a ToolDefinition with toolkit config. / 创建"""
    return ToolDefinition(
        name=f"test_{method_name}",
        description="Test tool",
        parameters=[],
        config={
            "_toolkit_content": toolkit_content,
            "_toolkit_method": method_name,
            "_toolkit_trusted": trusted,
            "_valves_config": {},
        },
    )


# --------------------------------------------------------------------------- #
# Subprocess mode tests
# --------------------------------------------------------------------------- #


class TestSubprocessExecution:
    """Test subprocess sandbox mode. / 测试"""

    SIMPLE_TOOLKIT = """class Tools:
    def hello(self, name: str = "World") -> str:
        return f"Hello, {name}!"

    def add(self, a: int = 0, b: int = 0) -> int:
        return a + b
"""

    @pytest.mark.asyncio
    async def test_simple_execution(self) -> None:
        """Normal toolkit execution in subprocess. / 说明"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(self.SIMPLE_TOOLKIT, "hello")
        result = await executor.execute(defn, "call_1", {"name": "Test"})
        assert result.success
        assert "Hello, Test!" in result.output

    @pytest.mark.asyncio
    async def test_math_execution(self) -> None:
        """Toolkit returning numeric result. / 获取/返回"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(self.SIMPLE_TOOLKIT, "add")
        result = await executor.execute(defn, "call_2", {"a": 3, "b": 7})
        assert result.success
        assert "10" in result.output

    @pytest.mark.asyncio
    async def test_missing_tools_class(self) -> None:
        """Toolkit without Tools class. / 说明"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition("def hello(): return 'hi'", "hello")
        result = await executor.execute(defn, "call_3", {})
        assert not result.success
        assert "Tools" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_method(self) -> None:
        """Toolkit with missing method. / 说明"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(self.SIMPLE_TOOLKIT, "nonexistent")
        result = await executor.execute(defn, "call_4", {})
        assert not result.success
        assert "nonexistent" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Subprocess execution timeout. / 说明"""
        slow_toolkit = """
import time
class Tools:
    def slow(self) -> str:
        time.sleep(60)
        return "done"
"""
        executor = ToolkitExecutor(timeout=2, sandbox_mode="subprocess")
        defn = _make_definition(slow_toolkit, "slow")
        result = await executor.execute(defn, "call_5", {})
        assert not result.success
        assert "timed out" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_nonzero_child_exit_is_reported(self, monkeypatch) -> None:
        """模拟子进程退出码并验证父进程错误 / Stub the child exit and exercise parent error handling."""
        child = Mock(
            returncode=42,
            communicate=AsyncMock(return_value=(b"", b"fixture child failure")),
        )
        spawn = AsyncMock(return_value=child)
        monkeypatch.setattr(toolkit_executor.asyncio, "create_subprocess_exec", spawn)
        definition = _make_definition(self.SIMPLE_TOOLKIT)
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        result = await executor.execute(definition, "call_6", {})

        assert result.success is False
        assert (
            "Sandbox process exited with code 42: fixture child failure" in result.error
        )
        child.communicate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_in_toolkit(self) -> None:
        """Exception in toolkit code is captured. / 说明"""
        error_toolkit = """class Tools:
    def fail(self) -> str:
        raise ValueError("intentional error")"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(error_toolkit, "fail")
        result = await executor.execute(defn, "call_7", {})
        assert not result.success
        assert "intentional error" in (result.error or "")

    @pytest.mark.asyncio
    async def test_dict_return(self) -> None:
        """Toolkit returning dict is serialized as JSON. / 获取/返回"""
        dict_toolkit = """class Tools:
    def get_data(self) -> dict:
        return {"key": "value", "num": 42}"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(dict_toolkit, "get_data")
        result = await executor.execute(defn, "call_8", {})
        assert result.success
        assert "key" in result.output
        assert "value" in result.output

    @pytest.mark.asyncio
    async def test_async_method(self) -> None:
        """Toolkit with async method. / 说明"""
        async_toolkit = """
import asyncio
class Tools:
    async def async_hello(self, name: str = "World") -> str:
        await asyncio.sleep(0.01)
        return f"Async Hello, {name}!"
"""
        executor = ToolkitExecutor(sandbox_mode="subprocess")
        defn = _make_definition(async_toolkit, "async_hello")
        result = await executor.execute(defn, "call_9", {"name": "Async"})
        assert result.success
        assert "Async Hello, Async!" in result.output


# --------------------------------------------------------------------------- #
# Inprocess mode tests
# --------------------------------------------------------------------------- #


class TestInprocessExecution:
    """Test inprocess mode (backward compatibility). / 测试"""

    SIMPLE_TOOLKIT = '''class Tools:
    def greet(self, name: str = "World") -> str:
        return f"Hi, {name}!"'''

    @pytest.mark.asyncio
    async def test_inprocess_execution(self) -> None:
        """Inprocess mode works like before. / 说明"""
        executor = ToolkitExecutor(sandbox_mode="inprocess")
        defn = _make_definition(self.SIMPLE_TOOLKIT, "greet")
        result = await executor.execute(defn, "call_10", {"name": "Dev"})
        assert result.success
        assert "Hi, Dev!" in result.output


# --------------------------------------------------------------------------- #
# Execution policy tests / 执行策略测试
# --------------------------------------------------------------------------- #


POLICY_TOOLKIT = """class Tools:
    def hello(self) -> str:
        return "fixture"
"""


@pytest.fixture
def execution_boundary(monkeypatch):
    spawn = AsyncMock(side_effect=AssertionError("Toolkit reached child creation"))
    load = Mock(side_effect=AssertionError("Toolkit reached module loading"))
    monkeypatch.setattr(toolkit_executor.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(toolkit_executor, "_load_toolkit_module", load)
    return spawn, load


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
@pytest.mark.parametrize(
    "trust_fields",
    [
        pytest.param({}, id="missing"),
        pytest.param({"_toolkit_trusted": False}, id="false"),
        pytest.param({"_toolkit_trusted": None}, id="null"),
        pytest.param({"_toolkit_trusted": "false"}, id="text-false"),
        pytest.param({"_toolkit_trusted": "true"}, id="text-true"),
        pytest.param({"_toolkit_trusted": 1}, id="integer"),
    ],
)
async def test_rejects_untrusted_toolkit_before_dispatch(
    mode, trust_fields, execution_boundary
):
    """仅拦截两个执行入口 / Sentinels observe dispatch while the real executor decides."""
    definition = _make_definition(POLICY_TOOLKIT)
    definition.config.pop("_toolkit_trusted")
    definition.config.update(trust_fields)
    result = await ToolkitExecutor(sandbox_mode=mode).execute(definition, "trust", {})

    spawn, load = execution_boundary
    assert (spawn.await_count, load.call_count) == (0, 0)
    assert result.success is False
    assert (
        result.error
        == "Blocked: untrusted Toolkit execution requires an isolated runtime"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["production", " StAgInG "])
async def test_deployed_environments_reject_inprocess(
    environment, monkeypatch, execution_boundary
):
    """设置环境并观测执行入口 / Set the environment and observe dispatch without replacing policy."""
    monkeypatch.setattr(settings, "APP_ENV", environment)
    definition = _make_definition(POLICY_TOOLKIT)
    result = await ToolkitExecutor(sandbox_mode="inprocess").execute(
        definition, "release-mode", {}
    )

    spawn, load = execution_boundary
    assert (spawn.await_count, load.call_count) == (0, 0)
    assert result.success is False
    assert (
        result.error == "In-process Toolkit execution is disabled outside development"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "configured_mode"),
    [
        pytest.param("unknown", "subprocess", id="explicit-unknown"),
        pytest.param("", "subprocess", id="explicit-empty"),
        pytest.param(None, "unknown", id="configured-unknown"),
    ],
)
async def test_invalid_sandbox_mode_is_rejected(
    mode, configured_mode, monkeypatch, execution_boundary
):
    """固定配置并观测执行入口 / Set the configured default and observe dispatch without replacing policy."""
    monkeypatch.setattr(toolkit_executor, "_SANDBOX_MODE", configured_mode)
    definition = _make_definition(POLICY_TOOLKIT)
    result = await ToolkitExecutor(sandbox_mode=mode).execute(
        definition, "bad-mode", {}
    )

    spawn, load = execution_boundary
    assert (spawn.await_count, load.call_count) == (0, 0)
    assert result.success is False
    assert result.error == "Unsupported Toolkit execution mode"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
async def test_trusted_toolkit_still_checks_blocked_imports(mode, execution_boundary):
    """拦截执行并用静态输入验证扫描 / Guard dispatch while the real scanner evaluates an inert import."""
    definition = _make_definition("import os\n" + POLICY_TOOLKIT)
    result = await ToolkitExecutor(sandbox_mode=mode).execute(definition, "scan", {})

    spawn, load = execution_boundary
    assert (spawn.await_count, load.call_count) == (0, 0)
    assert result.success is False
    assert result.error == "Toolkit blocked: Blocked import 'os' at line 1"


@pytest.mark.parametrize(
    ("kind", "source", "symbol"),
    [
        pytest.param(kind, source, symbol, id=f"{kind}-{symbol}")
        for symbol in (
            "__builtins__",
            "__builtin__",
            "__import__",
            "globals",
            "locals",
            "vars",
            "getattr",
        )
        for kind, source in (
            ("name", f"marker = {symbol}"),
            ("key", f"marker = {symbol!r}"),
        )
    ],
)
def test_scanner_reports_reflection_markers(kind, source, symbol):
    assert scan_toolkit_security(source) == [
        f"Blocked reflection {kind} '{symbol}' at line 1"
    ]


# --------------------------------------------------------------------------- #
# Output truncation
# --------------------------------------------------------------------------- #


class TestOutputTruncation:
    """Test output size limiting. / 测试"""

    @pytest.mark.asyncio
    async def test_output_truncated(self) -> None:
        """Large output is truncated. / 说明"""
        large_toolkit = """class Tools:
    def big(self) -> str:
        return "x" * 50000"""
        executor = ToolkitExecutor(
            max_output_size=1000,
            sandbox_mode="subprocess",
        )
        defn = _make_definition(large_toolkit, "big")
        result = await executor.execute(defn, "call_13", {})
        assert result.success
        assert len(result.output) <= 1020  # 1000 + truncation message
        assert "truncated" in result.output

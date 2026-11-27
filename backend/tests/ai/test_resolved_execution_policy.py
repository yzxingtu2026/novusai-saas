"""Test type: behavioral.

Scope: ordinary skill resolution through the default ToolSandbox executor map.
Real dependencies: SkillResolver, ToolSandbox, and both execution policies.
Mocked dependencies: subprocess creation is blocked by the fixture in refusal
tests only. The system Toolkit executes a local arithmetic fixture. No model or
tool response is mocked.
"""

import asyncio

import pytest

from app.ai.skills.resolver import SkillResolver
from app.ai.tools.sandbox import ToolSandbox
from app.ai.tools.types import ToolResult
from app.enums.agent import SkillTypeEnum
from app.enums.skill import SkillStatusEnum
from app.models.ai.skill import Skill

TOOLKIT_SOURCE = '''\
class Tools:
    def add(self, left: int, right: int) -> int:
        """Add two numbers."""
        return left + right
'''


@pytest.fixture
def reject_process_start(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: object, **kwargs: object) -> None:
        pytest.fail("A tool denied by policy attempted to start a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject)


async def _run_toolkit(is_system: bool, call_id: str) -> ToolResult:
    skill = Skill(
        id=41,
        name="Local arithmetic",
        type=SkillTypeEnum.TOOLKIT.value,
        status=SkillStatusEnum.ACTIVE.value,
        is_active=True,
        is_system=is_system,
        toolkit_content=TOOLKIT_SOURCE,
        timeout=10,
    )
    resolved = await SkillResolver().resolve(
        [skill], config_overrides={41: {"_toolkit_trusted": not is_system}}
    )
    (tool,) = resolved.tools
    assert (tool.name, tool.source_skill_id) == ("add", 41)
    assert tool.config["_toolkit_trusted"] is is_system

    return await ToolSandbox(tenant_id=2, agent_id=3).execute(
        call_id, "add", {"left": 17, "right": 25}, resolved.tools
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("reject_process_start")
async def test_tenant_toolkit_is_refused_despite_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.tools.executors.toolkit_executor._SANDBOX_MODE", "subprocess"
    )

    result = await _run_toolkit(False, "tenant-toolkit")

    assert (result.tool_call_id, result.name) == ("tenant-toolkit", "add")
    assert result.success is False
    assert result.output == ""
    assert (
        result.error
        == "Blocked: untrusted Toolkit execution requires an isolated runtime"
    )


@pytest.mark.asyncio
async def test_system_toolkit_retains_trusted_subprocess_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.tools.executors.toolkit_executor._SANDBOX_MODE", "subprocess"
    )

    result = await _run_toolkit(True, "system-toolkit")

    assert (result.tool_call_id, result.name) == ("system-toolkit", "add")
    assert result.success is True
    assert result.output == "42"
    assert result.error == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("reject_process_start")
async def test_resolved_code_skill_is_refused_before_execution() -> None:
    skill = Skill(
        id=42,
        name="Local calculation",
        type=SkillTypeEnum.CODE_EXECUTION.value,
        status=SkillStatusEnum.ACTIVE.value,
        is_active=True,
        is_system=False,
        timeout=10,
    )
    resolved = await SkillResolver().resolve([skill])
    (tool,) = resolved.tools
    assert (tool.name, tool.source_skill_id) == ("execute_code", 42)

    result = await ToolSandbox(tenant_id=2, agent_id=3).execute(
        "tenant-code", "execute_code", {"code": "print(6 * 7)"}, resolved.tools
    )

    assert (result.tool_call_id, result.name) == ("tenant-code", "execute_code")
    assert result.success is False
    assert result.output == ""
    assert result.error == "Code execution requires an isolated runtime"

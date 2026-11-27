"""Test type: structural. Exercise the real inventory and context-assembly seam.

Regression for BUG-2026-11-27-RUNTIME-INVENTORY-CONTEXT, observed before a
conversation was created in canonical smoke scenario SCENARIO-001.
The repository and grant lookups supply a synthetic agent with no bindings.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.skills.resolver import SkillResolveResult
from app.services.ai.runtime_inventory_service import RuntimeInventoryService


@pytest.mark.structural
async def test_manifest_uses_the_real_context_assembler_without_turn_arguments(
    monkeypatch,
) -> None:
    service = RuntimeInventoryService(AsyncMock())
    agent = SimpleNamespace(id=33, name="Fixture agent", owner_tenant_id=11, model=None)
    monkeypatch.setattr(service, "_resolve_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(service, "_load_kb_bindings", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service, "_load_skill_result", AsyncMock(return_value=SkillResolveResult())
    )

    manifest = await service.get_manifest(tenant_id=11, agent_id=33)

    assert (manifest["tenant_id"], manifest["agent_id"]) == (11, 33)
    assert (
        manifest["summary"]["agent_name"],
        manifest["summary"]["selection_live"],
    ) == (
        "Fixture agent",
        False,
    )
    assert manifest["tools"] == []

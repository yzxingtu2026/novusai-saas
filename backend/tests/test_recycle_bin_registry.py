from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.shared import recycle_bin_registry as registry
from app.enums.common import DeleteLevelEnum
from app.exceptions import ValidationException
from app.models.ai.agent import Agent


def test_get_module_codes_for_side_includes_periodic_tasks() -> None:
    assert "periodic_tasks" in registry.get_module_codes_for_side("admin")


def test_get_module_config_rejects_missing_module() -> None:
    with pytest.raises(ValidationException):
        registry.get_module_config("missing_module", "admin")


def test_get_module_config_rejects_wrong_side() -> None:
    with pytest.raises(ValidationException):
        registry.get_module_config("periodic_tasks", "tenant")


def test_get_delete_scope_maps() -> None:
    assert registry.get_delete_scope("admin") == DeleteLevelEnum.ADMIN.value
    assert registry.get_delete_scope("tenant") == DeleteLevelEnum.TENANT.value


def test_get_service_rejects_missing_tenant_id() -> None:
    with pytest.raises(ValidationException):
        registry.get_service("agents", "tenant", db=object(), tenant_id=None)


@pytest.mark.asyncio
async def test_serialize_global_recycle_items_includes_tenant_name() -> None:
    lookup = AsyncMock()
    lookup.execute.return_value = SimpleNamespace(all=lambda: [(12, "Tenant Twelve")])

    result = await registry.serialize_deleted_items(
        lookup, "agents", [Agent(id=71, owner_tenant_id=12)]
    )

    assert [(row["id"], row["tenant_id"], row["tenant_name"]) for row in result] == [
        (71, 12, "Tenant Twelve")
    ]

"""Behavioral: installed bundled plugins keep their runtime metadata on upgrade."""

from copy import deepcopy

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.system.plugin import Plugin
from app.plugins.loader import PluginLoader
from migrations.versions import platform_builtin_rollout_v1 as rollout
from tests.integration.storage_migration_test_support import create_model_tables
from tests.integration.storage_migration_test_support import (
    owned_storage as storage_fixture,
)

owned_storage = storage_fixture
pytestmark = [pytest.mark.behavioral, pytest.mark.asyncio]


@pytest.mark.parametrize(
    ("source", "revoke_platform", "customized"),
    [
        ("local", False, False),
        ("local", True, False),
        ("marketplace", False, False),
        ("local", False, True),
    ],
)
async def test_rollout_reconciles_bundled_manifests_and_preserves_removed_grants(
    owned_storage, source, revoke_platform, customized
):
    await create_model_tables(owned_storage, Plugin)
    loader = PluginLoader()
    originals = {}
    current = {}
    prior_grants = {}
    async with owned_storage.factory() as db:
        for name in ("storage-billing", "weather-widget", "storage-migration"):
            current[name] = loader.load_manifest(name).model_dump()
            previous = deepcopy(current[name])
            if name == "storage-migration":
                previous["extensions"]["tasks"] = []
            else:
                previous["capabilities"] = [
                    cap for cap in previous["capabilities"]
                    if cap not in {"config:read", "storage:credential_use"}
                ]
            if customized:
                previous["homepage"] = "https://custom.example.test"
            originals[name] = previous
            grants = [
                cap for cap in previous["capabilities"]
                if not (revoke_platform and cap == "platform:read")
            ]
            prior_grants[name] = grants
            db.add(
                Plugin(
                    name=name,
                    display_name=name,
                    scope=previous["scope"],
                    version=previous["version"],
                    install_source=source,
                    manifest=previous,
                    granted_capabilities=grants,
                    status="enabled",
                    config={"value": "persisted"},
                )
            )
        await db.commit()

    for action in (rollout.upgrade, rollout.upgrade, rollout.downgrade):
        def invoke(bind, action=action):
            with Operations.context(MigrationContext.configure(bind)):
                action()

        async with owned_storage.engine.begin() as conn:
            await conn.run_sync(invoke)

        async with owned_storage.factory() as db:
            plugins = (await db.scalars(select(Plugin))).all()
            assert {p.name for p in plugins} == set(originals)
            applies = source == "local" and not customized and action is rollout.upgrade
            for plugin in plugins:
                name = plugin.name
                manifest = current[name] if applies else originals[name]
                grants = prior_grants[name]
                if applies and name == "weather-widget":
                    grants = [*grants, "config:read"]
                if applies and name == "storage-billing" and not revoke_platform:
                    grants = [*grants, "storage:credential_use", "config:read"]
                assert plugin.manifest == manifest
                assert plugin.granted_capabilities == grants
                assert plugin.config == {"value": "persisted"}
                assert (plugin.status, plugin.version) == ("enabled", manifest["version"])

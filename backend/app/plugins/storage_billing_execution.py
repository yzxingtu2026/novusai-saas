"""Host-owned official billing execution / 宿主持有凭据的官方账单采集。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any

from app.core.i18n import _
from app.plugins.exceptions import PluginSecurityError
from app.plugins.host_read_facade import _redact_storage_options
from app.plugins.module_loader import load_plugin_module
from app.services.common.storage_config_resolver import StorageConfigResolver

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.plugins.host_read_facade import HostReadFacade
    from app.storage import StorageConfig


@dataclass(frozen=True, slots=True)
class StorageBillingFetchRequest:
    """Fixed provider, date and period input / 固定的服务商与账期参数。"""

    driver_code: str
    billing_date: date
    period_type: str
    period_start: date
    period_end: date

    def __post_init__(self) -> None:
        if (
            not isinstance(self.driver_code, str)
            or self.period_type not in ("daily", "monthly")
            or any(
                type(value) is not date
                for value in (self.billing_date, self.period_start, self.period_end)
            )
            or not self.period_start <= self.billing_date <= self.period_end
            or (self.period_type == "daily" and self.period_start != self.period_end)
        ):
            raise PluginSecurityError(message="Invalid storage billing request.")


class _BillingStorageView:
    """Supply private storage only inside the host call / 只在宿主调用内提供存储凭据。"""

    def __init__(self, host: HostReadFacade, storage: StorageConfig) -> None:
        self.get_enabled_storage_drivers = host.get_enabled_storage_drivers
        self.get_plugin_runtime_summary = host.get_plugin_runtime_summary
        self._storage = storage

    async def get_platform_storage_context(self) -> dict[str, Any]:
        storage = self._storage
        return {
            "storage_mode": "platform",
            "storage_config": {
                "driver": storage.driver,
                "root_path": storage.root_path,
                "options": dict(storage.options or {}),
            },
        }


async def _read_billing_prerequisites(
    db: AsyncSession, tenant_id: int, *, granted_capabilities: frozenset[str]
) -> dict[str, Any]:
    from app.plugins.context_factory import create_plugin_context
    from app.plugins.loader import PluginLoader

    bindings = load_plugin_module("storage-billing", "services.binding_service")
    if bindings is None:
        raise PluginSecurityError(message="Storage billing runtime is unavailable.")

    manifest = PluginLoader().load_manifest("storage-billing")
    context = create_plugin_context(manifest.name, manifest, db, list(granted_capabilities))
    service = bindings.StorageBillingBindingService(context)
    return _redact_storage_options(await service.get_tenant_prerequisites(tenant_id))


async def _execute_storage_billing_fetch(
    db: AsyncSession,
    request: StorageBillingFetchRequest | dict[str, Any],
    *,
    host_read: HostReadFacade,
) -> Any:
    if isinstance(request, dict):
        try:
            request = StorageBillingFetchRequest(**request)
        except TypeError:
            raise PluginSecurityError(
                message="Invalid storage billing request."
            ) from None
    if not isinstance(request, StorageBillingFetchRequest):
        raise PluginSecurityError(message="Invalid storage billing request.")

    providers = load_plugin_module("storage-billing", "providers")
    reconciliation = load_plugin_module(
        "storage-billing", "services.reconciliation_service"
    )
    constants = load_plugin_module("storage-billing", "constants")
    if providers is None or reconciliation is None or constants is None:
        raise PluginSecurityError(message="Storage billing runtime is unavailable.")

    def outcome(status: str, message: str) -> Any:
        return providers.BillingFetchResult(
            provider_code=request.driver_code,
            driver_code=request.driver_code,
            billing_date=request.billing_date,
            period_type=request.period_type,
            period_start=request.period_start,
            period_end=request.period_end,
            source_status=status,
            error_message=message,
        )

    adapter = providers.get_provider_adapter(request.driver_code)
    if adapter is None:
        return outcome(
            "skipped", _("No official billing adapter registered for this driver.")
        )

    storage = await StorageConfigResolver(db).resolve_platform_config()
    if storage.driver != request.driver_code:
        return outcome(
            "skipped",
            _("Current platform storage driver does not match this provider."),
        )

    service = reconciliation.StorageBillingReconciliationService(
        db, host_read=_BillingStorageView(host_read, storage)
    )
    profiles = service._profile_service
    profile = await profiles.get_provider_runtime_profile(request.driver_code)
    if not profile["enabled"]:
        return outcome("skipped", _("Provider profile is disabled."))
    if request.period_type not in profile["supported_period_types"]:
        return outcome(
            "not_implemented", _("Provider does not support this period type.")
        )

    validation = await profiles.validate_provider_profile(request.driver_code)
    if validation["errors"]:
        implemented = constants.get_provider_implemented_bill_sources(
            request.driver_code
        )
        status = (
            "failed" if profile.get("bill_source") in implemented else "not_implemented"
        )
        return outcome(status, " ".join(validation["errors"]))

    result = await adapter.fetch_official_bill(
        providers.BillingFetchRequest(
            driver_code=request.driver_code,
            billing_date=request.billing_date,
            period_type=request.period_type,
            period_start=request.period_start,
            period_end=request.period_end,
            profile=profile,
        )
    )
    if not isinstance(result, providers.BillingFetchResult):
        raise RuntimeError("Storage billing provider returned an invalid result.")

    return replace(
        result,
        error_message=(
            _("Official billing provider request failed.")
            if result.source_status == "failed"
            else result.error_message
        ),
        raw_payload_json=_redact_storage_options(result.raw_payload_json),
        charge_items=[
            replace(
                item,
                details_json=_redact_storage_options(item.details_json),
                tag_values=_redact_storage_options(item.tag_values),
            )
            for item in result.charge_items
        ],
    )

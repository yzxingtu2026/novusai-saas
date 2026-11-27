"""Register optional fixed-image request routes / 注册可选固定镜像请求路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import ActiveAdmin
from app.core.response import success
from app.rbac.decorators import action_create, action_read, action_update
from app.services.system.plugin_image_operation_contract import BuildContract
from app.services.system.plugin_image_operation_service import (
    ImageOperationError,
    PluginImageOperationService,
)

RequestID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class PrepareImageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build: BuildContract
    preview_token: Annotated[str, Field(max_length=4096)] = ""


def register_plugin_image_operation_routes(controller):
    async def prepare(body: PrepareImageOperation, admin: ActiveAdmin, operation):
        if body.build.operation != operation:
            raise ImageOperationError("plugin_image_operation_mismatch", status=422)
        try:
            return success(
                data=await PluginImageOperationService().prepare(
                    body.build, admin.id, body.preview_token
                )
            )
        except ImageOperationError:
            raise
        except Exception:
            raise ImageOperationError("plugin_image_preparation_unconfirmed") from None

    async def status(request_id: RequestID, admin: ActiveAdmin, operation):
        try:
            return success(
                data=await PluginImageOperationService().status(
                    request_id, admin.id, operation=operation
                )
            )
        except ImageOperationError:
            raise
        except Exception:
            raise ImageOperationError("plugin_image_status_unavailable") from None

    async def latest(admin: ActiveAdmin, operation, **plugin):
        try:
            return success(
                data=await PluginImageOperationService().latest_status(
                    admin.id, operation=operation, **plugin
                )
            )
        except ImageOperationError:
            raise
        except Exception:
            raise ImageOperationError("plugin_image_status_unavailable") from None

    router = controller.router

    @router.post("/image-installs", status_code=202)
    @action_create("action.plugin.install")
    async def prepare_image_install(
        request: Request, body: PrepareImageOperation, admin: ActiveAdmin
    ):
        return await prepare(body, admin, "install")

    @router.post("/image-upgrades", status_code=202)
    @action_update("action.plugin.upgrade")
    async def prepare_image_upgrade(
        request: Request, body: PrepareImageOperation, admin: ActiveAdmin
    ):
        return await prepare(body, admin, "upgrade")

    @router.post("/image-rollbacks", status_code=202)
    @action_update("action.plugin.rollback")
    async def prepare_image_rollback(
        request: Request, body: PrepareImageOperation, admin: ActiveAdmin
    ):
        return await prepare(body, admin, "rollback")

    @router.get("/image-installs/{request_id}")
    @action_read("action.plugin.install")
    async def image_install_status(
        request: Request, request_id: RequestID, admin: ActiveAdmin
    ):
        return await status(request_id, admin, "install")

    @router.get("/image-upgrades/{request_id}")
    @action_read("action.plugin.upgrade")
    async def image_upgrade_status(
        request: Request, request_id: RequestID, admin: ActiveAdmin
    ):
        return await status(request_id, admin, "upgrade")

    @router.get("/image-rollbacks/{request_id}")
    @action_read("action.plugin.rollback")
    async def image_rollback_status(
        request: Request, request_id: RequestID, admin: ActiveAdmin
    ):
        return await status(request_id, admin, "rollback")

    @router.get("/image-installs/{plugin_name}/latest")
    @action_read("action.plugin.install")
    async def latest_image_install(
        request: Request, plugin_name: str, admin: ActiveAdmin
    ):
        return await latest(admin, "install", plugin_name=plugin_name)

    @router.get("/{plugin_id}/image-upgrades/latest")
    @action_read("action.plugin.upgrade")
    async def latest_image_upgrade(
        request: Request, plugin_id: int, admin: ActiveAdmin
    ):
        return await latest(admin, "upgrade", plugin_id=plugin_id)

    @router.get("/{plugin_id}/image-rollbacks/latest")
    @action_read("action.plugin.rollback")
    async def latest_image_rollback(
        request: Request, plugin_id: int, admin: ActiveAdmin
    ):
        return await latest(admin, "rollback", plugin_id=plugin_id)

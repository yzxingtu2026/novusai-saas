"""Read or settle fixed-image proof through the local CLI / 本地 CLI 读取或结算固定镜像证明。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from pydantic import TypeAdapter

from app.core.logging import suppress_console_logging
from app.services.system.plugin_image_operation_contract import Identifier, Sha


async def _call(action, request_id, operator_id, **options):
    from app.core.database import close_database
    from app.services.system.plugin_image_operation_service import (
        PluginImageOperationService,
    )

    try:
        service = PluginImageOperationService()
        return await getattr(service, action)(request_id, operator_id, **options)
    finally:
        await close_database()


def _run(action, request_id, operator_id, **options):
    try:
        TypeAdapter(Identifier).validate_python(request_id, strict=True)
        for field in ("release_sha", "observation_sha"):
            if field in options:
                TypeAdapter(Sha).validate_python(options[field], strict=True)
        with suppress_console_logging():
            data = asyncio.run(_call(action, request_id, operator_id, **options))
    except Exception:
        click.echo(
            json.dumps(
                {
                    "success": False,
                    "reason": "plugin_image_command_unconfirmed",
                    "request_id": request_id,
                    "next_action": "read_request_status",
                }
            )
        )
        raise click.exceptions.Exit(1) from None
    click.echo(json.dumps({"success": True, "data": data}, ensure_ascii=False))


def _identity(command):
    command = click.option("--operator-id", required=True, type=click.IntRange(min=1))(
        command
    )
    return click.option("--request-id", required=True)(command)


def _archives(command):
    file_path = click.Path(path_type=Path, dir_okay=False)
    command = click.option("--source-archive", type=file_path)(command)
    return click.option("--target-archive", required=True, type=file_path)(command)


@click.group("image-delivery")
def image_delivery_cmd():
    """Read and record fixed-image receipts / 读取与记录固定镜像回执。"""


@image_delivery_cmd.command("status")
@_identity
def status(**options):
    _run("status", **options)


@image_delivery_cmd.command("build-contract")
@_identity
@_archives
def build_contract(**options):
    _run("build_contract", **options)


@image_delivery_cmd.command("observe-database")
@_identity
def observe_database(**options):
    _run("observe_database", **options)


@image_delivery_cmd.command("record-phase")
@_identity
@_archives
@click.option(
    "phase",
    "--phase",
    required=True,
    type=click.Choice(
        [
            "prepared",
            "old_started",
            "old_completed",
            "apply_started",
            "applied",
            "uncertain",
        ]
    ),
)
def record_phase(**options):
    _run("transition", **options)


@image_delivery_cmd.command("record-verified")
@_identity
@_archives
@click.option(
    "release_path",
    "--release",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option(
    "observation_path",
    "--observation",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--release-sha", required=True)
@click.option("--observation-sha", required=True)
def record_verified(**options):
    _run("record", **options)

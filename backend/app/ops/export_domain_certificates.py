"""Emit a private certificate snapshot for the opt-in host publisher."""

import asyncio
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout


async def export_snapshot():
    os.environ["NOVUSAI_CLI_DISABLE_FILE_LOGGING"] = "1"
    from app.core.logging import LogManager

    LogManager.init(enable_console=False, enable_file=False)
    from app.core.database import async_engine, async_session_factory
    from app.services.system.ssl_certificate_service import SslCertificateService

    try:
        async with async_session_factory() as db:
            return await SslCertificateService(db).export_publishable_certificates()
    finally:
        await async_engine.dispose()


def main():
    try:
        # The host publisher decodes all stdout as a single private JSON snapshot.
        with (
            open(os.devnull, "w", encoding="utf-8") as quiet,
            redirect_stdout(quiet),
            redirect_stderr(quiet),
        ):
            snapshot = asyncio.run(export_snapshot())
    except Exception:
        # Never serialize exception context, PEM, keys or connection credentials.
        print("domain_certificate_export_failed", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

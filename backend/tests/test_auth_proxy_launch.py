"""启动入口和可信代理 / API launch and trusted proxies."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from uvicorn import Config

from app.cli_commands import core_commands
from app.core import rate_limit
from app.core.config import settings
from app.core.rate_limit import get_request_client_ip


def test_docker_api_executes_uvicorn_with_explicit_proxy_setting() -> None:
    """Test type: structural."""
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")
    api_stage = source.split("FROM runtime AS api", 1)[1]
    api_stage = api_stage.split("FROM runtime AS worker", 1)[0]
    cmd = next(line for line in api_stage.splitlines() if line.startswith("CMD "))
    shell, flags, command = json.loads(cmd.removeprefix("CMD "))
    assert (shell, flags) == ("sh", "-ec")
    assert command.startswith("exec uvicorn app.main:app --host 0.0.0.0 --port 8000 ")
    assert '--proxy-headers --forwarded-allow-ips "${TRUSTED_PROXY_IPS:-}"' in command
    assert command.endswith(" --workers 2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowlist", "peer", "client_ip", "scheme"),
    [
        ("", "127.0.0.1", "127.0.0.1", "http"),
        ("10.42.0.0/16", "10.42.0.8", "192.0.2.21", "https"),
    ],
    ids=("direct-access", "configured-proxy"),
)
async def test_cli_passes_proxy_allowlist_to_uvicorn(
    monkeypatch, tmp_path, allowlist, peer, client_ip, scheme
) -> None:
    """Test type: behavioral."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core_commands, "_BACKEND_DIR", tmp_path)
    monkeypatch.setattr(core_commands, "_get_venv_python", lambda: "python")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", allowlist)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    launch = Mock()
    monkeypatch.setattr(core_commands, "_run_uvicorn", launch)

    result = CliRunner().invoke(
        core_commands.run_cmd,
        ["--host", "127.0.0.1", "--port", "8123", "--no-reload", "--workers", "3"],
    )

    assert result.exit_code == 0, result.output
    launch.assert_called_once()
    command = launch.call_args.args[0]
    assert command[:8] == [
        "python",
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
    ]
    assert command[-2:] == ["--workers", "3"]
    assert command.count("--forwarded-allow-ips") == 1
    forwarded_allow_ips = command[command.index("--forwarded-allow-ips") + 1]
    assert forwarded_allow_ips == allowlist

    app = FastAPI()

    @app.get("/")
    async def read_identity(request: Request):
        return {"ip": get_request_client_ip(request), "scheme": request.url.scheme}

    config = Config(
        app,
        proxy_headers="--proxy-headers" in command,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=None,
    )
    config.load()
    async with AsyncClient(
        transport=ASGITransport(app=config.loaded_app, client=(peer, 43210)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/",
            headers={"X-Forwarded-For": "192.0.2.21", "X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    assert response.json() == {"ip": client_ip, "scheme": scheme}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forwarded_clients", "expected_ip"),
    [
        (
            ("2001:db8::21", "2001:0DB8:0000:0000:0000:0000:0000:0021"),
            "2001:db8::21",
        ),
        (("invalid-address", "another-invalid-address"), None),
    ],
    ids=("equivalent-ipv6", "malformed-identity"),
)
async def test_uvicorn_client_rewrites_share_the_expected_login_counter(
    monkeypatch, forwarded_clients, expected_ip
) -> None:
    """Test type: behavioral."""
    allowlist = "10.42.0.0/16"
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", allowlist)
    digest = sha256((expected_ip or "unknown").encode()).hexdigest()
    expected_key = f"auth:login_rate:{digest}"
    counts = {expected_key: 9}

    async def increment(key: str, ttl: int) -> int:
        assert ttl == 60
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    monkeypatch.setattr(rate_limit, "cache_increment", increment)
    app = FastAPI()

    @app.post("/login")
    async def login(request: Request):
        limited = await rate_limit.check_login_rate_limit(request)
        if limited is not None:
            return limited
        return {"ip": get_request_client_ip(request), "scheme": request.url.scheme}

    config = Config(
        app, proxy_headers=True, forwarded_allow_ips=allowlist, log_config=None
    )
    config.load()
    async with AsyncClient(
        transport=ASGITransport(app=config.loaded_app, client=("10.42.0.8", 43210)),
        base_url="http://test",
        headers={"X-Forwarded-Proto": "https"},
    ) as client:
        admitted = await client.post(
            "/login", headers={"X-Forwarded-For": forwarded_clients[0]}
        )
        limited = await client.post(
            "/login", headers={"X-Forwarded-For": forwarded_clients[1]}
        )

    assert admitted.status_code == 200, admitted.text
    assert admitted.json() == {"ip": expected_ip, "scheme": "https"}
    assert limited.status_code == 429, limited.text
    assert limited.json()["code"] == 4290
    assert counts == {expected_key: 11}

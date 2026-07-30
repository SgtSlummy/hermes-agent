from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_api_server_assembles_occult_runtime_when_enabled(monkeypatch):
    occult_http = MagicMock()
    monkeypatch.setattr(
        "agent.occult.runtime.build_occult_http",
        lambda _config: occult_http,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"occult": {"enabled": True}},
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )

    adapter = await runner._create_adapter(
        Platform.API_SERVER,
        PlatformConfig(enabled=True),
    )

    assert isinstance(adapter, APIServerAdapter)
    assert adapter._occult_http is occult_http


@pytest.mark.asyncio
async def test_occult_runtime_assembly_logs_safe_actionable_error(
    monkeypatch,
    caplog,
):
    def fail(_config):
        raise RuntimeError("Ollama is unavailable")

    monkeypatch.setattr("agent.occult.runtime.build_occult_http", fail)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"occult": {"enabled": True}},
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )

    adapter = await runner._create_adapter(
        Platform.API_SERVER,
        PlatformConfig(enabled=True),
    )

    assert isinstance(adapter, APIServerAdapter)
    assert "Ollama is unavailable" in caplog.text

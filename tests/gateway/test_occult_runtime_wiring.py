from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_api_server_assembles_occult_runtime_when_enabled(monkeypatch):
    occult_http = MagicMock()
    captured = {}

    def build(config):
        captured.update(config)
        return occult_http

    monkeypatch.setattr(
        "agent.occult.runtime.build_occult_http",
        build,
    )
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
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
    assert captured == {"occult": {"enabled": True}}


@pytest.mark.asyncio
async def test_occult_runtime_assembly_logs_safe_actionable_error(
    monkeypatch,
    caplog,
):
    def fail(_config):
        raise RuntimeError("Ollama is unavailable")

    monkeypatch.setattr("agent.occult.runtime.build_occult_http", fail)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"occult": {"enabled": True}},
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )

    with pytest.raises(
        RuntimeError,
        match="enabled Occult runtime could not be assembled",
    ):
        await runner._create_adapter(
            Platform.API_SERVER,
            PlatformConfig(enabled=True),
        )

    assert "Ollama is unavailable" in caplog.text


@pytest.mark.asyncio
async def test_api_server_builds_real_occult_runtime_in_temporary_profile(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "occult": {
                "enabled": True,
                "contract_version": "1.0.0",
                "local_base_url": "http://127.0.0.1:11434/v1",
                "local_model": "qwen2.5:3b",
                "provider_timeout_seconds": 30,
                "maximum_concurrent_requests": 2,
            }
        },
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
    assert adapter._occult_http is not None
    assert (tmp_path / "occult" / "readings.db").exists()
    await adapter._occult_http.aclose()

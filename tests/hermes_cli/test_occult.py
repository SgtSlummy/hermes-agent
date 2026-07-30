import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from hermes_cli.occult import (
    OccultCLIError,
    _api_base_url,
    _open_occult_url,
    cmd_occult,
    run_tui_occult_command,
)


@pytest.fixture(autouse=True)
def _isolate_path_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def read(self):
        return json.dumps(self.payload).encode()

    def __iter__(self):
        yield from self.payload


def test_occult_cli_requires_virtual_token(monkeypatch):
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    with pytest.raises(OccultCLIError, match="required"):
        cmd_occult(SimpleNamespace(occult_action="agents"))


def test_occult_api_url_comes_from_api_server_config(monkeypatch):
    monkeypatch.delenv("OCCULT_API_URL", raising=False)
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(
            platforms={
                Platform.API_SERVER: SimpleNamespace(
                    extra={"host": "::1", "port": 9443}
                )
            }
        ),
    )

    assert _api_base_url() == "http://[::1]:9443"


def test_occult_api_url_uses_effective_gateway_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("OCCULT_API_URL", raising=False)
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  api_server:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      host: 127.0.0.1\n"
        "      port: 8642\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    monkeypatch.setenv("API_SERVER_HOST", "127.0.0.2")
    monkeypatch.setenv("API_SERVER_PORT", "9555")

    assert _api_base_url() == "http://127.0.0.2:9555"


@pytest.mark.parametrize("extra", ["invalid", {"port": "not-a-number"}])
def test_occult_api_url_normalizes_malformed_config(monkeypatch, extra):
    monkeypatch.delenv("OCCULT_API_URL", raising=False)
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(
            platforms={Platform.API_SERVER: SimpleNamespace(extra=extra)}
        ),
    )

    with pytest.raises(OccultCLIError, match="platforms.api_server.extra"):
        _api_base_url()


def test_occult_transport_disables_ambient_proxies(monkeypatch):
    seen = {}

    class Opener:
        @staticmethod
        def open(request, *, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response({"ok": True})

    def build_opener(*handlers):
        proxy_handler = next(
            handler
            for handler in handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        )
        seen["proxies"] = proxy_handler.proxies
        seen["redirects_disabled"] = any(
            handler.__class__.__name__ == "_NoOccultRedirect"
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    request = urllib.request.Request("http://127.0.0.1:8642/v1/occult/decks")

    with _open_occult_url(request, timeout=9) as response:
        assert json.loads(response.read()) == {"ok": True}

    assert seen == {
        "url": "http://127.0.0.1:8642/v1/occult/decks",
        "timeout": 9,
        "proxies": {},
        "redirects_disabled": True,
    }


def test_remote_occult_transport_preserves_ambient_proxies(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy.internal:8443"},
    )

    class Opener:
        @staticmethod
        def open(request, *, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response({"ok": True})

    def build_opener(*handlers):
        proxy_handler = next(
            handler
            for handler in handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        )
        seen["proxies"] = proxy_handler.proxies
        seen["redirects_disabled"] = any(
            handler.__class__.__name__ == "_NoOccultRedirect"
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    request = urllib.request.Request(
        "https://occult.internal.example/v1/occult/decks"
    )

    with _open_occult_url(request, timeout=9) as response:
        assert json.loads(response.read()) == {"ok": True}

    assert seen == {
        "url": "https://occult.internal.example/v1/occult/decks",
        "timeout": 9,
        "proxies": {"https": "http://proxy.internal:8443"},
        "redirects_disabled": True,
    }


def test_occult_status_uses_authenticated_real_endpoints(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.headers, timeout))
        if request.full_url.endswith("major-arcana"):
            return _Response({"data": [{"agent_id": "occult.major.magician"}]})
        return _Response({"data": [{"card_id": "minor.swords.king.test"}]})

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(SimpleNamespace(occult_action="status"))

    output = json.loads(capsys.readouterr().out)
    assert output["agents"][0]["agent_id"] == "occult.major.magician"
    assert output["routes"][0]["card_id"] == "minor.swords.king.test"
    assert len(seen) == 2
    assert all(
        headers["Authorization"] == "Bearer occult_private" for _, headers, _ in seen
    )


def test_occult_invoke_builds_versioned_contract(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"occult": {"provider_timeout_seconds": 240}},
    )
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data.decode()))
        captured["client_timeout"] = timeout
        return _Response({"output": "done"})

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(
        SimpleNamespace(
            occult_action="invoke",
            agent="occult.major.magician",
            message="Build it.",
            card="minor.swords.king.test",
            orientation="upright",
            mode="local_first",
            allow_paid=False,
            maximum_cost=0.0,
            maximum_fallbacks=1,
            idempotency_key="cli-test",
        )
    )

    assert captured["contract_version"] == "1.0.0"
    assert captured["routing"]["mode"] == "manual"
    assert captured["minor_arcana"] == "minor.swords.king.test"
    assert captured["client_timeout"] == 270
    assert json.loads(capsys.readouterr().out)["output"] == "done"


def test_occult_reading_events_follow_streams_terminal_event(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"occult": {"provider_timeout_seconds": 600}},
    )
    event = {
        "sequence": 2,
        "event_type": "reading.completed",
        "reading_id": "reading_test",
    }

    def urlopen(request, timeout):
        assert request.headers["Accept"] == "text/event-stream"
        assert request.full_url.endswith("/events?stream=1")
        assert timeout == 630
        return _Response([
            b"id: 2\n",
            b"event: reading.completed\n",
            f"data: {json.dumps(event)}\n".encode(),
            b"\n",
        ])

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(
        SimpleNamespace(
            occult_action="reading-events",
            reading_id="reading_test",
            follow=True,
        )
    )

    assert json.loads(capsys.readouterr().out) == event


def test_tui_occult_command_runs_real_reading_control(monkeypatch):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.method
        return _Response({"reading_id": "reading_test", "state": "cancelled"})

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    output = run_tui_occult_command("reading-cancel reading_test")

    assert seen["url"].endswith("/v1/occult/readings/reading_test/cancel")
    assert seen["method"] == "POST"
    assert json.loads(output)["state"] == "cancelled"


def test_tui_occult_command_rejects_unknown_or_missing_arguments():
    with pytest.raises(OccultCLIError, match="usage"):
        run_tui_occult_command("reading-status")
    with pytest.raises(OccultCLIError, match="usage"):
        run_tui_occult_command("summon")


def test_tui_occult_command_defaults_to_status(monkeypatch):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")

    def urlopen(request, timeout):
        return _Response({"data": []})

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    assert json.loads(run_tui_occult_command("")) == {"agents": [], "routes": []}


def test_occult_token_issue_uses_separate_admin_credential(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_ADMIN_KEY", "admin-private")
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    captured = {}

    def urlopen(request, timeout):
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.data.decode())
        return _Response({
            "token_id": "council",
            "token": "occult_returned_once",
            "secret_once": True,
        })

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(
        SimpleNamespace(
            occult_action="token-issue",
            token_id="council",
            allow_agent=["occult.major.magician"],
            allow_route=["minor.pentacles.ace.local.test"],
            allow_tool=[],
            allow_memory=["project"],
            requests_per_minute=10,
            maximum_budget=0.0,
            expires_at=None,
        )
    )

    assert captured["headers"]["X-occult-admin-key"] == "admin-private"
    assert captured["payload"]["allowed_memory_namespaces"] == ["project"]
    assert json.loads(capsys.readouterr().out)["secret_once"] is True


def test_occult_token_admin_requires_dedicated_key(monkeypatch):
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.setenv("OCCULT_API_KEY", "user-token-is-not-admin")
    with pytest.raises(OccultCLIError, match="OCCULT_ADMIN_KEY"):
        cmd_occult(SimpleNamespace(occult_action="token-list"))


def test_occult_pairings_filters_by_encoded_agent(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        return _Response({"data": []})

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(
        SimpleNamespace(
            occult_action="pairings",
            agent="occult.major.magician:test",
        )
    )

    assert seen["url"].endswith(
        "/v1/occult/pairings?agent_id=occult.major.magician%3Atest"
    )
    assert json.loads(capsys.readouterr().out) == {"data": []}


def test_occult_deck_validation_encodes_identifier(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        return _Response({
            "deck_id": "occult.deck.development:test",
            "valid": True,
        })

    monkeypatch.setattr("hermes_cli.occult._open_occult_url", urlopen)
    cmd_occult(
        SimpleNamespace(
            occult_action="deck-validate",
            deck_id="occult.deck.development:test",
        )
    )

    assert seen["url"].endswith(
        "/v1/occult/decks/occult.deck.development%3Atest/validate"
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True

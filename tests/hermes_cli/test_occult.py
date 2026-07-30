import json
from types import SimpleNamespace

import pytest

from hermes_cli.occult import OccultCLIError, cmd_occult, run_tui_occult_command


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


def test_occult_status_uses_authenticated_real_endpoints(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.headers, timeout))
        if request.full_url.endswith("major-arcana"):
            return _Response({"data": [{"agent_id": "occult.major.magician"}]})
        return _Response({"data": [{"card_id": "minor.swords.king.test"}]})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
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
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data.decode()))
        return _Response({"output": "done"})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
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
    assert json.loads(capsys.readouterr().out)["output"] == "done"


def test_occult_reading_events_follow_streams_terminal_event(monkeypatch, capsys):
    monkeypatch.setenv("OCCULT_API_KEY", "occult_private")
    event = {
        "sequence": 2,
        "event_type": "reading.completed",
        "reading_id": "reading_test",
    }

    def urlopen(request, timeout):
        assert request.headers["Accept"] == "text/event-stream"
        assert request.full_url.endswith("/events?stream=1")
        return _Response([
            b"id: 2\n",
            b"event: reading.completed\n",
            f"data: {json.dumps(event)}\n".encode(),
            b"\n",
        ])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
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

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
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

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert json.loads(run_tui_occult_command("")) == {"agents": [], "routes": []}

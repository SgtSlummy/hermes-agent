import json
from types import SimpleNamespace

import pytest

from hermes_cli.occult import OccultCLIError, cmd_occult


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


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

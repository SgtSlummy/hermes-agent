import json
import urllib.request
from pathlib import Path

import pytest

from agent.occult.runtime import (
    STARTER_AGENT_IDS,
    STARTER_CARD_ID,
    STARTER_DECK_ID,
    OccultRuntimeError,
    build_occult_http,
    normalize_loopback_openai_url,
)
from agent.occult.virtual_tokens import VirtualTokenPolicy


def _config(model: str = "qwen2.5:3b"):
    return {
        "occult": {
            "enabled": True,
            "contract_version": "1.0.0",
            "local_base_url": "http://127.0.0.1:11434/v1",
            "local_model": model,
            "provider_timeout_seconds": 30,
            "maximum_concurrent_requests": 2,
        }
    }


def test_disabled_runtime_has_no_side_effects(tmp_path: Path):
    assert build_occult_http({"occult": {"enabled": False}}, home=tmp_path) is None
    assert not (tmp_path / "occult").exists()


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/v1",
        "http://127.0.0.1:11434/other",
        "http://user:password@127.0.0.1:11434/v1",
    ],
)
def test_local_provider_rejects_non_loopback_or_unsafe_urls(value: str):
    with pytest.raises(OccultRuntimeError):
        normalize_loopback_openai_url(value)


def test_runtime_installs_signed_starters_route_and_deck(tmp_path: Path):
    http = build_occult_http(
        _config(),
        environ={"OCCULT_ADMIN_KEY": "a" * 32},
        home=tmp_path,
    )
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="test-local",
            allowed_agent_ids=frozenset(STARTER_AGENT_IDS),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )

    assert {item["agent_id"] for item in http.service.agents(token)} == set(
        STARTER_AGENT_IDS
    )
    assert [item["card_id"] for item in http.service.routes(token)] == [STARTER_CARD_ID]
    assert [item["deck_id"] for item in http.service.decks(token)] == [STARTER_DECK_ID]
    assert http.service.validate_deck(token, STARTER_DECK_ID)["valid"] is True
    assert http.admin_key_digest is not None
    http.close()


def test_real_runtime_path_composes_agent_and_invokes_local_adapter(
    tmp_path: Path,
    monkeypatch,
):
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "assembled"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1},
            }).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="invoke-local",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    result = http.service.invoke(
        token,
        {
            "contract_version": "1.0.0",
            "invocation_id": "inv-runtime",
            "idempotency_key": "runtime-1",
            "agent_id": "occult.major.magician",
            "orientation": "upright",
            "input": {"message": "Build the test."},
            "required_capabilities": ["text"],
            "routing": {
                "mode": "local_only",
                "free_only": True,
                "local_only": True,
                "maximum_fallbacks": 0,
                "maximum_cost_usd": 0,
            },
            "deck_id": STARTER_DECK_ID,
            "spread_id": None,
            "metadata": {},
        },
    )

    assert result["output"] == "assembled"
    assert result["route"]["selected_card_id"] == STARTER_CARD_ID
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    message = captured["payload"]["messages"][0]["content"]
    assert "# Major Arcana" in message
    assert "The Magician" in message
    assert "# Task\nBuild the test." in message
    http.close()

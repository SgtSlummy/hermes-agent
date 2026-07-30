import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application

from agent.occult.http import OccultHTTPAdapter
from agent.occult.readings import ReadingStore
from tests.agent.occult.test_service import _service


@pytest.mark.asyncio
async def test_openai_models_are_authenticated_major_arcana(tmp_path: Path):
    service, token, _seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.get("/v1/models")
        assert unauthorized.status == 401
        assert (await unauthorized.json())["error"]["code"] == "invalid_api_key"

        response = await client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload == {
            "object": "list",
            "data": [
                {
                    "id": "occult.major.magician",
                    "object": "model",
                    "created": 0,
                    "owned_by": "occult-system",
                }
            ],
        }


@pytest.mark.asyncio
async def test_openai_chat_maps_model_messages_and_occult_override(tmp_path: Path):
    service, token, seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "chatcmpl_test",
            },
            json={
                "model": "occult.major.magician",
                "messages": [
                    {"role": "system", "content": "Stay concise."},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Build it."}],
                    },
                ],
                "user": "local-client",
                "occult": {
                    "minor_arcana": "minor.pentacles.ace.local.test",
                    "orientation": "reversed",
                },
            },
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["id"] == "chatcmpl_test"
        assert payload["object"] == "chat.completion"
        assert payload["model"] == "occult.major.magician"
        assert payload["choices"] == [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "completed"},
                "finish_reason": "stop",
            }
        ]
        assert payload["usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
        assert payload["occult"]["route_summary"]["selected_card_id"] == (
            "minor.pentacles.ace.local.test"
        )
        assert "SYSTEM:\nStay concise." in seen[0]
        assert "USER:\nBuild it." in seen[0]


@pytest.mark.asyncio
async def test_openai_chat_stream_is_valid_single_result_sse(tmp_path: Path):
    service, token, _seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "occult.major.magician",
                "messages": [{"role": "user", "content": "Build it."}],
                "stream": True,
            },
        )

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        frames = [
            line.removeprefix("data: ")
            for line in (await response.text()).splitlines()
            if line.startswith("data: ")
        ]
        assert frames[-1] == "[DONE]"
        first = json.loads(frames[0])
        final = json.loads(frames[1])
        assert first["choices"][0]["delta"]["content"] == "completed"
        assert final["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openai_chat_rejects_unsupported_fields_before_provider_call(
    tmp_path: Path,
):
    service, token, seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "occult.major.magician",
                "messages": [{"role": "user", "content": "Build it."}],
                "tools": [],
            },
        )

        assert response.status == 400
        error = (await response.json())["error"]
        assert error["param"] == "tools"
        assert error["code"] == "unsupported_parameter"
        assert seen == []

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application
from openai import AsyncOpenAI

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


@pytest.mark.asyncio
async def test_openai_responses_maps_text_input_and_occult_override(tmp_path: Path):
    service, token, seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "resp_test",
            },
            json={
                "model": "occult.major.magician",
                "instructions": "Stay concise.",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Build it."},
                        ],
                    }
                ],
                "metadata": {"client": "local"},
                "occult": {
                    "minor_arcana": "minor.pentacles.ace.local.test",
                    "orientation": "reversed",
                },
            },
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["id"] == "resp_test"
        assert payload["object"] == "response"
        assert payload["status"] == "completed"
        assert payload["store"] is False
        assert payload["model"] == "occult.major.magician"
        assert payload["metadata"] == {"client": "local"}
        assert payload["output"][0]["type"] == "message"
        assert payload["output"][0]["content"][0] == {
            "type": "output_text",
            "text": "completed",
            "annotations": [],
            "logprobs": [],
        }
        assert payload["usage"]["total_tokens"] == 12
        assert payload["occult"]["route_summary"]["selected_card_id"] == (
            "minor.pentacles.ace.local.test"
        )
        assert "INSTRUCTIONS:\nStay concise." in seen[0]
        assert "USER:\nBuild it." in seen[0]


@pytest.mark.asyncio
async def test_openai_responses_stream_emits_documented_lifecycle(tmp_path: Path):
    service, token, _seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "occult.major.magician",
                "input": "Build it.",
                "stream": True,
            },
        )

        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        lines = (await response.text()).splitlines()
        event_types = [
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        ]
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: ")
        ]
        assert event_types == [
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert [event["sequence_number"] for event in payloads] == list(range(9))
        assert payloads[4]["delta"] == "completed"
        assert payloads[-1]["response"]["status"] == "completed"
        assert payloads[-1]["response"]["usage"]["total_tokens"] == 12


@pytest.mark.asyncio
async def test_openai_responses_rejects_unsupported_tools_before_provider_call(
    tmp_path: Path,
):
    service, token, seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "occult.major.magician",
                "input": "Build it.",
                "tools": [],
            },
        )

        assert response.status == 400
        error = (await response.json())["error"]
        assert error["param"] == "tools"
        assert error["code"] == "unsupported_parameter"
        assert seen == []


@pytest.mark.asyncio
async def test_openai_responses_requires_virtual_token(tmp_path: Path):
    service, _token, seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "occult.major.magician", "input": "Build it."},
        )

        assert response.status == 401
        assert (await response.json())["error"]["code"] == "invalid_api_key"
        assert seen == []


@pytest.mark.asyncio
async def test_openai_sdk_parses_occult_response_and_stream(tmp_path: Path):
    service, token, _seen = _service()
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)

    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key=token,
            base_url=str(server.make_url("/v1/")),
        ) as client:
            response = await client.responses.create(
                model="occult.major.magician",
                input="Build it.",
            )
            assert response.status == "completed"
            assert response.output_text == "completed"
            assert response.usage is not None
            assert response.usage.total_tokens == 12

            stream = await client.responses.create(
                model="occult.major.magician",
                input="Build it.",
                stream=True,
            )
            event_types = [event.type async for event in stream]
            assert event_types[-1] == "response.completed"
            assert "response.output_text.delta" in event_types


@pytest.mark.asyncio
async def test_occult_native_routes_coexist_with_gateway_openai_routes(
    tmp_path: Path,
):
    service, token, _seen = _service()
    app = Application()

    async def gateway_models(_request):
        return Application

    async def gateway_chat(_request):
        return Application

    async def gateway_responses(_request):
        return Application

    app.router.add_get("/v1/models", gateway_models)
    app.router.add_post("/v1/chat/completions", gateway_chat)
    app.router.add_post("/v1/responses", gateway_responses)
    adapter = OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db"))
    adapter.register(app, include_openai_compat=False)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/v1/occult/major-arcana",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        assert (await response.json())["data"][0]["agent_id"] == (
            "occult.major.magician"
        )


@pytest.mark.asyncio
async def test_openai_chat_maps_capacity_pressure_to_server_error(tmp_path: Path):
    service, token, seen = _service(maximum_concurrent_requests=1)
    app = Application()
    OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db")).register(app)
    assert service.router._capacity.acquire(timeout=0)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "occult.major.magician",
                    "messages": [{"role": "user", "content": "Build it."}],
                },
            )
            error = (await response.json())["error"]
    finally:
        service.router._capacity.release()

    assert response.status == 503
    assert error == {
        "message": "Occult provider capacity is temporarily exhausted",
        "type": "server_error",
        "param": None,
        "code": "occult_capacity_exhausted",
    }
    assert seen == []

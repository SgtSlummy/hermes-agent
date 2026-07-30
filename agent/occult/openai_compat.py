"""OpenAI-compatible text surfaces backed by the authenticated Occult runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from aiohttp import web

from agent.occult.contracts import OCCULT_CONTRACT_VERSION, OccultContractError
from agent.occult.mythos import FailureKind, MythosRoutingError, RouterBusy
from agent.occult.service import OccultService
from agent.occult.virtual_tokens import VirtualTokenError, VirtualTokenRateLimitError


_SUPPORTED_ROLES = frozenset({"assistant", "developer", "system", "user"})
_SUPPORTED_FIELDS = frozenset({"messages", "model", "occult", "stream", "user"})
_SUPPORTED_RESPONSE_FIELDS = frozenset({
    "input",
    "instructions",
    "metadata",
    "model",
    "occult",
    "stream",
})


@dataclass(slots=True)
class OccultOpenAIAdapter:
    """Map deliberately small OpenAI text-generation subsets to Occult."""

    service: OccultService
    worker: Callable[..., Awaitable[Any]] = asyncio.to_thread

    def register(self, app: web.Application) -> None:
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.chat_completions)
        app.router.add_post("/v1/responses", self.responses)

    async def models(self, request: web.Request) -> web.Response:
        token = self._bearer(request)
        if token is None:
            return self._error(
                "Occult virtual token is required",
                401,
                error_type="authentication_error",
                code="invalid_api_key",
            )
        try:
            agents = self.service.agents(token)
            return web.json_response({
                "object": "list",
                "data": [
                    {
                        "id": agent["agent_id"],
                        "object": "model",
                        "created": 0,
                        "owned_by": "occult-system",
                    }
                    for agent in agents
                ],
            })
        except VirtualTokenRateLimitError as exc:
            return self._error(
                str(exc),
                429,
                error_type="rate_limit_error",
                code="rate_limit_exceeded",
            )
        except VirtualTokenError as exc:
            return self._error(
                str(exc),
                403,
                error_type="permission_error",
                code="permission_denied",
            )
        except Exception:
            return self._error(
                "Occult model listing failed",
                500,
                error_type="server_error",
                code="occult_models_failed",
            )

    async def chat_completions(self, request: web.Request) -> web.StreamResponse:
        token = self._bearer(request)
        if token is None:
            return self._error(
                "Occult virtual token is required",
                401,
                error_type="authentication_error",
                code="invalid_api_key",
            )
        try:
            parsed = await request.json()
            if not isinstance(parsed, Mapping):
                raise _OpenAIRequestError("request body must contain an object")
            payload = dict(parsed)
            unknown = sorted(set(payload) - _SUPPORTED_FIELDS)
            if unknown:
                raise _OpenAIRequestError(
                    "unsupported Chat Completions parameters: " + ", ".join(unknown),
                    param=unknown[0],
                    code="unsupported_parameter",
                )
            model = self._required_string(payload, "model")
            message = self._render_messages(payload.get("messages"))
            extension = self._occult_extension(payload.get("occult"))
            stream = payload.get("stream", False)
            if not isinstance(stream, bool):
                raise _OpenAIRequestError("stream must be a boolean", param="stream")

            invocation_id = self._invocation_id(request)
            invocation: dict[str, Any] = {
                "contract_version": OCCULT_CONTRACT_VERSION,
                "invocation_id": invocation_id,
                "idempotency_key": (
                    request.headers.get("Idempotency-Key") or invocation_id
                ),
                "agent_id": model,
                "orientation": extension.get("orientation", "upright"),
                "input": {"message": message},
                "required_capabilities": ["text"],
                "routing": extension.get(
                    "routing",
                    {
                        "mode": "local_first",
                        "free_only": True,
                        "local_only": False,
                        "maximum_fallbacks": 2,
                        "maximum_cost_usd": 0,
                    },
                ),
                "metadata": self._metadata(payload),
            }
            if "deck_id" in extension:
                invocation["deck_id"] = extension["deck_id"]
            manual_card_id = extension.get("minor_arcana")
            result = await self.worker(
                self.service.invoke,
                token,
                invocation,
                manual_card_id=manual_card_id,
            )
            if stream:
                return await self._stream_response(request, model, result)
            return web.json_response(self._completion(model, result))
        except _OpenAIRequestError as exc:
            return self._error(
                str(exc),
                400,
                param=exc.param,
                code=exc.code,
            )
        except VirtualTokenRateLimitError as exc:
            return self._error(
                str(exc),
                429,
                error_type="rate_limit_error",
                code="rate_limit_exceeded",
            )
        except VirtualTokenError as exc:
            return self._error(
                str(exc),
                403,
                error_type="permission_error",
                code="permission_denied",
            )
        except (OccultContractError, ValueError) as exc:
            return self._error(str(exc), 400)
        except RouterBusy:
            return self._error(
                "Occult provider capacity is temporarily exhausted",
                503,
                error_type="server_error",
                code="occult_capacity_exhausted",
            )
        except MythosRoutingError as exc:
            return self._routing_error(exc)
        except Exception:
            return self._error(
                "Occult chat completion failed",
                500,
                error_type="server_error",
                code="occult_completion_failed",
            )

    async def responses(self, request: web.Request) -> web.StreamResponse:
        token = self._bearer(request)
        if token is None:
            return self._error(
                "Occult virtual token is required",
                401,
                error_type="authentication_error",
                code="invalid_api_key",
            )
        try:
            parsed = await request.json()
            if not isinstance(parsed, Mapping):
                raise _OpenAIRequestError("request body must contain an object")
            payload = dict(parsed)
            unknown = sorted(set(payload) - _SUPPORTED_RESPONSE_FIELDS)
            if unknown:
                raise _OpenAIRequestError(
                    "unsupported Responses API parameters: " + ", ".join(unknown),
                    param=unknown[0],
                    code="unsupported_parameter",
                )
            model = self._required_string(payload, "model")
            instructions = self._optional_string(payload, "instructions")
            message = self._render_response_input(payload.get("input"))
            if instructions is not None:
                message = f"INSTRUCTIONS:\n{instructions}\n\n{message}"
            extension = self._occult_extension(payload.get("occult"))
            stream = payload.get("stream", False)
            if not isinstance(stream, bool):
                raise _OpenAIRequestError("stream must be a boolean", param="stream")
            metadata = self._response_metadata(payload.get("metadata"))
            invocation_id = self._invocation_id(request, prefix="resp")
            invocation: dict[str, Any] = {
                "contract_version": OCCULT_CONTRACT_VERSION,
                "invocation_id": invocation_id,
                "idempotency_key": (
                    request.headers.get("Idempotency-Key") or invocation_id
                ),
                "agent_id": model,
                "orientation": extension.get("orientation", "upright"),
                "input": {"message": message},
                "required_capabilities": ["text"],
                "routing": extension.get(
                    "routing",
                    {
                        "mode": "local_first",
                        "free_only": True,
                        "local_only": False,
                        "maximum_fallbacks": 2,
                        "maximum_cost_usd": 0,
                    },
                ),
                "metadata": metadata,
            }
            if "deck_id" in extension:
                invocation["deck_id"] = extension["deck_id"]
            result = await self.worker(
                self.service.invoke,
                token,
                invocation,
                manual_card_id=extension.get("minor_arcana"),
            )
            response_object = self._responses_object(
                model,
                result,
                instructions=instructions,
                metadata=metadata,
            )
            if stream:
                return await self._stream_responses(request, response_object)
            return web.json_response(response_object)
        except _OpenAIRequestError as exc:
            return self._error(
                str(exc),
                400,
                param=exc.param,
                code=exc.code,
            )
        except VirtualTokenRateLimitError as exc:
            return self._error(
                str(exc),
                429,
                error_type="rate_limit_error",
                code="rate_limit_exceeded",
            )
        except VirtualTokenError as exc:
            return self._error(
                str(exc),
                403,
                error_type="permission_error",
                code="permission_denied",
            )
        except (OccultContractError, ValueError) as exc:
            return self._error(str(exc), 400)
        except RouterBusy:
            return self._error(
                "Occult provider capacity is temporarily exhausted",
                503,
                error_type="server_error",
                code="occult_capacity_exhausted",
            )
        except MythosRoutingError as exc:
            return self._routing_error(exc)
        except Exception:
            return self._error(
                "Occult response generation failed",
                500,
                error_type="server_error",
                code="occult_response_failed",
            )

    @classmethod
    def _routing_error(cls, error: MythosRoutingError) -> web.Response:
        kinds = {kind for _, kind in error.failures}
        transient = {
            FailureKind.RATE_LIMIT,
            FailureKind.TIMEOUT,
            FailureKind.UNAVAILABLE,
            FailureKind.UNKNOWN,
        }
        if FailureKind.INVALID_REQUEST in kinds:
            return cls._error(
                "Occult provider rejected the request",
                400,
                code="invalid_request",
            )
        if FailureKind.AUTHENTICATION in kinds:
            return cls._error(
                "Occult provider authentication failed",
                502,
                error_type="server_error",
                code="provider_authentication_failed",
            )
        if FailureKind.INVALID_RESPONSE in kinds:
            return cls._error(
                "Occult provider returned an invalid response",
                502,
                error_type="server_error",
                code="invalid_provider_response",
            )
        if kinds and kinds.issubset(transient):
            return cls._error(
                "Occult provider is temporarily unavailable",
                503,
                error_type="server_error",
                code="occult_provider_unavailable",
            )
        return cls._error(
            "Occult provider rejected the invocation",
            502,
            error_type="server_error",
            code="occult_provider_failed",
        )

    @staticmethod
    def _required_string(payload: Mapping[str, Any], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise _OpenAIRequestError(
                f"{name} must be a non-empty string",
                param=name,
            )
        return value

    @staticmethod
    def _optional_string(payload: Mapping[str, Any], name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise _OpenAIRequestError(
                f"{name} must be a non-empty string",
                param=name,
            )
        return value

    @classmethod
    def _render_messages(cls, value: Any) -> str:
        if not isinstance(value, list) or not value:
            raise _OpenAIRequestError(
                "messages must contain at least one message",
                param="messages",
            )
        transcript: list[str] = []
        for index, message in enumerate(value):
            if not isinstance(message, Mapping):
                raise _OpenAIRequestError(
                    "each message must be an object",
                    param=f"messages.{index}",
                )
            role = message.get("role")
            if role not in _SUPPORTED_ROLES:
                raise _OpenAIRequestError(
                    "unsupported message role",
                    param=f"messages.{index}.role",
                    code="unsupported_value",
                )
            content = cls._text_content(
                message.get("content"),
                param=f"messages.{index}.content",
            )
            transcript.append(f"{str(role).upper()}:\n{content}")
        return "\n\n".join(transcript)

    @staticmethod
    def _text_content(value: Any, *, param: str) -> str:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            parts: list[str] = []
            for part in value:
                if (
                    not isinstance(part, Mapping)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                    or not part["text"].strip()
                ):
                    raise _OpenAIRequestError(
                        "only non-empty text content parts are supported",
                        param=param,
                        code="unsupported_content_type",
                    )
                parts.append(part["text"])
            return "\n".join(parts)
        raise _OpenAIRequestError(
            "message content must be non-empty text",
            param=param,
        )

    @classmethod
    def _render_response_input(cls, value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return f"USER:\n{value}"
        if not isinstance(value, list) or not value:
            raise _OpenAIRequestError(
                "input must be non-empty text or a non-empty message list",
                param="input",
            )
        transcript: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise _OpenAIRequestError(
                    "each input item must be a message object",
                    param=f"input.{index}",
                )
            item_type = item.get("type", "message")
            if item_type != "message":
                raise _OpenAIRequestError(
                    "only message input items are supported",
                    param=f"input.{index}.type",
                    code="unsupported_content_type",
                )
            role = item.get("role")
            if role not in _SUPPORTED_ROLES:
                raise _OpenAIRequestError(
                    "unsupported input message role",
                    param=f"input.{index}.role",
                    code="unsupported_value",
                )
            content = cls._response_text_content(
                item.get("content"),
                param=f"input.{index}.content",
            )
            transcript.append(f"{str(role).upper()}:\n{content}")
        return "\n\n".join(transcript)

    @staticmethod
    def _response_text_content(value: Any, *, param: str) -> str:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            parts: list[str] = []
            for part in value:
                if (
                    not isinstance(part, Mapping)
                    or part.get("type") not in {"input_text", "output_text"}
                    or not isinstance(part.get("text"), str)
                    or not part["text"].strip()
                ):
                    raise _OpenAIRequestError(
                        "only non-empty input_text and output_text parts are supported",
                        param=param,
                        code="unsupported_content_type",
                    )
                parts.append(part["text"])
            return "\n".join(parts)
        raise _OpenAIRequestError(
            "input message content must be non-empty text",
            param=param,
        )

    @staticmethod
    def _occult_extension(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise _OpenAIRequestError("occult must be an object", param="occult")
        allowed = {"deck_id", "minor_arcana", "orientation", "routing"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _OpenAIRequestError(
                "unsupported Occult parameters: " + ", ".join(unknown),
                param=f"occult.{unknown[0]}",
                code="unsupported_parameter",
            )
        extension = dict(value)
        for name in ("deck_id", "minor_arcana", "orientation"):
            if name in extension and (
                not isinstance(extension[name], str) or not extension[name].strip()
            ):
                raise _OpenAIRequestError(
                    f"occult.{name} must be a non-empty string",
                    param=f"occult.{name}",
                )
        if "routing" in extension and not isinstance(extension["routing"], Mapping):
            raise _OpenAIRequestError(
                "occult.routing must be an object",
                param="occult.routing",
            )
        return extension

    @staticmethod
    def _metadata(payload: Mapping[str, Any]) -> dict[str, str]:
        user = payload.get("user")
        if user is None:
            return {}
        if not isinstance(user, str) or not user.strip():
            raise _OpenAIRequestError("user must be a non-empty string", param="user")
        return {"openai_user": user}

    @staticmethod
    def _response_metadata(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if (
            not isinstance(value, Mapping)
            or len(value) > 16
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or not isinstance(item, str)
                or len(item) > 512
                for key, item in value.items()
            )
        ):
            raise _OpenAIRequestError(
                "metadata must contain at most 16 string key-value pairs",
                param="metadata",
            )
        return dict(value)

    @staticmethod
    def _invocation_id(request: web.Request, *, prefix: str = "chatcmpl") -> str:
        supplied = request.headers.get("X-Request-ID", "").strip()
        if supplied and len(supplied) <= 128:
            return supplied
        return f"{prefix}_{secrets.token_urlsafe(18)}"

    @staticmethod
    def _completion(model: str, result: Mapping[str, Any]) -> dict[str, Any]:
        usage = result["usage"]
        return {
            "id": result["invocation_id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result["output"]},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage["input_tokens"],
                "completion_tokens": usage["output_tokens"],
                "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            },
            "occult": {"route_summary": result["route"]},
        }

    @staticmethod
    def _responses_object(
        model: str,
        result: Mapping[str, Any],
        *,
        instructions: str | None,
        metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        created = int(time.time())
        response_id = str(result["invocation_id"])
        message_id = f"msg_{response_id.removeprefix('resp_')}"
        usage = result["usage"]
        output_text = {
            "type": "output_text",
            "text": result["output"],
            "annotations": [],
            "logprobs": [],
        }
        output_message = {
            "id": message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [output_text],
        }
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "completed_at": created,
            "background": False,
            "error": None,
            "incomplete_details": None,
            "instructions": instructions,
            "max_output_tokens": None,
            "max_tool_calls": None,
            "model": model,
            "output": [output_message],
            "parallel_tool_calls": False,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "none",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": {
                "input_tokens": usage["input_tokens"],
                "input_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                },
                "output_tokens": usage["output_tokens"],
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            },
            "metadata": dict(metadata),
            "occult": {"route_summary": result["route"]},
        }

    async def _stream_response(
        self,
        request: web.Request,
        model: str,
        result: Mapping[str, Any],
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        created = int(time.time())
        chunks: Sequence[Mapping[str, Any]] = (
            {
                "id": result["invocation_id"],
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": result["output"],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": result["invocation_id"],
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
        try:
            for chunk in chunks:
                data = json.dumps(chunk, separators=(",", ":"), sort_keys=True)
                await response.write(f"data: {data}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                pass
        return response

    async def _stream_responses(
        self,
        request: web.Request,
        completed: Mapping[str, Any],
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        final_response = dict(completed)
        output_message = dict(final_response["output"][0])
        output_text = dict(output_message["content"][0])
        in_progress = {
            **final_response,
            "status": "in_progress",
            "completed_at": None,
            "output": [],
            "usage": None,
        }
        events: Sequence[tuple[str, Mapping[str, Any]]] = (
            (
                "response.created",
                {"type": "response.created", "response": in_progress},
            ),
            (
                "response.in_progress",
                {"type": "response.in_progress", "response": in_progress},
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        **output_message,
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": output_message["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        **output_text,
                        "text": "",
                        "logprobs": [],
                    },
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": output_message["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": output_text["text"],
                    "logprobs": [],
                },
            ),
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": output_message["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": output_text["text"],
                    "logprobs": [],
                },
            ),
            (
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": output_message["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": output_text,
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": output_message,
                },
            ),
            (
                "response.completed",
                {"type": "response.completed", "response": final_response},
            ),
        )
        try:
            for sequence_number, (event_type, event) in enumerate(events):
                payload = {**event, "sequence_number": sequence_number}
                data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                await response.write(f"event: {event_type}\ndata: {data}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                pass
        return response

    @staticmethod
    def _bearer(request: web.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        return token or None

    @staticmethod
    def _error(
        message: str,
        status: int,
        *,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        code: str = "invalid_request",
    ) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": param,
                    "code": code,
                }
            },
            status=status,
        )


class _OpenAIRequestError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        code: str = "invalid_request",
    ) -> None:
        super().__init__(message)
        self.param = param
        self.code = code


__all__ = ["OccultOpenAIAdapter"]

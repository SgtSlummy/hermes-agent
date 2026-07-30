"""OpenAI-compatible chat surface backed by the authenticated Occult runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from aiohttp import web

from agent.occult.contracts import OCCULT_CONTRACT_VERSION, OccultContractError
from agent.occult.service import OccultService
from agent.occult.virtual_tokens import VirtualTokenError


_SUPPORTED_ROLES = frozenset({"assistant", "developer", "system", "user"})
_SUPPORTED_FIELDS = frozenset({"messages", "model", "occult", "stream", "user"})


@dataclass(slots=True)
class OccultOpenAIAdapter:
    """Map a deliberately small OpenAI Chat Completions subset to Occult."""

    service: OccultService

    def register(self, app: web.Application) -> None:
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._chat_completions)

    async def _models(self, request: web.Request) -> web.Response:
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
            return web.json_response(
                {
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
                }
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

    async def _chat_completions(self, request: web.Request) -> web.StreamResponse:
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
            result = await asyncio.to_thread(
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
        except VirtualTokenError as exc:
            return self._error(
                str(exc),
                403,
                error_type="permission_error",
                code="permission_denied",
            )
        except (OccultContractError, ValueError) as exc:
            return self._error(str(exc), 400)
        except Exception:
            return self._error(
                "Occult chat completion failed",
                500,
                error_type="server_error",
                code="occult_completion_failed",
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
    def _invocation_id(request: web.Request) -> str:
        supplied = request.headers.get("X-Request-ID", "").strip()
        if supplied and len(supplied) <= 128:
            return supplied
        return f"chatcmpl_{secrets.token_urlsafe(18)}"

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

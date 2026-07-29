"""aiohttp route binder for authenticated Occult-native surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from aiohttp import web

from agent.occult.contracts import (
    OCCULT_CONTRACT_VERSION,
    OccultContractError,
)
from agent.occult.readings import (
    CouncilNodeRequest,
    CouncilNodeResult,
    ReadingError,
    ReadingNode,
    ReadingPlan,
    ReadingStore,
)
from agent.occult.service import OccultService
from agent.occult.virtual_tokens import VirtualTokenError, VirtualTokenPolicy


@dataclass(slots=True)
class OccultHTTPAdapter:
    service: OccultService
    readings: ReadingStore
    reading_executor: Callable[[CouncilNodeRequest], CouncilNodeResult] | None = None
    admin_key_digest: bytes | None = None

    def register(self, app: web.Application) -> None:
        app.router.add_get("/v1/occult/major-arcana", self._agents)
        app.router.add_get("/v1/occult/minor-arcana", self._routes)
        app.router.add_post("/v1/occult/invoke", self._invoke)
        app.router.add_post("/v1/occult/readings", self._create_reading)
        app.router.add_get("/v1/occult/readings/{reading_id}", self._reading_status)
        app.router.add_get(
            "/v1/occult/readings/{reading_id}/events", self._reading_events
        )
        app.router.add_post(
            "/v1/occult/readings/{reading_id}/resume", self._resume_reading
        )
        app.router.add_post(
            "/v1/occult/readings/{reading_id}/cancel", self._cancel_reading
        )
        if self.admin_key_digest is not None:
            app.router.add_get("/v1/occult/admin/tokens", self._admin_tokens)
            app.router.add_post("/v1/occult/admin/tokens", self._admin_issue_token)
            app.router.add_post(
                "/v1/occult/admin/tokens/{token_id}/revoke",
                self._admin_revoke_token,
            )

    async def _agents(self, request: web.Request) -> web.Response:
        return await self._call(
            request, lambda token, _payload: {"data": self.service.agents(token)}
        )

    async def _routes(self, request: web.Request) -> web.Response:
        return await self._call(
            request, lambda token, _payload: {"data": self.service.routes(token)}
        )

    async def _invoke(self, request: web.Request) -> web.Response:
        token = self._bearer(request)
        if token is None:
            return self._error("Occult virtual token is required", 401)
        try:
            parsed = await request.json()
        except Exception:
            return self._bridge_error(
                "unknown",
                "OCCULT_INVALID_REQUEST",
                "request body must be valid JSON",
                400,
            )
        if not isinstance(parsed, Mapping):
            return self._bridge_error(
                "unknown",
                "OCCULT_INVALID_REQUEST",
                "request body must contain an object",
                400,
            )
        payload = dict(parsed)
        invocation_id = str(payload.get("invocation_id", "unknown"))

        def invoke() -> Mapping[str, Any]:
            invocation = dict(payload)
            manual_card_id = invocation.pop("minor_arcana", None)
            result = self.service.invoke(
                token,
                invocation,
                manual_card_id=(
                    str(manual_card_id) if manual_card_id is not None else None
                ),
            )
            return {
                "contract_version": result["contract_version"],
                "invocation_id": result["invocation_id"],
                "status": "completed",
                "summary": result["output"],
                "route_summary": result["route"],
                "artifacts": [],
                "error": None,
            }

        try:
            result = await asyncio.to_thread(invoke)
            return web.json_response(result)
        except VirtualTokenError as exc:
            return self._bridge_error(
                invocation_id,
                "OCCULT_FORBIDDEN",
                str(exc),
                403,
            )
        except (OccultContractError, ValueError) as exc:
            return self._bridge_error(
                invocation_id,
                "OCCULT_INVALID_REQUEST",
                str(exc),
                400,
            )
        except Exception:
            return self._bridge_error(
                invocation_id,
                "OCCULT_INVOCATION_FAILED",
                "Occult invocation failed",
                500,
                retryable=True,
            )

    async def _create_reading(self, request: web.Request) -> web.Response:
        def create(token: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            policy = self.service.token_authority.policy(token)
            nodes = tuple(
                ReadingNode(
                    node_id=str(node["node_id"]),
                    agent_id=str(node["agent_id"]),
                    task=str(node["task"]),
                    depends_on=tuple(node.get("depends_on", ())),
                )
                for node in payload.get("nodes", ())
            )
            requested_agents = {node.agent_id for node in nodes}
            if policy.allowed_agent_ids and not requested_agents.issubset(
                policy.allowed_agent_ids
            ):
                raise VirtualTokenError("virtual token does not allow requested agents")
            plan = ReadingPlan(
                spread_id=str(payload.get("spread_id", "")),
                nodes=nodes,
            )
            reading_id = self.readings.create(
                plan,
                idempotency_key=str(payload.get("idempotency_key", "")),
                contract_version=str(payload.get("contract_version", "")),
            )
            return self.readings.status(reading_id)

        return await self._call(request, create, require_json=True, status=202)

    async def _reading_status(self, request: web.Request) -> web.Response:
        return await self._call(
            request,
            lambda token, _payload: self._authorized_reading(
                token, request.match_info["reading_id"], "status"
            ),
        )

    async def _reading_events(self, request: web.Request) -> web.StreamResponse:
        if "text/event-stream" in request.headers.get(
            "Accept", ""
        ) or request.query.get("stream", "").lower() in {"1", "true", "yes"}:
            return await self._stream_reading_events(request)
        return await self._call(
            request,
            lambda token, _payload: {
                "data": self._authorized_reading(
                    token, request.match_info["reading_id"], "events"
                )
            },
        )

    async def _stream_reading_events(self, request: web.Request) -> web.StreamResponse:
        token = self._bearer(request)
        if token is None:
            return self._error("Occult virtual token is required", 401)
        reading_id = request.match_info["reading_id"]
        cursor_text = request.headers.get("Last-Event-ID") or request.query.get("after")
        try:
            cursor = -1 if cursor_text is None else int(cursor_text)
            if cursor_text is not None and cursor < 0:
                raise ValueError
        except ValueError:
            return self._error("invalid reading event cursor", 400)
        try:
            self._authorized_reading(token, reading_id, "status")
        except VirtualTokenError as exc:
            return self._error(str(exc), 403)
        except ReadingError as exc:
            return self._error(str(exc), 400)

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
        try:
            while True:
                status = self._authorized_reading(token, reading_id, "status")
                pending = self.readings.events(
                    reading_id,
                    after_sequence=None if cursor < 0 else cursor,
                )
                for event in pending:
                    cursor = int(event["sequence"])
                    payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
                    frame = (
                        f"id: {cursor}\n"
                        f"event: {event['event_type']}\n"
                        f"data: {payload}\n\n"
                    )
                    await response.write(frame.encode("utf-8"))

                if status["state"] in {"cancelled", "completed", "failed"}:
                    break
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except VirtualTokenError:
            # Authentication is checked on every poll. If a token expires or is
            # revoked after headers are sent, close the stream without leaking
            # policy details into an untrusted response body.
            pass
        finally:
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                pass
        return response

    async def _resume_reading(self, request: web.Request) -> web.Response:
        if self.reading_executor is None:
            return self._error("reading executor is unavailable", 503)
        return await self._call(
            request,
            lambda token, _payload: self._authorized_reading(
                token, request.match_info["reading_id"], "resume"
            ),
            worker_thread=True,
        )

    async def _cancel_reading(self, request: web.Request) -> web.Response:
        return await self._call(
            request,
            lambda token, _payload: self._authorized_reading(
                token, request.match_info["reading_id"], "cancel"
            ),
        )

    async def _admin_tokens(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return self._error("Occult administrator credential is required", 401)
        return web.json_response({"data": self.service.token_authority.statuses()})

    async def _admin_issue_token(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return self._error("Occult administrator credential is required", 401)
        try:
            parsed = await request.json()
            if not isinstance(parsed, Mapping):
                raise ValueError("request body must contain an object")
            policy = self._token_policy(parsed)
            plaintext = self.service.token_authority.issue(policy)
            return web.json_response(
                {
                    "token_id": policy.token_id,
                    "token": plaintext,
                    "secret_once": True,
                    "status": self.service.token_authority.status(policy.token_id),
                },
                status=201,
            )
        except (VirtualTokenError, ValueError) as exc:
            return self._error(str(exc), 400)
        except Exception:
            return self._error("Occult token issuance failed", 500)

    async def _admin_revoke_token(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return self._error("Occult administrator credential is required", 401)
        token_id = request.match_info["token_id"]
        try:
            self.service.token_authority.revoke(token_id)
            return web.json_response(self.service.token_authority.status(token_id))
        except VirtualTokenError as exc:
            return self._error(str(exc), 400)
        except Exception:
            return self._error("Occult token revocation failed", 500)

    def _authorized_reading(self, token: str, reading_id: str, operation: str) -> Any:
        policy = self.service.token_authority.policy(token)
        status = self.readings.status(reading_id)
        requested_agents = {node["agent_id"] for node in status["nodes"]}
        if policy.allowed_agent_ids and not requested_agents.issubset(
            policy.allowed_agent_ids
        ):
            raise VirtualTokenError("virtual token does not allow requested reading")
        if operation == "status":
            return status
        if operation == "events":
            return self.readings.events(reading_id)
        if operation == "cancel":
            return self.readings.cancel(reading_id)
        if operation == "resume" and self.reading_executor is not None:
            return self.readings.resume(reading_id, self.reading_executor)
        raise ReadingError("unsupported reading operation")

    def _admin_authorized(self, request: web.Request) -> bool:
        if self.admin_key_digest is None:
            return False
        supplied = request.headers.get("X-Occult-Admin-Key", "")
        digest = hashlib.sha256(supplied.encode("utf-8")).digest()
        return secrets.compare_digest(self.admin_key_digest, digest)

    @staticmethod
    def digest_admin_key(plaintext: str) -> bytes:
        if len(plaintext) < 32:
            raise ValueError("Occult administrator key must be at least 32 characters")
        return hashlib.sha256(plaintext.encode("utf-8")).digest()

    @staticmethod
    def _token_policy(payload: Mapping[str, Any]) -> VirtualTokenPolicy:
        allowed_fields = {
            "token_id",
            "allowed_agent_ids",
            "allowed_card_ids",
            "allowed_tools",
            "allowed_memory_namespaces",
            "requests_per_minute",
            "maximum_budget_usd",
            "expires_at",
        }
        unknown = set(payload) - allowed_fields
        if unknown:
            raise ValueError(
                "unknown virtual token fields: " + ", ".join(sorted(unknown))
            )

        def string_set(name: str, *, required: bool = False) -> frozenset[str]:
            value = payload.get(name, ())
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 256
                for item in value
            ):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(value) > 256:
                raise ValueError(f"{name} contains too many entries")
            if required and not value:
                raise ValueError(f"{name} requires at least one entry")
            return frozenset(value)

        token_id = payload.get("token_id")
        if not isinstance(token_id, str) or not token_id.strip() or len(token_id) > 128:
            raise ValueError("token_id must be a non-empty string")
        requests_per_minute = payload.get("requests_per_minute", 60)
        maximum_budget_usd = payload.get("maximum_budget_usd", 0.0)
        expires_at = payload.get("expires_at")
        return VirtualTokenPolicy(
            token_id=token_id,
            allowed_agent_ids=string_set("allowed_agent_ids", required=True),
            allowed_card_ids=string_set("allowed_card_ids", required=True),
            allowed_tools=string_set("allowed_tools"),
            allowed_memory_namespaces=string_set("allowed_memory_namespaces"),
            requests_per_minute=requests_per_minute,
            maximum_budget_usd=maximum_budget_usd,
            expires_at=expires_at,
        )

    async def _call(
        self,
        request: web.Request,
        callback: Callable[[str, Mapping[str, Any]], Any],
        *,
        require_json: bool = False,
        worker_thread: bool = False,
        status: int = 200,
    ) -> web.Response:
        token = self._bearer(request)
        if token is None:
            return self._error("Occult virtual token is required", 401)
        payload: Mapping[str, Any] = {}
        if require_json:
            try:
                parsed = await request.json()
            except Exception:
                return self._error("request body must be valid JSON", 400)
            if not isinstance(parsed, Mapping):
                return self._error("request body must contain an object", 400)
            payload = parsed
        try:
            if worker_thread:
                result = await asyncio.to_thread(callback, token, payload)
            else:
                result = callback(token, payload)
            return web.json_response(result, status=status)
        except VirtualTokenError as exc:
            return self._error(str(exc), 403)
        except (OccultContractError, ReadingError, ValueError) as exc:
            return self._error(str(exc), 400)
        except Exception:
            return self._error("Occult operation failed", 500)

    @staticmethod
    def _bearer(request: web.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        return token or None

    @staticmethod
    def _error(message: str, status: int) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "message": message,
                    "type": "occult_error",
                    "redacted": True,
                }
            },
            status=status,
        )

    @staticmethod
    def _bridge_error(
        invocation_id: str,
        code: str,
        message: str,
        status: int,
        *,
        retryable: bool = False,
    ) -> web.Response:
        return web.json_response(
            {
                "contract_version": OCCULT_CONTRACT_VERSION,
                "invocation_id": invocation_id,
                "status": "failed",
                "summary": "",
                "route_summary": None,
                "artifacts": [],
                "error": {
                    "contract_version": OCCULT_CONTRACT_VERSION,
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "redacted": True,
                },
            },
            status=status,
        )


__all__ = ["OccultHTTPAdapter"]

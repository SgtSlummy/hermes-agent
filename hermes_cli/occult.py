"""Thin CLI client for an explicitly enabled Occult HTTP surface."""

from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

from agent.occult.contracts import OCCULT_CONTRACT_VERSION


class OccultCLIError(RuntimeError):
    pass


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url = os.getenv("OCCULT_API_URL", "http://127.0.0.1:8642").rstrip("/")
    token = os.getenv("OCCULT_API_KEY", "").strip()
    if not token:
        raise OccultCLIError("OCCULT_API_KEY is required")
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            message = error.get("error", {}).get("message", "request failed")
        except Exception:
            message = "request failed"
        raise OccultCLIError(f"Occult API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Occult API unavailable: {exc}") from None


def _admin_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    base_url = os.getenv("OCCULT_API_URL", "http://127.0.0.1:8642").rstrip("/")
    admin_key = os.getenv("OCCULT_ADMIN_KEY", "").strip()
    if not admin_key:
        raise OccultCLIError("OCCULT_ADMIN_KEY is required")
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={
            "X-Occult-Admin-Key": admin_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            message = error.get("error", {}).get("message", "request failed")
        except Exception:
            message = "request failed"
        raise OccultCLIError(f"Occult API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Occult API unavailable: {exc}") from None


def _stream_events(reading_id: str) -> Iterator[dict[str, Any]]:
    base_url = os.getenv("OCCULT_API_URL", "http://127.0.0.1:8642").rstrip("/")
    token = os.getenv("OCCULT_API_KEY", "").strip()
    if not token:
        raise OccultCLIError("OCCULT_API_KEY is required")
    request = urllib.request.Request(
        base_url + f"/v1/occult/readings/{reading_id}/events?stream=1",
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if isinstance(event, dict):
                    yield event
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            message = error.get("error", {}).get("message", "request failed")
        except Exception:
            message = "request failed"
        raise OccultCLIError(f"Occult API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Occult API unavailable: {exc}") from None


def _reading_request(action: str, reading_id: str) -> Any:
    suffix = {
        "reading-status": "",
        "reading-events": "/events",
        "reading-resume": "/resume",
        "reading-cancel": "/cancel",
    }[action]
    method = "POST" if action in {"reading-resume", "reading-cancel"} else "GET"
    return _request(method, f"/v1/occult/readings/{reading_id}{suffix}")


def run_tui_occult_command(argument: str) -> str:
    """Run a bounded Occult inspector/control from the existing TUI composer."""

    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise OccultCLIError(f"invalid Occult command: {exc}") from None
    if not parts:
        parts = ["status"]
    action = parts[0].lower()
    if action == "status":
        if len(parts) != 1:
            raise OccultCLIError("usage: /occult status")
        result = {
            "agents": _request("GET", "/v1/occult/major-arcana")["data"],
            "routes": _request("GET", "/v1/occult/minor-arcana")["data"],
        }
    elif action == "agents":
        if len(parts) != 1:
            raise OccultCLIError("usage: /occult agents")
        result = _request("GET", "/v1/occult/major-arcana")
    elif action == "routes":
        if len(parts) != 1:
            raise OccultCLIError("usage: /occult routes")
        result = _request("GET", "/v1/occult/minor-arcana")
    elif action in {
        "reading-status",
        "reading-events",
        "reading-resume",
        "reading-cancel",
    }:
        if len(parts) != 2:
            raise OccultCLIError(f"usage: /occult {action} <reading-id>")
        result = _reading_request(action, parts[1])
    else:
        raise OccultCLIError(
            "usage: /occult "
            "[status|agents|routes|reading-status|reading-events|"
            "reading-resume|reading-cancel]"
        )
    return json.dumps(result, indent=2, sort_keys=True)


def cmd_occult(args) -> None:
    action = args.occult_action
    if action == "token-list":
        result = _admin_request("GET", "/v1/occult/admin/tokens")
    elif action == "token-issue":
        payload = {
            "token_id": args.token_id,
            "allowed_agent_ids": args.allow_agent,
            "allowed_card_ids": args.allow_route,
            "allowed_tools": args.allow_tool,
            "allowed_memory_namespaces": args.allow_memory,
            "requests_per_minute": args.requests_per_minute,
            "maximum_budget_usd": args.maximum_budget,
            "expires_at": args.expires_at,
        }
        result = _admin_request("POST", "/v1/occult/admin/tokens", payload)
    elif action == "token-revoke":
        result = _admin_request(
            "POST", f"/v1/occult/admin/tokens/{args.token_id}/revoke"
        )
    elif action == "status":
        result = {
            "agents": _request("GET", "/v1/occult/major-arcana")["data"],
            "routes": _request("GET", "/v1/occult/minor-arcana")["data"],
        }
    elif action == "agents":
        result = _request("GET", "/v1/occult/major-arcana")
    elif action == "routes":
        result = _request("GET", "/v1/occult/minor-arcana")
    elif action == "invoke":
        invocation_id = "inv_" + uuid.uuid4().hex
        payload = {
            "contract_version": OCCULT_CONTRACT_VERSION,
            "invocation_id": invocation_id,
            "idempotency_key": args.idempotency_key or invocation_id,
            "agent_id": args.agent,
            "orientation": args.orientation,
            "input": {"message": args.message},
            "required_capabilities": ["text"],
            "routing": {
                "mode": "manual" if args.card else args.mode,
                "free_only": not args.allow_paid,
                "local_only": args.mode == "local_only",
                "maximum_fallbacks": args.maximum_fallbacks,
                "maximum_cost_usd": args.maximum_cost,
            },
            "metadata": {},
        }
        if args.card:
            payload["minor_arcana"] = args.card
        result = _request("POST", "/v1/occult/invoke", payload)
    elif action == "reading-events" and args.follow:
        for event in _stream_events(args.reading_id):
            print(json.dumps(event, sort_keys=True))
        return
    elif action in {
        "reading-status",
        "reading-events",
        "reading-resume",
        "reading-cancel",
    }:
        result = _reading_request(action, args.reading_id)
    else:
        raise OccultCLIError("an Occult action is required")
    print(json.dumps(result, indent=2, sort_keys=True))


__all__ = ["OccultCLIError", "cmd_occult", "run_tui_occult_command"]

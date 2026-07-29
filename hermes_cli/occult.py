"""Thin CLI client for an explicitly enabled Occult HTTP surface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
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


def cmd_occult(args) -> None:
    action = args.occult_action
    if action == "status":
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
    elif action in {"reading-status", "reading-resume", "reading-cancel"}:
        suffix = {
            "reading-status": "",
            "reading-resume": "/resume",
            "reading-cancel": "/cancel",
        }[action]
        method = "GET" if action == "reading-status" else "POST"
        result = _request(method, f"/v1/occult/readings/{args.reading_id}{suffix}")
    else:
        raise OccultCLIError("an Occult action is required")
    print(json.dumps(result, indent=2, sort_keys=True))


__all__ = ["OccultCLIError", "cmd_occult"]

"""Thin CLI client for the explicitly enabled Tarot Router HTTP surface."""

from __future__ import annotations

import json
import ipaddress
import os
import secrets
import shlex
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

from agent.occult.contracts import OCCULT_CONTRACT_VERSION
from agent.occult.runtime import (
    DEFAULT_OLLAMA_BASE_URL,
    STARTER_AGENT_IDS,
    STARTER_CARD_ID,
    STARTER_DECK_ID,
    OccultRuntimeError,
    build_occult_http,
    discover_ollama_models,
    normalize_loopback_openai_url,
    validate_ollama_chat_model,
)
from agent.occult.virtual_tokens import VirtualTokenError, VirtualTokenPolicy
from hermes_cli import config as cli_config


class OccultCLIError(RuntimeError):
    pass


class _NoOccultRedirect(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding local credentials to redirects."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def initialize_occult(
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Initialize a secure local-only Tarot Router profile and starter deck."""

    if cli_config.is_managed():
        raise OccultCLIError(
            "Tarot Router initialization is unavailable in managed mode; "
            "ask the system administrator to configure the profile"
        )
    try:
        config = dict(cli_config.read_raw_config_strict())
    except ValueError as exc:
        raise OccultCLIError(
            f"Tarot Router initialization refused to overwrite existing configuration: {exc}"
        ) from None
    configured_occult = config.get("occult")
    if configured_occult is not None and not isinstance(configured_occult, dict):
        raise OccultCLIError("existing occult configuration must be an object")
    occult = dict(configured_occult or {})
    staged_env = cli_config.load_env()

    def credential(name: str) -> str:
        return str(os.getenv(name, "") or staged_env.get(name, "")).strip()

    try:
        provider_timeout_seconds = int(occult.get("provider_timeout_seconds", 120))
    except (TypeError, ValueError):
        raise OccultCLIError(
            "occult.provider_timeout_seconds must be a whole number from 1 to 600"
        ) from None
    if not 1 <= provider_timeout_seconds <= 600:
        raise OccultCLIError(
            "occult.provider_timeout_seconds must be a whole number from 1 to 600"
        )
    try:
        requested_base_url = (
            base_url
            if base_url is not None
            else occult.get("local_base_url", DEFAULT_OLLAMA_BASE_URL)
        )
        canonical_url = normalize_loopback_openai_url(str(requested_base_url))
        models = discover_ollama_models(canonical_url)
    except OccultRuntimeError as exc:
        raise OccultCLIError(str(exc)) from None
    selected_model = str(
        model if model is not None else occult.get("local_model") or models[0]
    ).strip()
    if selected_model not in models:
        raise OccultCLIError(f"model is not installed in Ollama: {selected_model}")
    try:
        validate_ollama_chat_model(
            canonical_url,
            selected_model,
            timeout_seconds=provider_timeout_seconds,
        )
    except OccultRuntimeError as exc:
        raise OccultCLIError(str(exc)) from None

    occult.update({
        "enabled": True,
        "contract_version": OCCULT_CONTRACT_VERSION,
        "local_base_url": canonical_url,
        "local_model": selected_model,
        "provider_timeout_seconds": provider_timeout_seconds,
        "maximum_concurrent_requests": int(
            occult.get("maximum_concurrent_requests", 4)
        ),
        "invocation_result_retention_seconds": float(
            occult.get("invocation_result_retention_seconds", 7 * 24 * 60 * 60)
        ),
        "invocation_identity_retention_seconds": float(
            occult.get("invocation_identity_retention_seconds", 28 * 24 * 60 * 60)
        ),
        "maximum_invocation_entries": int(
            occult.get("maximum_invocation_entries", 10_000)
        ),
        "reading_retention_seconds": float(
            occult.get("reading_retention_seconds", 30 * 24 * 60 * 60)
        ),
        "reading_identity_retention_seconds": float(
            occult.get(
                "reading_identity_retention_seconds",
                120 * 24 * 60 * 60,
            )
        ),
        "maximum_readings": int(occult.get("maximum_readings", 10_000)),
    })
    config["occult"] = occult
    platforms = dict(config.get("platforms") or {})
    api_server = dict(platforms.get("api_server") or {})
    api_server["enabled"] = True
    extra = dict(api_server.get("extra") or {})
    extra.update({"host": "127.0.0.1", "port": 8642})
    api_server["extra"] = extra
    platforms["api_server"] = api_server
    config["platforms"] = platforms

    admin_key = credential("OCCULT_ADMIN_KEY")
    if not admin_key:
        admin_key = "occult_admin_" + secrets.token_urlsafe(32)
    elif not admin_key.isascii() or len(admin_key) < 32:
        raise OccultCLIError(
            "OCCULT_ADMIN_KEY must be ASCII and at least 32 characters; "
            "unset it to generate a new key"
        )
    api_server_key = credential("API_SERVER_KEY")
    if not api_server_key:
        api_server_key = "hermes_api_" + secrets.token_urlsafe(32)
    elif not api_server_key.isascii() or len(api_server_key) < 32:
        raise OccultCLIError(
            "API_SERVER_KEY must be ASCII and at least 32 characters; "
            "unset it to generate a new key"
        )
    runtime_env = dict(os.environ)
    runtime_env["OCCULT_ADMIN_KEY"] = admin_key
    try:
        http = build_occult_http(config, environ=runtime_env)
    except OccultRuntimeError as exc:
        raise OccultCLIError(str(exc)) from None
    if http is None:
        raise OccultCLIError("Tarot Router runtime did not enable")

    token = credential("OCCULT_API_KEY")
    token_created = False
    issued_token_id: str | None = None
    if token:
        try:
            policy = http.service.token_authority.policy(token)
            if not (
                frozenset(STARTER_AGENT_IDS) <= policy.allowed_agent_ids
                and STARTER_CARD_ID in policy.allowed_card_ids
            ):
                token = ""
        except VirtualTokenError:
            token = ""
    if not token:
        existing_ids = {
            str(item["token_id"]) for item in http.service.token_authority.statuses()
        }
        token_id = "local-default"
        if token_id in existing_ids:
            token_id = "local-" + uuid.uuid4().hex[:12]
        token = http.service.token_authority.issue(
            VirtualTokenPolicy(
                token_id=token_id,
                allowed_agent_ids=frozenset(STARTER_AGENT_IDS),
                allowed_card_ids=frozenset({STARTER_CARD_ID}),
                allowed_tools=frozenset(),
                allowed_memory_namespaces=frozenset({"project", "agent", "reading"}),
                requests_per_minute=30,
                maximum_budget_usd=0.0,
            )
        )
        token_created = True
        issued_token_id = token_id

    try:
        try:
            cli_config.save_env_values({
                "API_SERVER_KEY": api_server_key,
                "OCCULT_ADMIN_KEY": admin_key,
                "OCCULT_API_KEY": token,
            })
        except Exception:
            if issued_token_id is not None:
                http.service.token_authority.discard(issued_token_id)
            raise
        cli_config.save_config(config)
    finally:
        http.close()

    return {
        "enabled": True,
        "provider": "ollama-local",
        "model": selected_model,
        "card_id": STARTER_CARD_ID,
        "deck_id": STARTER_DECK_ID,
        "agents": list(STARTER_AGENT_IDS),
        "token_created": token_created,
        "config_path": str(cli_config.get_config_path()),
        "secrets_path": str(cli_config.get_env_path()),
        "next": "restart the Hermes gateway, then run 'hermes tarot status'",
    }


def _api_base_url() -> str:
    """Resolve the Occult endpoint from config, with a legacy env override."""

    override = os.getenv("OCCULT_API_URL", "").strip()
    if override:
        return override.rstrip("/")
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    api_server = config.platforms.get(Platform.API_SERVER)
    extra = api_server.extra if api_server is not None else {}
    if not isinstance(extra, dict):
        raise OccultCLIError("platforms.api_server.extra must be a mapping")
    host = str(extra.get("host", "127.0.0.1"))
    try:
        port = int(extra.get("port", 8642))
    except (TypeError, ValueError):
        raise OccultCLIError(
            "platforms.api_server.extra.port must be an integer"
        ) from None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _client_timeout_seconds() -> float:
    """Keep CLI requests alive for the configured provider budget plus cleanup."""

    try:
        config = cli_config.read_raw_config() or {}
        provider_timeout = float(
            (config.get("occult") or {}).get("provider_timeout_seconds", 120)
        )
    except (OSError, TypeError, ValueError):
        provider_timeout = 120
    if not 1 <= provider_timeout <= 600:
        provider_timeout = 120
    return provider_timeout + 30


def _local_occult_status() -> dict[str, Any]:
    """Report activation state without requiring the local API to be running."""

    try:
        config = cli_config.read_raw_config() or {}
    except (OSError, TypeError, ValueError) as exc:
        raise OccultCLIError(f"could not read Tarot Router configuration: {exc}") from None
    occult = config.get("occult")
    if occult is not None and not isinstance(occult, dict):
        raise OccultCLIError("occult configuration must be an object")
    settings = occult or {}
    model = str(settings.get("local_model") or "").strip()
    initialized = bool(model)
    enabled = initialized and settings.get("enabled") is True
    result: dict[str, Any] = {
        "initialized": initialized,
        "enabled": enabled,
        "model": model or None,
    }
    if not initialized:
        result["next"] = "run 'hermes tarot init --model qwen2.5:3b'"
    elif not enabled:
        result["next"] = (
            "enable occult.enabled explicitly and restart the Hermes gateway"
        )
    return result


def _open_occult_url(request: urllib.request.Request, *, timeout: float):
    """Open an Occult request without redirects or loopback proxy exposure."""

    parsed = urllib.parse.urlparse(request.full_url)
    host = (parsed.hostname or "").lower()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    proxy_handler = (
        urllib.request.ProxyHandler({})
        if is_loopback
        else urllib.request.ProxyHandler()
    )
    handlers: list[urllib.request.BaseHandler] = [
        proxy_handler,
        _NoOccultRedirect(),
    ]
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url = _api_base_url()
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
        with _open_occult_url(request, timeout=_client_timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            message = error.get("error", {}).get("message", "request failed")
        except Exception:
            message = "request failed"
        raise OccultCLIError(f"Tarot Router API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Tarot Router API unavailable: {exc}") from None


def _admin_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    base_url = _api_base_url()
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
        with _open_occult_url(request, timeout=_client_timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            message = error.get("error", {}).get("message", "request failed")
        except Exception:
            message = "request failed"
        raise OccultCLIError(f"Tarot Router API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Tarot Router API unavailable: {exc}") from None


def _stream_events(reading_id: str) -> Iterator[dict[str, Any]]:
    base_url = _api_base_url()
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
        with _open_occult_url(request, timeout=_client_timeout_seconds()) as response:
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
        raise OccultCLIError(f"Tarot Router API error {exc.code}: {message}") from None
    except (OSError, ValueError) as exc:
        raise OccultCLIError(f"Tarot Router API unavailable: {exc}") from None


def _reading_request(action: str, reading_id: str) -> Any:
    suffix = {
        "reading-status": "",
        "reading-events": "/events",
        "reading-resume": "/resume",
        "reading-cancel": "/cancel",
    }[action]
    method = "POST" if action in {"reading-resume", "reading-cancel"} else "GET"
    return _request(method, f"/v1/occult/readings/{reading_id}{suffix}")


def run_tui_tarot_command(argument: str) -> str:
    """Run a bounded Tarot Router inspector/control from the TUI composer."""

    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise OccultCLIError(f"invalid Tarot Router command: {exc}") from None
    if not parts:
        parts = ["status"]
    action = parts[0].lower()
    if action == "status":
        if len(parts) != 1:
            raise OccultCLIError("usage: /tarot status")
        result = {
            "agents": _request("GET", "/v1/occult/major-arcana")["data"],
            "routes": _request("GET", "/v1/occult/minor-arcana")["data"],
        }
    elif action == "agents":
        if len(parts) != 1:
            raise OccultCLIError("usage: /tarot agents")
        result = _request("GET", "/v1/occult/major-arcana")
    elif action == "routes":
        if len(parts) != 1:
            raise OccultCLIError("usage: /tarot routes")
        result = _request("GET", "/v1/occult/minor-arcana")
    elif action == "decks":
        if len(parts) != 1:
            raise OccultCLIError("usage: /tarot decks")
        result = _request("GET", "/v1/occult/decks")
    elif action == "pairings":
        if len(parts) > 2:
            raise OccultCLIError("usage: /tarot pairings [agent-id]")
        suffix = (
            "?agent_id=" + urllib.parse.quote(parts[1], safe="")
            if len(parts) == 2
            else ""
        )
        result = _request("GET", "/v1/occult/pairings" + suffix)
    elif action == "deck-validate":
        if len(parts) != 2:
            raise OccultCLIError("usage: /tarot deck-validate <deck-id>")
        deck_id = urllib.parse.quote(parts[1], safe="")
        result = _request("GET", f"/v1/occult/decks/{deck_id}/validate")
    elif action in {
        "reading-status",
        "reading-events",
        "reading-resume",
        "reading-cancel",
    }:
        if len(parts) != 2:
            raise OccultCLIError(f"usage: /tarot {action} <reading-id>")
        result = _reading_request(action, parts[1])
    else:
        raise OccultCLIError(
            "usage: /tarot "
            "[status|agents|routes|decks|pairings|deck-validate|reading-status|"
            "reading-events|reading-resume|reading-cancel]"
        )
    return json.dumps(result, indent=2, sort_keys=True)


def run_tui_occult_command(argument: str) -> str:
    """Compatibility alias for the v1 ``/occult`` command."""

    return run_tui_tarot_command(argument)


def cmd_occult(args) -> None:
    action = args.occult_action
    if action == "init":
        result = initialize_occult(
            base_url=args.base_url,
            model=args.model,
        )
    elif action == "token-list":
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
        local_status = _local_occult_status()
        if not local_status["initialized"] or not local_status["enabled"]:
            result = local_status
        else:
            result = {
                "agents": _request("GET", "/v1/occult/major-arcana")["data"],
                "routes": _request("GET", "/v1/occult/minor-arcana")["data"],
            }
    elif action == "agents":
        result = _request("GET", "/v1/occult/major-arcana")
    elif action == "routes":
        result = _request("GET", "/v1/occult/minor-arcana")
    elif action == "decks":
        result = _request("GET", "/v1/occult/decks")
    elif action == "pairings":
        suffix = (
            "?agent_id=" + urllib.parse.quote(args.agent, safe="") if args.agent else ""
        )
        result = _request("GET", "/v1/occult/pairings" + suffix)
    elif action == "deck-validate":
        deck_id = urllib.parse.quote(args.deck_id, safe="")
        result = _request("GET", f"/v1/occult/decks/{deck_id}/validate")
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
        raise OccultCLIError("a Tarot Router action is required")
    print(json.dumps(result, indent=2, sort_keys=True))


__all__ = [
    "OccultCLIError",
    "cmd_occult",
    "initialize_occult",
    "run_tui_occult_command",
    "run_tui_tarot_command",
]

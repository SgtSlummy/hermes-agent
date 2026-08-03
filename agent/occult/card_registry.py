"""Persistent, secret-free provider and Minor Arcana card additions.

The bundled provider catalog remains immutable and reviewed.  This registry is
the deliberately small control-plane for operator-added metadata: it never
accepts credentials and never activates a route by itself.  Runtime enrollment
can later promote an eligible keyless entry after the normal Mythos safety
gates pass.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ALLOWED_AUTH = frozenset({"anonymous", "keyless", "bearer", "oauth", "oauth_pkce"})
_ALLOWED_SUITES = frozenset({"swords", "wands", "cups", "pentacles"})
_ALLOWED_ENROLLMENT = frozenset({"keyless", "preauthorized", "human_required"})
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "credential",
        "password",
        "refreshtoken",
        "secret",
        "token",
    }
)


class CardRegistryError(ValueError):
    """Raised for unsafe or invalid operator-added metadata."""


def _text(value: Any, name: str, *, maximum: int = 1000, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise CardRegistryError(f"{name} must be a string")
        return ""
    result = value.strip()
    if required and not result:
        raise CardRegistryError(f"{name} is required")
    if len(result) > maximum:
        raise CardRegistryError(f"{name} is too long")
    return result


def _safe_id(value: Any, name: str) -> str:
    result = _text(value, name, maximum=128, required=True)
    if not _SAFE_ID.fullmatch(result):
        raise CardRegistryError(f"{name} contains unsafe characters")
    return result


def _safe_model(value: Any, name: str) -> str:
    result = _text(value, name, maximum=256, required=True)
    if not _SAFE_MODEL.fullmatch(result):
        raise CardRegistryError(f"{name} contains unsafe characters")
    return result


def _list_of_strings(value: Any, name: str, *, maximum_items: int = 32) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise CardRegistryError(f"{name} must be a list")
    if len(value) > maximum_items or any(not isinstance(item, str) for item in value):
        raise CardRegistryError(f"{name} contains invalid entries")
    result = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if any(len(item) > 256 for item in result):
        raise CardRegistryError(f"{name} contains an entry that is too long")
    return result


def _validate_no_secrets(payload: Mapping[str, Any]) -> None:
    for key, value in payload.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized in _SECRET_FIELD_NAMES:
            raise CardRegistryError(f"{key} is not accepted; store credentials in the vault")
        if isinstance(value, Mapping):
            _validate_no_secrets(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    _validate_no_secrets(item)


def _validate_endpoint(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CardRegistryError("base_url must not contain credentials or query data")
    host = (parsed.hostname or "").lower().rstrip(".")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise CardRegistryError("base_url must use HTTPS (or HTTP for localhost)")
    if not host:
        raise CardRegistryError("base_url must include a host")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class CardRegistry:
    """JSON-backed registry containing only public metadata."""

    path: Path

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"providers": [], "cards": []}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CardRegistryError("card registry cannot be read") from exc
        if not isinstance(payload, Mapping):
            raise CardRegistryError("card registry must be an object")
        providers = payload.get("providers", [])
        cards = payload.get("cards", [])
        if not isinstance(providers, list) or not isinstance(cards, list):
            raise CardRegistryError("card registry lists are invalid")
        return {"providers": [dict(item) for item in providers if isinstance(item, Mapping)],
                "cards": [dict(item) for item in cards if isinstance(item, Mapping)]}

    def _write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="occult-registry-", suffix=".json", dir=self.path.parent)
        try:
            with open(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
                stream.write("\n")
            Path(name).replace(self.path)
        finally:
            try:
                Path(name).unlink()
            except FileNotFoundError:
                pass

    def providers(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._read()["providers"])

    def cards(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._read()["cards"])

    def register_provider(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _validate_no_secrets(payload)
        provider_id = _safe_id(payload.get("provider_id", payload.get("id")), "provider_id")
        name = _text(payload.get("name"), "name", maximum=160, required=True)
        auth_type = _text(payload.get("auth_type", "keyless"), "auth_type", maximum=32).lower()
        if auth_type not in _ALLOWED_AUTH:
            raise CardRegistryError("auth_type is unsupported")
        base_url = _validate_endpoint(_text(payload.get("base_url"), "base_url", maximum=512))
        official_hosts = _list_of_strings(payload.get("official_hosts"), "official_hosts")
        free_access = _text(payload.get("free_access", "unknown"), "free_access", maximum=48) or "unknown"
        capabilities = _list_of_strings(payload.get("capabilities"), "capabilities")
        zero_cost = tuple(_safe_model(item, "zero_cost_model_ids") for item in _list_of_strings(payload.get("zero_cost_model_ids"), "zero_cost_model_ids"))
        description = _text(payload.get("description"), "description", maximum=1000)
        soul = _text(payload.get("soul"), "soul", maximum=2000)
        enrollment_mode = "keyless" if auth_type in {"anonymous", "keyless"} else (
            "preauthorized" if auth_type == "bearer" else "human_required"
        )
        record = {
            "provider_id": provider_id,
            "name": name,
            "description": description or f"Operator-added {name} provider.",
            "soul": soul or "A newly registered route awaiting policy and health validation.",
            "free_access": free_access,
            "requires_card": bool(payload.get("requires_card", False)),
            "allowed_by_free_policy": free_access in {"anonymous_free", "recurring_free", "temporary_credit"},
            "adapter": _text(payload.get("adapter", "openai_compatible"), "adapter", maximum=64),
            "auth_type": auth_type,
            "official_hosts": list(official_hosts),
            "base_url": base_url or None,
            "capabilities": list(capabilities),
            "zero_cost_model_ids": list(zero_cost),
            "default_free_model": _text(payload.get("default_free_model"), "default_free_model", maximum=256) or None,
            "source_state": "operator_added",
            "activation": "keyless_pending_validation" if enrollment_mode == "keyless" else "awaiting_authorized_credential",
            "enrollment_mode": enrollment_mode,
            "requires_human_authorization": enrollment_mode == "human_required",
            "active_route_count": 0,
            "created_by": "dashboard",
        }
        state = self._read()
        existing = {item.get("provider_id") for item in state["providers"]}
        if provider_id in existing:
            raise CardRegistryError("provider_id is already registered")
        state["providers"].append(record)
        self._write(state)
        return record

    def register_card(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _validate_no_secrets(payload)
        card_id = _safe_id(payload.get("card_id", payload.get("id")), "card_id")
        name = _text(payload.get("name"), "name", maximum=160, required=True)
        suit = _text(payload.get("suit", "pentacles"), "suit", maximum=16).lower()
        if suit not in _ALLOWED_SUITES:
            raise CardRegistryError("suit must be swords, wands, cups, or pentacles")
        rank = _text(payload.get("rank", "page"), "rank", maximum=16).lower()
        provider_id = _safe_id(payload.get("provider_id"), "provider_id") if payload.get("provider_id") else None
        model_id = _safe_model(payload.get("model_id"), "model_id") if payload.get("model_id") else None
        capabilities = _list_of_strings(payload.get("capabilities"), "capabilities") or ("text",)
        record = {
            "card_id": card_id,
            "name": name,
            "description": _text(payload.get("description"), "description", maximum=1000) or f"Operator-added {name} card.",
            "soul": _text(payload.get("soul"), "soul", maximum=2000) or "A custom Minor Arcana card awaiting provider validation.",
            "suit": suit,
            "rank": rank,
            "provider_id": provider_id,
            "model_id": model_id,
            "capabilities": list(capabilities),
            "local": bool(payload.get("local", False)),
            "free": bool(payload.get("free", True)),
            "status": "pending_review",
            "activation": "pending_review",
            "source": "operator_added",
        }
        state = self._read()
        existing = {item.get("card_id") for item in state["cards"]}
        if card_id in existing:
            raise CardRegistryError("card_id is already registered")
        state["cards"].append(record)
        self._write(state)
        return record


__all__ = ["CardRegistry", "CardRegistryError"]

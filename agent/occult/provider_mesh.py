"""Explicit, free-only activation for reviewed OpenAI-compatible providers.

The provider catalog is intentionally inert.  This module turns only an
operator-selected subset into live Mythos routes, and only when the provider
passes the following gates:

* the catalog marks it as free and terms-compatible;
* the endpoint is HTTPS and resolves to a cataloged official host;
* the adapter is supported;
* bearer credentials come from an operator-provided environment reference; and
* every registered model is explicitly listed as zero-cost by the catalog.

No account creation, key scraping, quota evasion, or credential persistence is
performed here.  Secrets live only in the in-memory broker for the process and
are revealed to an adapter at the final request boundary.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.occult.credential_broker import InMemoryCredentialBroker, SecretValue
from agent.occult.mythos import (
    AdapterRequest,
    AdapterResponse,
    CallableProviderAdapter,
    FailureKind,
    MinorArcanaDescriptor,
    PrivacyClass,
    ProviderFailure,
)
from agent.occult.provider_catalog import CatalogProvider, ProviderCatalog


_SAFE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUPPORTED_ADAPTERS = frozenset({"openai_compatible"})
_SUPPORTED_AUTH = frozenset({"anonymous", "bearer"})
_DEFAULT_CHAT_PATH = "/chat/completions"
_MAX_MODEL_ID_LENGTH = 112
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ProviderMeshError(ValueError):
    """Invalid provider-mesh configuration or unsafe catalog metadata."""


@dataclass(frozen=True, slots=True)
class ProviderMeshConfig:
    """Explicit activation settings; disabled unless ``enabled`` is true."""

    enabled: bool = False
    provider_ids: tuple[str, ...] = ()
    allow_anonymous: bool = True
    allow_external_routes: bool = False
    discover_models: bool = True
    max_models_per_provider: int = 4
    timeout_seconds: float = 8.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProviderMeshConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ProviderMeshError("occult.provider_mesh must be an object")
        try:
            enabled = bool(value.get("enabled", False))
            raw_ids = value.get("provider_ids", value.get("providerIds", ()))
            if not isinstance(raw_ids, (list, tuple)):
                raise TypeError
            provider_ids = tuple(dict.fromkeys(str(item).strip() for item in raw_ids))
            allow_anonymous = bool(value.get("allow_anonymous", value.get("allowAnonymous", True)))
            allow_external_routes = bool(
                value.get("allow_external_routes", value.get("allowExternalRoutes", False))
            )
            discover_models = bool(
                value.get("discover_models", value.get("discoverModels", True))
            )
            max_models = int(
                value.get("max_models_per_provider", value.get("maxModelsPerProvider", 4))
            )
            timeout = float(value.get("timeout_seconds", value.get("timeoutSeconds", 8.0)))
        except (TypeError, ValueError):
            raise ProviderMeshError("invalid occult.provider_mesh settings") from None
        if any(not _SAFE_PROVIDER_ID.fullmatch(item) for item in provider_ids):
            raise ProviderMeshError("provider_mesh.provider_ids contains an invalid id")
        if not 1 <= max_models <= 16:
            raise ProviderMeshError("provider_mesh.max_models_per_provider must be 1-16")
        if not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ProviderMeshError("provider_mesh.timeout_seconds must be 1-60")
        return cls(
            enabled=enabled,
            provider_ids=provider_ids,
            allow_anonymous=allow_anonymous,
            allow_external_routes=allow_external_routes,
            discover_models=discover_models,
            max_models_per_provider=max_models,
            timeout_seconds=timeout,
        )


@dataclass(frozen=True, slots=True)
class ProviderMeshActivation:
    """Secret-free activation result suitable for diagnostics and tests."""

    adapters: Mapping[str, CallableProviderAdapter]
    routes: tuple[MinorArcanaDescriptor, ...]
    activated: tuple[str, ...]
    skipped: tuple[Mapping[str, str], ...]
    pending_authorization: tuple[str, ...]

    def report(self) -> dict[str, Any]:
        return {
            "activated": list(self.activated),
            "route_count": len(self.routes),
            "skipped": [dict(item) for item in self.skipped],
            "pending_authorization": list(self.pending_authorization),
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise urllib.error.URLError("provider redirect rejected")


def _official_https_url(
    provider: CatalogProvider,
    path: str,
) -> str:
    raw = str(provider.base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not host
        or not provider.official_hosts
        or not any(host == suffix.lower().lstrip(".") or host.endswith("." + suffix.lower().lstrip("."))
                   for suffix in provider.official_hosts)
    ):
        raise ProviderMeshError(f"provider {provider.provider_id} has an unsafe endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        raise ProviderMeshError(f"provider {provider.provider_id} endpoint is not public")
    if not path or not path.startswith("/") or "?" in path or "#" in path:
        raise ProviderMeshError(f"provider {provider.provider_id} has an unsafe API path")
    return raw + path


def _safe_model_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._:/-]", "_", str(value).strip())
    if not component:
        component = "model"
    if len(component) <= _MAX_MODEL_ID_LENGTH:
        return component
    digest = hashlib.sha256(component.encode("utf-8")).hexdigest()[:12]
    return component[: _MAX_MODEL_ID_LENGTH - 13] + "_" + digest


def _model_ids(payload: Any) -> tuple[str, ...]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        return ()
    result: list[str] = []
    for item in data:
        if isinstance(item, Mapping):
            model_id = str(item.get("id", "")).strip()
            if model_id:
                result.append(model_id)
    return tuple(dict.fromkeys(result))


def _provider_failure(exc: BaseException) -> ProviderFailure:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {401, 403}:
            return ProviderFailure(FailureKind.AUTHENTICATION)
        if exc.code == 429:
            return ProviderFailure(FailureKind.RATE_LIMIT)
        if exc.code in {408, 500, 502, 503, 504}:
            return ProviderFailure(FailureKind.UNAVAILABLE)
        return ProviderFailure(FailureKind.INVALID_REQUEST)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ProviderFailure(FailureKind.TIMEOUT)
    if isinstance(exc, (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError)):
        return ProviderFailure(FailureKind.UNAVAILABLE)
    return ProviderFailure(FailureKind.UNKNOWN)


class ExternalProviderAdapter(CallableProviderAdapter):
    """One provider-specific OpenAI-compatible adapter with no logged secret."""

    def __init__(self, *, adapter_id: str, handler) -> None:
        super().__init__(handler)
        self.adapter_id = adapter_id


def _make_handler(
    provider: CatalogProvider,
    *,
    timeout_seconds: float,
) -> tuple[ExternalProviderAdapter, str | None]:
    chat_path = provider.chat_path or _DEFAULT_CHAT_PATH
    chat_url = _official_https_url(provider, chat_path)
    models_url = (
        _official_https_url(provider, provider.models_path)
        if provider.models_path
        else None
    )
    opener = urllib.request.build_opener(_NoRedirect())

    def headers_for(credential: SecretValue | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Occult-Mythos-Router/1",
        }
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential.reveal_for_adapter()
        return headers

    def invoke(
        request: AdapterRequest,
        _route: MinorArcanaDescriptor,
        credential: SecretValue | None,
    ) -> AdapterResponse:
        body = json.dumps(
            {
                "model": request.model_id,
                "messages": [{"role": "user", "content": request.message}],
                "stream": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        outbound = urllib.request.Request(
            chat_url,
            data=body,
            method="POST",
            headers=headers_for(credential),
        )
        try:
            with opener.open(outbound, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise ProviderFailure(FailureKind.INVALID_RESPONSE) from None
        except Exception as exc:  # pragma: no cover - typed failures are tested below
            raise _provider_failure(exc) from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderFailure(FailureKind.INVALID_RESPONSE)
        return AdapterResponse(
            text=text,
            input_tokens=max(input_tokens, 0),
            output_tokens=max(output_tokens, 0),
        )

    adapter = ExternalProviderAdapter(
        adapter_id=f"mesh.{_safe_model_component(provider.provider_id)}",
        handler=invoke,
    )
    return adapter, models_url


def _discover_models(
    provider: CatalogProvider,
    *,
    models_url: str | None,
    credential: SecretValue | None,
    timeout_seconds: float,
) -> tuple[str, ...]:
    if models_url is None:
        return ()
    headers = {
        "Accept": "application/json",
        "User-Agent": "Occult-Mythos-Router/1",
    }
    if credential is not None:
        headers["Authorization"] = "Bearer " + credential.reveal_for_adapter()
    request = urllib.request.Request(models_url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return _model_ids(json.loads(response.read().decode("utf-8")))
    except Exception as exc:
        raise _provider_failure(exc) from None


def _select_models(
    provider: CatalogProvider,
    discovered: Sequence[str],
    *,
    max_models: int,
) -> tuple[str, ...]:
    allowed = tuple(
        model
        for model in dict.fromkeys(provider.zero_cost_model_ids)
        if _SAFE_MODEL_ID.fullmatch(model)
    )
    if not allowed and provider.default_free_model:
        allowed = (provider.default_free_model,)
    if not allowed:
        return ()
    if discovered:
        available = set(discovered)
        selected = tuple(model for model in allowed if model in available)
        if selected:
            return selected[:max_models]
        return ()
    return allowed[:max_models]


def _route_for(
    provider: CatalogProvider,
    model_id: str,
    *,
    adapter_id: str,
    credential_reference_id: str | None,
) -> MinorArcanaDescriptor:
    card_id = (
        f"minor.wands.knight.{_safe_model_component(provider.provider_id)}."
        f"{_safe_model_component(model_id)}"
    )
    capabilities = {"text"}
    if "tools" in provider.capabilities:
        capabilities.add("tool_calling")
    if "json" in provider.capabilities:
        capabilities.add("structured_output")
    return MinorArcanaDescriptor(
        card_id=card_id,
        provider_id=provider.provider_id,
        model_id=model_id,
        adapter_id=adapter_id,
        capabilities=frozenset(capabilities),
        local=False,
        free=True,
        privacy=PrivacyClass.EXTERNAL,
        quality_score=0.45,
        latency_ms=1500,
        estimated_request_cost_usd=0.0,
        quota_pool_id=f"{provider.provider_id}:default",
        credential_reference_id=credential_reference_id,
    )


def activate_provider_mesh(
    config: ProviderMeshConfig,
    *,
    catalog: ProviderCatalog,
    environ: Mapping[str, str],
    credential_broker: InMemoryCredentialBroker,
) -> ProviderMeshActivation:
    """Build explicitly selected free routes without mutating the catalog."""

    if not config.enabled:
        return ProviderMeshActivation({}, (), (), (), ())
    selected = set(config.provider_ids)
    candidates = tuple(
        provider
        for provider in catalog.list()
        if (provider.provider_id in selected if selected else provider.enabled)
    )
    adapters: dict[str, ExternalProviderAdapter] = {}
    routes: list[MinorArcanaDescriptor] = []
    activated: list[str] = []
    skipped: list[Mapping[str, str]] = []
    pending: list[str] = []
    for provider in candidates:
        if not provider.allowed_by_free_policy:
            skipped.append({"provider_id": provider.provider_id, "reason": "not_free_policy_allowed"})
            continue
        if not provider.terms_permit_tarot:
            skipped.append({"provider_id": provider.provider_id, "reason": "terms_not_verified"})
            continue
        if provider.allow_paid_models:
            skipped.append({"provider_id": provider.provider_id, "reason": "paid_models_not_allowed"})
            continue
        if provider.adapter not in _SUPPORTED_ADAPTERS:
            skipped.append({"provider_id": provider.provider_id, "reason": "adapter_not_supported"})
            continue
        if provider.auth_type not in _SUPPORTED_AUTH:
            skipped.append({"provider_id": provider.provider_id, "reason": "auth_type_not_supported"})
            continue
        if provider.auth_type == "anonymous" and not config.allow_anonymous:
            skipped.append({"provider_id": provider.provider_id, "reason": "anonymous_disabled"})
            continue
        credential_reference_id: str | None = None
        if provider.auth_type == "bearer":
            secret_name = next(
                (name for name in provider.secret_refs if str(environ.get(name, "")).strip()),
                None,
            )
            if secret_name is None:
                pending.append(provider.provider_id)
                continue
            reference = credential_broker.import_authorized(
                provider_id=provider.provider_id,
                secret=str(environ[secret_name]).strip(),
                quota_pool_id=f"{provider.provider_id}:default",
                source="user_authorized",
            )
            credential_reference_id = reference.reference_id
        else:
            reference = credential_broker.register_keyless(
                provider_id=provider.provider_id,
                quota_pool_id=f"{provider.provider_id}:default",
            )
            credential_reference_id = reference.reference_id

        try:
            adapter, models_url = _make_handler(
                provider,
                timeout_seconds=config.timeout_seconds,
            )
            credential = credential_broker.resolve(
                credential_reference_id
            ) if credential_reference_id is not None else None
            discovered = (
                _discover_models(
                    provider,
                    models_url=models_url,
                    credential=credential,
                    timeout_seconds=config.timeout_seconds,
                )
                if config.discover_models
                else ()
            )
            models = _select_models(
                provider,
                discovered,
                max_models=config.max_models_per_provider,
            )
            if not models:
                skipped.append({"provider_id": provider.provider_id, "reason": "no_verified_zero_cost_model"})
                continue
        except ProviderMeshError as exc:
            skipped.append({"provider_id": provider.provider_id, "reason": str(exc)})
            continue
        except ProviderFailure as exc:
            skipped.append({"provider_id": provider.provider_id, "reason": f"provider_probe_{exc.kind.value}"})
            continue

        adapters[adapter.adapter_id] = adapter
        for model_id in models:
            routes.append(
                _route_for(
                    provider,
                    model_id,
                    adapter_id=adapter.adapter_id,
                    credential_reference_id=credential_reference_id,
                )
            )
        activated.append(provider.provider_id)

    return ProviderMeshActivation(
        adapters=adapters,
        routes=tuple(routes),
        activated=tuple(sorted(set(activated))),
        skipped=tuple(skipped),
        pending_authorization=tuple(sorted(set(pending))),
    )


__all__ = [
    "ProviderMeshConfig",
    "ProviderMeshError",
    "ProviderMeshActivation",
    "ExternalProviderAdapter",
    "activate_provider_mesh",
]

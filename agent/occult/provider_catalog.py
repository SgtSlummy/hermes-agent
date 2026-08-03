"""Secret-free catalog and policy view for authorized free provider routes.

The catalog is intentionally separate from live Mythos routes. A provider can
be allowed by policy while still requiring a user-authorized credential,
adapter, quota check, and health check before it becomes active.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FREE_ACCESS = frozenset({"anonymous_free", "recurring_free", "temporary_credit"})
_CATALOG_NAME = "provider_catalog.json"
_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authorizationheader",
        "credential",
        "password",
        "refreshtoken",
        "secret",
        "token",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
)


def _assert_public_catalog(value: Any, path: str = "$") -> None:
    """Reject credential material while allowing documented URL metadata.

    The provider feed includes a ``secretRefs`` allowlist and a ``urls`` map
    whose ``credentials`` key is a documentation URL, not a secret.  The
    runtime's stricter invocation-state validator intentionally rejects both,
    so the catalog uses this narrower, schema-aware check instead.
    """

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            child_path = f"{path}.{key}"
            if normalized in _SECRET_KEYS:
                raise ProviderCatalogError(
                    f"provider catalog contains secret-shaped data at {child_path}"
                )
            if normalized == "secretrefs":
                if not isinstance(child, list) or not all(
                    isinstance(item, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", item)
                    for item in child
                ):
                    raise ProviderCatalogError(
                        f"provider catalog contains invalid secret reference metadata at {child_path}"
                    )
                continue
            _assert_public_catalog(child, child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_public_catalog(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS
    ):
        raise ProviderCatalogError(
            f"provider catalog contains secret-shaped data at {path}"
        )


class ProviderCatalogError(ValueError):
    """The bundled provider catalog is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class CatalogProvider:
    provider_id: str
    name: str
    free_access: str
    requires_card: bool
    adapter: str
    auth_type: str
    official_hosts: tuple[str, ...]
    base_url: str | None
    chat_path: str | None
    models_path: str | None
    secret_refs: tuple[str, ...]
    capabilities: tuple[str, ...]
    zero_cost_model_ids: tuple[str, ...]
    default_free_model: str | None
    allow_paid_models: bool
    terms_permit_tarot: bool
    allowed_data_classifications: tuple[str, ...]
    enabled: bool
    source_state: str
    description: str = ""
    soul: str = ""
    auth_url: str | None = None
    notes: str = ""

    @property
    def allowed_by_free_policy(self) -> bool:
        return (
            self.free_access in _FREE_ACCESS
            and not self.requires_card
            and self.free_access != "retired"
        )

    def public_contract(self) -> dict[str, Any]:
        enrollment_mode = (
            "keyless"
            if self.auth_type in {"anonymous", "keyless"}
            else "preauthorized"
            if self.auth_type == "bearer"
            else "human_required"
        )
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "description": self.description or self.notes or f"{self.name} provider route.",
            "soul": self.soul
            or (
                "Keyless free inference route; it can be enrolled without a credential when the endpoint is reachable."
                if enrollment_mode == "keyless"
                else "Authorized provider route; the router keeps credentials outside the agent and waits for a valid authorization."
            ),
            "free_access": self.free_access,
            "requires_card": self.requires_card,
            "allowed_by_free_policy": self.allowed_by_free_policy,
            "adapter": self.adapter,
            "auth_type": self.auth_type,
            "official_hosts": list(self.official_hosts),
            "base_url": self.base_url,
            "capabilities": list(self.capabilities),
            "zero_cost_model_ids": list(self.zero_cost_model_ids),
            "default_free_model": self.default_free_model,
            "terms_permit_tarot": self.terms_permit_tarot,
            "allowed_data_classifications": list(self.allowed_data_classifications),
            "auth_url": self.auth_url,
            "source_state": self.source_state,
            "enrollment_mode": enrollment_mode,
            "requires_human_authorization": enrollment_mode == "human_required",
            "active_route_count": 0,
            "activation": (
                "awaiting_authorized_credential"
                if self.allowed_by_free_policy
                else "blocked_by_free_policy"
            ),
        }


class ProviderCatalog:
    """Validated, immutable provider metadata with no credential material."""

    def __init__(self, providers: tuple[CatalogProvider, ...]) -> None:
        self._providers = providers

    @classmethod
    def from_path(cls, path: Path) -> "ProviderCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderCatalogError("provider catalog cannot be read") from None
        if not isinstance(payload, Mapping):
            raise ProviderCatalogError("provider catalog must be an object")
        if payload.get("schemaVersion") != "1.0" or not isinstance(
            payload.get("providers"), list
        ):
            raise ProviderCatalogError("unsupported provider catalog")
        _assert_public_catalog(payload)

        providers: list[CatalogProvider] = []
        seen: set[str] = set()
        for raw in payload["providers"]:
            if not isinstance(raw, Mapping):
                raise ProviderCatalogError("provider entries must be objects")
            provider_id = str(raw.get("id", "")).strip()
            if not _SAFE_ID.fullmatch(provider_id) or provider_id in seen:
                raise ProviderCatalogError("provider catalog contains an invalid or duplicate id")
            seen.add(provider_id)
            hosts = raw.get("officialHostSuffixes", ())
            capabilities = raw.get("capabilities", ())
            if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
                raise ProviderCatalogError(f"invalid official hosts for {provider_id}")
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                raise ProviderCatalogError(f"invalid capabilities for {provider_id}")
            api = raw.get("api")
            base_url = api.get("baseUrl") if isinstance(api, Mapping) else None
            if base_url is not None and not isinstance(base_url, str):
                raise ProviderCatalogError(f"invalid base URL for {provider_id}")
            chat_path = api.get("chatPath") if isinstance(api, Mapping) else None
            models_path = api.get("modelsPath") if isinstance(api, Mapping) else None
            if chat_path is not None and not isinstance(chat_path, str):
                raise ProviderCatalogError(f"invalid chat path for {provider_id}")
            if models_path is not None and not isinstance(models_path, str):
                raise ProviderCatalogError(f"invalid models path for {provider_id}")
            secret_refs = raw.get("secretRefs", ())
            if not isinstance(secret_refs, list) or not all(
                isinstance(item, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", item)
                for item in secret_refs
            ):
                raise ProviderCatalogError(f"invalid secret references for {provider_id}")
            zero_cost_model_ids = raw.get("zeroCostModelIds", ())
            if not isinstance(zero_cost_model_ids, list) or not all(
                isinstance(item, str) and item.strip()
                for item in zero_cost_model_ids
            ):
                raise ProviderCatalogError(
                    f"invalid zero-cost model ids for {provider_id}"
                )
            default_free_model = raw.get("defaultFreeModel")
            if default_free_model is not None and not isinstance(default_free_model, str):
                raise ProviderCatalogError(
                    f"invalid default free model for {provider_id}"
                )
            allowed_data = raw.get("allowedDataClassifications", ())
            if not isinstance(allowed_data, list) or not all(
                isinstance(item, str) and item.strip() for item in allowed_data
            ):
                raise ProviderCatalogError(
                    f"invalid data classifications for {provider_id}"
                )
            providers.append(
                CatalogProvider(
                    provider_id=provider_id,
                    name=str(raw.get("name", provider_id)),
                    free_access=str(raw.get("freeAccess", "unknown")),
                    requires_card=bool(raw.get("requiresCard", False)),
                    adapter=str(raw.get("adapter", "unknown")),
                    auth_type=str(raw.get("authType", "unknown")),
                    official_hosts=tuple(hosts),
                    base_url=base_url,
                    chat_path=chat_path,
                    models_path=models_path,
                    secret_refs=tuple(secret_refs),
                    capabilities=tuple(sorted(set(capabilities))),
                    zero_cost_model_ids=tuple(
                        dict.fromkeys(item.strip() for item in zero_cost_model_ids)
                    ),
                    default_free_model=(
                        default_free_model.strip()
                        if isinstance(default_free_model, str)
                        and default_free_model.strip()
                        else None
                    ),
                    allow_paid_models=bool(raw.get("allowPaidModels", False)),
                    terms_permit_tarot=bool(raw.get("termsPermitTarot", False)),
                    allowed_data_classifications=tuple(
                        sorted(set(item.strip() for item in allowed_data))
                    ),
                    enabled=bool(raw.get("enabled", False)),
                    source_state=str(raw.get("state", "unknown")),
                    description=str(raw.get("description", "")).strip(),
                    soul=str(raw.get("soul", "")).strip(),
                    auth_url=(
                        str((raw.get("urls") or {}).get("authentication", "")).strip()
                        or None
                        if isinstance(raw.get("urls"), Mapping)
                        else None
                    ),
                    notes=str(raw.get("notes", "")).strip(),
                )
            )
        return cls(tuple(sorted(providers, key=lambda item: item.provider_id)))

    def list(self) -> tuple[CatalogProvider, ...]:
        return self._providers

    def public(self) -> tuple[dict[str, Any], ...]:
        return tuple(provider.public_contract() for provider in self._providers)

    def summary(self) -> dict[str, int]:
        return {
            "cataloged": len(self._providers),
            "allowed_free": sum(item.allowed_by_free_policy for item in self._providers),
            "anonymous_free": sum(item.free_access == "anonymous_free" for item in self._providers),
            "recurring_free": sum(item.free_access == "recurring_free" for item in self._providers),
            "temporary_credit": sum(item.free_access == "temporary_credit" for item in self._providers),
            "card_required": sum(item.requires_card for item in self._providers),
            "blocked": sum(not item.allowed_by_free_policy for item in self._providers),
        }


def load_bundled_provider_catalog() -> ProviderCatalog:
    return ProviderCatalog.from_path(Path(__file__).with_name(_CATALOG_NAME))


__all__ = [
    "CatalogProvider",
    "ProviderCatalog",
    "ProviderCatalogError",
    "load_bundled_provider_catalog",
]

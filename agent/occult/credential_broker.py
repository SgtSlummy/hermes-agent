"""Opaque credential references for the Hermes-owned Occult boundary.

This module deliberately does not persist provider secrets. Existing Hermes
authentication stores and protected environment imports remain the source of
truth; the in-memory broker is only the process-scoped handoff to Mythos
adapters. This keeps secret material out of route descriptors, state files,
logs, errors, and public results.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class CredentialBoundaryError(ValueError):
    """A safe-to-surface credential-boundary failure."""


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_SOURCES = frozenset({
    "keyless",
    "oauth_authorized",
    "official_management_api",
    "service_account",
    "user_authorized",
})
_PROHIBITED_SOURCES = frozenset({
    "automated_account",
    "captcha_bypass",
    "disposable_email",
    "leaked",
    "public_repository",
    "quota_evasion",
    "scraped",
    "shared_community",
    "verification_bypass",
})


def _safe_identifier(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise CredentialBoundaryError(f"invalid {field}")
    return normalized


class SecretValue:
    """Secret wrapper whose string representations are always redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value:
            raise CredentialBoundaryError("credential value must not be empty")
        self.__value = value

    def reveal_for_adapter(self) -> str:
        """Reveal only at the final provider-adapter boundary."""

        return self.__value

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


@dataclass(frozen=True, slots=True)
class CredentialReference:
    """Non-secret metadata safe to attach to an internal route."""

    reference_id: str
    provider_id: str
    quota_pool_id: str
    source: str
    status: CredentialStatus = CredentialStatus.ACTIVE
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reference_id", _safe_identifier(self.reference_id, "reference id")
        )
        object.__setattr__(
            self, "provider_id", _safe_identifier(self.provider_id, "provider id")
        )
        object.__setattr__(
            self, "quota_pool_id", _safe_identifier(self.quota_pool_id, "quota pool id")
        )
        source = str(self.source or "").strip().lower()
        if source in _PROHIBITED_SOURCES or source not in _ALLOWED_SOURCES:
            raise CredentialBoundaryError("credential source is not authorized")
        object.__setattr__(self, "source", source)
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise CredentialBoundaryError("invalid credential expiration")

    def is_available(self, now: datetime | None = None) -> bool:
        if self.status is not CredentialStatus.ACTIVE:
            return False
        if self.expires_at is None:
            return True
        current = now or datetime.now(UTC)
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry > current


class InMemoryCredentialBroker:
    """Process-scoped broker that never serializes or displays secret values."""

    def __init__(self) -> None:
        self._references: dict[str, CredentialReference] = {}
        self._secrets: dict[str, SecretValue] = {}

    def import_authorized(
        self,
        *,
        provider_id: str,
        secret: str,
        quota_pool_id: str,
        source: str = "user_authorized",
        expires_at: datetime | None = None,
    ) -> CredentialReference:
        provider = _safe_identifier(provider_id, "provider id")
        quota_pool = _safe_identifier(quota_pool_id, "quota pool id")
        source_name = str(source or "").strip().lower()
        if source_name in _PROHIBITED_SOURCES or source_name not in _ALLOWED_SOURCES:
            raise CredentialBoundaryError("credential source is not authorized")

        reference = CredentialReference(
            reference_id=f"cred_{secrets.token_hex(8)}",
            provider_id=provider,
            quota_pool_id=quota_pool,
            source=source_name,
            expires_at=expires_at,
        )
        self._references[reference.reference_id] = reference
        self._secrets[reference.reference_id] = SecretValue(secret)
        return reference

    def register_keyless(
        self, *, provider_id: str, quota_pool_id: str
    ) -> CredentialReference:
        reference = CredentialReference(
            reference_id=f"keyless_{secrets.token_hex(8)}",
            provider_id=provider_id,
            quota_pool_id=quota_pool_id,
            source="keyless",
        )
        self._references[reference.reference_id] = reference
        return reference

    def resolve(self, reference_id: str) -> SecretValue | None:
        reference = self.reference(reference_id)
        if not reference.is_available():
            raise CredentialBoundaryError("credential reference is unavailable")
        return self._secrets.get(reference.reference_id)

    def reference(self, reference_id: str) -> CredentialReference:
        safe_id = _safe_identifier(reference_id, "reference id")
        try:
            return self._references[safe_id]
        except KeyError:
            raise CredentialBoundaryError("unknown credential reference") from None

    def revoke(self, reference_id: str) -> None:
        reference = self.reference(reference_id)
        self._references[reference.reference_id] = replace(
            reference, status=CredentialStatus.REVOKED
        )
        self._secrets.pop(reference.reference_id, None)

    def metadata(self) -> tuple[CredentialReference, ...]:
        return tuple(self._references.values())


__all__ = [
    "CredentialBoundaryError",
    "CredentialReference",
    "CredentialStatus",
    "InMemoryCredentialBroker",
    "SecretValue",
]

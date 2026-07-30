"""Deterministic, credential-isolated Mythos routing foundation.

The service is inert until explicitly constructed by a future Occult runtime.
It does not replace Hermes' current provider path, perform provider discovery,
or make network calls by itself. Provider I/O is delegated to registered
adapters after policy, trust, capability, quota, and health checks pass.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from agent.occult.contracts import (
    MinorArcanaRoute,
    OccultInvocation,
    RouteMode,
    RouteSummary,
    validate_invocation,
)
from agent.occult.credential_broker import (
    CredentialBoundaryError,
    InMemoryCredentialBroker,
    SecretValue,
)
from hermes_constants import get_hermes_home
from providers.base import ProviderProfile


class MythosError(RuntimeError):
    """Base class for safe-to-surface Mythos failures."""


class NoEligibleRoute(MythosError):
    """No active route satisfies the invocation policy."""


class RouteRegistrationError(MythosError):
    """A route or adapter cannot be registered safely."""


class ProviderTrustState(StrEnum):
    DISCOVERED = "discovered"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class PrivacyClass(StrEnum):
    LOCAL = "local"
    PRIVATE_EXTERNAL = "private_external"
    EXTERNAL = "external"


class FailureKind(StrEnum):
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderFailure(MythosError):
    """Normalized provider failure that never embeds upstream response bodies."""

    def __init__(
        self,
        kind: FailureKind,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"provider request failed: {kind.value}")
        if retry_after_seconds is not None and (
            not isinstance(retry_after_seconds, (int, float))
            or isinstance(retry_after_seconds, bool)
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be a finite non-negative number")
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


class MythosRoutingError(MythosError):
    """Bounded routing failed after one attempt per selected route."""

    def __init__(self, failures: tuple[tuple[str, FailureKind], ...]) -> None:
        super().__init__("all eligible Mythos routes failed")
        self.failures = failures


class AcquisitionAction(StrEnum):
    IMPORT_USER_AUTHORIZED = "import_user_authorized"
    KEYLESS_DISCOVERY = "keyless_discovery"
    OAUTH_REFRESH = "oauth_refresh"
    OFFICIAL_ROTATION = "official_rotation"
    SERVICE_ACCOUNT = "service_account"
    AUTOMATED_ACCOUNT = "automated_account"
    CAPTCHA_BYPASS = "captcha_bypass"
    LEAKED_OR_SHARED_KEY = "leaked_or_shared_key"
    QUOTA_EVASION = "quota_evasion"
    VERIFICATION_BYPASS = "verification_bypass"


_ALLOWED_ACQUISITION_ACTIONS = frozenset({
    AcquisitionAction.IMPORT_USER_AUTHORIZED,
    AcquisitionAction.KEYLESS_DISCOVERY,
    AcquisitionAction.OAUTH_REFRESH,
    AcquisitionAction.OFFICIAL_ROTATION,
    AcquisitionAction.SERVICE_ACCOUNT,
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SECRET_KEYS = frozenset({
    "accesstoken",
    "apikey",
    "authorization",
    "authorizationheader",
    "credential",
    "credentials",
    "password",
    "refreshtoken",
    "secret",
    "token",
})
_SECRET_VALUES = (
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


def enforce_acquisition_policy(action: AcquisitionAction | str) -> None:
    """Reject account, verification, leaked-key, and quota-evasion workflows."""

    try:
        normalized = AcquisitionAction(action)
    except ValueError:
        raise MythosError("unknown credential acquisition action") from None
    if normalized not in _ALLOWED_ACQUISITION_ACTIONS:
        raise MythosError("credential acquisition action is prohibited")


def _safe_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise RouteRegistrationError(f"invalid {field_name}")
    return normalized


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _assert_secret_free(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _normalized_key(key) in _SECRET_KEYS:
                raise MythosError(f"secret-shaped state field rejected at {path}")
            _assert_secret_free(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_secret_free(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_VALUES
    ):
        raise MythosError(f"secret-shaped state value rejected at {path}")


@dataclass(frozen=True, slots=True)
class MinorArcanaDescriptor:
    card_id: str
    provider_id: str
    model_id: str
    adapter_id: str
    capabilities: frozenset[str] = frozenset({"text"})
    local: bool = False
    free: bool = False
    privacy: PrivacyClass = PrivacyClass.EXTERNAL
    quality_score: float = 0.5
    latency_ms: int = 1000
    estimated_request_cost_usd: float = 0.0
    quota_pool_id: str = ""
    credential_reference_id: str | None = None
    trust_state: ProviderTrustState = ProviderTrustState.DISCOVERED

    def __post_init__(self) -> None:
        for field_name in (
            "card_id",
            "provider_id",
            "model_id",
            "adapter_id",
            "quota_pool_id",
        ):
            object.__setattr__(
                self, field_name, _safe_id(getattr(self, field_name), field_name)
            )
        if self.credential_reference_id is not None:
            object.__setattr__(
                self,
                "credential_reference_id",
                _safe_id(self.credential_reference_id, "credential_reference_id"),
            )
        if not 0 <= self.quality_score <= 1:
            raise RouteRegistrationError("quality_score must be between 0 and 1")
        if self.latency_ms < 0 or self.estimated_request_cost_usd < 0:
            raise RouteRegistrationError("latency and cost must not be negative")
        if self.local and self.privacy is not PrivacyClass.LOCAL:
            raise RouteRegistrationError("local routes require local privacy class")
        if not self.capabilities:
            raise RouteRegistrationError("route must declare at least one capability")

    def public_contract(self) -> MinorArcanaRoute:
        return MinorArcanaRoute(
            card_id=self.card_id,
            provider_id=self.provider_id,
            model_id=self.model_id,
            capabilities=tuple(sorted(self.capabilities)),
            local=self.local,
            free=self.free,
            healthy=True,
        )


def descriptor_from_provider(
    profile: ProviderProfile,
    *,
    model_id: str,
    card_id: str,
    adapter_id: str = "openai-compatible",
    capabilities: frozenset[str] = frozenset({"text", "tool_calling"}),
    local: bool = False,
    free: bool = False,
    quota_pool_id: str | None = None,
    credential_reference_id: str | None = None,
) -> MinorArcanaDescriptor:
    """Normalize an existing Hermes provider profile without reading secrets."""

    provider_id = _safe_id(profile.name, "provider_id")
    return MinorArcanaDescriptor(
        card_id=card_id,
        provider_id=provider_id,
        model_id=model_id,
        adapter_id=adapter_id,
        capabilities=capabilities,
        local=local,
        free=free,
        privacy=PrivacyClass.LOCAL if local else PrivacyClass.EXTERNAL,
        quota_pool_id=quota_pool_id or f"{provider_id}:default",
        credential_reference_id=credential_reference_id,
    )


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    invocation_id: str
    message: str
    model_id: str


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ProviderFailure(FailureKind.INVALID_RESPONSE)
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ProviderFailure(FailureKind.INVALID_RESPONSE)


class ProviderAdapter(Protocol):
    adapter_id: str

    def invoke(
        self,
        request: AdapterRequest,
        route: MinorArcanaDescriptor,
        credential: SecretValue | None,
    ) -> AdapterResponse: ...


AdapterHandler = Callable[
    [AdapterRequest, MinorArcanaDescriptor, SecretValue | None], AdapterResponse
]


class CallableProviderAdapter:
    """Offline adapter shell used by mock and transport-specific adapters."""

    adapter_id = "callable"

    def __init__(self, handler: AdapterHandler) -> None:
        self._handler = handler

    def invoke(
        self,
        request: AdapterRequest,
        route: MinorArcanaDescriptor,
        credential: SecretValue | None,
    ) -> AdapterResponse:
        return self._handler(request, route, credential)


class MockProviderAdapter(CallableProviderAdapter):
    adapter_id = "mock"


class LocalProviderAdapter(CallableProviderAdapter):
    adapter_id = "local"

    def invoke(
        self,
        request: AdapterRequest,
        route: MinorArcanaDescriptor,
        credential: SecretValue | None,
    ) -> AdapterResponse:
        if not route.local:
            raise ProviderFailure(FailureKind.INVALID_REQUEST)
        return super().invoke(request, route, credential)


class OpenAICompatibleAdapter(CallableProviderAdapter):
    adapter_id = "openai-compatible"


@dataclass(slots=True)
class QuotaPoolState:
    quota_pool_id: str
    remaining_requests: int | None = None
    cooldown_until: float = 0.0

    def __post_init__(self) -> None:
        self.quota_pool_id = _safe_id(self.quota_pool_id, "quota_pool_id")
        if self.remaining_requests is not None and (
            not isinstance(self.remaining_requests, int)
            or isinstance(self.remaining_requests, bool)
            or self.remaining_requests < 0
        ):
            raise RouteRegistrationError("remaining_requests must not be negative")
        if (
            not isinstance(self.cooldown_until, (int, float))
            or isinstance(self.cooldown_until, bool)
            or not math.isfinite(self.cooldown_until)
            or self.cooldown_until < 0
        ):
            raise RouteRegistrationError(
                "cooldown_until must be a finite non-negative number"
            )

    def available(self, now: float) -> bool:
        return self.cooldown_until <= now and (
            self.remaining_requests is None or self.remaining_requests > 0
        )


@dataclass(slots=True)
class RouteHealthState:
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    suspended: bool = False
    last_failure_kind: str | None = None
    last_success_at: float | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.consecutive_failures, int)
            or isinstance(self.consecutive_failures, bool)
            or self.consecutive_failures < 0
        ):
            raise RouteRegistrationError(
                "consecutive_failures must be a non-negative integer"
            )
        for field_name, value in (
            ("circuit_open_until", self.circuit_open_until),
            ("last_success_at", self.last_success_at),
        ):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise RouteRegistrationError(
                    f"{field_name} must be a finite non-negative number"
                )
        if not isinstance(self.suspended, bool):
            raise RouteRegistrationError("suspended must be a boolean")
        if self.last_failure_kind is not None and self.last_failure_kind not in {
            kind.value for kind in FailureKind
        }:
            raise RouteRegistrationError("invalid last_failure_kind")

    def available(self, now: float) -> bool:
        return not self.suspended and self.circuit_open_until <= now


class MythosStateStore:
    """Profile-safe persistence for non-secret health and quota metadata."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_hermes_home() / "occult" / "mythos-state.json")

    def save(
        self,
        health: Mapping[str, RouteHealthState],
        quotas: Mapping[str, QuotaPoolState],
    ) -> None:
        payload = {
            "version": 1,
            "health": {key: asdict(value) for key, value in sorted(health.items())},
            "quotas": {key: asdict(value) for key, value in sorted(quotas.items())},
        }
        _assert_secret_free(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def load(self) -> tuple[dict[str, RouteHealthState], dict[str, QuotaPoolState]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            _assert_secret_free(payload)
            if payload.get("version") != 1:
                raise MythosError("unsupported Mythos state version")
            health = {
                _safe_id(key, "card_id"): RouteHealthState(**value)
                for key, value in payload.get("health", {}).items()
            }
            quotas = {
                _safe_id(key, "quota_pool_id"): QuotaPoolState(**value)
                for key, value in payload.get("quotas", {}).items()
            }
            return health, quotas
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, MythosError):
                raise
            raise MythosError("invalid Mythos state file") from None


@dataclass(frozen=True, slots=True)
class RouteResult:
    response: AdapterResponse
    summary: RouteSummary


@dataclass(slots=True)
class MythosRouter:
    adapters: Mapping[str, ProviderAdapter]
    credential_broker: InMemoryCredentialBroker | None = None
    state_store: MythosStateStore | None = None
    failure_threshold: int = 2
    circuit_cooldown_seconds: float = 60.0
    clock: Callable[[], float] = time.time
    _routes: dict[str, MinorArcanaDescriptor] = field(default_factory=dict, init=False)
    _health: dict[str, RouteHealthState] = field(default_factory=dict, init=False)
    _quotas: dict[str, QuotaPoolState] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.circuit_cooldown_seconds < 0:
            raise ValueError("invalid circuit-breaker configuration")
        if self.state_store is not None:
            self._health, self._quotas = self.state_store.load()

    def discover(self, route: MinorArcanaDescriptor) -> MinorArcanaDescriptor:
        """Register as discovered regardless of caller-supplied trust state."""

        if route.adapter_id not in self.adapters:
            raise RouteRegistrationError("route references unknown adapter")
        discovered = replace(route, trust_state=ProviderTrustState.DISCOVERED)
        self._routes[discovered.card_id] = discovered
        self._health.setdefault(discovered.card_id, RouteHealthState())
        self._quotas.setdefault(
            discovered.quota_pool_id,
            QuotaPoolState(quota_pool_id=discovered.quota_pool_id),
        )
        self._persist()
        return discovered

    def review(self, card_id: str, *, approve: bool) -> MinorArcanaDescriptor:
        safe_card_id = _safe_id(card_id, "card_id")
        try:
            route = self._routes[safe_card_id]
        except KeyError:
            raise RouteRegistrationError("unknown route") from None
        target = (
            ProviderTrustState.ACTIVE if approve else ProviderTrustState.QUARANTINED
        )
        reviewed = replace(route, trust_state=target)
        self._routes[safe_card_id] = reviewed
        return reviewed

    def set_quota(
        self,
        quota_pool_id: str,
        *,
        remaining_requests: int | None = None,
        cooldown_until: float = 0.0,
    ) -> None:
        state = QuotaPoolState(
            quota_pool_id=quota_pool_id,
            remaining_requests=remaining_requests,
            cooldown_until=cooldown_until,
        )
        self._quotas[state.quota_pool_id] = state
        self._persist()

    def candidates(
        self,
        invocation: OccultInvocation | Mapping[str, Any],
        *,
        manual_card_id: str | None = None,
    ) -> tuple[MinorArcanaDescriptor, ...]:
        request = (
            invocation
            if isinstance(invocation, OccultInvocation)
            else validate_invocation(invocation)
        )
        now = self.clock()
        mode = request.routing.mode
        if mode is RouteMode.MANUAL and not manual_card_id:
            raise NoEligibleRoute("manual routing requires a card id")
        manual_id = (
            _safe_id(manual_card_id, "manual_card_id")
            if manual_card_id is not None
            else None
        )
        local_required = request.routing.local_only or mode is RouteMode.LOCAL_ONLY
        free_required = request.routing.free_only or mode is RouteMode.FREE_ONLY
        required = set(request.required_capabilities)

        eligible: list[MinorArcanaDescriptor] = []
        for route in self._routes.values():
            if route.trust_state is not ProviderTrustState.ACTIVE:
                continue
            if manual_id is not None and route.card_id != manual_id:
                continue
            if local_required and not route.local:
                continue
            if free_required and not route.free:
                continue
            if route.estimated_request_cost_usd > request.routing.maximum_cost_usd:
                continue
            if not required.issubset(route.capabilities):
                continue
            if not self._health[route.card_id].available(now):
                continue
            if not self._quotas[route.quota_pool_id].available(now):
                continue
            if not self._credential_available(route):
                continue
            eligible.append(route)

        eligible.sort(
            key=lambda route: (
                -self._score(route, mode),
                route.card_id,
            )
        )
        return tuple(eligible)

    def execute(
        self,
        invocation: OccultInvocation | Mapping[str, Any],
        *,
        manual_card_id: str | None = None,
    ) -> RouteResult:
        request = (
            invocation
            if isinstance(invocation, OccultInvocation)
            else validate_invocation(invocation)
        )
        routes = self.candidates(request, manual_card_id=manual_card_id)
        if not routes:
            raise NoEligibleRoute("no eligible Mythos route")

        failures: list[tuple[str, FailureKind]] = []
        maximum_attempts = request.routing.maximum_fallbacks + 1
        attempts = 0
        for route in routes:
            if attempts >= maximum_attempts:
                break
            if not self._route_available_now(route):
                continue
            adapter = self.adapters[route.adapter_id]
            try:
                credential = self._resolve_credential(route)
                response = adapter.invoke(
                    AdapterRequest(
                        invocation_id=request.invocation_id,
                        message=request.input.message,
                        model_id=route.model_id,
                    ),
                    route,
                    credential,
                )
            except CredentialBoundaryError:
                failure = ProviderFailure(FailureKind.AUTHENTICATION)
                attempts += 1
                self._record_failure(route, failure)
                failures.append((route.card_id, failure.kind))
                continue
            except ProviderFailure as failure:
                attempts += 1
                self._record_failure(route, failure)
                failures.append((route.card_id, failure.kind))
                if failure.kind is FailureKind.INVALID_REQUEST:
                    break
                continue
            except Exception:
                failure = ProviderFailure(FailureKind.UNKNOWN)
                attempts += 1
                self._record_failure(route, failure)
                failures.append((route.card_id, failure.kind))
                continue
            except Exception:
                failure = ProviderFailure(FailureKind.UNKNOWN)
                attempts += 1
                self._record_failure(route, failure)
                failures.append((route.card_id, failure.kind))
                continue

            self._record_success(route)
            return RouteResult(
                response=response,
                summary=RouteSummary(
                    invocation_id=request.invocation_id,
                    selected_card_id=route.card_id,
                    provider_id=route.provider_id,
                    model_id=route.model_id,
                    fallback_count=attempts,
                    explanation="selected by validated Mythos policy",
                ),
            )

        raise MythosRoutingError(tuple(failures)) from None

    def status(self) -> dict[str, Any]:
        """Return redacted operational metadata; prompts and refs are omitted."""

        now = self.clock()
        payload = {
            "routes": [
                {
                    "card_id": route.card_id,
                    "provider_id": route.provider_id,
                    "model_id": route.model_id,
                    "trust_state": route.trust_state.value,
                    "healthy": self._health[route.card_id].available(now),
                    "quota_available": self._quotas[route.quota_pool_id].available(now),
                }
                for route in sorted(
                    self._routes.values(), key=lambda item: item.card_id
                )
            ]
        }
        _assert_secret_free(payload)
        return payload

    def routes(self) -> tuple[MinorArcanaDescriptor, ...]:
        """Return immutable route descriptors without credentials or secrets."""

        return tuple(sorted(self._routes.values(), key=lambda route: route.card_id))

    def _credential_available(self, route: MinorArcanaDescriptor) -> bool:
        if route.credential_reference_id is None:
            return route.local
        if self.credential_broker is None:
            return False
        try:
            reference = self.credential_broker.reference(route.credential_reference_id)
        except CredentialBoundaryError:
            return False
        return (
            reference.provider_id == route.provider_id
            and reference.quota_pool_id == route.quota_pool_id
            and reference.is_available()
        )

    def _route_available_now(self, route: MinorArcanaDescriptor) -> bool:
        now = self.clock()
        return (
            self._routes[route.card_id].trust_state is ProviderTrustState.ACTIVE
            and self._health[route.card_id].available(now)
            and self._quotas[route.quota_pool_id].available(now)
            and self._credential_available(route)
        )

    def _resolve_credential(self, route: MinorArcanaDescriptor) -> SecretValue | None:
        if route.credential_reference_id is None:
            if route.local:
                return None
            raise CredentialBoundaryError("external route has no credential reference")
        if self.credential_broker is None:
            raise CredentialBoundaryError("credential broker is unavailable")
        return self.credential_broker.resolve(route.credential_reference_id)

    def _score(self, route: MinorArcanaDescriptor, mode: RouteMode) -> float:
        speed = 1 / (1 + (route.latency_ms / 1000))
        privacy = {
            PrivacyClass.LOCAL: 1.0,
            PrivacyClass.PRIVATE_EXTERNAL: 0.7,
            PrivacyClass.EXTERNAL: 0.3,
        }[route.privacy]
        base = route.quality_score * 0.45 + speed * 0.2 + privacy * 0.2
        base += 0.1 if route.free else 0
        base += 0.05 if route.local else 0
        if mode is RouteMode.QUALITY_FIRST:
            base += route.quality_score
        elif mode is RouteMode.SPEED_FIRST:
            base += speed
        elif mode is RouteMode.PRIVACY_FIRST:
            base += privacy
        elif mode is RouteMode.FREE_FIRST:
            base += 1.0 if route.free else 0
        elif mode is RouteMode.LOCAL_FIRST:
            base += 1.0 if route.local else 0
        return base

    def _record_failure(
        self, route: MinorArcanaDescriptor, failure: ProviderFailure
    ) -> None:
        now = self.clock()
        health = self._health[route.card_id]
        health.consecutive_failures += 1
        health.last_failure_kind = failure.kind.value
        if failure.kind is FailureKind.AUTHENTICATION:
            health.suspended = True
            self._routes[route.card_id] = replace(
                route, trust_state=ProviderTrustState.SUSPENDED
            )
        elif failure.kind is FailureKind.RATE_LIMIT:
            retry_after = (
                failure.retry_after_seconds
                if failure.retry_after_seconds is not None
                else self.circuit_cooldown_seconds
            )
            self._quotas[route.quota_pool_id].cooldown_until = now + max(retry_after, 0)
        elif (
            failure.kind in {FailureKind.UNAVAILABLE, FailureKind.UNKNOWN}
            and health.consecutive_failures >= self.failure_threshold
        ):
            health.circuit_open_until = now + self.circuit_cooldown_seconds
        self._persist()

    def _record_success(self, route: MinorArcanaDescriptor) -> None:
        health = self._health[route.card_id]
        health.consecutive_failures = 0
        health.circuit_open_until = 0
        health.last_failure_kind = None
        health.last_success_at = self.clock()
        quota = self._quotas[route.quota_pool_id]
        if quota.remaining_requests is not None:
            quota.remaining_requests = max(0, quota.remaining_requests - 1)
        self._persist()

    def _persist(self) -> None:
        if self.state_store is not None:
            self.state_store.save(self._health, self._quotas)


__all__ = [
    "AcquisitionAction",
    "AdapterRequest",
    "AdapterResponse",
    "CallableProviderAdapter",
    "FailureKind",
    "LocalProviderAdapter",
    "MinorArcanaDescriptor",
    "MockProviderAdapter",
    "MythosError",
    "MythosRouter",
    "MythosRoutingError",
    "MythosStateStore",
    "NoEligibleRoute",
    "OpenAICompatibleAdapter",
    "PrivacyClass",
    "ProviderFailure",
    "ProviderTrustState",
    "QuotaPoolState",
    "RouteRegistrationError",
    "RouteResult",
    "descriptor_from_provider",
    "enforce_acquisition_policy",
]

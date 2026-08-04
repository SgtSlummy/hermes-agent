"""Deterministic Major/Minor Arcana pairing and policy enforcement.

Pairing sessions snapshot one installed Major Arcana version. Activating or
rolling back a package therefore affects only new sessions, preserving prompt
cache boundaries and identity during an in-flight conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Sequence

from agent.occult.mythos import MinorArcanaDescriptor, PrivacyClass
from agent.occult.tarot_packages import (
    InstalledTarotPackage,
    TarotPackageError,
    TarotPackageManager,
)

_SENSITIVITY_ORDER = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


class PairingError(ValueError):
    """Safe-to-surface pairing or policy failure."""


class ToolAuthorization(StrEnum):
    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A provider-independent memory fragment offered to one pairing."""

    record_id: str
    namespace: str
    sensitivity: str
    content: str

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id is required")
        if not self.namespace.strip():
            raise ValueError("namespace is required")
        if self.sensitivity not in _SENSITIVITY_ORDER:
            raise ValueError("invalid memory sensitivity")
        if not self.content.strip():
            raise ValueError("memory content is required")


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """System-owned ceilings applied after package policy."""

    allowed_memory_namespaces: frozenset[str] = frozenset()
    maximum_memory_sensitivity: str = "internal"
    external_maximum_sensitivity: str = "public"
    allowed_tools: frozenset[str] = frozenset()
    approval_required_tools: frozenset[str] = frozenset()
    tool_risk_levels: Mapping[str, int] = field(default_factory=dict)
    maximum_risk_level: int = 0
    allow_external_routes: bool = False

    def __post_init__(self) -> None:
        if self.maximum_memory_sensitivity not in _SENSITIVITY_ORDER:
            raise ValueError("invalid maximum_memory_sensitivity")
        if self.external_maximum_sensitivity not in _SENSITIVITY_ORDER:
            raise ValueError("invalid external_maximum_sensitivity")
        if not 0 <= self.maximum_risk_level <= 3:
            raise ValueError("maximum_risk_level must be between 0 and 3")
        if not self.approval_required_tools.issubset(self.allowed_tools):
            raise ValueError("approval-required tools must be allowed")
        for tool, risk in self.tool_risk_levels.items():
            if tool not in self.allowed_tools or not 0 <= risk <= 3:
                raise ValueError("invalid tool risk mapping")
        object.__setattr__(
            self, "tool_risk_levels", MappingProxyType(dict(self.tool_risk_levels))
        )


@dataclass(frozen=True, slots=True)
class ToolAuthorizationDecision:
    tool_name: str
    authorization: ToolAuthorization
    reason: str


@dataclass(frozen=True, slots=True)
class PairingContext:
    """Secret-free prompt material for one Major/Minor pairing."""

    agent_id: str
    agent_name: str
    agent_version: str
    arcana_number: int
    orientation: str
    system_prompt: str
    behavior: str
    temperament: Mapping[str, float]
    memories: tuple[MemoryRecord, ...]
    card_id: str
    provider_id: str
    model_id: str
    privacy: PrivacyClass

    def render_system_prompt(self) -> str:
        """Render stable, inspectable prompt material without credentials."""

        temperament = "\n".join(
            f"- {axis}: {value:.3f}" for axis, value in self.temperament.items()
        )
        memory = "\n".join(
            f"- [{item.namespace}/{item.sensitivity}] {item.content}"
            for item in self.memories
        )
        if not memory:
            memory = "- None supplied"
        return (
            f"# Major Arcana\n"
            f"{self.agent_name} ({self.agent_id}, v{self.agent_version}, "
            f"{self.orientation})\n\n"
            f"{self.system_prompt}\n\n"
            f"# Orientation\n{self.behavior}\n\n"
            f"# Temperament\n{temperament}\n\n"
            f"# Permitted memory\n{memory}\n\n"
            f"# Minor Arcana route\n"
            f"{self.card_id} via {self.provider_id}/{self.model_id} "
            f"({self.privacy.value})"
        )


class PairingSession:
    """Immutable Major Arcana identity snapshot for a conversation/session."""

    def __init__(
        self,
        installed: InstalledTarotPackage,
        generation: int,
        runtime_policy: RuntimePolicy,
    ) -> None:
        self._installed = installed
        self.generation = generation
        self.runtime_policy = runtime_policy

    @classmethod
    def start(
        cls,
        manager: TarotPackageManager,
        agent_id: str,
        runtime_policy: RuntimePolicy,
    ) -> PairingSession:
        installed = manager.active(agent_id)
        if installed is None:
            raise PairingError("agent is not active")
        return cls(installed, manager.generation(), runtime_policy)

    @property
    def agent_id(self) -> str:
        return self._installed.package.manifest.agent.id

    @property
    def agent_version(self) -> str:
        return self._installed.package.manifest.agent.version

    def pair(
        self,
        route: MinorArcanaDescriptor,
        *,
        orientation: str = "upright",
        temperament_modifiers: Mapping[str, float] | None = None,
        memories: Sequence[MemoryRecord] = (),
    ) -> PairingContext:
        package = self._installed.package
        manifest = package.manifest
        if orientation not in {"upright", "reversed"}:
            raise PairingError("invalid orientation")
        if orientation == "reversed" and not manifest.orientation.reversed:
            raise PairingError("agent does not support reversed orientation")
        if (
            not package.routing.allow_external
            and not self.runtime_policy.allow_external_routes
            and route.privacy is not PrivacyClass.LOCAL
        ):
            raise PairingError("agent package does not allow external routes")
        if route.estimated_request_cost_usd > 0 and not package.routing.allow_paid:
            raise PairingError("agent package does not allow paid routes")
        if not set(package.routing.required_capabilities).issubset(route.capabilities):
            raise PairingError("route lacks required agent capabilities")

        behavior = (
            package.behavior.upright
            if orientation == "upright"
            else package.behavior.reversed
        )
        if behavior is None:
            raise PairingError("reversed behavior is unavailable")
        return PairingContext(
            agent_id=manifest.agent.id,
            agent_name=manifest.agent.name,
            agent_version=manifest.agent.version,
            arcana_number=manifest.agent.arcana_number,
            orientation=orientation,
            system_prompt=package.system_prompt,
            behavior=behavior,
            temperament=self._temperament(temperament_modifiers or {}),
            memories=self._memories(route, memories),
            card_id=route.card_id,
            provider_id=route.provider_id,
            model_id=route.model_id,
            privacy=route.privacy,
        )

    def authorize_tool(self, tool_name: str) -> ToolAuthorizationDecision:
        """Authorize a requested tool independently from model output."""

        package = self._installed.package
        if (
            tool_name not in package.tools.allowed
            or tool_name not in self.runtime_policy.allowed_tools
        ):
            return ToolAuthorizationDecision(
                tool_name, ToolAuthorization.DENIED, "tool is not allowed"
            )
        risk = self.runtime_policy.tool_risk_levels.get(tool_name, 3)
        ceiling = min(
            package.manifest.permissions.maximum_risk_level,
            self.runtime_policy.maximum_risk_level,
        )
        if risk > ceiling:
            return ToolAuthorizationDecision(
                tool_name, ToolAuthorization.DENIED, "tool exceeds risk ceiling"
            )
        if (
            tool_name in package.tools.approval_required
            or tool_name in self.runtime_policy.approval_required_tools
        ):
            return ToolAuthorizationDecision(
                tool_name,
                ToolAuthorization.APPROVAL_REQUIRED,
                "independent approval is required",
            )
        return ToolAuthorizationDecision(
            tool_name, ToolAuthorization.ALLOWED, "tool policy allows execution"
        )

    def _temperament(self, modifiers: Mapping[str, float]) -> Mapping[str, float]:
        axes = self._installed.package.manifest.temperament
        unknown = set(modifiers) - set(axes)
        if unknown:
            raise PairingError(
                f"unknown temperament axes: {', '.join(sorted(unknown))}"
            )
        values: dict[str, float] = {}
        for name in sorted(axes):
            axis = axes[name]
            modifier = modifiers.get(name, 0.0)
            if isinstance(modifier, bool) or not isinstance(modifier, (int, float)):
                raise PairingError("temperament modifiers must be numeric")
            values[name] = min(
                axis.maximum, max(axis.minimum, axis.default + float(modifier))
            )
        return MappingProxyType(values)

    def _memories(
        self,
        route: MinorArcanaDescriptor,
        memories: Sequence[MemoryRecord],
    ) -> tuple[MemoryRecord, ...]:
        package_memory = self._installed.package.memory
        allowed_namespaces = (
            set(package_memory.namespaces)
            & self.runtime_policy.allowed_memory_namespaces
        )
        limits = [
            package_memory.maximum_sensitivity,
            self.runtime_policy.maximum_memory_sensitivity,
        ]
        if route.privacy is not PrivacyClass.LOCAL:
            limits.extend([
                package_memory.external_maximum_sensitivity,
                self.runtime_policy.external_maximum_sensitivity,
                "internal"
                if route.privacy is PrivacyClass.PRIVATE_EXTERNAL
                else "public",
            ])
        maximum = min(_SENSITIVITY_ORDER[value] for value in limits)
        return tuple(
            record
            for record in memories
            if record.namespace in allowed_namespaces
            and _SENSITIVITY_ORDER[record.sensitivity] <= maximum
        )


__all__ = [
    "MemoryRecord",
    "PairingContext",
    "PairingError",
    "PairingSession",
    "RuntimePolicy",
    "ToolAuthorization",
    "ToolAuthorizationDecision",
]

"""Version 1 contracts shared by Hermes and Occult consumers.

This module owns wire-format validation only. It does not perform routing,
provider discovery, credential access, model invocation, or tool execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

OCCULT_CONTRACT_VERSION = "1.0.0"

SUPPORTED_CAPABILITIES = frozenset(
    {
        "audio_input",
        "audio_output",
        "citations",
        "embeddings",
        "reasoning",
        "streaming",
        "structured_output",
        "text",
        "tool_calling",
        "vision",
    }
)

_TERMINAL_EVENT_TYPES = frozenset(
    {"reading.cancelled", "reading.completed", "reading.failed"}
)
_SECRET_FIELD_NAMES = frozenset(
    {
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
    }
)


class OccultContractError(ValueError):
    """Base class for safe-to-surface Occult contract failures."""


class ContractVersionMismatch(OccultContractError):
    """The caller and Hermes do not implement the same contract version."""


class UnsupportedCapability(OccultContractError):
    """An invocation requires a capability Hermes cannot currently describe."""


class InvalidContractPayload(OccultContractError):
    """A payload is malformed or contains a forbidden field."""


class Orientation(StrEnum):
    UPRIGHT = "upright"
    REVERSED = "reversed"


class RouteMode(StrEnum):
    FREE_FIRST = "free_first"
    FREE_ONLY = "free_only"
    LOCAL_FIRST = "local_first"
    LOCAL_ONLY = "local_only"
    MANUAL = "manual"
    PRIVACY_FIRST = "privacy_first"
    QUALITY_FIRST = "quality_first"
    SPEED_FIRST = "speed_first"


class ReadingState(StrEnum):
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"


class EventType(StrEnum):
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_STARTED = "node.started"
    READING_CANCELLED = "reading.cancelled"
    READING_COMPLETED = "reading.completed"
    READING_FAILED = "reading.failed"
    READING_STARTED = "reading.started"
    ROUTE_SELECTED = "route.selected"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = OCCULT_CONTRACT_VERSION


class InvocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(min_length=1)


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RouteMode = RouteMode.LOCAL_FIRST
    free_only: bool = True
    local_only: bool = False
    maximum_fallbacks: int = Field(default=2, ge=0, le=10)
    maximum_cost_usd: float = Field(default=0.0, ge=0)


class OccultInvocation(_ContractModel):
    invocation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    orientation: Orientation = Orientation.UPRIGHT
    input: InvocationInput
    required_capabilities: tuple[str, ...] = ("text",)
    routing: RoutingPolicy = RoutingPolicy()
    deck_id: str | None = None
    spread_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MajorArcanaAgent(_ContractModel):
    agent_id: str
    name: str
    version: str
    capabilities: tuple[str, ...]
    maximum_risk_level: int = Field(ge=0, le=3)


class MinorArcanaRoute(_ContractModel):
    card_id: str
    provider_id: str
    model_id: str
    capabilities: tuple[str, ...]
    local: bool
    free: bool
    healthy: bool


class DeckDescriptor(_ContractModel):
    deck_id: str
    version: str
    allowed_agent_ids: tuple[str, ...]
    allowed_card_ids: tuple[str, ...]
    routing: RoutingPolicy


class SpreadNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    agent_id: str
    required_capabilities: tuple[str, ...] = ("text",)


class SpreadEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    target: str
    condition: str = "success"


class SpreadDescriptor(_ContractModel):
    spread_id: str
    version: str
    nodes: tuple[SpreadNode, ...]
    edges: tuple[SpreadEdge, ...]


class ReadingDescriptor(_ContractModel):
    reading_id: str
    spread_id: str
    state: ReadingState
    next_sequence: int = Field(ge=0)


class RouteSummary(_ContractModel):
    invocation_id: str
    selected_card_id: str
    provider_id: str
    model_id: str
    fallback_count: int = Field(ge=0)
    explanation: str


class OccultError(_ContractModel):
    code: str
    message: str
    retryable: bool = False
    redacted: Literal[True] = True


class ReadingEvent(_ContractModel):
    event_id: str
    reading_id: str
    sequence: int = Field(ge=0)
    event_type: EventType
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    error: OccultError | None = None


_CONTRACT_MODELS = (
    OccultInvocation,
    MajorArcanaAgent,
    MinorArcanaRoute,
    DeckDescriptor,
    SpreadDescriptor,
    ReadingDescriptor,
    RouteSummary,
    OccultError,
    ReadingEvent,
)
_ModelT = TypeVar("_ModelT", bound=_ContractModel)


def is_occult_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return the explicit Occult feature gate; missing or malformed is off."""

    if not isinstance(config, Mapping):
        return False
    occult = config.get("occult")
    return isinstance(occult, Mapping) and occult.get("enabled") is True


def _normalized_field_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _reject_secret_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _normalized_field_name(key) in _SECRET_FIELD_NAMES:
                raise InvalidContractPayload(
                    f"forbidden secret-shaped field at {path}.{key}"
                )
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


def _require_contract_version(payload: Mapping[str, Any]) -> None:
    actual = payload.get("contract_version")
    if actual != OCCULT_CONTRACT_VERSION:
        raise ContractVersionMismatch(
            "Occult contract version mismatch: "
            f"expected {OCCULT_CONTRACT_VERSION!r}, received {actual!r}"
        )


def _validate_model(
    model: type[_ModelT], payload: Mapping[str, Any]
) -> _ModelT:
    _reject_secret_fields(payload)
    _require_contract_version(payload)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"])
                for error in exc.errors()
            }
        )
        location = ", ".join(fields) or "payload"
        raise InvalidContractPayload(
            f"invalid {model.__name__} fields: {location}"
        ) from None


def _validate_capabilities(capabilities: Sequence[str]) -> None:
    unknown = sorted(set(capabilities) - SUPPORTED_CAPABILITIES)
    if unknown:
        raise UnsupportedCapability(
            "unsupported required capabilities: " + ", ".join(unknown)
        )


def validate_invocation(payload: Mapping[str, Any]) -> OccultInvocation:
    """Validate before any provider selection or execution occurs."""

    invocation = _validate_model(OccultInvocation, payload)
    _validate_capabilities(invocation.required_capabilities)
    return invocation


def validate_deck(payload: Mapping[str, Any]) -> DeckDescriptor:
    """Validate one versioned, secret-free deck descriptor."""

    return _validate_model(DeckDescriptor, payload)


def validate_event_stream(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[ReadingEvent, ...]:
    """Validate stable ordering and a single final terminal reading event."""

    if not payloads:
        raise InvalidContractPayload("event stream must not be empty")

    events = tuple(_validate_model(ReadingEvent, payload) for payload in payloads)
    reading_ids = {event.reading_id for event in events}
    if len(reading_ids) != 1:
        raise InvalidContractPayload("event stream mixes reading ids")

    expected = list(range(events[0].sequence, events[0].sequence + len(events)))
    actual = [event.sequence for event in events]
    if actual != expected:
        raise InvalidContractPayload(
            "event sequence must be contiguous and strictly increasing"
        )

    terminal_indexes = [
        index
        for index, event in enumerate(events)
        if event.event_type.value in _TERMINAL_EVENT_TYPES
    ]
    if terminal_indexes != [len(events) - 1]:
        raise InvalidContractPayload(
            "event stream must end with exactly one terminal reading event"
        )
    return events


def contract_json_schema() -> dict[str, Any]:
    """Return a stable, versioned bundle consumers can export as JSON."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://github.com/SgtSlummy/hermes-agent/"
            "occult/contracts/1.0.0"
        ),
        "title": "Occult System Contract",
        "contract_version": OCCULT_CONTRACT_VERSION,
        "models": {
            model.__name__: model.model_json_schema() for model in _CONTRACT_MODELS
        },
    }


def load_contract_schema() -> dict[str, Any]:
    """Load the packaged, language-neutral v1 JSON Schema bundle."""

    resource = files("agent.occult").joinpath(
        "spec", "v1", "contract.schema.json"
    )
    return json.loads(resource.read_text(encoding="utf-8"))


def load_contract_fixture(name: str) -> Any:
    """Load a packaged v1 fixture without accepting filesystem paths."""

    if not name or name != str(name).replace("\\", "/").split("/")[-1]:
        raise InvalidContractPayload("fixture name must be a plain filename")
    resource = files("agent.occult").joinpath("spec", "v1", "fixtures", name)
    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InvalidContractPayload(f"unknown contract fixture: {name}") from None

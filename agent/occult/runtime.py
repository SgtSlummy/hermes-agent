"""Production assembly for the feature-gated Occult runtime.

This module is the only place that turns the independently testable Occult
components into a live Hermes API surface.  Importing it has no side effects;
the caller must explicitly pass configuration with ``occult.enabled: true``.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.occult.contracts import OCCULT_CONTRACT_VERSION, is_occult_enabled
from agent.occult.decks import DeckError, DeckRegistry
from agent.occult.http import OccultHTTPAdapter
from agent.occult.idempotency import SQLiteInvocationResultStore
from agent.occult.mythos import (
    AdapterRequest,
    AdapterResponse,
    FailureKind,
    LocalProviderAdapter,
    MinorArcanaDescriptor,
    MythosRouter,
    MythosStateStore,
    PrivacyClass,
    ProviderFailure,
)
from agent.occult.pairing import RuntimePolicy
from agent.occult.readings import CouncilNodeRequest, CouncilNodeResult, ReadingStore
from agent.occult.service import OccultService
from agent.occult.tarot_packages import SystemPackagePolicy, TarotPackageManager
from agent.occult.virtual_tokens import SQLiteVirtualTokenStore, VirtualTokenAuthority
from hermes_constants import get_hermes_home

STARTER_CARD_ID = "minor.pentacles.ace.ollama.local"
STARTER_DECK_ID = "occult.deck.starter"
STARTER_AGENT_IDS = (
    "occult.major.magician",
    "occult.major.justice",
    "occult.major.temperance",
    "occult.major.judgement",
    "occult.major.world",
)
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"


class OccultRuntimeError(RuntimeError):
    """Safe-to-surface runtime assembly or local-provider failure."""


def _open_local_url(
    request: urllib.request.Request,
    *,
    timeout: float,
):
    """Open a loopback request without consulting ambient proxy settings."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def normalize_loopback_openai_url(value: str) -> str:
    """Return a canonical loopback OpenAI-compatible base URL."""

    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise OccultRuntimeError("local provider URL must be a loopback HTTP URL")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise OccultRuntimeError("local provider URL must resolve to loopback")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif path != "/v1":
        raise OccultRuntimeError("local provider URL path must be /v1")
    authority = parsed.netloc
    return urllib.parse.urlunparse((parsed.scheme, authority, path, "", "", ""))


def discover_ollama_models(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    *,
    timeout_seconds: float = 3.0,
) -> tuple[str, ...]:
    """Discover local Ollama models without sending credentials."""

    canonical = normalize_loopback_openai_url(base_url)
    request = urllib.request.Request(
        canonical + "/models",
        headers={"Accept": "application/json"},
    )
    try:
        with _open_local_url(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        raise OccultRuntimeError(
            "Ollama is unavailable; start Ollama and pull at least one model"
        ) from None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise OccultRuntimeError("Ollama returned an invalid model catalog")
    models = sorted({
        str(item.get("id", "")).strip()
        for item in data
        if isinstance(item, Mapping) and str(item.get("id", "")).strip()
    })
    if not models:
        raise OccultRuntimeError(
            "Ollama has no models; run 'ollama pull qwen2.5:3b' and retry"
        )
    return tuple(models)


def validate_ollama_chat_model(
    base_url: str,
    model_id: str,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Verify that an installed Ollama model accepts chat completions."""

    canonical = normalize_loopback_openai_url(base_url)
    selected_model = str(model_id or "").strip()
    if not selected_model:
        raise OccultRuntimeError("local chat model is required")
    body = json.dumps(
        {
            "model": selected_model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
            "stream": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        canonical + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with _open_local_url(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
            or not isinstance(choices[0].get("message"), Mapping)
        ):
            raise ValueError
    except (OSError, ValueError, urllib.error.URLError):
        raise OccultRuntimeError(
            f"Ollama model does not support chat completions: {selected_model}"
        ) from None


def _ollama_handler(
    base_url: str,
    timeout_seconds: float,
):
    canonical = normalize_loopback_openai_url(base_url)

    def invoke(
        request: AdapterRequest,
        _route: MinorArcanaDescriptor,
        _credential,
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
            canonical + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with _open_local_url(
                outbound,
                timeout=timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ProviderFailure(FailureKind.RATE_LIMIT) from None
            if exc.code in {408, 500, 502, 503, 504}:
                raise ProviderFailure(FailureKind.UNAVAILABLE) from None
            raise ProviderFailure(FailureKind.INVALID_REQUEST) from None
        except TimeoutError:
            raise ProviderFailure(FailureKind.TIMEOUT) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise ProviderFailure(FailureKind.UNAVAILABLE) from None

        try:
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderFailure(FailureKind.INVALID_RESPONSE) from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderFailure(FailureKind.INVALID_RESPONSE)
        return AdapterResponse(
            text=text,
            input_tokens=max(input_tokens, 0),
            output_tokens=max(output_tokens, 0),
        )

    return invoke


def _reading_executor(
    service: OccultService,
    readings: ReadingStore,
):
    """Adapt persisted Council nodes into authenticated Occult invocations."""

    def execute(
        plaintext_token: str,
        request: CouncilNodeRequest,
    ) -> CouncilNodeResult:
        cached = readings.cached_node_result(request.idempotency_key)
        if cached is not None:
            return cached
        sections = [request.task]
        for reference in request.input_artifact_references:
            artifact = readings.artifact(reference)
            sections.append(
                "# Dependency artifact\n"
                + json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            )
        digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
        result = service.invoke(
            plaintext_token,
            {
                "contract_version": request.contract_version,
                "invocation_id": f"inv_reading_{digest[:24]}",
                "idempotency_key": request.idempotency_key,
                "agent_id": request.agent_id,
                "orientation": "upright",
                "input": {"message": "\n\n".join(sections)},
                "required_capabilities": ["text"],
                "routing": {
                    "mode": "local_only",
                    "free_only": True,
                    "local_only": True,
                    "maximum_fallbacks": 0,
                    "maximum_cost_usd": 0,
                },
                "deck_id": STARTER_DECK_ID,
                "spread_id": request.reading_id,
                "metadata": {
                    "reading_id": request.reading_id,
                    "node_id": request.node_id,
                },
            },
            _skip_idempotency=True,
        )
        node_result = CouncilNodeResult(
            artifact={
                "content": result["output"],
                "media_type": "text/plain",
            },
            route_summary=result["route"],
        )
        return node_result

    return execute


def _starter_root() -> Path:
    return Path(__file__).with_name("starters")


def _trusted_starter_signers() -> dict[str, bytes]:
    path = _starter_root() / "starter_signers.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        signers = payload["signers"]
        return {
            str(signer_id): base64.b64decode(value, validate=True)
            for signer_id, value in signers.items()
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise OccultRuntimeError("bundled starter signer registry is invalid") from None


def _install_starter_agents(manager: TarotPackageManager) -> None:
    archives = sorted(_starter_root().glob("*.tarot"))
    if not archives:
        raise OccultRuntimeError("bundled starter agents are missing")
    for archive in archives:
        package = manager.validate(archive)
        agent_id = package.manifest.agent.id
        version = package.manifest.agent.version
        destination = manager.packages_root / agent_id / version
        if not destination.exists():
            manager.install(archive)
        else:
            manager.load(agent_id, version)
        if manager.active(agent_id) is None:
            manager.activate(agent_id, version)
    installed = {item.package.manifest.agent.id for item in manager.active_packages()}
    missing = set(STARTER_AGENT_IDS) - installed
    if missing:
        raise OccultRuntimeError("bundled starter agent set is incomplete")


def _install_starter_deck(
    registry: DeckRegistry,
    *,
    model_card_id: str,
) -> None:
    try:
        registry.get(STARTER_DECK_ID)
        return
    except DeckError:
        pass
    registry.put(
        {
            "contract_version": OCCULT_CONTRACT_VERSION,
            "deck_id": STARTER_DECK_ID,
            "version": "1.0.0",
            "allowed_agent_ids": list(STARTER_AGENT_IDS),
            "allowed_card_ids": [model_card_id],
            "routing": {
                "mode": "local_only",
                "free_only": True,
                "local_only": True,
                "maximum_fallbacks": 0,
                "maximum_cost_usd": 0.0,
            },
        },
        available_agent_ids=STARTER_AGENT_IDS,
        available_card_ids=(model_card_id,),
    )


def build_occult_http(
    config: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> OccultHTTPAdapter | None:
    """Assemble the live Occult HTTP adapter when explicitly enabled."""

    if not is_occult_enabled(config):
        return None
    occult = config.get("occult") if isinstance(config, Mapping) else None
    if not isinstance(occult, Mapping):
        raise OccultRuntimeError("occult configuration must be an object")
    configured_contract = str(occult.get("contract_version", "")).strip()
    if configured_contract != OCCULT_CONTRACT_VERSION:
        raise OccultRuntimeError(
            "occult.contract_version is incompatible with this Hermes release"
        )
    model_id = str(occult.get("local_model", "")).strip()
    if not model_id:
        raise OccultRuntimeError(
            "occult.local_model is required; run 'hermes occult init'"
        )
    base_url = normalize_loopback_openai_url(
        str(occult.get("local_base_url", DEFAULT_OLLAMA_BASE_URL))
    )
    try:
        timeout_seconds = float(occult.get("provider_timeout_seconds", 120))
        maximum_concurrency = int(occult.get("maximum_concurrent_requests", 4))
    except (TypeError, ValueError) as exc:
        raise OccultRuntimeError(
            "Occult numeric settings must contain valid numbers"
        ) from exc
    if not 1 <= timeout_seconds <= 600:
        raise OccultRuntimeError("occult.provider_timeout_seconds must be 1-600")
    if not 1 <= maximum_concurrency <= 64:
        raise OccultRuntimeError("occult.maximum_concurrent_requests must be 1-64")

    profile_home = home or get_hermes_home()
    root = profile_home / "occult"
    package_manager = TarotPackageManager(
        trusted_signers=_trusted_starter_signers(),
        system_policy=SystemPackagePolicy(
            available_tools=frozenset(),
            maximum_risk_level=0,
            allow_paid=False,
            allow_external=False,
            maximum_memory_sensitivity="internal",
            external_maximum_sensitivity="public",
        ),
        root=root / "major_arcana",
    )
    _install_starter_agents(package_manager)

    adapter = LocalProviderAdapter(_ollama_handler(base_url, timeout_seconds))
    router = MythosRouter(
        adapters={adapter.adapter_id: adapter},
        state_store=MythosStateStore(root / "mythos-state.json"),
        maximum_concurrent_requests=maximum_concurrency,
    )
    route = MinorArcanaDescriptor(
        card_id=STARTER_CARD_ID,
        provider_id="ollama-local",
        model_id=model_id,
        adapter_id=adapter.adapter_id,
        capabilities=frozenset({"text"}),
        local=True,
        free=True,
        privacy=PrivacyClass.LOCAL,
        quality_score=0.5,
        latency_ms=1000,
        estimated_request_cost_usd=0.0,
        quota_pool_id="ollama-local:default",
    )
    router.discover(route)
    router.review(route.card_id, approve=True)

    token_store = SQLiteVirtualTokenStore(root / "virtual_tokens.db")
    token_authority = VirtualTokenAuthority(store=token_store)
    deck_registry = DeckRegistry(root / "decks.json")
    _install_starter_deck(deck_registry, model_card_id=route.card_id)
    service = OccultService(
        package_manager=package_manager,
        router=router,
        token_authority=token_authority,
        runtime_policy=RuntimePolicy(
            allowed_memory_namespaces=frozenset({"project", "agent", "reading"}),
            maximum_memory_sensitivity="internal",
            external_maximum_sensitivity="public",
            allowed_tools=frozenset(),
            maximum_risk_level=0,
        ),
        deck_registry=deck_registry,
        invocation_store=SQLiteInvocationResultStore(root / "invocations.db"),
    )
    readings = ReadingStore(root / "readings.db")
    env = os.environ if environ is None else environ
    admin_key = str(env.get("OCCULT_ADMIN_KEY", "")).strip()
    return OccultHTTPAdapter(
        service=service,
        readings=readings,
        reading_executor=_reading_executor(service, readings),
        admin_key_digest=(
            OccultHTTPAdapter.digest_admin_key(admin_key) if admin_key else None
        ),
    )


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "OccultRuntimeError",
    "STARTER_AGENT_IDS",
    "STARTER_CARD_ID",
    "STARTER_DECK_ID",
    "build_occult_http",
    "discover_ollama_models",
    "normalize_loopback_openai_url",
    "validate_ollama_chat_model",
]

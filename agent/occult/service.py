"""Authenticated Occult application service.

The service composes contracts, virtual-token policy, Major Arcana snapshots,
and Mythos routing. It is synchronous because the current provider adapter
contract is synchronous; HTTP handlers may dispatch it to a worker thread.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.occult.contracts import OccultInvocation, RoutingPolicy, validate_invocation
from agent.occult.decks import DeckError, DeckRegistry
from agent.occult.idempotency import SQLiteInvocationResultStore
from agent.occult.mythos import MythosRouter
from agent.occult.pairing import (
    MemoryRecord,
    PairingSession,
    RuntimePolicy,
    ToolAuthorization,
)
from agent.occult.provider_catalog import ProviderCatalog, load_bundled_provider_catalog
from agent.occult.tarot_packages import TarotPackageManager
from agent.occult.virtual_tokens import VirtualTokenAuthority, VirtualTokenError


@dataclass(slots=True)
class OccultService:
    package_manager: TarotPackageManager
    router: MythosRouter
    token_authority: VirtualTokenAuthority
    runtime_policy: RuntimePolicy
    deck_registry: DeckRegistry | None = None
    invocation_store: SQLiteInvocationResultStore | None = None
    provider_catalog: ProviderCatalog = field(default_factory=load_bundled_provider_catalog)

    def agents(self, plaintext_token: str) -> tuple[dict[str, Any], ...]:
        policy = self.token_authority.policy(plaintext_token)
        result = []
        for installed in self.package_manager.active_packages():
            agent = installed.package.manifest.agent
            if policy.allowed_agent_ids and agent.id not in policy.allowed_agent_ids:
                continue
            result.append({
                "agent_id": agent.id,
                "name": agent.name,
                "version": agent.version,
                "arcana_number": agent.arcana_number,
                "capabilities": list(installed.package.manifest.capabilities),
            })
        return tuple(result)

    def routes(self, plaintext_token: str) -> tuple[dict[str, Any], ...]:
        policy = self.token_authority.policy(plaintext_token)
        return tuple(
            {
                "card_id": route.card_id,
                "provider_id": route.provider_id,
                "model_id": route.model_id,
                "capabilities": sorted(route.capabilities),
                "local": route.local,
                "free": route.free,
                "privacy": route.privacy.value,
                "trust_state": route.trust_state.value,
            }
            for route in self.router.routes()
            if not policy.allowed_card_ids or route.card_id in policy.allowed_card_ids
        )

    def providers(self, plaintext_token: str) -> dict[str, Any]:
        """Return secret-free catalog policy and live-route counts."""

        self.token_authority.policy(plaintext_token)
        active_counts: dict[str, int] = {}
        for route in self.router.routes():
            active_counts[route.provider_id] = active_counts.get(route.provider_id, 0) + 1
        catalog = []
        for provider in self.provider_catalog.list():
            item = provider.public_contract()
            item["active_route_count"] = active_counts.get(provider.provider_id, 0)
            item["activation"] = (
                "active"
                if item["active_route_count"]
                else item["activation"]
            )
            catalog.append(item)
        return {"summary": self.provider_catalog.summary(), "providers": catalog}

    def decks(self, plaintext_token: str) -> tuple[dict[str, Any], ...]:
        policy = self.token_authority.policy(plaintext_token)
        if self.deck_registry is None:
            return ()
        result = []
        for descriptor in self.deck_registry.list():
            if policy.allowed_agent_ids and not set(
                descriptor.allowed_agent_ids
            ).issubset(policy.allowed_agent_ids):
                continue
            if policy.allowed_card_ids and not set(
                descriptor.allowed_card_ids
            ).issubset(policy.allowed_card_ids):
                continue
            payload = descriptor.model_dump(mode="json")
            payload["active"] = True
            result.append(payload)
        return tuple(result)

    def pairings(
        self, plaintext_token: str, *, agent_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        policy = self.token_authority.policy(plaintext_token)
        pairs = []
        for installed in self.package_manager.active_packages():
            current_agent = installed.package.manifest.agent.id
            if agent_id is not None and current_agent != agent_id:
                continue
            if (
                policy.allowed_agent_ids
                and current_agent not in policy.allowed_agent_ids
            ):
                continue
            session = PairingSession.start(
                self.package_manager, current_agent, self.runtime_policy
            )
            for route in self.router.routes():
                if (
                    policy.allowed_card_ids
                    and route.card_id not in policy.allowed_card_ids
                ):
                    continue
                try:
                    session.pair(route)
                except ValueError:
                    continue
                pairs.append({
                    "agent_id": current_agent,
                    "agent_version": session.agent_version,
                    "card_id": route.card_id,
                    "provider_id": route.provider_id,
                    "model_id": route.model_id,
                })
        return tuple(pairs)

    def validate_deck(self, plaintext_token: str, deck_id: str) -> dict[str, Any]:
        policy = self.token_authority.policy(plaintext_token)
        if self.deck_registry is None:
            raise DeckError("deck runtime is not configured")
        deck = self.deck_registry.get(deck_id)
        if (
            policy.allowed_agent_ids
            and not set(deck.allowed_agent_ids).issubset(policy.allowed_agent_ids)
        ) or (
            policy.allowed_card_ids
            and not set(deck.allowed_card_ids).issubset(policy.allowed_card_ids)
        ):
            raise VirtualTokenError("virtual token does not allow requested deck")
        status = self.deck_registry.validate_current(
            deck.deck_id,
            available_agent_ids=(
                installed.package.manifest.agent.id
                for installed in self.package_manager.active_packages()
            ),
            available_card_ids=(route.card_id for route in self.router.routes()),
        )
        pairings = self.pairings(plaintext_token)
        compatible = sum(
            1
            for pairing in pairings
            if pairing["agent_id"] in deck.allowed_agent_ids
            and pairing["card_id"] in deck.allowed_card_ids
        )
        status["compatible_pairings"] = compatible
        status["valid"] = bool(status["valid"] and compatible)
        return status

    def invoke(
        self,
        plaintext_token: str,
        payload: OccultInvocation | Mapping[str, Any],
        *,
        manual_card_id: str | None = None,
        memories: Sequence[MemoryRecord] = (),
        requested_tools: frozenset[str] = frozenset(),
        _skip_idempotency: bool = False,
    ) -> dict[str, Any]:
        """Validate, authorize, pair, route, and return a secret-free result."""

        request = (
            payload
            if isinstance(payload, OccultInvocation)
            else validate_invocation(payload)
        )
        token_policy = self.token_authority.policy(plaintext_token)
        if (
            token_policy.allowed_agent_ids
            and request.agent_id not in token_policy.allowed_agent_ids
        ):
            raise VirtualTokenError("virtual token does not allow requested agent")
        allowed_deck_cards: frozenset[str] | None = None
        if request.deck_id is not None:
            if self.deck_registry is None:
                raise DeckError("deck runtime is not configured")
            deck = self.deck_registry.get(request.deck_id)
            if request.agent_id not in deck.allowed_agent_ids:
                raise DeckError("deck does not allow requested agent")
            allowed_deck_cards = frozenset(deck.allowed_card_ids)
            request = request.model_copy(
                update={
                    "routing": RoutingPolicy(
                        mode=deck.routing.mode,
                        free_only=(request.routing.free_only or deck.routing.free_only),
                        local_only=(
                            request.routing.local_only or deck.routing.local_only
                        ),
                        maximum_fallbacks=min(
                            request.routing.maximum_fallbacks,
                            deck.routing.maximum_fallbacks,
                        ),
                        maximum_cost_usd=min(
                            request.routing.maximum_cost_usd,
                            deck.routing.maximum_cost_usd,
                        ),
                    )
                }
            )
        memory_namespaces = frozenset(record.namespace for record in memories)
        if self.invocation_store is not None and not _skip_idempotency:
            semantic_request = request.model_dump(mode="json")
            semantic_request.pop("invocation_id", None)
            semantic_request.pop("idempotency_key", None)
            fingerprint_payload = {
                "request": semantic_request,
                "manual_card_id": manual_card_id,
                "memories": [asdict(record) for record in memories],
                "requested_tools": sorted(requested_tools),
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            def authorize_replay(result: Mapping[str, Any]) -> None:
                route_summary = result.get("route")
                card_id = (
                    route_summary.get("selected_card_id")
                    if isinstance(route_summary, Mapping)
                    else None
                )
                if not isinstance(card_id, str) or not card_id:
                    raise VirtualTokenError("stored invocation route is invalid")
                if (
                    token_policy.allowed_card_ids
                    and card_id not in token_policy.allowed_card_ids
                ) or (
                    allowed_deck_cards is not None
                    and card_id not in allowed_deck_cards
                ):
                    raise VirtualTokenError(
                        "virtual token does not allow stored invocation route"
                    )
                replay_lease = self.token_authority.reserve(
                    plaintext_token,
                    agent_id=request.agent_id,
                    card_id=card_id,
                    tools=requested_tools,
                    memory_namespaces=memory_namespaces,
                    maximum_cost_usd=0.0,
                )
                replay_lease.release()

            return self.invocation_store.run(
                token_policy.token_id,
                request.idempotency_key,
                fingerprint,
                lambda: self.invoke(
                    plaintext_token,
                    request,
                    manual_card_id=manual_card_id,
                    memories=memories,
                    requested_tools=requested_tools,
                    _skip_idempotency=True,
                ),
                on_replay=authorize_replay,
            )
        candidates = tuple(
            route
            for route in self.router.candidates(request, manual_card_id=manual_card_id)
            if not token_policy.allowed_card_ids
            or route.card_id in token_policy.allowed_card_ids
            if allowed_deck_cards is None or route.card_id in allowed_deck_cards
        )
        if not candidates:
            raise VirtualTokenError("virtual token has no eligible permitted route")
        route = candidates[0]
        maximum_cost = route.estimated_request_cost_usd
        session = PairingSession.start(
            self.package_manager,
            request.agent_id,
            self.runtime_policy,
        )
        context = session.pair(
            route,
            orientation=request.orientation.value,
            memories=memories,
        )
        for tool_name in sorted(requested_tools):
            decision = session.authorize_tool(tool_name)
            if decision.authorization is ToolAuthorization.DENIED:
                raise VirtualTokenError(decision.reason)
            if decision.authorization is ToolAuthorization.APPROVAL_REQUIRED:
                raise VirtualTokenError(
                    f"tool requires independent approval: {tool_name}"
                )

        lease = self.token_authority.reserve(
            plaintext_token,
            agent_id=request.agent_id,
            card_id=route.card_id,
            tools=requested_tools,
            memory_namespaces=memory_namespaces,
            maximum_cost_usd=maximum_cost,
        )
        try:
            composed_message = (
                context.render_system_prompt() + "\n\n# Task\n" + request.input.message
            )
            routed_request = request.model_copy(
                update={
                    "input": request.input.model_copy(
                        update={"message": composed_message}
                    )
                }
            )
            result = self.router.execute(routed_request, manual_card_id=route.card_id)
            lease.commit(route.estimated_request_cost_usd)
        except Exception:
            lease.release()
            raise
        return {
            "contract_version": request.contract_version,
            "invocation_id": request.invocation_id,
            "agent_id": context.agent_id,
            "agent_version": context.agent_version,
            "orientation": context.orientation,
            "output": result.response.text,
            "usage": {
                "input_tokens": result.response.input_tokens,
                "output_tokens": result.response.output_tokens,
            },
            "route": result.summary.model_dump(mode="json"),
        }


__all__ = ["OccultService"]

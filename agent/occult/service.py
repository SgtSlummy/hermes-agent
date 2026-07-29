"""Authenticated Occult application service.

The service composes contracts, virtual-token policy, Major Arcana snapshots,
and Mythos routing. It is synchronous because the current provider adapter
contract is synchronous; HTTP handlers may dispatch it to a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.occult.contracts import OccultInvocation, validate_invocation
from agent.occult.mythos import MythosRouter
from agent.occult.pairing import (
    MemoryRecord,
    PairingSession,
    RuntimePolicy,
    ToolAuthorization,
)
from agent.occult.tarot_packages import TarotPackageManager
from agent.occult.virtual_tokens import VirtualTokenAuthority, VirtualTokenError


@dataclass(slots=True)
class OccultService:
    package_manager: TarotPackageManager
    router: MythosRouter
    token_authority: VirtualTokenAuthority
    runtime_policy: RuntimePolicy

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

    def invoke(
        self,
        plaintext_token: str,
        payload: OccultInvocation | Mapping[str, Any],
        *,
        manual_card_id: str | None = None,
        memories: Sequence[MemoryRecord] = (),
        requested_tools: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Validate, authorize, pair, route, and return a secret-free result."""

        request = (
            payload
            if isinstance(payload, OccultInvocation)
            else validate_invocation(payload)
        )
        token_policy = self.token_authority.policy(plaintext_token)
        candidates = tuple(
            route
            for route in self.router.candidates(request, manual_card_id=manual_card_id)
            if not token_policy.allowed_card_ids
            or route.card_id in token_policy.allowed_card_ids
        )
        if not candidates:
            raise VirtualTokenError("virtual token has no eligible permitted route")
        route = candidates[0]
        memory_namespaces = frozenset(record.namespace for record in memories)
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

"""Scoped virtual tokens for Occult-facing Hermes surfaces.

Only a SHA-256 digest of each high-entropy token is retained. Callers receive
the plaintext once and agents never receive provider credentials.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable


class VirtualTokenError(PermissionError):
    """Safe-to-surface authentication, authorization, or budget failure."""


@dataclass(frozen=True, slots=True)
class VirtualTokenPolicy:
    token_id: str
    allowed_agent_ids: frozenset[str] = frozenset()
    allowed_card_ids: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()
    allowed_memory_namespaces: frozenset[str] = frozenset()
    requests_per_minute: int = 60
    maximum_budget_usd: float = 0.0
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not self.token_id.strip():
            raise ValueError("token_id is required")
        if self.requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        if self.maximum_budget_usd < 0:
            raise ValueError("maximum_budget_usd cannot be negative")
        if self.expires_at is not None and self.expires_at <= 0:
            raise ValueError("expires_at must be a positive timestamp")


@dataclass(slots=True)
class _TokenState:
    policy: VirtualTokenPolicy
    digest: bytes
    calls: deque[float] = field(default_factory=deque)
    committed_cost_usd: float = 0.0
    reserved_cost_usd: float = 0.0
    revoked: bool = False


class VirtualTokenLease:
    """A bounded cost reservation released on failure or cancellation."""

    def __init__(
        self,
        authority: VirtualTokenAuthority,
        token_id: str,
        reserved_cost_usd: float,
    ) -> None:
        self._authority = authority
        self.token_id = token_id
        self.reserved_cost_usd = reserved_cost_usd
        self._closed = False

    def commit(self, actual_cost_usd: float = 0.0) -> None:
        if self._closed:
            raise VirtualTokenError("token reservation is already closed")
        self._authority._finish(
            self.token_id,
            reserved=self.reserved_cost_usd,
            actual=actual_cost_usd,
        )
        self._closed = True

    def release(self) -> None:
        if not self._closed:
            self._authority._finish(
                self.token_id,
                reserved=self.reserved_cost_usd,
                actual=None,
            )
            self._closed = True

    def __enter__(self) -> VirtualTokenLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.release()


class VirtualTokenAuthority:
    """Thread-safe virtual-token issuer and policy enforcement boundary."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._tokens: dict[str, _TokenState] = {}
        self._digest_index: dict[bytes, str] = {}
        self._lock = RLock()

    def issue(self, policy: VirtualTokenPolicy) -> str:
        """Issue a token once; retain only its digest."""

        with self._lock:
            if policy.token_id in self._tokens:
                raise VirtualTokenError("token id already exists")
            plaintext = "occult_" + secrets.token_urlsafe(32)
            digest = self._digest(plaintext)
            self._tokens[policy.token_id] = _TokenState(policy, digest)
            self._digest_index[digest] = policy.token_id
            return plaintext

    def revoke(self, token_id: str) -> None:
        with self._lock:
            state = self._tokens.get(token_id)
            if state is None:
                raise VirtualTokenError("unknown virtual token")
            state.revoked = True

    def policy(self, plaintext: str) -> VirtualTokenPolicy:
        with self._lock:
            return self._authenticate(plaintext).policy

    def reserve(
        self,
        plaintext: str,
        *,
        agent_id: str,
        card_id: str | None = None,
        tools: frozenset[str] = frozenset(),
        memory_namespaces: frozenset[str] = frozenset(),
        maximum_cost_usd: float = 0.0,
    ) -> VirtualTokenLease:
        if maximum_cost_usd < 0:
            raise VirtualTokenError("maximum cost cannot be negative")
        with self._lock:
            state = self._authenticate(plaintext)
            policy = state.policy
            self._authorize_value(policy.allowed_agent_ids, agent_id, "agent")
            if card_id is not None:
                self._authorize_value(policy.allowed_card_ids, card_id, "route")
            if not tools.issubset(policy.allowed_tools):
                raise VirtualTokenError("virtual token does not allow requested tools")
            if not memory_namespaces.issubset(policy.allowed_memory_namespaces):
                raise VirtualTokenError("virtual token does not allow requested memory")
            self._consume_rate_limit(state)
            projected = (
                state.committed_cost_usd + state.reserved_cost_usd + maximum_cost_usd
            )
            if projected > policy.maximum_budget_usd:
                raise VirtualTokenError("virtual token budget exceeded")
            state.reserved_cost_usd += maximum_cost_usd
            return VirtualTokenLease(self, policy.token_id, maximum_cost_usd)

    def status(self, token_id: str) -> dict[str, object]:
        """Return secret-free operational metadata."""

        with self._lock:
            state = self._tokens.get(token_id)
            if state is None:
                raise VirtualTokenError("unknown virtual token")
            return {
                "token_id": state.policy.token_id,
                "revoked": state.revoked,
                "expires_at": state.policy.expires_at,
                "requests_in_window": len(state.calls),
                "committed_cost_usd": state.committed_cost_usd,
                "reserved_cost_usd": state.reserved_cost_usd,
            }

    def _authenticate(self, plaintext: str) -> _TokenState:
        if not isinstance(plaintext, str) or not plaintext.startswith("occult_"):
            raise VirtualTokenError("invalid virtual token")
        digest = self._digest(plaintext)
        token_id = self._digest_index.get(digest)
        state = self._tokens.get(token_id or "")
        if state is None or not secrets.compare_digest(state.digest, digest):
            raise VirtualTokenError("invalid virtual token")
        if state.revoked:
            raise VirtualTokenError("virtual token is revoked")
        if (
            state.policy.expires_at is not None
            and self.clock() >= state.policy.expires_at
        ):
            raise VirtualTokenError("virtual token is expired")
        return state

    def _consume_rate_limit(self, state: _TokenState) -> None:
        now = self.clock()
        cutoff = now - 60
        while state.calls and state.calls[0] <= cutoff:
            state.calls.popleft()
        if len(state.calls) >= state.policy.requests_per_minute:
            raise VirtualTokenError("virtual token rate limit exceeded")
        state.calls.append(now)

    def _finish(
        self,
        token_id: str,
        *,
        reserved: float,
        actual: float | None,
    ) -> None:
        with self._lock:
            state = self._tokens[token_id]
            state.reserved_cost_usd = max(0.0, state.reserved_cost_usd - reserved)
            if actual is None:
                return
            if actual < 0:
                raise VirtualTokenError("actual cost cannot be negative")
            if (
                state.committed_cost_usd + state.reserved_cost_usd + actual
                > state.policy.maximum_budget_usd
            ):
                raise VirtualTokenError("actual cost exceeds virtual token budget")
            state.committed_cost_usd += actual

    @staticmethod
    def _authorize_value(allowed: frozenset[str], value: str, kind: str) -> None:
        if allowed and value not in allowed:
            raise VirtualTokenError(f"virtual token does not allow requested {kind}")

    @staticmethod
    def _digest(plaintext: str) -> bytes:
        return hashlib.sha256(plaintext.encode("utf-8")).digest()


__all__ = [
    "VirtualTokenAuthority",
    "VirtualTokenError",
    "VirtualTokenLease",
    "VirtualTokenPolicy",
]

"""Scoped virtual tokens for Occult-facing Hermes surfaces.

Only a SHA-256 digest of each high-entropy token is retained. Callers receive
the plaintext once and agents never receive provider credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Callable

from hermes_constants import get_hermes_home

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STORE_SCHEMA_VERSION = 1


def _decode_scope(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise ValueError("invalid stored virtual token scope")
    decoded: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SAFE_IDENTIFIER.fullmatch(item):
            raise ValueError("invalid stored virtual token scope")
        decoded.append(item)
    return frozenset(decoded)


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
        if not isinstance(self.token_id, str) or not _SAFE_IDENTIFIER.fullmatch(
            self.token_id
        ):
            raise ValueError("invalid token_id")
        for field_name in (
            "allowed_agent_ids",
            "allowed_card_ids",
            "allowed_tools",
            "allowed_memory_namespaces",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, frozenset) or any(
                not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value)
                for value in values
            ):
                raise ValueError(f"invalid {field_name}")
        if (
            isinstance(self.requests_per_minute, bool)
            or not isinstance(self.requests_per_minute, int)
            or self.requests_per_minute < 1
        ):
            raise ValueError("requests_per_minute must be positive")
        if (
            isinstance(self.maximum_budget_usd, bool)
            or not isinstance(self.maximum_budget_usd, (int, float))
            or not math.isfinite(self.maximum_budget_usd)
            or self.maximum_budget_usd < 0
        ):
            raise ValueError("maximum_budget_usd cannot be negative")
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at)
            or self.expires_at <= 0
        ):
            raise ValueError("expires_at must be a positive timestamp")


@dataclass(slots=True)
class _TokenState:
    policy: VirtualTokenPolicy
    digest: bytes
    calls: deque[float] = field(default_factory=deque)
    committed_cost_usd: float = 0.0
    reserved_cost_usd: float = 0.0
    revoked: bool = False


class SQLiteVirtualTokenStore:
    """Profile-scoped digest and policy persistence for virtual tokens."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_hermes_home() / "occult" / "virtual_tokens.db")
        self._lock = RLock()
        try:
            self._prepare_path()
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._initialize_schema()
        except VirtualTokenError:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                connection.close()
            raise VirtualTokenError("virtual token store is unavailable") from exc

    def load(self) -> tuple[_TokenState, ...]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM virtual_tokens ORDER BY token_id"
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise VirtualTokenError("virtual token store is unavailable") from exc
        states: list[_TokenState] = []
        for row in rows:
            try:
                policy = self._decode_policy(str(row["policy_json"]))
                digest = bytes(row["digest"])
                committed_cost = float(row["committed_cost_usd"])
                revoked_value = row["revoked"]
            except (TypeError, ValueError) as exc:
                raise VirtualTokenError(
                    "stored virtual token state is invalid"
                ) from exc
            if (
                policy.token_id != row["token_id"]
                or len(digest) != hashlib.sha256().digest_size
                or not math.isfinite(committed_cost)
                or committed_cost < 0
                or revoked_value not in (0, 1)
            ):
                raise VirtualTokenError("stored virtual token policy is inconsistent")
            states.append(
                _TokenState(
                    policy=policy,
                    digest=digest,
                    committed_cost_usd=committed_cost,
                    revoked=bool(revoked_value),
                )
            )
        return tuple(states)

    def insert(self, policy: VirtualTokenPolicy, digest: bytes) -> None:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("invalid virtual token digest")
        now = time.time()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO virtual_tokens (
                        token_id, digest, policy_json, committed_cost_usd,
                        revoked, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        policy.token_id,
                        sqlite3.Binary(digest),
                        self._encode_policy(policy),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise VirtualTokenError("token id or digest already exists") from exc
        except sqlite3.DatabaseError as exc:
            raise VirtualTokenError("virtual token store is unavailable") from exc

    def update_committed_cost(self, token_id: str, amount: float) -> None:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("invalid committed virtual token cost")
        self._update(token_id, "committed_cost_usd", amount)

    def update_revoked(self, token_id: str, revoked: bool) -> None:
        self._update(token_id, "revoked", int(revoked))

    def delete(self, token_id: str) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM virtual_tokens WHERE token_id = ?",
                    (token_id,),
                )
        except sqlite3.DatabaseError as exc:
            raise VirtualTokenError("virtual token store is unavailable") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteVirtualTokenStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _prepare_path(self) -> None:
        parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent_created:
            try:
                self.path.parent.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
        if self.path.is_symlink():
            raise VirtualTokenError("virtual token store cannot be a symbolic link")
        file_created = False
        if not self.path.exists():
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
                file_created = True
        if file_created:
            try:
                self.path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass

    def _initialize_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, _STORE_SCHEMA_VERSION):
            raise VirtualTokenError("unsupported virtual token store schema")
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS virtual_tokens (
                token_id TEXT PRIMARY KEY,
                digest BLOB NOT NULL UNIQUE,
                policy_json TEXT NOT NULL,
                committed_cost_usd REAL NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        if version == 0:
            self._conn.execute(f"PRAGMA user_version={_STORE_SCHEMA_VERSION}")

    def _update(self, token_id: str, column: str, value: object) -> None:
        if column not in {"committed_cost_usd", "revoked"}:
            raise ValueError("unsupported virtual token update")
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    f"UPDATE virtual_tokens SET {column} = ?, updated_at = ? "
                    "WHERE token_id = ?",
                    (value, time.time(), token_id),
                )
        except sqlite3.DatabaseError as exc:
            raise VirtualTokenError("virtual token store is unavailable") from exc
        if cursor.rowcount != 1:
            raise VirtualTokenError("unknown virtual token")

    @staticmethod
    def _encode_policy(policy: VirtualTokenPolicy) -> str:
        return json.dumps(
            {
                "token_id": policy.token_id,
                "allowed_agent_ids": sorted(policy.allowed_agent_ids),
                "allowed_card_ids": sorted(policy.allowed_card_ids),
                "allowed_tools": sorted(policy.allowed_tools),
                "allowed_memory_namespaces": sorted(policy.allowed_memory_namespaces),
                "requests_per_minute": policy.requests_per_minute,
                "maximum_budget_usd": policy.maximum_budget_usd,
                "expires_at": policy.expires_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_policy(payload: str) -> VirtualTokenPolicy:
        try:
            values = json.loads(payload)
            if not isinstance(values, dict) or set(values) != {
                "token_id",
                "allowed_agent_ids",
                "allowed_card_ids",
                "allowed_tools",
                "allowed_memory_namespaces",
                "requests_per_minute",
                "maximum_budget_usd",
                "expires_at",
            }:
                raise ValueError("unexpected virtual token policy fields")
            return VirtualTokenPolicy(
                token_id=values["token_id"],
                allowed_agent_ids=_decode_scope(values["allowed_agent_ids"]),
                allowed_card_ids=_decode_scope(values["allowed_card_ids"]),
                allowed_tools=_decode_scope(values["allowed_tools"]),
                allowed_memory_namespaces=_decode_scope(
                    values["allowed_memory_namespaces"]
                ),
                requests_per_minute=values["requests_per_minute"],
                maximum_budget_usd=values["maximum_budget_usd"],
                expires_at=values["expires_at"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VirtualTokenError("stored virtual token policy is invalid") from exc


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

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        store: SQLiteVirtualTokenStore | None = None,
    ) -> None:
        self.clock = clock
        self.store = store
        self._tokens: dict[str, _TokenState] = {}
        self._digest_index: dict[bytes, str] = {}
        self._lock = RLock()
        if store is not None:
            for state in store.load():
                if (
                    state.policy.token_id in self._tokens
                    or state.digest in self._digest_index
                ):
                    raise VirtualTokenError("duplicate stored virtual token")
                self._tokens[state.policy.token_id] = state
                self._digest_index[state.digest] = state.policy.token_id

    def issue(self, policy: VirtualTokenPolicy) -> str:
        """Issue a token once; retain only its digest."""

        with self._lock:
            if policy.token_id in self._tokens:
                raise VirtualTokenError("token id already exists")
            plaintext = "occult_" + secrets.token_urlsafe(32)
            digest = self._digest(plaintext)
            if self.store is not None:
                self.store.insert(policy, digest)
            self._tokens[policy.token_id] = _TokenState(policy, digest)
            self._digest_index[digest] = policy.token_id
            return plaintext

    def revoke(self, token_id: str) -> None:
        with self._lock:
            state = self._tokens.get(token_id)
            if state is None:
                raise VirtualTokenError("unknown virtual token")
            if self.store is not None:
                self.store.update_revoked(token_id, True)
            state.revoked = True

    def discard(self, token_id: str) -> None:
        """Delete an unexposed token issued during a failed atomic setup."""

        with self._lock:
            state = self._tokens.get(token_id)
            if state is None:
                raise VirtualTokenError("unknown virtual token")
            if state.revoked:
                raise VirtualTokenError("revoked virtual token cannot be discarded")
            if state.calls or state.committed_cost_usd or state.reserved_cost_usd:
                raise VirtualTokenError("active virtual token cannot be discarded")
            if self.store is not None:
                self.store.delete(token_id)
            self._tokens.pop(token_id, None)
            self._digest_index.pop(state.digest, None)

    def policy(self, plaintext: str) -> VirtualTokenPolicy:
        with self._lock:
            return self._authenticate(plaintext).policy

    def recognizes(self, plaintext: str) -> bool:
        """Return whether this authority issued the token, even if inactive."""

        if not isinstance(plaintext, str) or not plaintext:
            return False
        digest = self._digest(plaintext)
        with self._lock:
            token_id = self._digest_index.get(digest)
            state = self._tokens.get(token_id or "")
            return (
                state is not None
                and secrets.compare_digest(state.digest, digest)
            )

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
        if (
            isinstance(maximum_cost_usd, bool)
            or not isinstance(maximum_cost_usd, (int, float))
            or not math.isfinite(maximum_cost_usd)
            or maximum_cost_usd < 0
        ):
            raise VirtualTokenError("maximum cost must be finite and non-negative")
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
            policy = state.policy
            return {
                "token_id": policy.token_id,
                "revoked": state.revoked,
                "allowed_agent_ids": sorted(policy.allowed_agent_ids),
                "allowed_card_ids": sorted(policy.allowed_card_ids),
                "allowed_tools": sorted(policy.allowed_tools),
                "allowed_memory_namespaces": sorted(policy.allowed_memory_namespaces),
                "requests_per_minute": policy.requests_per_minute,
                "maximum_budget_usd": policy.maximum_budget_usd,
                "expires_at": policy.expires_at,
                "requests_in_window": len(state.calls),
                "committed_cost_usd": state.committed_cost_usd,
                "reserved_cost_usd": state.reserved_cost_usd,
            }

    def statuses(self) -> tuple[dict[str, object], ...]:
        """Return all token metadata without digests or plaintext secrets."""

        with self._lock:
            return tuple(self.status(token_id) for token_id in sorted(self._tokens))

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
            remaining_reserved = max(0.0, state.reserved_cost_usd - reserved)
            if actual is None:
                state.reserved_cost_usd = remaining_reserved
                return
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or not math.isfinite(actual)
                or actual < 0
            ):
                raise VirtualTokenError("actual cost must be finite and non-negative")
            if (
                state.committed_cost_usd + remaining_reserved + actual
                > state.policy.maximum_budget_usd
            ):
                raise VirtualTokenError("actual cost exceeds virtual token budget")
            committed_cost = state.committed_cost_usd + actual
            if self.store is not None:
                self.store.update_committed_cost(token_id, committed_cost)
            state.reserved_cost_usd = remaining_reserved
            state.committed_cost_usd = committed_cost

    @staticmethod
    def _authorize_value(allowed: frozenset[str], value: str, kind: str) -> None:
        if allowed and value not in allowed:
            raise VirtualTokenError(f"virtual token does not allow requested {kind}")

    @staticmethod
    def _digest(plaintext: str) -> bytes:
        return hashlib.sha256(plaintext.encode("utf-8")).digest()


__all__ = [
    "SQLiteVirtualTokenStore",
    "VirtualTokenAuthority",
    "VirtualTokenError",
    "VirtualTokenLease",
    "VirtualTokenPolicy",
]

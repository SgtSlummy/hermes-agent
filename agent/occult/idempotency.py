"""Durable, token-scoped idempotency for Occult invocations."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from .locking import ExactKeyLockPool


class InvocationIdempotencyError(ValueError):
    """Raised when an idempotency key is reused for different input."""


class SQLiteInvocationResultStore:
    """Serialize identical requests and replay their durable results."""

    def __init__(
        self,
        path: Path,
        *,
        retention_seconds: float = 7 * 24 * 60 * 60,
        maximum_entries: int = 10_000,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if maximum_entries <= 0:
            raise ValueError("maximum_entries must be positive")
        self.path = path
        self.retention_seconds = float(retention_seconds)
        self.maximum_entries = int(maximum_entries)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._db_lock = RLock()
        self._key_locks = ExactKeyLockPool()
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS invocation_identities (
                owner_token_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_token_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS invocation_results (
                owner_token_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_token_id, idempotency_key)
            );
            INSERT OR IGNORE INTO invocation_identities (
                owner_token_id, idempotency_key,
                request_fingerprint, created_at
            )
            SELECT
                owner_token_id, idempotency_key,
                request_fingerprint, created_at
            FROM invocation_results;
            """
        )

    def run(
        self,
        owner_token_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        callback: Callable[[], Mapping[str, Any]],
        *,
        on_replay: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        lock_key = (owner_token_id, idempotency_key)
        with self._key_locks.acquire(lock_key):
            with self._db_lock:
                row = self._conn.execute(
                    """
                    SELECT request_fingerprint, result_json
                    FROM invocation_results
                    WHERE owner_token_id = ? AND idempotency_key = ?
                    """,
                    lock_key,
                ).fetchone()
            if row is not None:
                if row["request_fingerprint"] != request_fingerprint:
                    raise InvocationIdempotencyError(
                        "idempotency key was reused with different input"
                    )
                result = dict(json.loads(str(row["result_json"])))
                if on_replay is not None:
                    on_replay(result)
                return result
            with self._db_lock:
                identity = self._conn.execute(
                    """
                    SELECT request_fingerprint
                    FROM invocation_identities
                    WHERE owner_token_id = ? AND idempotency_key = ?
                    """,
                    lock_key,
                ).fetchone()
            if identity is not None:
                if identity["request_fingerprint"] != request_fingerprint:
                    raise InvocationIdempotencyError(
                        "idempotency key was reused with different input"
                    )
                raise InvocationIdempotencyError(
                    "idempotency result expired; submit the request with a new key"
                )

            result = dict(callback())
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            with self._db_lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    now = time.time()
                    self._conn.execute(
                        """
                        INSERT INTO invocation_identities (
                            owner_token_id, idempotency_key,
                            request_fingerprint, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            owner_token_id,
                            idempotency_key,
                            request_fingerprint,
                            now,
                        ),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO invocation_results (
                            owner_token_id, idempotency_key,
                            request_fingerprint, result_json, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            owner_token_id,
                            idempotency_key,
                            request_fingerprint,
                            encoded,
                            now,
                        ),
                    )
                    self._prune(now)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            return result

    def _prune(self, now: float) -> None:
        """Bound completed result retention; in-flight calls live in key locks."""

        self._conn.execute(
            "DELETE FROM invocation_results WHERE created_at < ?",
            (now - self.retention_seconds,),
        )
        self._conn.execute(
            """
            DELETE FROM invocation_results
            WHERE rowid IN (
                SELECT rowid FROM invocation_results
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.maximum_entries,),
        )

    def close(self) -> None:
        with self._db_lock:
            self._conn.close()


__all__ = ["InvocationIdempotencyError", "SQLiteInvocationResultStore"]

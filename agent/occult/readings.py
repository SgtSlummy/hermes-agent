"""Restart-safe, idempotent Council reading persistence.

Council nodes receive only opaque input references and an idempotency key.
Artifact content remains in Hermes-owned storage and event payloads contain
only redacted route summaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from agent.occult.contracts import OCCULT_CONTRACT_VERSION
from hermes_constants import get_hermes_home

from .locking import ExactKeyLockPool

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEGACY_OWNER = "legacy-unclaimed"
_TERMINAL_TYPES = frozenset({
    "reading.cancelled",
    "reading.completed",
    "reading.failed",
})
_SECRET_FIELDS = frozenset({
    "accesstoken",
    "apikey",
    "authorization",
    "credential",
    "password",
    "refreshtoken",
    "secret",
    "token",
})


class ReadingError(ValueError):
    """Safe-to-surface reading validation or lifecycle failure."""


class RetryableReadingError(RuntimeError):
    """A transient node failure that leaves its reading resumable."""


@dataclass(frozen=True, slots=True)
class ReadingNode:
    node_id: str
    agent_id: str
    task: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, field in ((self.node_id, "node id"), (self.agent_id, "agent id")):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"invalid {field}")
        if not self.task.strip() or len(self.task) > 10_000:
            raise ValueError("invalid node task")


@dataclass(frozen=True, slots=True)
class ReadingPlan:
    spread_id: str
    nodes: tuple[ReadingNode, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.spread_id):
            raise ValueError("invalid spread id")
        if not self.nodes:
            raise ValueError("reading plan requires nodes")
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("reading plan has duplicate node ids")
        known = set(ids)
        for node in self.nodes:
            if node.node_id in node.depends_on:
                raise ValueError("reading node cannot depend on itself")
            if not set(node.depends_on).issubset(known):
                raise ValueError("reading node has unknown dependency")
        self._topological_order()

    def _topological_order(self) -> tuple[str, ...]:
        remaining = {node.node_id: set(node.depends_on) for node in self.nodes}
        order: list[str] = []
        while remaining:
            ready = sorted(node_id for node_id, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError("reading plan contains a cycle")
            for node_id in ready:
                order.append(node_id)
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(order)


@dataclass(frozen=True, slots=True)
class CouncilNodeRequest:
    contract_version: str
    reading_id: str
    node_id: str
    agent_id: str
    task: str
    idempotency_key: str
    input_artifact_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CouncilNodeResult:
    artifact: Mapping[str, Any]
    route_summary: Mapping[str, Any]


class ReadingStore:
    """SQLite-backed reading, node, event, and artifact store."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        retention_seconds: float = 30 * 24 * 60 * 60,
        identity_retention_seconds: float | None = None,
        maximum_readings: int = 10_000,
    ) -> None:
        if not math.isfinite(retention_seconds) or retention_seconds <= 0:
            raise ValueError("reading retention_seconds must be positive")
        if identity_retention_seconds is None:
            identity_retention_seconds = retention_seconds * 4
        if (
            not math.isfinite(identity_retention_seconds)
            or identity_retention_seconds <= retention_seconds
        ):
            raise ValueError(
                "reading identity_retention_seconds must exceed retention_seconds"
            )
        if maximum_readings <= 0:
            raise ValueError("maximum_readings must be positive")
        self.path = path or (get_hermes_home() / "occult" / "readings.db")
        self.retention_seconds = float(retention_seconds)
        self.identity_retention_seconds = float(identity_retention_seconds)
        self.maximum_readings = int(maximum_readings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = RLock()
        self._execution_locks = ExactKeyLockPool()
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS readings (
                reading_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                owner_token_id TEXT NOT NULL DEFAULT 'local',
                contract_version TEXT NOT NULL,
                spread_id TEXT NOT NULL,
                plan_fingerprint TEXT,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reading_nodes (
                reading_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                task TEXT NOT NULL,
                dependencies_json TEXT NOT NULL,
                position INTEGER NOT NULL,
                state TEXT NOT NULL,
                artifact_reference TEXT,
                PRIMARY KEY (reading_id, node_id),
                FOREIGN KEY (reading_id) REFERENCES readings(reading_id)
            );
            CREATE TABLE IF NOT EXISTS reading_artifacts (
                artifact_reference TEXT PRIMARY KEY,
                reading_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (reading_id) REFERENCES readings(reading_id)
            );
            CREATE TABLE IF NOT EXISTS reading_events (
                reading_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (reading_id, sequence),
                FOREIGN KEY (reading_id) REFERENCES readings(reading_id)
            );
            CREATE TABLE IF NOT EXISTS reading_node_results (
                idempotency_key TEXT PRIMARY KEY,
                artifact_json TEXT NOT NULL,
                route_summary_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        with self._transaction():
            terminal_rows = self._conn.execute(
                """
                SELECT reading_id, MIN(sequence) AS terminal_sequence
                FROM reading_events
                WHERE event_type IN (
                    'reading.cancelled', 'reading.completed', 'reading.failed'
                )
                GROUP BY reading_id
                """
            ).fetchall()
            for terminal in terminal_rows:
                reading_id = str(terminal["reading_id"])
                terminal_sequence = int(terminal["terminal_sequence"])
                late_events = self._conn.execute(
                    """
                    SELECT data_json
                    FROM reading_events
                    WHERE reading_id = ? AND sequence > ?
                    """,
                    (reading_id, terminal_sequence),
                ).fetchall()
                affected_nodes: set[str] = set()
                for event in late_events:
                    try:
                        data = json.loads(str(event["data_json"]))
                        node_id = str(data["node_id"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    affected_nodes.add(node_id)
                for node_id in affected_nodes:
                    artifact = self._conn.execute(
                        """
                        SELECT artifact_reference FROM reading_nodes
                        WHERE reading_id = ? AND node_id = ?
                        """,
                        (reading_id, node_id),
                    ).fetchone()
                    if artifact is not None and artifact["artifact_reference"]:
                        self._conn.execute(
                            "DELETE FROM reading_artifacts "
                            "WHERE artifact_reference = ?",
                            (artifact["artifact_reference"],),
                        )
                    self._conn.execute(
                        """
                        UPDATE reading_nodes
                        SET state = 'pending', artifact_reference = NULL
                        WHERE reading_id = ? AND node_id = ?
                        """,
                        (reading_id, node_id),
                    )
                    self._conn.execute(
                        "DELETE FROM reading_node_results WHERE idempotency_key = ?",
                        (f"{reading_id}:{node_id}",),
                    )
                self._conn.execute(
                    """
                    DELETE FROM reading_events
                    WHERE reading_id = ? AND sequence > ?
                    """,
                    (reading_id, terminal_sequence),
                )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_reading_event
                ON reading_events(reading_id)
                WHERE event_type IN (
                    'reading.cancelled', 'reading.completed', 'reading.failed'
                )
                """
            )
        with self._transaction():
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(readings)").fetchall()
            }
            if "owner_token_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE readings ADD COLUMN "
                    f"owner_token_id TEXT NOT NULL DEFAULT '{_LEGACY_OWNER}'"
                )
                legacy_rows = self._conn.execute(
                    "SELECT reading_id, idempotency_key FROM readings"
                ).fetchall()
                for row in legacy_rows:
                    scoped_key = hashlib.sha256(
                        _LEGACY_OWNER.encode("utf-8")
                        + b"\0"
                        + str(row["idempotency_key"]).encode("utf-8")
                    ).hexdigest()
                    self._conn.execute(
                        "UPDATE readings SET idempotency_key = ? WHERE reading_id = ?",
                        (scoped_key, row["reading_id"]),
                    )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(readings)").fetchall()
            }
            if "plan_fingerprint" not in columns:
                self._conn.execute(
                    "ALTER TABLE readings ADD COLUMN plan_fingerprint TEXT"
                )
            missing_fingerprints = self._conn.execute(
                """
                SELECT reading_id, spread_id
                FROM readings
                WHERE plan_fingerprint IS NULL
                """
            ).fetchall()
            for reading in missing_fingerprints:
                nodes = self._conn.execute(
                    """
                    SELECT node_id, agent_id, task, dependencies_json
                    FROM reading_nodes
                    WHERE reading_id = ?
                    ORDER BY node_id
                    """,
                    (reading["reading_id"],),
                ).fetchall()
                if not nodes:
                    continue
                try:
                    fingerprint = _stored_plan_fingerprint(
                        str(reading["spread_id"]),
                        nodes,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                self._conn.execute(
                    "UPDATE readings SET plan_fingerprint = ? WHERE reading_id = ?",
                    (fingerprint, reading["reading_id"]),
                )

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        plan: ReadingPlan,
        *,
        idempotency_key: str,
        contract_version: str,
        owner_token_id: str = "local",
    ) -> str:
        if contract_version != OCCULT_CONTRACT_VERSION:
            raise ReadingError("Occult contract version mismatch")
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ReadingError("invalid idempotency key")
        if not _SAFE_ID.fullmatch(owner_token_id):
            raise ReadingError("invalid reading owner")
        if owner_token_id == _LEGACY_OWNER:
            raise ReadingError("reserved reading owner")
        plan_fingerprint = _plan_fingerprint(plan)
        stored_idempotency_key = hashlib.sha256(
            owner_token_id.encode("utf-8") + b"\0" + idempotency_key.encode("utf-8")
        ).hexdigest()
        legacy_idempotency_key = hashlib.sha256(
            _LEGACY_OWNER.encode("utf-8") + b"\0" + idempotency_key.encode("utf-8")
        ).hexdigest()
        now = time.time()
        reading_id = "reading_" + uuid.uuid4().hex
        with self._transaction():
            self._prune_terminal_readings(now)
            existing = self._conn.execute(
                """
                SELECT reading_id, plan_fingerprint
                FROM readings WHERE idempotency_key = ?
                """,
                (stored_idempotency_key,),
            ).fetchone()
            if existing is not None:
                self._verify_or_adopt_plan(existing, plan_fingerprint)
                return str(existing["reading_id"])
            legacy = self._conn.execute(
                """
                SELECT reading_id, owner_token_id, plan_fingerprint FROM readings
                WHERE idempotency_key = ?
                """,
                (legacy_idempotency_key,),
            ).fetchone()
            if legacy is not None and legacy["owner_token_id"] in {
                _LEGACY_OWNER,
                owner_token_id,
            }:
                self._verify_or_adopt_plan(legacy, plan_fingerprint)
                if legacy["owner_token_id"] == _LEGACY_OWNER:
                    self._conn.execute(
                        """
                        UPDATE readings SET owner_token_id = ?, updated_at = ?
                        WHERE reading_id = ? AND owner_token_id = ?
                        """,
                        (
                            owner_token_id,
                            time.time(),
                            legacy["reading_id"],
                            _LEGACY_OWNER,
                        ),
                    )
                return str(legacy["reading_id"])
            reading_count = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM readings
                    WHERE state NOT IN ('cancelled', 'completed', 'failed')
                    OR updated_at >= ?
                    """,
                    (now - self.retention_seconds,),
                ).fetchone()["count"]
            )
            if reading_count >= self.maximum_readings:
                raise ReadingError("reading capacity is exhausted")
            self._conn.execute(
                """
                INSERT INTO readings (
                    reading_id, idempotency_key, contract_version,
                    owner_token_id, spread_id, plan_fingerprint,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    reading_id,
                    stored_idempotency_key,
                    contract_version,
                    owner_token_id,
                    plan.spread_id,
                    plan_fingerprint,
                    now,
                    now,
                ),
            )
            for position, node_id in enumerate(plan._topological_order()):
                node = next(item for item in plan.nodes if item.node_id == node_id)
                self._conn.execute(
                    """
                    INSERT INTO reading_nodes (
                        reading_id, node_id, agent_id, task,
                        dependencies_json, position, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        reading_id,
                        node.node_id,
                        node.agent_id,
                        node.task,
                        json.dumps(node.depends_on),
                        position,
                    ),
                )
            self._append_event(
                reading_id,
                "reading.started",
                {"spread_id": plan.spread_id},
            )
        return reading_id

    def run(
        self,
        reading_id: str,
        executor: Callable[[CouncilNodeRequest], CouncilNodeResult],
        *,
        maximum_nodes: int | None = None,
    ) -> dict[str, Any]:
        with self._execution_locks.acquire(reading_id):
            return self._run_unlocked(
                reading_id,
                executor,
                maximum_nodes=maximum_nodes,
            )

    def _run_unlocked(
        self,
        reading_id: str,
        executor: Callable[[CouncilNodeRequest], CouncilNodeResult],
        *,
        maximum_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Run ready nodes; completed nodes are never executed again."""

        executed = 0
        while maximum_nodes is None or executed < maximum_nodes:
            with self._transaction():
                reading = self._reading(reading_id)
                if reading["contract_version"] != OCCULT_CONTRACT_VERSION:
                    raise ReadingError("Occult contract version mismatch")
                if reading["state"] in {"cancelled", "completed", "failed"}:
                    return self.status(reading_id)
                self._conn.execute(
                    "UPDATE readings SET state = 'running', updated_at = ? "
                    "WHERE reading_id = ?",
                    (time.time(), reading_id),
                )
                node = self._next_ready_node(reading_id)
                if node is None:
                    incomplete = self._conn.execute(
                        "SELECT COUNT(*) AS count FROM reading_nodes "
                        "WHERE reading_id = ? AND state != 'completed'",
                        (reading_id,),
                    ).fetchone()["count"]
                    if incomplete == 0:
                        self._terminal(reading_id, "reading.completed", {})
                        self._conn.execute(
                            "UPDATE readings SET state = 'completed', updated_at = ? "
                            "WHERE reading_id = ?",
                            (time.time(), reading_id),
                        )
                    return self.status(reading_id)
                self._conn.execute(
                    "UPDATE reading_nodes SET state = 'running' "
                    "WHERE reading_id = ? AND node_id = ?",
                    (reading_id, node["node_id"]),
                )
                self._append_event(
                    reading_id,
                    "node.started",
                    {"node_id": node["node_id"], "agent_id": node["agent_id"]},
                )
                request = self._node_request(reading_id, node)

            try:
                result = executor(request)
                _reject_secret_fields(result.artifact)
                _reject_secret_fields(result.route_summary)
            except RetryableReadingError:
                with self._transaction():
                    reading = self._reading(reading_id)
                    if reading["state"] in {"cancelled", "completed", "failed"}:
                        return self.status(reading_id)
                    self._append_event(
                        reading_id,
                        "node.failed",
                        {
                            "node_id": node["node_id"],
                            "redacted": True,
                            "retryable": True,
                        },
                    )
                    self._conn.execute(
                        "UPDATE reading_nodes SET state = 'pending' "
                        "WHERE reading_id = ? AND node_id = ?",
                        (reading_id, node["node_id"]),
                    )
                    self._conn.execute(
                        "UPDATE readings SET state = 'pending', updated_at = ? "
                        "WHERE reading_id = ?",
                        (time.time(), reading_id),
                    )
                raise
            except Exception:
                with self._transaction():
                    reading = self._reading(reading_id)
                    if reading["state"] in {"cancelled", "completed", "failed"}:
                        return self.status(reading_id)
                    self._append_event(
                        reading_id,
                        "node.failed",
                        {"node_id": node["node_id"], "redacted": True},
                    )
                    self._terminal(
                        reading_id,
                        "reading.failed",
                        {"node_id": node["node_id"], "redacted": True},
                    )
                    self._conn.execute(
                        "UPDATE readings SET state = 'failed', updated_at = ? "
                        "WHERE reading_id = ?",
                        (time.time(), reading_id),
                    )
                raise ReadingError("Council node execution failed") from None

            with self._transaction():
                reading = self._reading(reading_id)
                if reading["state"] in {"cancelled", "completed", "failed"}:
                    return self.status(reading_id)
                self._cache_node_result(request.idempotency_key, result)
                artifact_reference = "artifact_" + uuid.uuid4().hex
                self._conn.execute(
                    """
                    INSERT INTO reading_artifacts (
                        artifact_reference, reading_id, node_id,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_reference,
                        reading_id,
                        node["node_id"],
                        json.dumps(result.artifact, sort_keys=True),
                        time.time(),
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE reading_nodes
                    SET state = 'completed', artifact_reference = ?
                    WHERE reading_id = ? AND node_id = ?
                    """,
                    (artifact_reference, reading_id, node["node_id"]),
                )
                self._append_event(
                    reading_id,
                    "node.completed",
                    {
                        "node_id": node["node_id"],
                        "artifact_reference": artifact_reference,
                        "route_summary": dict(result.route_summary),
                    },
                )
            executed += 1
        return self.status(reading_id)

    def resume(
        self,
        reading_id: str,
        executor: Callable[[CouncilNodeRequest], CouncilNodeResult],
        *,
        maximum_nodes: int | None = None,
    ) -> dict[str, Any]:
        with self._execution_locks.acquire(reading_id):
            with self._transaction():
                reading = self._reading(reading_id)
                if reading["state"] in {"cancelled", "completed", "failed"}:
                    return self.status(reading_id)
                self._conn.execute(
                    "UPDATE reading_nodes SET state = 'pending' "
                    "WHERE reading_id = ? AND state = 'running'",
                    (reading_id,),
                )
            return self._run_unlocked(
                reading_id,
                executor,
                maximum_nodes=maximum_nodes,
            )

    def cancel(self, reading_id: str) -> dict[str, Any]:
        with self._transaction():
            reading = self._reading(reading_id)
            if reading["state"] not in {"cancelled", "completed", "failed"}:
                self._terminal(reading_id, "reading.cancelled", {})
                self._conn.execute(
                    "UPDATE readings SET state = 'cancelled', updated_at = ? "
                    "WHERE reading_id = ?",
                    (time.time(), reading_id),
                )
        return self.status(reading_id)

    def status(self, reading_id: str) -> dict[str, Any]:
        reading = self._reading(reading_id)
        nodes = self._conn.execute(
            """
            SELECT node_id, agent_id, state, artifact_reference
            FROM reading_nodes WHERE reading_id = ? ORDER BY position
            """,
            (reading_id,),
        ).fetchall()
        return {
            "contract_version": reading["contract_version"],
            "reading_id": reading_id,
            "spread_id": reading["spread_id"],
            "state": reading["state"],
            "nodes": [dict(node) for node in nodes],
        }

    def owner_token_id(self, reading_id: str) -> str:
        """Return the non-secret token identifier that owns a reading."""

        return str(self._reading(reading_id)["owner_token_id"])

    def claim_legacy_owner(self, reading_id: str, token_id: str) -> str:
        """Atomically claim a pre-ownership reading for an authorized token."""

        if not _SAFE_ID.fullmatch(token_id):
            raise ReadingError("invalid reading owner")
        if token_id == _LEGACY_OWNER:
            raise ReadingError("reserved reading owner")
        with self._transaction():
            owner = str(self._reading(reading_id)["owner_token_id"])
            if owner == _LEGACY_OWNER:
                self._conn.execute(
                    """
                    UPDATE readings SET owner_token_id = ?, updated_at = ?
                    WHERE reading_id = ? AND owner_token_id = ?
                    """,
                    (token_id, time.time(), reading_id, _LEGACY_OWNER),
                )
                owner = token_id
            return owner

    def events(
        self, reading_id: str, *, after_sequence: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        self._reading(reading_id)
        if after_sequence is not None and after_sequence < 0:
            raise ReadingError("event sequence cannot be negative")
        if after_sequence is None:
            rows = self._conn.execute(
                "SELECT * FROM reading_events WHERE reading_id = ? ORDER BY sequence",
                (reading_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM reading_events "
                "WHERE reading_id = ? AND sequence > ? ORDER BY sequence",
                (reading_id, after_sequence),
            ).fetchall()
        return tuple(
            {
                "contract_version": OCCULT_CONTRACT_VERSION,
                "event_id": row["event_id"],
                "reading_id": reading_id,
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "data": json.loads(row["data_json"]),
                "error": None,
            }
            for row in rows
        )

    def artifact(self, artifact_reference: str) -> Mapping[str, Any]:
        row = self._conn.execute(
            "SELECT payload_json FROM reading_artifacts WHERE artifact_reference = ?",
            (artifact_reference,),
        ).fetchone()
        if row is None:
            raise ReadingError("unknown artifact reference")
        return json.loads(row["payload_json"])

    def cached_node_result(
        self,
        idempotency_key: str,
    ) -> CouncilNodeResult | None:
        row = self._conn.execute(
            """
            SELECT artifact_json, route_summary_json
            FROM reading_node_results
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return CouncilNodeResult(
            artifact=json.loads(row["artifact_json"]),
            route_summary=json.loads(row["route_summary_json"]),
        )

    def cache_node_result_if_active(
        self,
        reading_id: str,
        idempotency_key: str,
        result: CouncilNodeResult,
    ) -> bool:
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ReadingError("invalid idempotency key")
        _reject_secret_fields(result.artifact)
        _reject_secret_fields(result.route_summary)
        with self._transaction():
            reading = self._reading(reading_id)
            if reading["state"] in {"cancelled", "completed", "failed"}:
                return False
            self._cache_node_result(idempotency_key, result)
            return True

    def _cache_node_result(
        self,
        idempotency_key: str,
        result: CouncilNodeResult,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO reading_node_results (
                idempotency_key, artifact_json,
                route_summary_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                idempotency_key,
                json.dumps(result.artifact, sort_keys=True),
                json.dumps(result.route_summary, sort_keys=True),
                time.time(),
            ),
        )

    def _node_request(self, reading_id: str, node: sqlite3.Row) -> CouncilNodeRequest:
        dependencies = json.loads(node["dependencies_json"])
        references: list[str] = []
        for dependency in dependencies:
            row = self._conn.execute(
                "SELECT artifact_reference FROM reading_nodes "
                "WHERE reading_id = ? AND node_id = ?",
                (reading_id, dependency),
            ).fetchone()
            if row is None or row["artifact_reference"] is None:
                raise ReadingError("node dependency is incomplete")
            references.append(str(row["artifact_reference"]))
        return CouncilNodeRequest(
            contract_version=OCCULT_CONTRACT_VERSION,
            reading_id=reading_id,
            node_id=node["node_id"],
            agent_id=node["agent_id"],
            task=node["task"],
            idempotency_key=f"{reading_id}:{node['node_id']}",
            input_artifact_references=tuple(references),
        )

    def _next_ready_node(self, reading_id: str) -> sqlite3.Row | None:
        nodes = self._conn.execute(
            "SELECT * FROM reading_nodes WHERE reading_id = ? ORDER BY position",
            (reading_id,),
        ).fetchall()
        states = {row["node_id"]: row["state"] for row in nodes}
        for node in nodes:
            if node["state"] != "pending":
                continue
            dependencies = json.loads(node["dependencies_json"])
            if all(
                states.get(dependency) == "completed" for dependency in dependencies
            ):
                return node
        return None

    def _reading(self, reading_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM readings WHERE reading_id = ?", (reading_id,)
        ).fetchone()
        if row is None:
            raise ReadingError("unknown reading")
        return row

    def _prune_terminal_readings(self, now: float) -> None:
        expired = self._conn.execute(
            """
            SELECT reading_id FROM readings
            WHERE state IN ('cancelled', 'completed', 'failed')
            AND updated_at < ?
            """,
            (now - self.retention_seconds,),
        ).fetchall()
        for row in expired:
            reading_id = str(row["reading_id"])
            result_prefix = f"{reading_id}:"
            self._conn.execute(
                """
                DELETE FROM reading_node_results
                WHERE substr(idempotency_key, 1, ?) = ?
                """,
                (len(result_prefix), result_prefix),
            )
            self._conn.execute(
                "DELETE FROM reading_events WHERE reading_id = ?",
                (reading_id,),
            )
            self._conn.execute(
                "DELETE FROM reading_artifacts WHERE reading_id = ?",
                (reading_id,),
            )
            self._conn.execute(
                "DELETE FROM reading_nodes WHERE reading_id = ?",
                (reading_id,),
            )
        self._conn.execute(
            """
            DELETE FROM readings
            WHERE state IN ('cancelled', 'completed', 'failed')
            AND updated_at < ?
            """,
            (now - self.identity_retention_seconds,),
        )

    def _verify_or_adopt_plan(
        self,
        row: sqlite3.Row,
        plan_fingerprint: str,
    ) -> None:
        stored = row["plan_fingerprint"]
        if stored is None:
            self._conn.execute(
                """
                UPDATE readings SET plan_fingerprint = ?, updated_at = ?
                WHERE reading_id = ? AND plan_fingerprint IS NULL
                """,
                (plan_fingerprint, time.time(), row["reading_id"]),
            )
            return
        if str(stored) != plan_fingerprint:
            raise ReadingError(
                "idempotency key was reused with a different reading plan"
            )

    def _append_event(
        self, reading_id: str, event_type: str, data: Mapping[str, Any]
    ) -> None:
        _reject_secret_fields(data)
        sequence = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS sequence "
            "FROM reading_events WHERE reading_id = ?",
            (reading_id,),
        ).fetchone()["sequence"]
        self._conn.execute(
            """
            INSERT INTO reading_events (
                reading_id, sequence, event_id, event_type, occurred_at, data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                reading_id,
                sequence,
                "event_" + uuid.uuid4().hex,
                event_type,
                datetime.now(UTC).isoformat(),
                json.dumps(data, sort_keys=True),
            ),
        )

    def _terminal(
        self, reading_id: str, event_type: str, data: Mapping[str, Any]
    ) -> None:
        if event_type not in _TERMINAL_TYPES:
            raise ReadingError("invalid terminal event")
        existing = self._conn.execute(
            "SELECT event_type FROM reading_events WHERE reading_id = ? "
            "AND event_type IN (?, ?, ?)",
            (reading_id, *_TERMINAL_TYPES),
        ).fetchone()
        if existing is None:
            self._append_event(reading_id, event_type, data)

    def _transaction(self):
        return _Transaction(self._conn, self._lock)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock

    def __enter__(self):
        self.lock.acquire()
        self.connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.lock.release()


def _plan_fingerprint(plan: ReadingPlan) -> str:
    payload = {
        "spread_id": plan.spread_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "agent_id": node.agent_id,
                "task": node.task,
                "depends_on": sorted(node.depends_on),
            }
            for node in sorted(plan.nodes, key=lambda item: item.node_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stored_plan_fingerprint(
    spread_id: str,
    rows: Sequence[sqlite3.Row],
) -> str:
    plan = ReadingPlan(
        spread_id=spread_id,
        nodes=tuple(
            ReadingNode(
                node_id=str(row["node_id"]),
                agent_id=str(row["agent_id"]),
                task=str(row["task"]),
                depends_on=tuple(json.loads(str(row["dependencies_json"]))),
            )
            for row in rows
        ),
    )
    return _plan_fingerprint(plan)


def _reject_secret_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SECRET_FIELDS:
                raise ReadingError(f"secret-shaped field rejected at {path}")
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


__all__ = [
    "CouncilNodeRequest",
    "CouncilNodeResult",
    "ReadingError",
    "ReadingNode",
    "ReadingPlan",
    "ReadingStore",
    "RetryableReadingError",
]

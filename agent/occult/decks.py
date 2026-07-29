"""Persistent, secret-free Occult deck registry."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent.occult.contracts import DeckDescriptor, validate_deck
from hermes_constants import get_hermes_home

_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class DeckError(ValueError):
    """Safe-to-surface deck persistence or validation failure."""


class DeckRegistry:
    """Atomically store the current validated version of each deck."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_hermes_home() / "occult" / "decks.json")
        self._lock = threading.RLock()
        self._read()

    def list(self) -> tuple[DeckDescriptor, ...]:
        with self._lock:
            registry = self._read()
            return tuple(
                self._descriptor(payload)
                for _, payload in sorted(registry["decks"].items())
            )

    def get(self, deck_id: str) -> DeckDescriptor:
        safe_id = self._identifier(deck_id, "deck id")
        with self._lock:
            payload = self._read()["decks"].get(safe_id)
        if payload is None:
            raise DeckError("unknown deck")
        return self._descriptor(payload)

    def put(
        self,
        payload: DeckDescriptor | Mapping[str, Any],
        *,
        available_agent_ids: Iterable[str],
        available_card_ids: Iterable[str],
    ) -> DeckDescriptor:
        descriptor = self._descriptor(payload)
        self._validate_available(
            descriptor,
            available_agent_ids=available_agent_ids,
            available_card_ids=available_card_ids,
        )
        with self._lock:
            registry = self._read()
            registry["decks"][descriptor.deck_id] = descriptor.model_dump(mode="json")
            self._write(registry)
        return descriptor

    def delete(self, deck_id: str) -> None:
        safe_id = self._identifier(deck_id, "deck id")
        with self._lock:
            registry = self._read()
            if safe_id not in registry["decks"]:
                raise DeckError("unknown deck")
            del registry["decks"][safe_id]
            self._write(registry)

    def validate_current(
        self,
        deck_id: str,
        *,
        available_agent_ids: Iterable[str],
        available_card_ids: Iterable[str],
    ) -> dict[str, Any]:
        descriptor = self.get(deck_id)
        agents = frozenset(available_agent_ids)
        cards = frozenset(available_card_ids)
        missing_agents = sorted(set(descriptor.allowed_agent_ids) - agents)
        missing_cards = sorted(set(descriptor.allowed_card_ids) - cards)
        return {
            "deck_id": descriptor.deck_id,
            "version": descriptor.version,
            "valid": not missing_agents and not missing_cards,
            "missing_agent_ids": missing_agents,
            "missing_card_ids": missing_cards,
        }

    @classmethod
    def _descriptor(
        cls,
        payload: DeckDescriptor | Mapping[str, Any],
    ) -> DeckDescriptor:
        raw = (
            payload.model_dump(mode="json")
            if isinstance(payload, DeckDescriptor)
            else payload
        )
        if not isinstance(raw, Mapping):
            raise DeckError("invalid deck descriptor")
        try:
            descriptor = validate_deck(raw)
        except ValueError as exc:
            raise DeckError(str(exc)) from None
        cls._identifier(descriptor.deck_id, "deck id")
        if not _SEMVER.fullmatch(descriptor.version):
            raise DeckError("deck version must use semantic versioning")
        if not descriptor.allowed_agent_ids:
            raise DeckError(
                "deck requires at least one agent; at least one allowed agent is required"
            )
        if not descriptor.allowed_card_ids:
            raise DeckError(
                "deck requires at least one route; at least one allowed route is required"
            )
        if len(descriptor.allowed_agent_ids) > 256:
            raise DeckError("deck allows too many agents")
        if len(descriptor.allowed_card_ids) > 256:
            raise DeckError("deck allows too many routes")
        if len(set(descriptor.allowed_agent_ids)) != len(descriptor.allowed_agent_ids):
            raise DeckError("deck contains duplicate agents")
        if len(set(descriptor.allowed_card_ids)) != len(descriptor.allowed_card_ids):
            raise DeckError("deck contains duplicate routes")
        for agent_id in descriptor.allowed_agent_ids:
            cls._identifier(agent_id, "agent id")
        for card_id in descriptor.allowed_card_ids:
            cls._identifier(card_id, "route id")
        return descriptor

    @staticmethod
    def _validate_available(
        descriptor: DeckDescriptor,
        *,
        available_agent_ids: Iterable[str],
        available_card_ids: Iterable[str],
    ) -> None:
        missing_agents = set(descriptor.allowed_agent_ids) - set(available_agent_ids)
        if missing_agents:
            raise DeckError("deck references unavailable agents")
        missing_cards = set(descriptor.allowed_card_ids) - set(available_card_ids)
        if missing_cards:
            raise DeckError("deck references unavailable routes")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": _SCHEMA_VERSION, "decks": {}}
        try:
            registry = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise DeckError("invalid deck registry") from None
        if (
            not isinstance(registry, dict)
            or set(registry) != {"schema_version", "decks"}
            or registry.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(registry.get("decks"), dict)
        ):
            raise DeckError("unsupported deck registry")
        for deck_id, payload in registry["decks"].items():
            if self._identifier(deck_id, "deck id") != deck_id:
                raise DeckError("invalid deck registry")
            if self._descriptor(payload).deck_id != deck_id:
                raise DeckError("deck registry identity mismatch")
        return registry

    def _write(self, registry: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".decks.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(registry, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError:
            raise DeckError("deck registry update failed") from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _identifier(value: object, field: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_ID.fullmatch(normalized):
            raise DeckError(f"invalid {field}")
        return normalized


__all__ = ["DeckError", "DeckRegistry"]

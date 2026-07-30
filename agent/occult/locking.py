"""Exact-key lock lifecycle for long-running Occult operations."""

from __future__ import annotations

from _thread import LockType
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock


@dataclass(slots=True)
class _LockEntry:
    lock: LockType
    references: int = 0


class ExactKeyLockPool:
    """Serialize identical keys without blocking hash-colliding work."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[Hashable, _LockEntry] = {}

    @contextmanager
    def acquire(self, key: Hashable) -> Iterator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(Lock())
                self._entries[key] = entry
            entry.references += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.references -= 1
                if entry.references == 0:
                    self._entries.pop(key, None)


__all__ = ["ExactKeyLockPool"]

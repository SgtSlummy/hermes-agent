from threading import Event, Thread

import pytest

from agent.occult import locking
from agent.occult.locking import ExactKeyLockPool


def test_exact_key_lock_pool_does_not_serialize_unrelated_keys():
    pool = ExactKeyLockPool()
    first_entered = Event()
    second_entered = Event()
    release = Event()

    def hold_first():
        with pool.acquire("first"):
            first_entered.set()
            assert release.wait(timeout=5)

    def enter_second():
        assert first_entered.wait(timeout=5)
        with pool.acquire("second"):
            second_entered.set()

    first = Thread(target=hold_first)
    second = Thread(target=enter_second)
    first.start()
    second.start()

    assert second_entered.wait(timeout=5)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()


def test_interrupted_key_lock_acquisition_releases_reference():
    class InterruptingLock:
        @staticmethod
        def acquire():
            raise KeyboardInterrupt

        @staticmethod
        def release():
            pytest.fail("an unacquired lock must not be released")

    pool = ExactKeyLockPool()
    with pool._guard:
        pool._entries["key"] = locking._LockEntry(InterruptingLock())

    with pytest.raises(KeyboardInterrupt):
        with pool.acquire("key"):
            pytest.fail("interrupted acquisition must not enter")

    assert "key" not in pool._entries

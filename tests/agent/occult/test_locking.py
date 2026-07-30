from threading import Event, Thread

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

import sqlite3

import pytest

from agent.occult.virtual_tokens import (
    SQLiteVirtualTokenStore,
    VirtualTokenAuthority,
    VirtualTokenError,
    VirtualTokenPolicy,
)


def _policy(**overrides):
    values = {
        "token_id": "client-1",
        "allowed_agent_ids": frozenset({"occult.major.magician"}),
        "allowed_card_ids": frozenset({"minor.swords.king.test"}),
        "allowed_tools": frozenset({"read_file"}),
        "allowed_memory_namespaces": frozenset({"project"}),
        "requests_per_minute": 2,
        "maximum_budget_usd": 1.0,
        "expires_at": 200.0,
    }
    values.update(overrides)
    return VirtualTokenPolicy(**values)


def test_token_is_returned_once_and_status_is_secret_free():
    authority = VirtualTokenAuthority(clock=lambda: 100.0)
    plaintext = authority.issue(_policy())

    assert plaintext.startswith("occult_")
    assert authority.policy(plaintext).token_id == "client-1"
    status = authority.status("client-1")
    assert status["token_id"] == "client-1"
    assert plaintext not in repr(status)
    with pytest.raises(VirtualTokenError, match="already exists"):
        authority.issue(_policy())


def test_legacy_reading_owner_token_id_is_reserved():
    with pytest.raises(ValueError, match="reserved token_id"):
        _policy(token_id="legacy-unclaimed")


def test_unexposed_token_can_be_discarded_without_persistent_orphan(tmp_path):
    path = tmp_path / "virtual_tokens.db"
    store = SQLiteVirtualTokenStore(path)
    authority = VirtualTokenAuthority(clock=lambda: 100.0, store=store)
    plaintext = authority.issue(_policy())

    authority.discard("client-1")

    assert authority.recognizes(plaintext) is False
    assert authority.statuses() == ()
    store.close()
    restarted_store = SQLiteVirtualTokenStore(path)
    restarted = VirtualTokenAuthority(clock=lambda: 100.0, store=restarted_store)
    assert restarted.statuses() == ()
    restarted_store.close()


def test_revoked_token_cannot_be_discarded_from_gateway_recognition():
    authority = VirtualTokenAuthority(clock=lambda: 100.0)
    plaintext = authority.issue(_policy())
    authority.revoke("client-1")

    with pytest.raises(VirtualTokenError, match="revoked.*cannot be discarded"):
        authority.discard("client-1")

    assert authority.recognizes(plaintext) is True


def test_scope_rate_expiry_and_revocation_are_enforced():
    now = [100.0]
    authority = VirtualTokenAuthority(clock=lambda: now[0])
    plaintext = authority.issue(_policy())

    with pytest.raises(VirtualTokenError, match="agent"):
        authority.reserve(plaintext, agent_id="occult.major.world")
    with pytest.raises(VirtualTokenError, match="route"):
        authority.reserve(
            plaintext,
            agent_id="occult.major.magician",
            card_id="minor.cups.page.other",
        )
    with pytest.raises(VirtualTokenError, match="tools"):
        authority.reserve(
            plaintext,
            agent_id="occult.major.magician",
            tools=frozenset({"write_file"}),
        )
    with pytest.raises(VirtualTokenError, match="memory"):
        authority.reserve(
            plaintext,
            agent_id="occult.major.magician",
            memory_namespaces=frozenset({"global"}),
        )

    authority.reserve(
        plaintext, agent_id="occult.major.magician", maximum_cost_usd=0.1
    ).release()
    authority.reserve(
        plaintext, agent_id="occult.major.magician", maximum_cost_usd=0.1
    ).release()
    with pytest.raises(VirtualTokenError, match="rate limit"):
        authority.reserve(plaintext, agent_id="occult.major.magician")

    now[0] = 161.0
    authority.reserve(plaintext, agent_id="occult.major.magician").release()
    authority.revoke("client-1")
    with pytest.raises(VirtualTokenError, match="revoked"):
        authority.policy(plaintext)

    expiring = authority.issue(_policy(token_id="client-2", expires_at=162.0))
    now[0] = 162.0
    with pytest.raises(VirtualTokenError, match="expired"):
        authority.policy(expiring)


def test_budget_reservations_commit_or_release():
    authority = VirtualTokenAuthority(clock=lambda: 100.0)
    plaintext = authority.issue(_policy(requests_per_minute=10))

    failed = authority.reserve(
        plaintext,
        agent_id="occult.major.magician",
        maximum_cost_usd=0.75,
    )
    failed.release()
    assert authority.status("client-1")["committed_cost_usd"] == 0

    successful = authority.reserve(
        plaintext,
        agent_id="occult.major.magician",
        maximum_cost_usd=0.75,
    )
    successful.commit(0.6)
    assert authority.status("client-1")["committed_cost_usd"] == 0.6

    with pytest.raises(VirtualTokenError, match="budget"):
        authority.reserve(
            plaintext,
            agent_id="occult.major.magician",
            maximum_cost_usd=0.5,
        )

    with pytest.raises(VirtualTokenError, match="finite"):
        authority.reserve(
            plaintext,
            agent_id="occult.major.magician",
            maximum_cost_usd=float("nan"),
        )

    invalid_commit = authority.reserve(
        plaintext,
        agent_id="occult.major.magician",
        maximum_cost_usd=0.1,
    )
    with pytest.raises(VirtualTokenError, match="finite"):
        invalid_commit.commit(float("nan"))
    invalid_commit.release()
    assert authority.status("client-1")["reserved_cost_usd"] == 0


def test_persistent_tokens_survive_restart_without_plaintext(tmp_path):
    path = tmp_path / "virtual_tokens.db"
    first_store = SQLiteVirtualTokenStore(path)
    first = VirtualTokenAuthority(clock=lambda: 100.0, store=first_store)
    plaintext = first.issue(_policy(requests_per_minute=10))
    first.reserve(
        plaintext,
        agent_id="occult.major.magician",
        maximum_cost_usd=0.5,
    ).commit(0.4)
    first.reserve(
        plaintext,
        agent_id="occult.major.magician",
        maximum_cost_usd=0.2,
    )
    first_store.close()

    assert all(
        plaintext.encode() not in candidate.read_bytes()
        for candidate in path.parent.glob("virtual_tokens.db*")
    )

    second_store = SQLiteVirtualTokenStore(path)
    second = VirtualTokenAuthority(clock=lambda: 100.0, store=second_store)
    assert second.policy(plaintext).token_id == "client-1"
    status = second.status("client-1")
    assert status["committed_cost_usd"] == 0.4
    assert status["reserved_cost_usd"] == 0
    assert status["allowed_agent_ids"] == ["occult.major.magician"]
    second.revoke("client-1")
    second_store.close()

    third_store = SQLiteVirtualTokenStore(path)
    third = VirtualTokenAuthority(clock=lambda: 100.0, store=third_store)
    with pytest.raises(VirtualTokenError, match="revoked"):
        third.policy(plaintext)
    assert third.statuses()[0]["revoked"] is True
    third_store.close()


def test_persistent_token_store_rejects_unknown_schema(tmp_path):
    path = tmp_path / "virtual_tokens.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()

    with pytest.raises(VirtualTokenError, match="unsupported"):
        SQLiteVirtualTokenStore(path)


def test_persistent_token_store_rejects_malformed_policy(tmp_path):
    path = tmp_path / "virtual_tokens.db"
    with SQLiteVirtualTokenStore(path) as store:
        VirtualTokenAuthority(store=store).issue(_policy())
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE virtual_tokens SET policy_json = ? WHERE token_id = ?",
        ("{}", "client-1"),
    )
    connection.commit()
    connection.close()

    with SQLiteVirtualTokenStore(path) as store:
        with pytest.raises(VirtualTokenError, match="policy is invalid"):
            VirtualTokenAuthority(store=store)


def test_virtual_token_policy_rejects_unsafe_persistent_values():
    with pytest.raises(ValueError, match="token_id"):
        _policy(token_id="../escape")
    with pytest.raises(ValueError, match="allowed_tools"):
        _policy(allowed_tools=frozenset({"../../secret"}))
    with pytest.raises(ValueError, match="maximum_budget"):
        _policy(maximum_budget_usd=float("nan"))

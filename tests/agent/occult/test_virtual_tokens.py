import pytest

from agent.occult.virtual_tokens import (
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

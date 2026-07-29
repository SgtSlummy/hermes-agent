import pytest

from agent.occult.contracts import DeckDescriptor, RoutingPolicy
from agent.occult.decks import DeckError, DeckRegistry

AGENTS = {"occult.major.magician"}
ROUTES = {"minor.pentacles.ace.local.test"}


def _deck(**overrides):
    values = {
        "deck_id": "occult.deck.development",
        "version": "1.0.0",
        "allowed_agent_ids": ("occult.major.magician",),
        "allowed_card_ids": ("minor.pentacles.ace.local.test",),
        "routing": RoutingPolicy(
            mode="local_only",
            free_only=True,
            local_only=True,
            maximum_fallbacks=1,
            maximum_cost_usd=0,
        ),
    }
    values.update(overrides)
    return DeckDescriptor(**values)


def test_deck_registry_persists_and_updates_version_atomically(tmp_path):
    path = tmp_path / "decks.json"
    first = DeckRegistry(path)
    first.put(
        _deck(),
        available_agent_ids=AGENTS,
        available_card_ids=ROUTES,
    )
    first.put(
        _deck(version="1.1.0"),
        available_agent_ids=AGENTS,
        available_card_ids=ROUTES,
    )

    second = DeckRegistry(path)
    assert second.get("occult.deck.development").version == "1.1.0"
    assert second.list() == (second.get("occult.deck.development"),)


def test_deck_registry_rejects_unscoped_or_unavailable_members(tmp_path):
    registry = DeckRegistry(tmp_path / "decks.json")
    with pytest.raises(DeckError, match=r"at least one (allowed )?agent"):
        registry.put(
            _deck(allowed_agent_ids=()),
            available_agent_ids=AGENTS,
            available_card_ids=ROUTES,
        )
    with pytest.raises(DeckError, match="unavailable agents"):
        registry.put(
            _deck(allowed_agent_ids=("occult.major.world",)),
            available_agent_ids=AGENTS,
            available_card_ids=ROUTES,
        )


def test_deck_validation_reports_routes_removed_after_install(tmp_path):
    registry = DeckRegistry(tmp_path / "decks.json")
    registry.put(
        _deck(),
        available_agent_ids=AGENTS,
        available_card_ids=ROUTES,
    )
    status = registry.validate_current(
        "occult.deck.development",
        available_agent_ids=AGENTS,
        available_card_ids=(),
    )
    assert status["valid"] is False
    assert status["missing_card_ids"] == ["minor.pentacles.ace.local.test"]

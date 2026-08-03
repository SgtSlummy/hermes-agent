from pathlib import Path

import pytest

from agent.occult.card_registry import CardRegistry, CardRegistryError


def test_registry_adds_public_provider_and_card_without_activation(tmp_path: Path):
    registry = CardRegistry(tmp_path / "registry.json")
    provider = registry.register_provider(
        {
            "provider_id": "local-test",
            "name": "Local Test",
            "auth_type": "keyless",
            "base_url": "http://127.0.0.1:11434/v1",
            "official_hosts": ["127.0.0.1"],
            "free_access": "anonymous_free",
            "zero_cost_model_ids": ["test-model"],
        }
    )
    card = registry.register_card(
        {
            "card_id": "minor.pentacles.page.local-test",
            "name": "Local Test Page",
            "provider_id": "local-test",
            "model_id": "test-model",
        }
    )

    assert provider["enrollment_mode"] == "keyless"
    assert provider["activation"] == "keyless_pending_validation"
    assert card["status"] == "pending_review"
    assert registry.providers()[0]["provider_id"] == "local-test"
    assert registry.cards()[0]["card_id"] == "minor.pentacles.page.local-test"


def test_registry_rejects_credentials_and_insecure_public_endpoints(tmp_path: Path):
    registry = CardRegistry(tmp_path / "registry.json")
    with pytest.raises(CardRegistryError, match="not accepted"):
        registry.register_provider(
            {"provider_id": "unsafe", "name": "Unsafe", "api_key": "never"}
        )
    with pytest.raises(CardRegistryError, match="HTTPS"):
        registry.register_provider(
            {"provider_id": "unsafe", "name": "Unsafe", "base_url": "http://example.test/v1"}
        )

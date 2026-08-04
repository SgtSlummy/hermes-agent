from pathlib import Path

import pytest

from agent.occult.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogError,
    load_bundled_provider_catalog,
)


def test_bundled_catalog_is_secret_free_and_exposes_free_policy():
    catalog = load_bundled_provider_catalog()
    summary = catalog.summary()
    assert summary["cataloged"] == len(catalog.list())
    assert summary["cataloged"] > 0
    assert summary["allowed_free"] == (
        summary["anonymous_free"]
        + summary["recurring_free"]
        + summary["temporary_credit"]
    )
    assert summary["blocked"] == summary["cataloged"] - summary["allowed_free"]
    openai = next(item for item in catalog.public() if item["provider_id"] == "openai")
    assert openai["allowed_by_free_policy"] is False
    assert openai["activation"] == "blocked_by_free_policy"
    gemini = next(item for item in catalog.public() if item["provider_id"] == "gemini")
    assert gemini["activation"] == "awaiting_authorized_credential"
    kilo = next(item for item in catalog.public() if item["provider_id"] == "kilo")
    assert kilo["enrollment_mode"] == "keyless"
    assert kilo["activation"] == "keyless_pending_validation"
    cloudflare = next(
        item for item in catalog.public() if item["provider_id"] == "cloudflare-workers-ai"
    )
    assert cloudflare["activation"] == "adapter_pending"
    snowflake = next(
        item for item in catalog.public() if item["provider_id"] == "snowflake-cortex"
    )
    assert snowflake["activation"] == "terms_pending"
    assert all("secret" not in str(item).lower() for item in catalog.public())


def test_catalog_rejects_secret_shaped_state(tmp_path: Path):
    path = tmp_path / "catalog.json"
    path.write_text(
        '{"schemaVersion":"1.0","providers":[{"id":"x","secret":"bad"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ProviderCatalogError, match="secret-shaped"):
        ProviderCatalog.from_path(path)

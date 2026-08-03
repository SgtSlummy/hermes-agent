from agent.occult.credential_broker import InMemoryCredentialBroker
from agent.occult.provider_catalog import CatalogProvider, ProviderCatalog
from agent.occult.provider_mesh import (
    ProviderMeshConfig,
    ProviderMeshError,
    activate_provider_mesh,
)


def _provider(
    provider_id: str = "anonymous-test",
    *,
    auth_type: str = "anonymous",
    base_url: str = "https://api.example.test/v1",
    secret_refs: tuple[str, ...] = (),
) -> CatalogProvider:
    return CatalogProvider(
        provider_id=provider_id,
        name="Test provider",
        free_access="anonymous_free" if auth_type == "anonymous" else "recurring_free",
        requires_card=False,
        adapter="openai_compatible",
        auth_type=auth_type,
        official_hosts=("example.test",),
        base_url=base_url,
        chat_path="/chat/completions",
        models_path=None,
        secret_refs=secret_refs,
        capabilities=("chat",),
        zero_cost_model_ids=("test-model",),
        default_free_model="test-model",
        allow_paid_models=False,
        terms_permit_tarot=True,
        allowed_data_classifications=("public",),
        enabled=False,
        source_state="eligibility_verified",
    )


def test_mesh_requires_explicit_selection_and_registers_keyless_route():
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=("anonymous-test",), discover_models=False),
        catalog=ProviderCatalog((_provider(),)),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.activated == ("anonymous-test",)
    assert result.pending_authorization == ()
    assert len(result.routes) == 1
    assert result.routes[0].free is True
    assert result.routes[0].credential_reference_id is not None
    assert "test-model" in result.routes[0].card_id


def test_mesh_never_activates_bearer_provider_without_authorized_secret():
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=("bearer-test",), discover_models=False),
        catalog=ProviderCatalog(
            (_provider("bearer-test", auth_type="bearer", secret_refs=("TEST_API_KEY",)),)
        ),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.routes == ()
    assert result.pending_authorization == ("bearer-test",)


def test_mesh_rejects_non_https_or_untrusted_endpoint():
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=("unsafe-test",), discover_models=False),
        catalog=ProviderCatalog(
            (_provider("unsafe-test", base_url="http://127.0.0.1:9999/v1"),)
        ),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.routes == ()
    assert result.skipped[0]["reason"] == "provider unsafe-test has an unsafe endpoint"


def test_mesh_config_rejects_paid_or_unbounded_settings():
    try:
        ProviderMeshConfig.from_mapping(
            {"enabled": True, "max_models_per_provider": 100}
        )
    except ProviderMeshError as exc:
        assert "max_models_per_provider" in str(exc)
    else:
        raise AssertionError("invalid provider mesh configuration was accepted")

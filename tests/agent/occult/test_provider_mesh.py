from dataclasses import replace

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


def test_mesh_can_unattendedly_enroll_reviewed_keyless_catalog_entries():
    result = activate_provider_mesh(
        ProviderMeshConfig(
            enabled=True,
            auto_enroll_keyless=True,
            discover_models=False,
        ),
        catalog=ProviderCatalog((_provider(), _provider("bearer-test", auth_type="bearer", secret_refs=("TEST_API_KEY",)))),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.activated == ("anonymous-test",)
    assert result.pending_authorization == ()


def test_full_free_mesh_enrolls_existing_bearer_and_reports_missing_authority():
    result = activate_provider_mesh(
        ProviderMeshConfig(
            enabled=True,
            auto_enroll_free=True,
            discover_models=False,
        ),
        catalog=ProviderCatalog(
            (
                _provider(),
                _provider(
                    "bearer-authorized",
                    auth_type="bearer",
                    secret_refs=("TEST_API_KEY",),
                ),
                _provider(
                    "bearer-pending",
                    auth_type="bearer",
                    secret_refs=("MISSING_API_KEY",),
                ),
            )
        ),
        environ={"TEST_API_KEY": "authorized-test-secret"},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.activated == ("anonymous-test", "bearer-authorized")
    assert result.pending_authorization == ("bearer-pending",)
    assert {route.provider_id for route in result.routes} == {
        "anonymous-test",
        "bearer-authorized",
    }


def test_mesh_card_ids_are_safe_when_provider_model_uses_path_separator():
    provider = replace(_provider(), zero_cost_model_ids=("org/model",))
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=(provider.provider_id,), discover_models=False),
        catalog=ProviderCatalog((provider,)),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert len(result.routes) == 1
    assert "/" not in result.routes[0].card_id
    assert result.routes[0].model_id == "org/model"


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


def test_mesh_rejects_unverified_terms_metadata():
    provider = replace(_provider(), terms_permit_tarot=False)
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=(provider.provider_id,), discover_models=False),
        catalog=ProviderCatalog((provider,)),
        environ={},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.routes == ()
    assert result.skipped == (
        {"provider_id": provider.provider_id, "reason": "terms_not_verified"},
    )


def test_mesh_config_rejects_paid_or_unbounded_settings():
    try:
        ProviderMeshConfig.from_mapping(
            {"enabled": True, "max_models_per_provider": 100}
        )
    except ProviderMeshError as exc:
        assert "max_models_per_provider" in str(exc)
    else:
        raise AssertionError("invalid provider mesh configuration was accepted")


def test_mesh_config_reads_full_free_enrollment_flag():
    config = ProviderMeshConfig.from_mapping(
        {"enabled": True, "autoEnrollFree": True}
    )

    assert config.auto_enroll_free is True


def test_mesh_accepts_gemini_openai_compatible_adapter_with_authorized_secret():
    provider = replace(
        _provider("gemini-test", auth_type="bearer", secret_refs=("GEMINI_API_KEY",)),
        adapter="google_gemini",
    )
    result = activate_provider_mesh(
        ProviderMeshConfig(enabled=True, provider_ids=(provider.provider_id,), discover_models=False),
        catalog=ProviderCatalog((provider,)),
        environ={"GEMINI_API_KEY": "authorized-gemini-secret"},
        credential_broker=InMemoryCredentialBroker(),
    )

    assert result.activated == ("gemini-test",)
    assert result.routes[0].provider_id == "gemini-test"

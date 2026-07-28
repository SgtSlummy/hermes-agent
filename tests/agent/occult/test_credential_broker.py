from datetime import UTC, datetime, timedelta

import pytest

from agent.occult.credential_broker import (
    CredentialBoundaryError,
    CredentialStatus,
    InMemoryCredentialBroker,
    SecretValue,
)


def test_secret_value_is_redacted_until_adapter_boundary():
    secret = SecretValue("sk-example-private-value")

    assert str(secret) == "[REDACTED]"
    assert repr(secret) == "SecretValue('[REDACTED]')"
    assert secret.reveal_for_adapter() == "sk-example-private-value"


def test_authorized_import_returns_only_opaque_metadata():
    broker = InMemoryCredentialBroker()

    reference = broker.import_authorized(
        provider_id="example",
        secret="sk-example-private-value",
        quota_pool_id="example:account-primary",
    )

    assert reference.reference_id.startswith("cred_")
    assert reference.provider_id == "example"
    assert reference.quota_pool_id == "example:account-primary"
    assert "private-value" not in repr(reference)
    assert "private-value" not in repr(broker.metadata())
    assert (
        broker
        .resolve(reference.reference_id)
        .reveal_for_adapter()
        .endswith("private-value")
    )


@pytest.mark.parametrize(
    "source",
    [
        "automated_account",
        "captcha_bypass",
        "disposable_email",
        "leaked",
        "public_repository",
        "quota_evasion",
        "scraped",
        "shared_community",
        "verification_bypass",
        "unknown_source",
    ],
)
def test_prohibited_or_unknown_credential_sources_are_rejected(source):
    broker = InMemoryCredentialBroker()

    with pytest.raises(
        CredentialBoundaryError, match="credential source is not authorized"
    ) as exc_info:
        broker.import_authorized(
            provider_id="example",
            secret="must-not-appear",
            quota_pool_id="example:primary",
            source=source,
        )

    assert "must-not-appear" not in str(exc_info.value)


def test_expired_and_revoked_credentials_cannot_be_resolved():
    broker = InMemoryCredentialBroker()
    expired = broker.import_authorized(
        provider_id="example",
        secret="expired-private-value",
        quota_pool_id="example:primary",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(CredentialBoundaryError, match="unavailable"):
        broker.resolve(expired.reference_id)

    active = broker.import_authorized(
        provider_id="example",
        secret="active-private-value",
        quota_pool_id="example:primary",
    )
    broker.revoke(active.reference_id)

    assert broker.reference(active.reference_id).status is CredentialStatus.REVOKED
    with pytest.raises(CredentialBoundaryError, match="unavailable"):
        broker.resolve(active.reference_id)


def test_keyless_route_has_no_secret_value():
    broker = InMemoryCredentialBroker()
    reference = broker.register_keyless(
        provider_id="ollama",
        quota_pool_id="ollama:local",
    )

    assert reference.source == "keyless"
    assert broker.resolve(reference.reference_id) is None


def test_identifiers_are_rejected_before_storage():
    broker = InMemoryCredentialBroker()

    with pytest.raises(CredentialBoundaryError, match="invalid provider id"):
        broker.import_authorized(
            provider_id="../provider",
            secret="private",
            quota_pool_id="example:primary",
        )

    assert broker.metadata() == ()


def test_invalid_expiration_is_rejected_without_echoing_secret():
    broker = InMemoryCredentialBroker()

    with pytest.raises(
        CredentialBoundaryError, match="invalid credential expiration"
    ) as exc_info:
        broker.import_authorized(
            provider_id="example",
            secret="must-not-appear",
            quota_pool_id="example:primary",
            expires_at="tomorrow",
        )

    assert "must-not-appear" not in str(exc_info.value)

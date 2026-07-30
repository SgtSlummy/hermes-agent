import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent.occult.contracts import RouteMode, validate_invocation
from agent.occult.credential_broker import InMemoryCredentialBroker
from agent.occult.mythos import (
    AcquisitionAction,
    AdapterResponse,
    FailureKind,
    LocalProviderAdapter,
    MinorArcanaDescriptor,
    MockProviderAdapter,
    MythosError,
    MythosRouter,
    MythosRoutingError,
    MythosStateStore,
    NoEligibleRoute,
    OpenAICompatibleAdapter,
    PrivacyClass,
    ProviderFailure,
    ProviderTrustState,
    RouterBusy,
    RouteRegistrationError,
    RouteHealthState,
    descriptor_from_provider,
    enforce_acquisition_policy,
)
from providers.base import ProviderProfile


def _invocation(
    *,
    mode: RouteMode = RouteMode.LOCAL_FIRST,
    free_only: bool = False,
    local_only: bool = False,
    maximum_fallbacks: int = 2,
    maximum_cost_usd: float = 1.0,
):
    return validate_invocation({
        "contract_version": "1.0.0",
        "invocation_id": "inv_mythos_test",
        "idempotency_key": "mythos:test",
        "agent_id": "occult.major.magician",
        "input": {"message": "Route this request safely."},
        "required_capabilities": ["text"],
        "routing": {
            "mode": mode.value,
            "free_only": free_only,
            "local_only": local_only,
            "maximum_fallbacks": maximum_fallbacks,
            "maximum_cost_usd": maximum_cost_usd,
        },
        "metadata": {},
    })


def _route(
    card_id: str,
    *,
    adapter_id: str = "local",
    provider_id: str = "local-test",
    model_id: str | None = None,
    local: bool = True,
    free: bool = True,
    privacy: PrivacyClass | None = None,
    quality_score: float = 0.5,
    latency_ms: int = 100,
    cost: float = 0.0,
    quota_pool_id: str | None = None,
    credential_reference_id: str | None = None,
    trust_state: ProviderTrustState = ProviderTrustState.DISCOVERED,
):
    return MinorArcanaDescriptor(
        card_id=card_id,
        provider_id=provider_id,
        model_id=model_id or f"{provider_id}-model",
        adapter_id=adapter_id,
        capabilities=frozenset({"text"}),
        local=local,
        free=free,
        privacy=privacy or (PrivacyClass.LOCAL if local else PrivacyClass.EXTERNAL),
        quality_score=quality_score,
        latency_ms=latency_ms,
        estimated_request_cost_usd=cost,
        quota_pool_id=quota_pool_id or f"{provider_id}:primary",
        credential_reference_id=credential_reference_id,
        trust_state=trust_state,
    )


def _activate(router: MythosRouter, route: MinorArcanaDescriptor):
    discovered = router.discover(route)
    return router.review(discovered.card_id, approve=True)


@pytest.mark.parametrize(
    ("adapter_type", "adapter_id", "local"),
    [
        (MockProviderAdapter, "mock", False),
        (LocalProviderAdapter, "local", True),
        (OpenAICompatibleAdapter, "openai-compatible", False),
    ],
)
def test_mock_local_and_openai_compatible_adapters_share_contract(
    adapter_type, adapter_id, local
):
    seen = []

    def handler(request, route, credential):
        seen.append((request, route, credential))
        return AdapterResponse(text=f"ok:{request.model_id}", output_tokens=2)

    broker = InMemoryCredentialBroker()
    credential_reference_id = None
    if not local:
        credential_reference_id = broker.import_authorized(
            provider_id=f"{adapter_id}-provider",
            secret=f"{adapter_id}-private-value",
            quota_pool_id=f"{adapter_id}-provider:primary",
        ).reference_id
    router = MythosRouter(
        adapters={adapter_id: adapter_type(handler)},
        credential_broker=broker,
    )
    route = _route(
        f"minor.test.{adapter_id}",
        adapter_id=adapter_id,
        provider_id=f"{adapter_id}-provider",
        local=local,
        quota_pool_id=f"{adapter_id}-provider:primary",
        credential_reference_id=credential_reference_id,
    )
    _activate(router, route)

    result = router.execute(_invocation())

    assert result.response.text == f"ok:{route.model_id}"
    assert result.summary.selected_card_id == route.card_id
    assert len(seen) == 1
    assert seen[0][0].message == "Route this request safely."
    assert (seen[0][2] is None) is local


def test_discovery_cannot_auto_activate_external_route():
    router = MythosRouter(
        adapters={"openai-compatible": OpenAICompatibleAdapter(lambda *_: None)}
    )
    route = _route(
        "minor.external.discovered",
        adapter_id="openai-compatible",
        provider_id="external",
        local=False,
        trust_state=ProviderTrustState.ACTIVE,
    )

    discovered = router.discover(route)

    assert discovered.trust_state is ProviderTrustState.DISCOVERED
    assert router.candidates(_invocation()) == ()


def test_local_only_never_invokes_external_adapter():
    calls = {"local": 0, "external": 0}

    def local_handler(*_):
        calls["local"] += 1
        return AdapterResponse(text="local")

    def external_handler(*_):
        calls["external"] += 1
        return AdapterResponse(text="external")

    broker = InMemoryCredentialBroker()
    external_reference = broker.import_authorized(
        provider_id="external",
        secret="external-private-value",
        quota_pool_id="external:primary",
    )
    router = MythosRouter(
        adapters={
            "local": LocalProviderAdapter(local_handler),
            "openai-compatible": OpenAICompatibleAdapter(external_handler),
        },
        credential_broker=broker,
    )
    _activate(router, _route("minor.local.safe", quality_score=0.1))
    _activate(
        router,
        _route(
            "minor.external.strong",
            adapter_id="openai-compatible",
            provider_id="external",
            local=False,
            quality_score=1.0,
            quota_pool_id=external_reference.quota_pool_id,
            credential_reference_id=external_reference.reference_id,
        ),
    )

    result = router.execute(_invocation(local_only=True))

    assert result.response.text == "local"
    assert calls == {"local": 1, "external": 0}


def test_free_only_excludes_priced_route():
    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(lambda *_: AdapterResponse(text="ok"))}
    )
    _activate(
        router,
        _route("minor.local.free", free=True, quality_score=0.1),
    )
    _activate(
        router,
        _route(
            "minor.local.paid",
            free=False,
            quality_score=1.0,
            cost=0.01,
        ),
    )

    candidates = router.candidates(_invocation(free_only=True))

    assert [route.card_id for route in candidates] == ["minor.local.free"]


def test_free_first_and_quality_first_apply_distinct_ranking():
    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(lambda *_: AdapterResponse(text="ok"))}
    )
    _activate(
        router,
        _route("minor.local.free", free=True, quality_score=0.1, latency_ms=500),
    )
    _activate(
        router,
        _route(
            "minor.local.paid",
            free=False,
            quality_score=1.0,
            latency_ms=50,
            cost=0.01,
        ),
    )

    free_first = router.candidates(_invocation(mode=RouteMode.FREE_FIRST))
    quality_first = router.candidates(_invocation(mode=RouteMode.QUALITY_FIRST))

    assert free_first[0].card_id == "minor.local.free"
    assert quality_first[0].card_id == "minor.local.paid"


def test_manual_mode_requires_and_selects_exact_card():
    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(lambda *_: AdapterResponse(text="ok"))}
    )
    _activate(router, _route("minor.local.one"))
    _activate(router, _route("minor.local.two"))

    with pytest.raises(NoEligibleRoute, match="requires a card id"):
        router.candidates(_invocation(mode=RouteMode.MANUAL))

    candidates = router.candidates(
        _invocation(mode=RouteMode.MANUAL),
        manual_card_id="minor.local.two",
    )
    assert [route.card_id for route in candidates] == ["minor.local.two"]


def test_credentials_on_one_account_share_one_quota_pool():
    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(lambda *_: AdapterResponse(text="ok"))}
    )
    shared_pool = "local-test:shared-account"
    _activate(router, _route("minor.local.one", quota_pool_id=shared_pool))
    _activate(router, _route("minor.local.two", quota_pool_id=shared_pool))
    router.set_quota(shared_pool, remaining_requests=1)

    router.execute(_invocation())

    assert router.candidates(_invocation()) == ()


def test_authentication_failure_suspends_route_without_retrying_it():
    attempts = {"primary": 0, "fallback": 0}

    def handler(_request, route, _credential):
        name = "primary" if route.card_id.endswith("primary") else "fallback"
        attempts[name] += 1
        if name == "primary":
            raise ProviderFailure(FailureKind.AUTHENTICATION)
        return AdapterResponse(text="fallback")

    broker = InMemoryCredentialBroker()
    primary_ref = broker.import_authorized(
        provider_id="external",
        secret="primary-private",
        quota_pool_id="external:primary",
    )
    fallback_ref = broker.import_authorized(
        provider_id="external",
        secret="fallback-private",
        quota_pool_id="external:fallback",
    )
    router = MythosRouter(
        adapters={"openai-compatible": OpenAICompatibleAdapter(handler)},
        credential_broker=broker,
    )
    _activate(
        router,
        _route(
            "minor.external.primary",
            adapter_id="openai-compatible",
            provider_id="external",
            local=False,
            quality_score=1.0,
            quota_pool_id=primary_ref.quota_pool_id,
            credential_reference_id=primary_ref.reference_id,
        ),
    )
    _activate(
        router,
        _route(
            "minor.external.fallback",
            adapter_id="openai-compatible",
            provider_id="external",
            local=False,
            quality_score=0.5,
            quota_pool_id=fallback_ref.quota_pool_id,
            credential_reference_id=fallback_ref.reference_id,
        ),
    )

    first = router.execute(_invocation())
    second = router.execute(_invocation())

    assert first.response.text == second.response.text == "fallback"
    assert first.summary.fallback_count == 1
    assert attempts == {"primary": 1, "fallback": 2}
    primary_status = next(
        route
        for route in router.status()["routes"]
        if route["card_id"] == "minor.external.primary"
    )
    assert primary_status["trust_state"] == "suspended"


def test_rate_limit_cools_shared_pool_then_recovers():
    now = [100.0]
    calls = {"primary": 0}

    def handler(_request, route, _credential):
        if route.card_id == "minor.local.primary":
            calls["primary"] += 1
            if calls["primary"] == 1:
                raise ProviderFailure(
                    FailureKind.RATE_LIMIT,
                    retry_after_seconds=10,
                )
        return AdapterResponse(text=route.card_id)

    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(handler)},
        clock=lambda: now[0],
    )
    _activate(
        router,
        _route("minor.local.primary", quality_score=1.0, quota_pool_id="shared"),
    )
    _activate(
        router,
        _route("minor.local.peer", quality_score=0.8, quota_pool_id="shared"),
    )
    _activate(
        router,
        _route("minor.local.fallback", quality_score=0.5, quota_pool_id="fallback"),
    )

    result = router.execute(_invocation(maximum_fallbacks=2))

    assert result.response.text == "minor.local.fallback"
    assert not any(
        route.quota_pool_id == "shared" for route in router.candidates(_invocation())
    )

    now[0] = 111.0
    recovered = router.candidates(_invocation())
    assert recovered[0].card_id == "minor.local.primary"


def test_fallback_attempts_are_bounded():
    attempts = []

    def handler(_request, route, _credential):
        attempts.append(route.card_id)
        raise ProviderFailure(FailureKind.UNAVAILABLE)

    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(handler)},
        failure_threshold=1,
    )
    for index in range(3):
        _activate(
            router,
            _route(f"minor.local.{index}", quality_score=1 - (index / 10)),
        )

    with pytest.raises(MythosRoutingError) as exc_info:
        router.execute(_invocation(maximum_fallbacks=1))

    assert len(exc_info.value.failures) == 2
    assert attempts == ["minor.local.0", "minor.local.1"]


def test_queue_pressure_fails_closed_and_releases_capacity():
    started = Event()
    release = Event()

    def handler(_request, _route, _credential):
        started.set()
        assert release.wait(timeout=5)
        return AdapterResponse(text="completed")

    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(handler)},
        maximum_concurrent_requests=1,
        capacity_wait_seconds=0,
    )
    _activate(router, _route("minor.local.capacity"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(router.execute, _invocation())
        assert started.wait(timeout=5)
        with pytest.raises(RouterBusy, match="capacity is exhausted"):
            router.execute(_invocation())
        assert router.status()["capacity"] == {
            "maximum": 1,
            "in_flight": 1,
            "rejected": 1,
        }
        release.set()
        assert first.result(timeout=5).response.text == "completed"

    assert router.status()["capacity"] == {
        "maximum": 1,
        "in_flight": 0,
        "rejected": 1,
    }


def test_timeout_opens_circuit_without_exposing_provider_details():
    def handler(*_):
        raise ProviderFailure(FailureKind.TIMEOUT)

    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(handler)},
        failure_threshold=1,
    )
    _activate(router, _route("minor.local.timeout"))

    with pytest.raises(MythosRoutingError) as exc_info:
        router.execute(_invocation(maximum_fallbacks=0))

    assert exc_info.value.failures == (("minor.local.timeout", FailureKind.TIMEOUT),)
    assert router.status()["routes"][0]["healthy"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_concurrent_requests", 0),
        ("capacity_wait_seconds", -1),
    ],
)
def test_invalid_capacity_configuration_fails_closed(field, value):
    with pytest.raises(ValueError):
        MythosRouter(adapters={}, **{field: value})


def test_unexpected_adapter_exception_is_redacted_and_normalized():
    def handler(*_):
        raise RuntimeError("Bearer must-never-cross-the-boundary")

    router = MythosRouter(
        adapters={"local": LocalProviderAdapter(handler)},
        failure_threshold=1,
    )
    _activate(router, _route("minor.local.failure"))

    with pytest.raises(MythosRoutingError) as exc_info:
        router.execute(_invocation(maximum_fallbacks=0))

    assert exc_info.value.failures == (("minor.local.failure", FailureKind.UNKNOWN),)
    assert "must-never-cross" not in str(exc_info.value)
    assert "must-never-cross" not in repr(exc_info.value.failures)


def test_profile_safe_state_round_trip_contains_no_credentials(tmp_path):
    state_path = tmp_path / "profile" / "occult" / "mythos-state.json"
    store = MythosStateStore(state_path)
    health = {
        "minor.local.safe": RouteHealthState(
            consecutive_failures=1,
            last_failure_kind="unavailable",
        )
    }
    from agent.occult.mythos import QuotaPoolState

    quotas = {
        "local:primary": QuotaPoolState(
            quota_pool_id="local:primary",
            remaining_requests=3,
        )
    }

    store.save(health, quotas)
    loaded_health, loaded_quotas = store.load()
    serialized = state_path.read_text(encoding="utf-8")

    assert loaded_health["minor.local.safe"].consecutive_failures == 1
    assert loaded_quotas["local:primary"].remaining_requests == 3
    assert "credential" not in serialized.lower()
    assert "authorization" not in serialized.lower()
    assert json.loads(serialized)["version"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "health": {
                "minor.local.safe": {
                    "consecutive_failures": -1,
                    "circuit_open_until": 0,
                    "suspended": False,
                    "last_failure_kind": None,
                    "last_success_at": None,
                }
            },
            "quotas": {},
        },
        {
            "version": 1,
            "health": {},
            "quotas": {
                "local:primary": {
                    "quota_pool_id": "local:primary",
                    "remaining_requests": 1,
                    "cooldown_until": float("nan"),
                }
            },
        },
    ],
)
def test_state_store_rejects_malformed_health_and_quota_values(tmp_path, payload):
    state_path = tmp_path / "mythos-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RouteRegistrationError):
        MythosStateStore(state_path).load()


def test_status_omits_credential_reference_and_secret():
    broker = InMemoryCredentialBroker()
    reference = broker.import_authorized(
        provider_id="external",
        secret="sk-never-serialize-this",
        quota_pool_id="external:primary",
    )
    router = MythosRouter(
        adapters={
            "openai-compatible": OpenAICompatibleAdapter(
                lambda *_: AdapterResponse(text="ok")
            )
        },
        credential_broker=broker,
    )
    _activate(
        router,
        _route(
            "minor.external.safe",
            adapter_id="openai-compatible",
            provider_id="external",
            local=False,
            quota_pool_id=reference.quota_pool_id,
            credential_reference_id=reference.reference_id,
        ),
    )

    serialized = json.dumps(router.status(), sort_keys=True)

    assert reference.reference_id not in serialized
    assert "never-serialize" not in serialized
    assert "credential" not in serialized.lower()


@pytest.mark.parametrize(
    "action",
    [
        AcquisitionAction.AUTOMATED_ACCOUNT,
        AcquisitionAction.CAPTCHA_BYPASS,
        AcquisitionAction.LEAKED_OR_SHARED_KEY,
        AcquisitionAction.QUOTA_EVASION,
        AcquisitionAction.VERIFICATION_BYPASS,
    ],
)
def test_prohibited_acquisition_actions_fail_closed(action):
    with pytest.raises(MythosError, match="prohibited"):
        enforce_acquisition_policy(action)


@pytest.mark.parametrize(
    "action",
    [
        AcquisitionAction.IMPORT_USER_AUTHORIZED,
        AcquisitionAction.KEYLESS_DISCOVERY,
        AcquisitionAction.OAUTH_REFRESH,
        AcquisitionAction.OFFICIAL_ROTATION,
        AcquisitionAction.SERVICE_ACCOUNT,
    ],
)
def test_authorized_acquisition_actions_are_explicit(action):
    enforce_acquisition_policy(action)


def test_existing_provider_profile_normalizes_without_credentials():
    descriptor = descriptor_from_provider(
        ProviderProfile(
            name="example",
            base_url="https://api.example.invalid/v1",
            env_vars=("EXAMPLE_API_KEY",),
        ),
        model_id="example-model",
        card_id="minor.swords.page.example",
        free=True,
    )

    assert descriptor.provider_id == "example"
    assert descriptor.model_id == "example-model"
    assert descriptor.credential_reference_id is None
    assert descriptor.trust_state is ProviderTrustState.DISCOVERED

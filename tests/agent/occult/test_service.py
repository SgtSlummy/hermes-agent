import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application

from agent.occult.contracts import DeckDescriptor, RoutingPolicy
from agent.occult.decks import DeckRegistry
from agent.occult.http import OccultHTTPAdapter
from agent.occult.idempotency import (
    InvocationIdempotencyError,
    SQLiteInvocationResultStore,
)
from agent.occult.mythos import (
    AdapterResponse,
    FailureKind,
    MinorArcanaDescriptor,
    MockProviderAdapter,
    MythosRouter,
    PrivacyClass,
    ProviderFailure,
)
from agent.occult.pairing import RuntimePolicy
from agent.occult.readings import (
    CouncilNodeResult,
    ReadingNode,
    ReadingPlan,
    ReadingStore,
    RetryableReadingError,
)
from agent.occult.service import OccultService
from agent.occult.tarot_packages import (
    AgentDefinition,
    BehaviorDefinition,
    EntrypointDefinition,
    MemoryDefinition,
    OrientationDefinition,
    PermissionDefinition,
    RoutingDefinition,
    TarotManifest,
    TemperamentAxis,
    ToolDefinition,
    ValidatedTarotPackage,
)
from agent.occult.virtual_tokens import (
    VirtualTokenAuthority,
    VirtualTokenError,
    VirtualTokenPolicy,
)


class _PackageManager:
    def __init__(
        self,
        agent_ids: tuple[str, ...] = ("occult.major.magician",),
    ):
        card_profiles = {
            "occult.major.magician": ("The Magician", 1),
            "occult.major.justice": ("Justice", 11),
            "occult.major.temperance": ("Temperance", 14),
        }
        installed = []
        for agent_id in agent_ids:
            name, number = card_profiles[agent_id]
            manifest = TarotManifest(
                format_version="1.0",
                agent=AgentDefinition(
                    id=agent_id,
                    name=name,
                    arcana_number=number,
                    version="1.0.0",
                    description="Executes a bounded Council role.",
                ),
                orientation=OrientationDefinition(upright=True, reversed=True),
                capabilities=("text",),
                temperament={
                    "precision": TemperamentAxis(
                        default=0.8,
                        minimum=0.5,
                        maximum=1.0,
                    )
                },
                permissions=PermissionDefinition(maximum_risk_level=0),
                entrypoints=EntrypointDefinition(
                    system_prompt="system_prompt.md",
                    behavior="behavior.yaml",
                    routing="routing.yaml",
                    memory="memory.yaml",
                    tools="tools.yaml",
                ),
            )
            package = ValidatedTarotPackage(
                manifest=manifest,
                behavior=BehaviorDefinition(
                    upright="Execute carefully.",
                    reversed="Check feasibility.",
                ),
                routing=RoutingDefinition(
                    required_capabilities=("text",),
                    allow_paid=False,
                    allow_external=False,
                ),
                memory=MemoryDefinition(
                    namespaces=("project",),
                    maximum_sensitivity="internal",
                    external_maximum_sensitivity="public",
                ),
                tools=ToolDefinition(),
                system_prompt="Preserve user intent.",
                signer_id="test",
                files={},
            )
            installed.append(
                SimpleNamespace(package=package, path=Path("test-package"))
            )
        self._installed_by_id = {
            item.package.manifest.agent.id: item for item in installed
        }

    def active(self, agent_id):
        return self._installed_by_id.get(agent_id)

    def active_packages(self):
        return tuple(self._installed_by_id.values())

    def generation(self):
        return 1


def _service(
    agent_ids: tuple[str, ...] = ("occult.major.magician",),
    *,
    maximum_concurrent_requests: int = 8,
    requests_per_minute: int = 10,
    invocation_store: SQLiteInvocationResultStore | None = None,
    provider_invoke=None,
):
    seen_messages: list[str] = []

    def invoke(request, _route, _credential):
        seen_messages.append(request.message)
        if provider_invoke is not None:
            return provider_invoke(request, _route, _credential)
        return AdapterResponse("completed", input_tokens=10, output_tokens=2)

    router = MythosRouter(
        adapters={"mock": MockProviderAdapter(invoke)},
        maximum_concurrent_requests=maximum_concurrent_requests,
    )
    route = MinorArcanaDescriptor(
        card_id="minor.pentacles.ace.local.test",
        provider_id="local-test",
        model_id="test-model",
        adapter_id="mock",
        capabilities=frozenset({"text"}),
        local=True,
        free=True,
        privacy=PrivacyClass.LOCAL,
        quota_pool_id="local-test:primary",
    )
    router.discover(route)
    router.review(route.card_id, approve=True)
    authority = VirtualTokenAuthority()
    plaintext = authority.issue(
        VirtualTokenPolicy(
            token_id="client",
            allowed_agent_ids=frozenset(agent_ids),
            allowed_card_ids=frozenset({route.card_id}),
            requests_per_minute=requests_per_minute,
            maximum_budget_usd=0,
        )
    )
    service = OccultService(
        package_manager=_PackageManager(agent_ids),
        router=router,
        token_authority=authority,
        runtime_policy=RuntimePolicy(),
        invocation_store=invocation_store,
    )
    return service, plaintext, seen_messages


def _invocation():
    return {
        "contract_version": "1.0.0",
        "invocation_id": "inv_service_test",
        "idempotency_key": "service:test",
        "agent_id": "occult.major.magician",
        "input": {"message": "Build the system."},
        "required_capabilities": ["text"],
        "routing": {
            "mode": "local_only",
            "free_only": True,
            "local_only": True,
            "maximum_fallbacks": 0,
            "maximum_cost_usd": 0,
        },
        "metadata": {},
    }


def test_service_validates_authorizes_pairs_and_routes():
    service, token, seen = _service()

    result = service.invoke(token, _invocation())

    assert result["output"] == "completed"
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert result["route"]["selected_card_id"] == "minor.pentacles.ace.local.test"
    assert "# Major Arcana" in seen[0]
    assert "# Task\nBuild the system." in seen[0]
    assert "occult_" not in seen[0]


def test_contract_mismatch_fails_before_provider_call():
    service, token, seen = _service()
    payload = _invocation()
    payload["contract_version"] = "999.0.0"

    with pytest.raises(ValueError, match="version mismatch"):
        service.invoke(token, payload)
    assert seen == []


def test_token_route_allowlist_filters_before_provider_call():
    service, _token, seen = _service()
    restricted_token = service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="restricted-client",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({"minor.swords.king.unavailable"}),
            maximum_budget_usd=0,
        )
    )

    with pytest.raises(
        VirtualTokenError,
        match="no eligible permitted route",
    ):
        service.invoke(restricted_token, _invocation())
    assert seen == []


def test_idempotency_replays_consume_rate_limit(tmp_path: Path):
    store = SQLiteInvocationResultStore(tmp_path / "invocations.db")
    service, token, seen = _service(
        requests_per_minute=2,
        invocation_store=store,
    )

    first = service.invoke(token, _invocation())
    replay = service.invoke(token, _invocation())

    assert replay == first
    assert len(seen) == 1
    with pytest.raises(VirtualTokenError, match="rate limit"):
        service.invoke(token, _invocation())
    store.close()


def test_durable_invocation_results_have_bounded_retention(tmp_path: Path):
    path = tmp_path / "invocations.db"
    store = SQLiteInvocationResultStore(path, maximum_entries=2)
    calls = []

    for index in range(3):
        store.run(
            "client",
            f"key-{index}",
            f"fingerprint-{index}",
            lambda index=index: calls.append(index) or {"index": index},
        )

    with sqlite3.connect(path) as connection:
        retained = connection.execute(
            "SELECT idempotency_key FROM invocation_results "
            "ORDER BY created_at, rowid"
        ).fetchall()
    replay = store.run(
        "client",
        "key-2",
        "fingerprint-2",
        lambda: pytest.fail("retained result must replay"),
    )
    with pytest.raises(InvocationIdempotencyError, match="result expired"):
        store.run(
            "client",
            "key-0",
            "fingerprint-0",
            lambda: pytest.fail("expired identity must not execute again"),
        )
    with pytest.raises(InvocationIdempotencyError, match="different input"):
        store.run(
            "client",
            "key-0",
            "different-fingerprint",
            lambda: pytest.fail("reused identity must not execute again"),
        )
    with sqlite3.connect(path) as connection:
        identities = connection.execute(
            "SELECT COUNT(*) FROM invocation_identities"
        ).fetchone()[0]
    store.close()

    assert retained == [("key-1",), ("key-2",)]
    assert identities == 3
    assert replay == {"index": 2}
    assert calls == [0, 1, 2]


def test_idempotency_identity_horizon_expires_keys_explicitly(
    tmp_path: Path,
    monkeypatch,
):
    now = [100.0]
    monkeypatch.setattr("agent.occult.idempotency.time.time", lambda: now[0])
    store = SQLiteInvocationResultStore(
        tmp_path / "invocations.db",
        retention_seconds=10,
        identity_retention_seconds=20,
    )
    calls = []
    store.run(
        "client",
        "old-key",
        "old-fingerprint",
        lambda: calls.append("old") or {"value": "old"},
    )

    now[0] = 121.0
    reused = store.run(
        "client",
        "old-key",
        "new-fingerprint",
        lambda: calls.append("reused") or {"value": "reused"},
    )
    store.close()

    assert reused == {"value": "reused"}
    assert calls == ["old", "reused"]


@pytest.mark.asyncio
async def test_http_surface_requires_virtual_token_and_runs_real_operations(
    tmp_path: Path,
):
    service, token, seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")

    def execute(_token, request):
        return CouncilNodeResult(
            artifact={"node": request.node_id},
            route_summary={"card_id": "minor.pentacles.ace.local.test"},
        )

    app = Application()
    OccultHTTPAdapter(service, readings, execute).register(app)
    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.get("/v1/occult/major-arcana")
        assert unauthorized.status == 401

        headers = {"Authorization": f"Bearer {token}"}
        agents = await client.get("/v1/occult/major-arcana", headers=headers)
        assert agents.status == 200
        assert (await agents.json())["data"][0]["agent_id"] == ("occult.major.magician")

        invoke_response = await client.post(
            "/v1/occult/invoke", headers=headers, json=_invocation()
        )
        assert invoke_response.status == 200
        bridge_result = await invoke_response.json()
        assert set(bridge_result) == {
            "contract_version",
            "invocation_id",
            "status",
            "summary",
            "route_summary",
            "artifacts",
            "error",
        }
        assert bridge_result["status"] == "completed"
        assert bridge_result["summary"] == "completed"
        assert (
            bridge_result["route_summary"]["invocation_id"]
            == (bridge_result["invocation_id"])
        )
        assert bridge_result["artifacts"] == []
        assert bridge_result["error"] is None

        reading_payload = {
            "contract_version": "1.0.0",
            "idempotency_key": "http-reading",
            "spread_id": "occult.spread.single",
            "nodes": [
                {
                    "node_id": "build",
                    "agent_id": "occult.major.magician",
                    "task": "Build.",
                }
            ],
        }
        created = await client.post(
            "/v1/occult/readings", headers=headers, json=reading_payload
        )
        assert created.status == 202
        reading_id = (await created.json())["reading_id"]
        resumed = await client.post(
            f"/v1/occult/readings/{reading_id}/resume", headers=headers
        )
        assert resumed.status == 200
        assert (await resumed.json())["state"] == "completed"
        events = await client.get(
            f"/v1/occult/readings/{reading_id}/events", headers=headers
        )
        event_data = (await events.json())["data"]
        assert event_data[-1]["event_type"] == "reading.completed"
        stream = await client.get(
            f"/v1/occult/readings/{reading_id}/events?stream=1",
            headers={**headers, "Accept": "text/event-stream"},
        )
        assert stream.status == 200
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        frames = await stream.text()
        assert "event: reading.started" in frames
        assert "event: reading.completed" in frames
        assert frames.count("event: reading.completed") == 1

        final_sequence = event_data[-1]["sequence"]
        resumed_stream = await client.get(
            f"/v1/occult/readings/{reading_id}/events?stream=1",
            headers={**headers, "Last-Event-ID": str(final_sequence)},
        )
        assert resumed_stream.status == 200
        assert await resumed_stream.text() == ""
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_reading_creation_consumes_virtual_token_rate_limit(tmp_path: Path):
    service, token, _seen = _service(requests_per_minute=1)
    readings = ReadingStore(tmp_path / "readings.db")
    app = Application()
    OccultHTTPAdapter(service, readings).register(app)
    headers = {"Authorization": f"Bearer {token}"}

    def payload(key: str):
        return {
            "contract_version": "1.0.0",
            "idempotency_key": key,
            "spread_id": "occult.spread.rate-limited-create",
            "nodes": [
                {
                    "node_id": "build",
                    "agent_id": "occult.major.magician",
                    "task": "Build.",
                }
            ],
        }

    async with TestClient(TestServer(app)) as client:
        created = await client.post(
            "/v1/occult/readings",
            headers=headers,
            json=payload("first"),
        )
        limited = await client.post(
            "/v1/occult/readings",
            headers=headers,
            json=payload("second"),
        )
        body = await limited.json()

    assert created.status == 202
    assert limited.status == 429
    assert body["error"]["retryable"] is True
    assert body["error"]["redacted"] is True
    readings.close()


@pytest.mark.asyncio
async def test_retryable_reading_failure_returns_503_and_remains_pending(
    tmp_path: Path,
):
    service, token, _seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")
    owner = service.token_authority.policy(token).token_id
    reading_id = readings.create(
        ReadingPlan(
            spread_id="occult.spread.retryable-http",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="retryable-http",
        contract_version="1.0.0",
        owner_token_id=owner,
    )

    def unavailable(_token, _request):
        raise RetryableReadingError("provider unavailable")

    app = Application()
    OccultHTTPAdapter(service, readings, unavailable).register(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/occult/readings/{reading_id}/resume",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = await response.json()

    assert response.status == 503
    assert body["error"]["retryable"] is True
    assert readings.status(reading_id)["state"] == "pending"
    readings.close()


@pytest.mark.asyncio
async def test_permanent_provider_rejections_are_not_retryable_http_failures(
    tmp_path: Path,
):
    def reject(_request, _route, _credential):
        raise ProviderFailure(FailureKind.INVALID_REQUEST)

    service, token, _seen = _service(provider_invoke=reject)
    readings = ReadingStore(tmp_path / "readings.db")
    app = Application()
    adapter = OccultHTTPAdapter(service, readings)
    adapter.register(app)
    headers = {"Authorization": f"Bearer {token}"}

    async with TestClient(TestServer(app)) as client:
        native = await client.post(
            "/v1/occult/invoke",
            headers=headers,
            json=_invocation(),
        )
        native_body = await native.json()
        compatible = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "occult.major.magician",
                "messages": [{"role": "user", "content": "Reject this."}],
            },
        )
        compatible_body = await compatible.json()

    assert native.status == 400
    assert native_body["error"]["retryable"] is False
    assert native_body["error"]["code"] == "OCCULT_INVALID_REQUEST"
    assert compatible.status == 400
    assert compatible_body["error"]["code"] == "invalid_request"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_runtime_shutdown_drains_cancelled_request_workers(tmp_path: Path):
    from threading import Event

    service, _token, _seen = _service()
    adapter = OccultHTTPAdapter(service, ReadingStore(tmp_path / "readings.db"))
    started = Event()
    release = Event()

    def blocking_worker():
        started.set()
        release.wait(timeout=5)
        return "done"

    request_task = asyncio.create_task(adapter._run_worker(blocking_worker))
    assert await asyncio.to_thread(started.wait, 1)
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    with pytest.raises(RuntimeError, match="must be drained"):
        adapter.close()

    close_task = asyncio.create_task(adapter.aclose())
    await asyncio.sleep(0)
    assert close_task.done() is False
    release.set()
    await close_task


@pytest.mark.asyncio
async def test_http_event_stream_requires_token_and_valid_cursor(tmp_path: Path):
    service, token, _seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")
    reading_id = readings.create(
        ReadingPlan(
            spread_id="occult.spread.single",
            nodes=(
                ReadingNode(
                    node_id="build",
                    agent_id="occult.major.magician",
                    task="Build.",
                ),
            ),
        ),
        idempotency_key="stream-auth",
        contract_version="1.0.0",
    )
    readings.cancel(reading_id)
    app = Application()
    OccultHTTPAdapter(service, readings).register(app)

    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.get(
            f"/v1/occult/readings/{reading_id}/events?stream=1"
        )
        assert unauthorized.status == 401
        invalid_cursor = await client.get(
            f"/v1/occult/readings/{reading_id}/events?stream=1&after=-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert invalid_cursor.status == 400
        assert (await invalid_cursor.json())["error"]["redacted"] is True


@pytest.mark.asyncio
async def test_http_event_stream_finishes_after_authenticated_cancellation(
    tmp_path: Path,
):
    service, token, _seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")
    reading_id = readings.create(
        ReadingPlan(
            spread_id="occult.spread.single",
            nodes=(
                ReadingNode(
                    node_id="build",
                    agent_id="occult.major.magician",
                    task="Build.",
                ),
            ),
        ),
        idempotency_key="stream-cancel",
        contract_version="1.0.0",
        owner_token_id="client",
    )
    app = Application()
    OccultHTTPAdapter(service, readings).register(app)
    headers = {"Authorization": f"Bearer {token}"}

    async with TestClient(TestServer(app)) as client:
        stream = await client.get(
            f"/v1/occult/readings/{reading_id}/events?stream=1",
            headers={**headers, "Accept": "text/event-stream"},
        )
        cancelled = await client.post(
            f"/v1/occult/readings/{reading_id}/cancel", headers=headers
        )
        assert cancelled.status == 200
        frames = await stream.text()

    assert frames.count("event: reading.cancelled") == 1
    assert "event: reading.completed" not in frames


@pytest.mark.asyncio
async def test_http_token_admin_is_separate_gated_and_secret_once(tmp_path: Path):
    service, user_token, _seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")
    admin_key = "admin-" + ("a" * 40)
    app = Application()
    adapter = OccultHTTPAdapter(
        service,
        readings,
        admin_key_digest=OccultHTTPAdapter.digest_admin_key(admin_key),
    )
    adapter.register(app)
    payload = {
        "token_id": "council-client",
        "allowed_agent_ids": ["occult.major.magician"],
        "allowed_card_ids": ["minor.pentacles.ace.local.test"],
        "allowed_tools": [],
        "allowed_memory_namespaces": [],
        "requests_per_minute": 5,
        "maximum_budget_usd": 0,
        "expires_at": None,
    }

    async with TestClient(TestServer(app)) as client:
        bearer_only = await client.post(
            "/v1/occult/admin/tokens",
            headers={"Authorization": f"Bearer {user_token}"},
            json=payload,
        )
        assert bearer_only.status == 401

        admin_headers = {"X-Occult-Admin-Key": admin_key}
        unscoped = await client.post(
            "/v1/occult/admin/tokens",
            headers=admin_headers,
            json={**payload, "allowed_agent_ids": []},
        )
        assert unscoped.status == 400

        issued = await client.post(
            "/v1/occult/admin/tokens", headers=admin_headers, json=payload
        )
        assert issued.status == 201
        issued_payload = await issued.json()
        plaintext = issued_payload["token"]
        assert plaintext.startswith("occult_")
        assert issued_payload["secret_once"] is True

        listed = await client.get("/v1/occult/admin/tokens", headers=admin_headers)
        listing = await listed.json()
        assert listed.status == 200
        assert plaintext not in repr(listing)
        assert any(token["token_id"] == "council-client" for token in listing["data"])

        permitted = await client.get(
            "/v1/occult/major-arcana",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert permitted.status == 200

        revoked = await client.post(
            "/v1/occult/admin/tokens/council-client/revoke",
            headers=admin_headers,
        )
        assert revoked.status == 200
        assert (await revoked.json())["revoked"] is True
        routed_request = SimpleNamespace(
            headers={"Authorization": f"Bearer {plaintext}"}
        )
        assert adapter.handles_openai_request(routed_request) is True
        denied = await client.get(
            "/v1/occult/major-arcana",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert denied.status == 403


@pytest.mark.asyncio
async def test_http_token_admin_routes_are_absent_without_admin_digest(tmp_path: Path):
    service, _token, _seen = _service()
    app = Application()
    OccultHTTPAdapter(
        service,
        ReadingStore(tmp_path / "readings.db"),
    ).register(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/occult/admin/tokens")
    assert response.status == 404
    with pytest.raises(ValueError, match="at least 32"):
        OccultHTTPAdapter.digest_admin_key("short")


@pytest.mark.asyncio
async def test_http_deck_admin_pairing_and_deck_invocation(tmp_path: Path):
    service, token, seen = _service()
    service.deck_registry = DeckRegistry(tmp_path / "decks.json")
    admin_key = "admin-" + ("d" * 40)
    app = Application()
    OccultHTTPAdapter(
        service,
        ReadingStore(tmp_path / "readings.db"),
        admin_key_digest=OccultHTTPAdapter.digest_admin_key(admin_key),
    ).register(app)
    deck = DeckDescriptor(
        deck_id="occult.deck.development",
        version="1.0.0",
        allowed_agent_ids=("occult.major.magician",),
        allowed_card_ids=("minor.pentacles.ace.local.test",),
        routing=RoutingPolicy(
            mode="local_only",
            free_only=True,
            local_only=True,
            maximum_fallbacks=1,
            maximum_cost_usd=0,
        ),
    ).model_dump(mode="json")
    admin = {"X-Occult-Admin-Key": admin_key}
    bearer = {"Authorization": f"Bearer {token}"}

    async with TestClient(TestServer(app)) as client:
        installed = await client.post(
            "/v1/occult/admin/decks", headers=admin, json=deck
        )
        assert installed.status == 201
        decks = await client.get("/v1/occult/decks", headers=bearer)
        assert (await decks.json())["data"][0]["active"] is True
        pairings = await client.get("/v1/occult/pairings", headers=bearer)
        assert (await pairings.json())["data"][0]["agent_id"] == (
            "occult.major.magician"
        )
        validation = await client.get(
            "/v1/occult/decks/occult.deck.development/validate",
            headers=bearer,
        )
        assert validation.status == 200
        assert (await validation.json()) == {
            "deck_id": "occult.deck.development",
            "version": "1.0.0",
            "valid": True,
            "missing_agent_ids": [],
            "missing_card_ids": [],
            "compatible_pairings": 1,
        }
        invocation = {**_invocation(), "deck_id": "occult.deck.development"}
        invoked = await client.post(
            "/v1/occult/invoke", headers=bearer, json=invocation
        )
        assert invoked.status == 200
        assert seen


@pytest.mark.asyncio
async def test_http_bridge_returns_strict_redacted_failure_profile(tmp_path: Path):
    service, token, seen = _service()
    app = Application()
    OccultHTTPAdapter(
        service,
        ReadingStore(tmp_path / "readings.db"),
    ).register(app)

    payload = _invocation()
    payload["contract_version"] = "2.0.0"
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/occult/invoke",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        result = await response.json()

    assert response.status == 400
    assert result["invocation_id"] == payload["invocation_id"]
    assert result["status"] == "failed"
    assert result["route_summary"] is None
    assert result["error"] == {
        "contract_version": "1.0.0",
        "code": "OCCULT_INVALID_REQUEST",
        "message": (
            "Occult contract version mismatch: expected '1.0.0', received '2.0.0'"
        ),
        "retryable": False,
        "redacted": True,
    }
    assert seen == []


@pytest.mark.asyncio
async def test_http_bridge_maps_capacity_pressure_to_retryable_503(tmp_path: Path):
    service, token, seen = _service(maximum_concurrent_requests=1)
    app = Application()
    OccultHTTPAdapter(
        service,
        ReadingStore(tmp_path / "readings.db"),
    ).register(app)
    assert service.router._capacity.acquire(timeout=0)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/occult/invoke",
                headers={"Authorization": f"Bearer {token}"},
                json=_invocation(),
            )
            result = await response.json()
    finally:
        service.router._capacity.release()

    assert response.status == 503
    assert result["status"] == "failed"
    assert result["error"] == {
        "contract_version": "1.0.0",
        "code": "OCCULT_CAPACITY_EXHAUSTED",
        "message": "Occult provider capacity is temporarily exhausted",
        "retryable": True,
        "redacted": True,
    }
    assert seen == []

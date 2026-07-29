from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application

from agent.occult.http import OccultHTTPAdapter
from agent.occult.mythos import (
    AdapterResponse,
    MinorArcanaDescriptor,
    MockProviderAdapter,
    MythosRouter,
    PrivacyClass,
)
from agent.occult.pairing import RuntimePolicy
from agent.occult.readings import CouncilNodeResult, ReadingStore
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
    def __init__(self):
        manifest = TarotManifest(
            format_version="1.0",
            agent=AgentDefinition(
                id="occult.major.magician",
                name="The Magician",
                arcana_number=1,
                version="1.0.0",
                description="Builds systems.",
            ),
            orientation=OrientationDefinition(upright=True, reversed=True),
            capabilities=("text",),
            temperament={
                "precision": TemperamentAxis(default=0.8, minimum=0.5, maximum=1.0)
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
                upright="Build carefully.", reversed="Check feasibility."
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
        self.installed = SimpleNamespace(package=package, path=Path("test-package"))

    def active(self, agent_id):
        return (
            self.installed
            if agent_id == self.installed.package.manifest.agent.id
            else None
        )

    def active_packages(self):
        return (self.installed,)

    def generation(self):
        return 1


def _service():
    seen_messages: list[str] = []

    def invoke(request, _route, _credential):
        seen_messages.append(request.message)
        return AdapterResponse("completed", input_tokens=10, output_tokens=2)

    router = MythosRouter(adapters={"mock": MockProviderAdapter(invoke)})
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
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({route.card_id}),
            requests_per_minute=10,
            maximum_budget_usd=0,
        )
    )
    service = OccultService(
        package_manager=_PackageManager(),
        router=router,
        token_authority=authority,
        runtime_policy=RuntimePolicy(),
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


@pytest.mark.asyncio
async def test_http_surface_requires_virtual_token_and_runs_real_operations(
    tmp_path: Path,
):
    service, token, seen = _service()
    readings = ReadingStore(tmp_path / "readings.db")

    def execute(request):
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
        assert (await events.json())["data"][-1]["event_type"] == ("reading.completed")
    assert len(seen) == 1


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

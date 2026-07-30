import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from agent.occult.readings import (
    CouncilNodeRequest,
    ReadingNode,
    ReadingPlan,
    RetryableReadingError,
)
from agent.occult.runtime import (
    STARTER_AGENT_IDS,
    STARTER_CARD_ID,
    STARTER_DECK_ID,
    OccultRuntimeError,
    build_occult_http,
    normalize_loopback_openai_url,
)
from agent.occult.virtual_tokens import (
    VirtualTokenError,
    VirtualTokenPolicy,
)


def _config(model: str = "qwen2.5:3b"):
    return {
        "occult": {
            "enabled": True,
            "contract_version": "1.0.0",
            "local_base_url": "http://127.0.0.1:11434/v1",
            "local_model": model,
            "provider_timeout_seconds": 30,
            "maximum_concurrent_requests": 2,
        }
    }


def test_disabled_runtime_has_no_side_effects(tmp_path: Path):
    assert build_occult_http({"occult": {"enabled": False}}, home=tmp_path) is None
    assert not (tmp_path / "occult").exists()


def test_runtime_rejects_incompatible_contract_before_side_effects(tmp_path: Path):
    config = _config()
    config["occult"]["contract_version"] = "2.0.0"

    with pytest.raises(OccultRuntimeError, match="contract_version is incompatible"):
        build_occult_http(config, home=tmp_path)

    assert not (tmp_path / "occult").exists()


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/v1",
        "http://127.0.0.1:11434/other",
        "http://user:password@127.0.0.1:11434/v1",
    ],
)
def test_local_provider_rejects_non_loopback_or_unsafe_urls(value: str):
    with pytest.raises(OccultRuntimeError):
        normalize_loopback_openai_url(value)


def test_runtime_installs_signed_starters_route_and_deck(tmp_path: Path):
    http = build_occult_http(
        _config(),
        environ={"OCCULT_ADMIN_KEY": "a" * 32},
        home=tmp_path,
    )
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="test-local",
            allowed_agent_ids=frozenset(STARTER_AGENT_IDS),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )

    assert {item["agent_id"] for item in http.service.agents(token)} == set(
        STARTER_AGENT_IDS
    )
    assert [item["card_id"] for item in http.service.routes(token)] == [STARTER_CARD_ID]
    assert [item["deck_id"] for item in http.service.decks(token)] == [STARTER_DECK_ID]
    assert http.service.validate_deck(token, STARTER_DECK_ID)["valid"] is True
    assert http.admin_key_digest is not None
    http.close()


def test_runtime_applies_configured_persistence_bounds(tmp_path: Path):
    config = _config()
    config["occult"].update({
        "invocation_result_retention_seconds": 60,
        "invocation_identity_retention_seconds": 120,
        "maximum_invocation_entries": 17,
        "reading_retention_seconds": 180,
        "maximum_readings": 19,
    })

    http = build_occult_http(config, home=tmp_path)
    assert http is not None

    assert http.service.invocation_store.retention_seconds == 60
    assert http.service.invocation_store.identity_retention_seconds == 120
    assert http.service.invocation_store.maximum_entries == 17
    assert http.readings.retention_seconds == 180
    assert http.readings.maximum_readings == 19
    http.close()


def test_bundled_starter_version_replaces_an_older_active_version(
    tmp_path: Path,
):
    from agent.occult import runtime

    active_versions = {agent_id: "0.9.0" for agent_id in STARTER_AGENT_IDS}
    activated: list[tuple[str, str]] = []
    validation_index = 0

    class Manager:
        packages_root = tmp_path

        def validate(self, _archive):
            nonlocal validation_index
            agent_id = STARTER_AGENT_IDS[validation_index]
            validation_index += 1
            version = "1.0.0"
            (self.packages_root / agent_id / version).mkdir(parents=True)
            return SimpleNamespace(
                manifest=SimpleNamespace(
                    agent=SimpleNamespace(id=agent_id, version=version)
                )
            )

        def load(self, agent_id, version):
            return SimpleNamespace(
                manifest=SimpleNamespace(
                    agent=SimpleNamespace(id=agent_id, version=version)
                )
            )

        def active(self, agent_id):
            version = active_versions.get(agent_id)
            if version is None:
                return None
            return SimpleNamespace(
                package=SimpleNamespace(
                    manifest=SimpleNamespace(
                        agent=SimpleNamespace(id=agent_id, version=version)
                    )
                )
            )

        def activate(self, agent_id, version):
            active_versions[agent_id] = version
            activated.append((agent_id, version))

        def active_packages(self):
            return tuple(self.active(agent_id) for agent_id in STARTER_AGENT_IDS)

    runtime._install_starter_agents(Manager())

    assert activated == [(agent_id, "1.0.0") for agent_id in STARTER_AGENT_IDS]


def test_runtime_restores_the_canonical_starter_deck(tmp_path: Path):
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    http.close()

    path = tmp_path / "occult" / "decks.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["decks"][STARTER_DECK_ID]["allowed_agent_ids"] = ["occult.major.magician"]
    payload["decks"][STARTER_DECK_ID]["routing"]["local_only"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    restarted = build_occult_http(_config(), home=tmp_path)
    assert restarted is not None
    restored = restarted.service.deck_registry.get(STARTER_DECK_ID)

    assert set(restored.allowed_agent_ids) == set(STARTER_AGENT_IDS)
    assert restored.allowed_card_ids == (STARTER_CARD_ID,)
    assert restored.routing.local_only is True
    restarted.close()


def test_openai_dispatch_recognizes_only_issued_occult_tokens(tmp_path: Path):
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(token_id="dispatch-local")
    )

    assert http.handles_openai_request(
        make_mocked_request(
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    assert not http.handles_openai_request(
        make_mocked_request(
            "GET",
            "/v1/models",
            headers={"Authorization": "Bearer occult_not-issued"},
        )
    )
    http.close()


def test_real_runtime_path_composes_agent_and_invokes_local_adapter(
    tmp_path: Path,
    monkeypatch,
):
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "assembled"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1},
            }).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr("agent.occult.runtime._open_local_url", fake_urlopen)
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="invoke-local",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    result = http.service.invoke(
        token,
        {
            "contract_version": "1.0.0",
            "invocation_id": "inv-runtime",
            "idempotency_key": "runtime-1",
            "agent_id": "occult.major.magician",
            "orientation": "upright",
            "input": {"message": "Build the test."},
            "required_capabilities": ["text"],
            "routing": {
                "mode": "local_only",
                "free_only": True,
                "local_only": True,
                "maximum_fallbacks": 0,
                "maximum_cost_usd": 0,
            },
            "deck_id": STARTER_DECK_ID,
            "spread_id": None,
            "metadata": {},
        },
    )

    assert result["output"] == "assembled"
    assert result["route"]["selected_card_id"] == STARTER_CARD_ID
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    message = captured["payload"]["messages"][0]["content"]
    assert "# Major Arcana" in message
    assert "The Magician" in message
    assert "# Task\nBuild the test." in message
    http.close()


def test_runtime_executes_reading_nodes_through_occult_service(
    tmp_path: Path,
    monkeypatch,
):
    responses = iter(("built", "audited"))
    captured: list[dict[str, object]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": next(responses)}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }).encode()

    def fake_open(request, *, timeout):
        captured.append(json.loads(request.data.decode()))
        return Response()

    monkeypatch.setattr("agent.occult.runtime._open_local_url", fake_open)
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="reading-local",
            allowed_agent_ids=frozenset({
                "occult.major.magician",
                "occult.major.justice",
            }),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    reading_id = http.readings.create(
        ReadingPlan(
            spread_id="occult.spread.runtime",
            nodes=(
                ReadingNode("build", "occult.major.magician", "Build."),
                ReadingNode(
                    "audit",
                    "occult.major.justice",
                    "Audit.",
                    depends_on=("build",),
                ),
            ),
        ),
        idempotency_key="runtime-reading",
        contract_version="1.0.0",
        owner_token_id="reading-local",
    )

    status = http._authorized_reading(token, reading_id, "resume")

    assert status["state"] == "completed"
    assert len(captured) == 2
    audit_message = captured[1]["messages"][0]["content"]
    assert "# Dependency artifact" in audit_message
    assert "built" in audit_message
    http.close()


def test_reading_access_is_bound_to_creating_virtual_token(tmp_path: Path):
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    owner = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="reading-owner",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
        )
    )
    other = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="reading-other",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
        )
    )
    reading_id = http.readings.create(
        ReadingPlan(
            spread_id="occult.spread.owner",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="owner-check",
        contract_version="1.0.0",
        owner_token_id="reading-owner",
    )

    assert http._authorized_reading(owner, reading_id, "status")["state"] == "pending"
    with pytest.raises(VirtualTokenError, match="does not allow requested reading"):
        http._authorized_reading(other, reading_id, "status")
    http.close()


def test_transient_provider_failure_keeps_runtime_reading_resumable(
    tmp_path: Path,
    monkeypatch,
):
    def timeout(_request, *, timeout):
        raise TimeoutError

    monkeypatch.setattr("agent.occult.runtime._open_local_url", timeout)
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="retryable-runtime",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    reading_id = http.readings.create(
        ReadingPlan(
            spread_id="occult.spread.retryable-runtime",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="retryable-runtime-reading",
        contract_version="1.0.0",
        owner_token_id="retryable-runtime",
    )

    with pytest.raises(RetryableReadingError):
        http._authorized_reading(token, reading_id, "resume")
    assert http.readings.status(reading_id)["state"] == "pending"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps({
                "choices": [{"message": {"content": "built"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode()

    monkeypatch.setattr(
        "agent.occult.runtime._open_local_url",
        lambda _request, *, timeout: Response(),
    )
    completed = http._authorized_reading(token, reading_id, "resume")

    assert completed["state"] == "completed"
    http.close()


def test_virtual_token_rate_limit_keeps_runtime_reading_resumable(
    tmp_path: Path,
):
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="rate-limited-runtime",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
            requests_per_minute=1,
        )
    )
    reading_id = http.readings.create(
        ReadingPlan(
            spread_id="occult.spread.rate-limited-runtime",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="rate-limited-runtime-reading",
        contract_version="1.0.0",
        owner_token_id="rate-limited-runtime",
    )

    lease = http.service.token_authority.reserve(
        token,
        agent_id="occult.major.magician",
        card_id=STARTER_CARD_ID,
    )
    lease.release()

    with pytest.raises(RetryableReadingError, match="temporarily rate limited"):
        http._authorized_reading(token, reading_id, "resume")
    assert http.readings.status(reading_id)["state"] == "pending"
    http.close()


def test_local_provider_requests_ignore_ambient_proxies(monkeypatch):
    captured: dict[str, object] = {}

    class Opener:
        def open(self, request, *, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return object()

    def fake_build_opener(handler):
        captured["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    from agent.occult.runtime import _open_local_url

    _open_local_url(
        urllib.request.Request("http://127.0.0.1:11434/v1/models"),
        timeout=1,
    )

    assert captured["proxies"] == {}
    assert captured["url"] == "http://127.0.0.1:11434/v1/models"


def test_runtime_close_releases_token_store_when_reading_close_fails(
    tmp_path: Path,
    monkeypatch,
):
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    store = http.service.token_authority.store
    assert store is not None
    original_readings_close = http.readings.close
    original_store_close = store.close
    store_closed = False

    def fail_readings_close():
        raise RuntimeError("reading close failed")

    def mark_store_closed():
        nonlocal store_closed
        store_closed = True

    monkeypatch.setattr(http.readings, "close", fail_readings_close)
    monkeypatch.setattr(store, "close", mark_store_closed)

    with pytest.raises(RuntimeError, match="reading close failed"):
        http.close()

    assert store_closed is True
    original_readings_close()
    original_store_close()


def test_runtime_persists_and_validates_invocation_idempotency(
    tmp_path: Path,
    monkeypatch,
):
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            nonlocal calls
            calls += 1
            return json.dumps({
                "choices": [{"message": {"content": f"result-{calls}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }).encode()

    monkeypatch.setattr(
        "agent.occult.runtime._open_local_url",
        lambda _request, *, timeout: Response(),
    )
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="idempotent-local",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    payload = {
        "contract_version": "1.0.0",
        "invocation_id": "inv-first",
        "idempotency_key": "persistent-request",
        "agent_id": "occult.major.magician",
        "orientation": "upright",
        "input": {"message": "Build once."},
        "required_capabilities": ["text"],
        "routing": {
            "mode": "local_only",
            "free_only": True,
            "local_only": True,
            "maximum_fallbacks": 0,
            "maximum_cost_usd": 0,
        },
        "deck_id": STARTER_DECK_ID,
        "spread_id": None,
        "metadata": {},
    }

    first = http.service.invoke(token, payload)
    retry = http.service.invoke(
        token,
        {**payload, "invocation_id": "inv-retry"},
    )

    assert first == retry
    assert calls == 1
    assert (
        http.service.token_authority.status("idempotent-local")["requests_in_window"]
        == 2
    )
    with pytest.raises(ValueError, match="different input"):
        http.service.invoke(
            token,
            {
                **payload,
                "invocation_id": "inv-conflict",
                "input": {"message": "Different task."},
            },
        )
    http.close()

    restarted = build_occult_http(_config(), home=tmp_path)
    assert restarted is not None
    assert restarted.service.invoke(token, payload) == first
    assert calls == 1
    restarted.close()


def test_reading_executor_reuses_durable_idempotent_result_after_restart(
    tmp_path: Path,
    monkeypatch,
):
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "durable"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode()

    def fake_open(_request, *, timeout):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("agent.occult.runtime._open_local_url", fake_open)
    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    token = http.service.token_authority.issue(
        VirtualTokenPolicy(
            token_id="durable-reading",
            allowed_agent_ids=frozenset({"occult.major.magician"}),
            allowed_card_ids=frozenset({STARTER_CARD_ID}),
        )
    )
    reading_id = http.readings.create(
        ReadingPlan(
            spread_id="occult.spread.durable",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="durable-reading",
        contract_version="1.0.0",
        owner_token_id="durable-reading",
    )
    request = CouncilNodeRequest(
        contract_version="1.0.0",
        reading_id=reading_id,
        node_id="build",
        agent_id="occult.major.magician",
        task="Build.",
        idempotency_key=f"{reading_id}:build",
        input_artifact_references=(),
    )
    executor = http.reading_executor
    assert executor is not None
    http.readings.run(
        reading_id,
        lambda node_request: executor(token, node_request),
        maximum_nodes=1,
    )
    first = http.readings.cached_node_result(request.idempotency_key)
    assert first is not None
    http.close()

    http = build_occult_http(_config(), home=tmp_path)
    assert http is not None
    executor = http.reading_executor
    assert executor is not None
    second = executor(token, request)

    assert second == first
    assert calls == 1
    http.close()

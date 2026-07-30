from pathlib import Path
import sqlite3
from threading import Event, Thread

import pytest

from agent.occult.contracts import (
    OCCULT_CONTRACT_VERSION,
    validate_event_stream,
)
from agent.occult.readings import (
    CouncilNodeResult,
    ReadingError,
    ReadingNode,
    ReadingPlan,
    ReadingStore,
)


def _plan() -> ReadingPlan:
    return ReadingPlan(
        spread_id="occult.spread.build-review-synthesis",
        nodes=(
            ReadingNode("build", "occult.major.magician", "Build the artifact."),
            ReadingNode(
                "review",
                "occult.major.justice",
                "Review the build.",
                ("build",),
            ),
            ReadingNode(
                "synthesis",
                "occult.major.temperance",
                "Synthesize the result.",
                ("build", "review"),
            ),
        ),
    )


def test_idempotent_create_and_contract_mismatch_precede_execution(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    first = store.create(
        _plan(),
        idempotency_key="same-request",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    second = store.create(
        _plan(),
        idempotency_key="same-request",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    assert first == second

    with pytest.raises(ReadingError, match="version mismatch"):
        store.create(
            _plan(),
            idempotency_key="wrong-version",
            contract_version="999.0.0",
        )


def test_idempotency_and_ownership_are_scoped_per_virtual_token(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")

    first = store.create(
        _plan(),
        idempotency_key="shared-client-key",
        contract_version=OCCULT_CONTRACT_VERSION,
        owner_token_id="client-a",
    )
    second = store.create(
        _plan(),
        idempotency_key="shared-client-key",
        contract_version=OCCULT_CONTRACT_VERSION,
        owner_token_id="client-b",
    )

    assert first != second
    assert store.owner_token_id(first) == "client-a"
    assert store.owner_token_id(second) == "client-b"


def test_three_node_reading_resumes_without_reexecuting_completed_node(
    tmp_path: Path,
):
    path = tmp_path / "readings.db"
    calls: list[str] = []

    def execute(request):
        calls.append(request.node_id)
        assert request.contract_version == OCCULT_CONTRACT_VERSION
        assert request.idempotency_key.endswith(f":{request.node_id}")
        if request.node_id == "build":
            assert request.input_artifact_references == ()
        elif request.node_id == "review":
            assert len(request.input_artifact_references) == 1
        else:
            assert len(request.input_artifact_references) == 2
        return CouncilNodeResult(
            artifact={"result": request.node_id},
            route_summary={
                "card_id": "minor.swords.king.test",
                "provider_id": "test-provider",
                "model_id": "test-model",
            },
        )

    first_store = ReadingStore(path)
    reading_id = first_store.create(
        _plan(),
        idempotency_key="restart-safe",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    partial = first_store.run(reading_id, execute, maximum_nodes=1)
    assert partial["state"] == "running"
    assert calls == ["build"]
    first_store.close()

    restarted = ReadingStore(path)
    completed = restarted.resume(reading_id, execute)
    assert completed["state"] == "completed"
    assert calls == ["build", "review", "synthesis"]
    assert [node["state"] for node in completed["nodes"]] == [
        "completed",
        "completed",
        "completed",
    ]

    events = restarted.events(reading_id)
    validate_event_stream(events)
    assert restarted.events(reading_id, after_sequence=events[-2]["sequence"]) == (
        events[-1],
    )
    terminal = [
        event
        for event in events
        if event["event_type"].startswith("reading.")
        and event["event_type"]
        in {
            "reading.cancelled",
            "reading.completed",
            "reading.failed",
        }
    ]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "reading.completed"

    restarted.resume(reading_id, execute)
    assert calls == ["build", "review", "synthesis"]
    assert len(restarted.events(reading_id)) == len(events)


def test_event_cursor_rejects_negative_sequence(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        _plan(),
        idempotency_key="event-cursor",
        contract_version=OCCULT_CONTRACT_VERSION,
    )

    with pytest.raises(ReadingError, match="cannot be negative"):
        store.events(reading_id, after_sequence=-1)


def test_cancellation_emits_one_terminal_event(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        _plan(),
        idempotency_key="cancel-once",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    store.cancel(reading_id)
    store.cancel(reading_id)

    events = store.events(reading_id)
    validate_event_stream(events)
    assert events[-1]["event_type"] == "reading.cancelled"


def test_cancellation_during_execution_discards_late_result(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        _plan(),
        idempotency_key="cancel-in-flight",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    started = Event()
    release = Event()
    result: dict[str, object] = {}

    def execute(_request):
        started.set()
        assert release.wait(timeout=5)
        return CouncilNodeResult(
            artifact={"result": "late"},
            route_summary={"provider_id": "test-provider"},
        )

    thread = Thread(
        target=lambda: result.update(store.run(reading_id, execute)),
        daemon=True,
    )
    thread.start()
    assert started.wait(timeout=5)
    store.cancel(reading_id)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["state"] == "cancelled"
    events = store.events(reading_id)
    validate_event_stream(events)
    assert events[-1]["event_type"] == "reading.cancelled"
    assert all(event["event_type"] != "node.completed" for event in events)


def test_council_boundary_rejects_secret_shaped_results(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        _plan(),
        idempotency_key="secret-rejected",
        contract_version=OCCULT_CONTRACT_VERSION,
    )

    def unsafe(_request):
        return CouncilNodeResult(
            artifact={"api_key": "must-not-cross"},
            route_summary={"provider_id": "test-provider"},
        )

    with pytest.raises(ReadingError, match="execution failed"):
        store.run(reading_id, unsafe)
    events = store.events(reading_id)
    validate_event_stream(events)
    assert events[-1]["event_type"] == "reading.failed"
    assert "must-not-cross" not in repr(events)


def test_plan_rejects_cycles():
    with pytest.raises(ValueError, match="cycle"):
        ReadingPlan(
            spread_id="occult.spread.invalid",
            nodes=(
                ReadingNode("a", "occult.major.magician", "A", ("b",)),
                ReadingNode("b", "occult.major.justice", "B", ("a",)),
            ),
        )


def test_concurrent_resume_executes_each_node_once(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        ReadingPlan(
            spread_id="occult.spread.concurrent",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="concurrent-resume",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    started = Event()
    release = Event()
    calls: list[str] = []

    def execute(request):
        calls.append(request.node_id)
        started.set()
        assert release.wait(timeout=5)
        return CouncilNodeResult(
            artifact={"content": "built"},
            route_summary={"card_id": "minor.pentacles.ace.local"},
        )

    threads = [
        Thread(target=store.resume, args=(reading_id, execute))
        for _ in range(2)
    ]
    threads[0].start()
    assert started.wait(timeout=5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert calls == ["build"]
    assert store.status(reading_id)["state"] == "completed"


def test_cancelled_reading_does_not_cache_late_node_result(tmp_path: Path):
    store = ReadingStore(tmp_path / "readings.db")
    reading_id = store.create(
        ReadingPlan(
            spread_id="occult.spread.cancelled-cache",
            nodes=(ReadingNode("build", "occult.major.magician", "Build."),),
        ),
        idempotency_key="cancelled-cache",
        contract_version=OCCULT_CONTRACT_VERSION,
    )
    store.cancel(reading_id)
    key = f"{reading_id}:build"

    cached = store.cache_node_result_if_active(
        reading_id,
        key,
        CouncilNodeResult(
            artifact={"content": "late"},
            route_summary={"card_id": "minor.pentacles.ace.local"},
        ),
    )

    assert cached is False
    assert store.cached_node_result(key) is None


def test_legacy_reading_can_be_claimed_by_first_authorized_token(tmp_path: Path):
    path = tmp_path / "readings.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE readings (
            reading_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            contract_version TEXT NOT NULL,
            spread_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO readings VALUES (
            'reading_legacy', 'legacy-key', '1.0.0',
            'occult.spread.legacy', 'pending', 1, 1
        );
        """
    )
    connection.close()

    store = ReadingStore(path)

    assert store.owner_token_id("reading_legacy") == "legacy-unclaimed"
    retried = store.create(
        _plan(),
        idempotency_key="legacy-key",
        contract_version=OCCULT_CONTRACT_VERSION,
        owner_token_id="client-a",
    )
    assert retried == "reading_legacy"
    assert store.claim_legacy_owner("reading_legacy", "client-b") == "client-a"
    other_owner = store.create(
        _plan(),
        idempotency_key="legacy-key",
        contract_version=OCCULT_CONTRACT_VERSION,
        owner_token_id="client-b",
    )
    assert other_owner != "reading_legacy"

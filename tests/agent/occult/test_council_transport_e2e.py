import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from aiohttp.web import Application

from agent.occult.contracts import load_contract_fixture
from agent.occult.http import OccultHTTPAdapter
from agent.occult.readings import ReadingStore
from tests.agent.occult.test_service import _service


def _council_root() -> Path:
    configured = os.environ.get("AGENTS_COUNCIL_ROOT", "").strip()
    if not configured:
        pytest.skip("AGENTS_COUNCIL_ROOT is required for the cross-repository gate")
    root = Path(configured).resolve()
    required = (
        root / "package.json",
        root / "scripts" / "occultHermesE2E.ts",
        root / "src" / "core" / "occult" / "spec" / "v1" / "fixtures",
    )
    if not all(path.exists() for path in required):
        pytest.fail("AGENTS_COUNCIL_ROOT is not a compatible Council checkout")
    return root


def _bun_executable(council_root: Path) -> str:
    configured = os.environ.get("BUN_EXE", "").strip()
    candidates = (
        configured,
        shutil.which("bun"),
        str(council_root / "node_modules" / "electrobun" / "dist-win-x64" / "bun.exe"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("Bun is required for the Agents Council cross-repository gate")


def _assert_shared_fixtures(council_root: Path) -> None:
    fixture_root = council_root / "src" / "core" / "occult" / "spec" / "v1" / "fixtures"
    for name in ("invocation.valid.json", "events.valid.json"):
        council_fixture = json.loads((fixture_root / name).read_text(encoding="utf-8"))
        assert council_fixture == load_contract_fixture(name)


@pytest.mark.asyncio
async def test_agents_council_calls_live_hermes_and_resumes_after_restart(
    tmp_path: Path,
):
    council_root = _council_root()
    _assert_shared_fixtures(council_root)
    bun = _bun_executable(council_root)
    agent_ids = (
        "occult.major.magician",
        "occult.major.justice",
        "occult.major.temperance",
    )
    service, token, seen_messages = _service(agent_ids)
    app = Application()
    OccultHTTPAdapter(
        service,
        ReadingStore(tmp_path / "hermes-readings.db"),
    ).register(app)

    server = TestServer(app)
    await server.start_server()
    try:
        environment = {
            **os.environ,
            "NO_COLOR": "1",
            "OCCULT_E2E_HERMES_URL": str(server.make_url("/")),
            "OCCULT_E2E_HERMES_TOKEN": token,
        }
        result = await asyncio.to_thread(
            subprocess.run,
            [bun, "run", "test:occult-hermes-e2e"],
            cwd=council_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        await server.close()

    safe_output = (result.stdout + result.stderr).replace(token, "[REDACTED]")
    assert result.returncode == 0, safe_output
    report = json.loads(
        next(
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith("{")
        )
    )
    assert report == {
        "contract_version": "1.0.0",
        "reading_id": report["reading_id"],
        "state": "completed",
        "node_attempts": {"build": 1, "review": 1, "synthesis": 1},
        "terminal_event": "reading.completed",
    }
    assert len(seen_messages) == 3
    for task in (
        "Build the production artifact.",
        "Review the production artifact.",
        "Synthesize the approved result.",
    ):
        assert sum(task in message for message in seen_messages) == 1
    assert token not in safe_output

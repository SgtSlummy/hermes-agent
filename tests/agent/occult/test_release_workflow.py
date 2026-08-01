import re
from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "occult-production-gate.yml"
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_occult_production_workflow_is_valid_yaml_and_sha_pinned():
    payload = yaml.load(_workflow_text(), Loader=yaml.BaseLoader)

    assert isinstance(payload, dict)
    assert set(payload["jobs"]) == {"platform", "council", "nix", "stage", "promote"}
    for line in _workflow_text().splitlines():
        if "uses:" in line and "./.github/actions/" not in line:
            assert "@" in line
            assert len(line.rsplit("@", 1)[1].split()[0]) == 40


def test_occult_production_workflow_preserves_release_invariants():
    text = _workflow_text()
    payload = yaml.load(text, Loader=yaml.BaseLoader)
    promote_job = payload["jobs"]["promote"]
    promote_condition = " ".join(promote_job["if"].split())
    promote_runs = "\n".join(
        step["run"] for step in promote_job["steps"] if "run" in step
    )

    for expected in (
        "ubuntu-latest, macos-latest, windows-latest",
        "uv sync --frozen --extra dev --extra occult",
        "bun install --frozen-lockfile",
        "test_council_transport_e2e.py",
        "tests/hermes_cli/test_backup.py",
        "nix flake check",
        "npm run build --prefix web",
        "(cd web && npm audit --omit=dev --audit-level=critical)",
        "(cd ui-tui && npm audit --omit=dev --audit-level=critical)",
        "(cd website && npm audit --omit=dev --audit-level=critical)",
        "npm run build --prefix ui-tui",
        "uv run --frozen npm run prebuild --prefix website",
        "npm run build --prefix website",
        "docker buildx build --output type=oci",
        "scripts/occult_release.py assemble",
        "scripts/occult_release.py verify staged",
        "sigstore/gh-action-sigstore-python@",
        "environment: occult-production",
        "scripts/occult_release.py promote staged promoted",
        "scripts/install-occult.ps1",
        "scripts/install-occult.sh",
        "scripts/occult-install-manifest.json",
        "scripts/occult-sigstore-requirements.lock",
        "OCCULT-INSTALL-SHA256SUMS.txt",
        "canary_report_sha256",
        '.candidate_status == "passed"',
        '.overall_status == "candidate_passed"',
        ".promotion_eligible == false",
        ".checks.installer_source_contract",
        ".release.ollama_model == $model",
        'test "$COUNCIL_REF" = "$EXPECTED_COUNCIL_REF"',
        "git -C council rev-parse HEAD",
        "uv export",
        'SOURCE_DATE_EPOCH: "0"',
        "--no-header",
        "--format requirements-txt",
        "occult-requirements.lock",
        "verify_canary_artifact hermes_wheel",
        "council_windows_x64",
        "gh release download",
        "--latest=false",
        "gateway/platforms/api_server.py",
        "tests/gateway/test_occult_runtime_wiring.py",
        "--sort=name",
        "gzip -n",
    ):
        assert expected in text
    assert "github.ref == 'refs/heads/main'" in promote_condition
    assert re.search(
        r'(?m)^\s*VERSION\s*=\s*["\']?\$\{RELEASE_VERSION#v\}["\']?\s*$',
        promote_runs,
    )
    assert re.search(
        r'(?m)^\s*CANARY\s*=\s*["\']?'
        r"docs/occult/evidence/launch-canary-v\$\{VERSION\}\.json"
        r'["\']?\s*$',
        promote_runs,
    )
    stage = text.split("  stage:", 1)[1].split("\n  promote:", 1)[0]
    assert "include-hidden-files: true" in stage
    promote = text.split("  promote:", 1)[1]
    assert "uv sync --frozen --extra occult" in promote
    assert "npm run build" not in promote
    assert "uv build" not in promote
    assert "docker build" not in promote

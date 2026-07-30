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

    for expected in (
        "ubuntu-latest, macos-latest, windows-latest",
        "uv sync --frozen --extra dev --extra homeassistant",
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
    ):
        assert expected in text
    promote = text.split("  promote:", 1)[1]
    assert "npm run build" not in promote
    assert "uv build" not in promote
    assert "docker build" not in promote

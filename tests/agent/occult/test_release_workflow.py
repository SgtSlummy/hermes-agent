import json
import re
from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "occult-production-gate.yml"
)
PROMOTION_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "tarot-router-promote.yml"
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
        ".checks.installer_interface",
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


def test_public_canary_promotion_workflow_verifies_published_bytes_before_latest():
    text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")
    payload = yaml.load(text, Loader=yaml.BaseLoader)

    inputs = payload["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "version",
        "release_commit",
        "council_ref",
        "public_canary_report_sha256",
    }
    assert all(value["required"] == "true" for value in inputs.values())
    assert all(value["type"] == "string" for value in inputs.values())
    assert payload["permissions"] == {"contents": "read"}
    assert set(payload["jobs"]) == {
        "verify-release",
        "verify-posix",
        "promote-latest",
    }
    jobs = payload["jobs"]
    for job in jobs.values():
        assert "github.ref == 'refs/heads/main'" in job["if"]
        for step in job["steps"]:
            action = step.get("uses")
            if action and not action.startswith("./.github/actions/"):
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action)

    release = jobs["verify-release"]
    assert release["runs-on"] == "ubuntu-latest"
    assert "environment" not in release
    assert release["permissions"] == {"contents": "read"}
    release_steps = release["steps"]
    release_named = {
        step["name"]: step for step in release_steps if "name" in step
    }
    release_order = [
        "Download the already-published release bytes",
        "Verify Sigstore identities and signed checksum manifests offline",
        "Verify the signed release bundle contents",
    ]
    actual_release_order = [step.get("name") for step in release_steps]
    assert [actual_release_order.index(name) for name in release_order] == sorted(
        actual_release_order.index(name) for name in release_order
    )

    download = release_named[release_order[0]]
    assert download["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "VERSION_INPUT": "${{ inputs.version }}",
        "RELEASE_COMMIT_INPUT": "${{ inputs.release_commit }}",
        "COUNCIL_REF": "${{ inputs.council_ref }}",
        "REPORT_SHA256": "${{ inputs.public_canary_report_sha256 }}",
    }
    download_run = download["run"]
    assert re.search(
        r'^\[\[ "\$RELEASE_COMMIT" =~ \^\[0-9a-f\]\{40\}\$ \]\]$',
        download_run,
        re.MULTILINE,
    )
    assert download_run.index('git checkout --detach "$RELEASE_COMMIT"') < download_run.index(
        'gh release download "$TAG"'
    )
    assert download_run.index("EXPECTED_COUNCIL_COMMIT=") < download_run.index(
        'gh release download "$TAG"'
    )
    assert re.search(r"and \.overall_status == \"passed\"", download_run)
    assert re.search(r"and \.promotion_eligible == true", download_run)
    assert re.search(r"and \.contains_secrets == false", download_run)
    for binding in (
        '.schema_version == "1.0.0"',
        '.scope == "public Windows x64 launch canary"',
        '.platform == {"os":"Windows","architecture":"x86_64"}',
        ".release.hermes_cli == $hermes_cli",
        ".release.council_commit == $council_commit",
        ".release.runtime_contract == $contract",
        ".release.council_state_schema == $council_state",
        ".release.ollama_model == $model",
    ):
        assert binding in download_run
    check_keys_match = re.search(
        r"EXPECTED_CHECK_KEYS='([^']+)'", download_run
    )
    assert check_keys_match is not None
    assert set(json.loads(check_keys_match.group(1))) == {
        "audit_redaction",
        "backup_restore",
        "candidate_artifact_installation",
        "council_pause_restart_resume",
        "council_source_provenance",
        "default_model_binding",
        "disabled_before_initialization",
        "explicit_local_initialization",
        "gateway_restart",
        "installer_idempotent_rerun",
        "installer_interface",
        "installer_tamper_repair",
        "packaged_council_mcp_flow",
        "rollback_previous_checksummed_releases",
        "temporary_secret_cleanup",
        "versions",
        "zero_cost_major_arcana_invocation",
    }
    assert "with_entries(.value = .value.name)" in download_run
    assert ".assets[] | [.name, .digest]" in download_run

    sigstore_run = release_named[release_order[1]]["run"]
    assert sigstore_run.count("verify_identity \\") == 4
    assert "--offline" in sigstore_run
    assert "OCCULT-INSTALL-SHA256SUMS.txt" in sigstore_run
    assert "RELEASE-SHA256SUMS.txt" in sigstore_run

    bundle_run = release_named[release_order[2]]["run"]
    assert bundle_run.index("tar -tzf") < bundle_run.index("tar -xzf")
    assert bundle_run.index("tar -xzf") < bundle_run.index(
        "sha256sum -c SHA256SUMS.txt"
    )

    posix_job = jobs["verify-posix"]
    assert posix_job["needs"] == "verify-release"
    assert posix_job["permissions"] == {"contents": "read"}
    assert posix_job["timeout-minutes"] == "90"
    assert "matrix.runner" in posix_job["runs-on"]
    assert posix_job["strategy"]["fail-fast"] == "false"
    assert posix_job["strategy"]["matrix"]["include"] == [
        {
            "runner": "ubuntu-24.04",
            "platform": "linux",
            "architecture": "x64",
            "system": "Linux",
            "machine": "x86_64",
        },
        {
            "runner": "ubuntu-24.04-arm",
            "platform": "linux",
            "architecture": "arm64",
            "system": "Linux",
            "machine": "aarch64",
        },
        {
            "runner": "macos-15-intel",
            "platform": "darwin",
            "architecture": "x64",
            "system": "Darwin",
            "machine": "x86_64",
        },
        {
            "runner": "macos-15",
            "platform": "darwin",
            "architecture": "arm64",
            "system": "Darwin",
            "machine": "arm64",
        },
    ]
    posix_named = {
        step["name"]: step for step in posix_job["steps"] if "name" in step
    }
    authenticate = posix_named["Authenticate the exact POSIX installer"]
    authenticate_run = authenticate["run"]
    assert authenticate["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "VERSION_INPUT": "${{ inputs.version }}",
        "RELEASE_COMMIT_INPUT": "${{ inputs.release_commit }}",
        "EXPECTED_SYSTEM": "${{ matrix.system }}",
        "EXPECTED_MACHINE": "${{ matrix.machine }}",
    }
    assert authenticate_run.index('test "$(git rev-parse HEAD)"') < (
        authenticate_run.index('gh release download "$TAG"')
    )
    assert authenticate_run.index("sigstore\" verify identity") < (
        authenticate_run.index("expected=\"$(awk")
    )
    assert "--offline" in authenticate_run

    posix = posix_named[
        "Exercise signed POSIX installer reuse, repair, and skip transition"
    ]
    assert posix["env"] == {
        "HOME": "${{ runner.temp }}/tarot-posix-home",
        "XDG_BIN_HOME": "${{ runner.temp }}/tarot-posix-bin",
        "HERMES_HOME": "${{ runner.temp }}/tarot-posix-hermes-home",
    }
    posix_run = posix["run"]
    installer_calls = [
        match.start()
        for match in re.finditer(r"^run_installer$", posix_run, re.MULTILINE)
    ]
    assert len(installer_calls) == 4
    skip_calls = [
        match.start()
        for match in re.finditer(r"^run_skip_installer$", posix_run, re.MULTILINE)
    ]
    assert len(skip_calls) == 2
    tamper = posix_run.index("tampered by protected POSIX canary")
    repair_assertion = posix_run.index("repaired_hermes_environment")
    assert installer_calls[1] < tamper < installer_calls[2] < repair_assertion
    assert repair_assertion < installer_calls[3]
    assert installer_calls[3] < skip_calls[0] < skip_calls[1]
    assert posix_run.count(".occult_initialized == false") == 3
    assert "first_receipt_mtime" in posix_run
    assert "repaired_receipt_mtime" in posix_run
    assert ".council_release == null" in posix_run
    assert 'test ! -L "$XDG_BIN_HOME/council"' in posix_run

    promote = jobs["promote-latest"]
    assert promote["needs"] == ["verify-release", "verify-posix"]
    assert promote["runs-on"] == "ubuntu-latest"
    assert promote["environment"] == "occult-production"
    assert promote["permissions"] == {"contents": "write"}
    assert len(promote["steps"]) == 4
    promote_named = {
        step["name"]: step for step in promote["steps"] if "name" in step
    }
    promote_step = promote_named[
        "Revalidate current release bytes and promote Hermes"
    ]
    assert promote_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "VERSION_INPUT": "${{ inputs.version }}",
        "RELEASE_COMMIT": "${{ inputs.release_commit }}",
        "COUNCIL_REF": "${{ inputs.council_ref }}",
        "REPORT_SHA256": "${{ inputs.public_canary_report_sha256 }}",
    }
    promote_run = promote_step["run"]
    assert 'gh release download "$TAG"' in promote_run
    assert ".assets[] | [.name, .digest]" in promote_run
    assert 'echo "$REPORT_SHA256  $PUBLIC/$REPORT" | sha256sum -c -' in promote_run
    assert promote_run.count("verify_identity \\") == 4
    assert "--offline" in promote_run
    assert "RELEASE_ASSET_SNAPSHOT" in promote_run
    assert "CURRENT_RELEASE_SNAPSHOT" in promote_run
    assert "RELEASE_STATE_SNAPSHOT" in promote_run
    assert "CURRENT_RELEASE_STATE" in promote_run
    assert promote_run.count(
        "gh release view --repo SgtSlummy/agents-council"
    ) == 2
    for binding in (
        '.schema_version == "1.0.0"',
        '.scope == "public Windows x64 launch canary"',
        '.platform == {"os":"Windows","architecture":"x86_64"}',
        ".release.hermes_cli == $hermes_cli",
        ".release.council_commit == $council_commit",
        ".release.runtime_contract == $contract",
        ".release.council_state_schema == $council_state",
        ".release.ollama_model == $model",
    ):
        assert binding in promote_run
    assert promote_run.index('gh release download "$TAG"') < promote_run.index(
        'gh release edit "$TAG"'
    )
    assert promote_run.index("CURRENT_RELEASE_SNAPSHOT") < promote_run.index(
        'gh release edit "$TAG"'
    )
    assert promote_run.index(
        "gh release view --repo SgtSlummy/agents-council"
    ) < promote_run.index('gh release edit "$TAG"')
    assert "--latest" in promote_run

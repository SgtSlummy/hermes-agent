import hashlib
import json
import re
import tomllib
from pathlib import Path

from agent.occult.contracts import OCCULT_CONTRACT_VERSION


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "scripts" / "occult-install-manifest.json"
POWERSHELL = ROOT / "scripts" / "install-occult.ps1"
SHELL = ROOT / "scripts" / "install-occult.sh"
CANARY = ROOT / "scripts" / "run-occult-launch-canary.py"
CANARY_EVIDENCE = ROOT / "docs" / "occult" / "evidence" / "launch-canary-v1.0.6.json"
QUICKSTART = ROOT / "docs" / "tarot-router" / "quickstart.md"
LEGACY_QUICKSTART = ROOT / "docs" / "occult" / "quickstart.md"
README = ROOT / "README.md"
SIGSTORE_INPUT = ROOT / "scripts" / "occult-sigstore-requirements.in"
SIGSTORE_LOCK = ROOT / "scripts" / "occult-sigstore-requirements.lock"

REQUIRED_RECEIPT_FIELDS = {
    "schema_version",
    "occult_release_version",
    "hermes_cli_version",
    "hermes_wheel",
    "hermes_wheel_sha256",
    "hermes_requirements",
    "hermes_requirements_sha256",
    "sigstore_requirements",
    "sigstore_requirements_sha256",
    "hermes_environment",
    "install_manifest_sha256",
    "council_release",
    "council_archive_sha256",
    "council_environment",
    "contract_version",
    "council_state_schema",
    "occult_initialized",
    "occult_enabled",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_manifest_has_safe_cross_platform_release_metadata():
    manifest = json.loads(_text(MANIFEST))
    council = manifest["council"]
    rollback = manifest["rollback"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["schema_version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["occult_release_version"])
    assert re.fullmatch(r"v\d+\.\d+\.\d+", council["release_tag"])
    assert re.fullmatch(r"[0-9a-f]{40}", council["commit_sha"])
    assert council["contract_version"] == OCCULT_CONTRACT_VERSION
    assert isinstance(council["state_schema"], int) and council["state_schema"] > 0
    assert set(council["assets"]) == {
        "linux-x64",
        "linux-arm64",
        "macos-x64",
        "macos-arm64",
        "windows-x64",
    }
    assert all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", asset)
        for asset in council["assets"].values()
    )
    assert re.fullmatch(r"v\d+\.\d+\.\d+", rollback["hermes_release_tag"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", rollback["hermes_cli_version"])
    assert re.fullmatch(r"[0-9a-f]{64}", rollback["hermes_wheel_sha256"])
    assert re.fullmatch(r"v\d+\.\d+\.\d+", rollback["council_release_tag"])
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        rollback["council_windows_x64_sha256"],
    )
    assert manifest["hermes_requirements_asset"].endswith(".lock")
    assert manifest["sigstore_requirements_asset"].endswith(".lock")
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        manifest["sigstore_requirements_sha256"],
    )
    assert ":" in manifest["ollama_model"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["sigstore_python_version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["uv_version"])


def test_install_manifest_wheel_matches_python_package_metadata():
    manifest = json.loads(_text(MANIFEST))
    project = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]
    normalized_name = project["name"].replace("-", "_")

    assert manifest["hermes_cli_version"] == project["version"]
    assert manifest["hermes_wheel_asset"] == (
        f"{normalized_name}-{project['version']}-py3-none-any.whl"
    )


def test_sigstore_verifier_lock_pins_inventory_parser_and_matches_installers():
    manifest = json.loads(_text(MANIFEST))
    lock_hash = _sha256(SIGSTORE_LOCK)

    assert "packaging==25.0" in _text(SIGSTORE_INPUT)
    assert "packaging==25.0" in _text(SIGSTORE_LOCK)
    assert manifest["sigstore_requirements_sha256"] == lock_hash
    assert lock_hash in _text(POWERSHELL)
    assert lock_hash in _text(SHELL)


def test_windows_installer_verifies_before_writing_application_files():
    text = _text(POWERSHELL)

    for option in (
        "[string]$Version",
        "[string]$InstallRoot",
        "[switch]$InitializeLocal",
        "[switch]$SkipCouncil",
        "[switch]$VerifyOnly",
    ):
        assert option in text
    assert "sigstore verify identity" not in text
    assert '"verify", "identity"' in text
    assert '"--offline"' in text
    assert "Get-FileHash" in text
    assert "Invoke-WebRequest" in text
    assert "-TimeoutSec 60" in text
    assert "$attempt -le 3" in text
    assert "uv-x86_64-pc-windows-msvc.zip" in text
    assert "0a23463216d09c6a72ff80ef5dc5a795f07dc1575cb84d24596c2f124a441b7b" in text
    assert "astral.sh/uv" not in text
    assert "Assert-SafeTarArchive" in text
    assert "the running installer does not match" in text
    assert "refs/tags/$councilTag" in text
    assert "[StringSplitOptions]::RemoveEmptyEntries" in text
    assert '"--require-hashes"' in text
    assert '"--no-deps"' in text
    assert '"--no-index"' in text
    assert '"tool", "run"' not in text
    assert "New-SigstoreVerifier" in text
    assert "$SigstoreRequirementsSha256" in text
    assert "hermes_requirements_asset" in text
    assert "hermes-environments" in text
    assert '"--clear"' not in text
    assert '"hermes.new-$environmentId.exe"' in text
    assert '"council.new-" + [Guid]::NewGuid().ToString("N") + ".exe"' in text
    assert "hermes.exe.new-" not in text
    assert "council.exe.new-" not in text
    assert "function Get-OccultState" in text
    assert "function Test-SafeLeafName" in text
    assert "function Test-VersionToken" in text
    assert "function Test-HermesEnvironmentRecords" in text
    assert "*.dist-info/RECORD" in text
    assert "installed != expected_inventory" in text
    assert 'for bootstrap_name in ("_virtualenv.pth", "_virtualenv.py")' in text
    assert "-Requirements $requirementsPath" in text
    assert "-Wheel $wheelPath" in text
    assert '"inspect-occult-state-" + [Guid]::NewGuid().ToString("N")' in text
    assert "[System.IO.File]::WriteAllText" in text
    assert "& $Python $stateScriptPath" in text
    assert "& $venvPython -c $stateScript" not in text
    assert "$requiredReceiptProperties" in text
    assert "$missingReceiptProperties.Count -eq 0" in text
    receipt_fields_match = re.search(
        r"\$requiredReceiptProperties = @\((.*?)\n\s*\)",
        text,
        flags=re.DOTALL,
    )
    assert receipt_fields_match is not None
    assert set(re.findall(r'"([a-z0-9_]+)"', receipt_fields_match.group(1))) == (
        REQUIRED_RECEIPT_FIELDS
    )
    assert "Test-SafeLeafName $existingReceipt.hermes_environment" in text
    assert "Test-SafeLeafName `\n                $existingReceipt.council_environment" in text
    assert "$expectedCouncilEnvironmentMatches" in text
    assert "Verified existing Occult release v$normalizedVersion; no application files changed" in text
    assert text.index("$requiredReceiptProperties") < text.index(
        '$environmentId = "$normalizedVersion-"'
    )
    assert "Existing Occult initialization was preserved" in text
    assert "$existingReceipt.occult_initialized -is [System.Boolean]" in text
    assert "$existingReceipt.occult_enabled -is [System.Boolean]" in text
    assert "-Expected $expectedCouncilRelease.TrimStart(\"v\")" in text
    assert "occult_enabled = $enabled" in text
    assert text.index("Assert-SigstoreIdentity") < text.index(
        'Write-Step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert text.index("if ($VerifyOnly)") < text.index(
        'Write-Step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    reuse_initialize_block = text.index("if ($InitializeLocal)")
    reuse_initialize_call = text.index(
        '"occult", "init", "--model", $Model',
        reuse_initialize_block,
    )
    assert "$hermesExecutable" in text[
        reuse_initialize_block:reuse_initialize_call
    ]
    staged_install = text.index('$environmentId = "$normalizedVersion-"')
    initialize_block = text.index("if ($InitializeLocal)", staged_install)
    initialize_call = text.index(
        '"occult", "init", "--model", $Model',
        initialize_block,
    )
    activation = text.index('Write-Step "Activating the fully staged local commands"')
    assert "$hermesStagedExecutable" in text[initialize_block:initialize_call]
    assert initialize_call < activation
    assert "NousResearch/hermes-agent" not in text
    assert "agents-council@latest" not in text


def test_unix_installer_verifies_before_writing_application_files():
    text = _text(SHELL)

    for option in (
        "--version",
        "--install-root",
        "--initialize-local",
        "--skip-council",
        "--verify-only",
    ):
        assert option in text
    assert '"$sigstore_cmd" verify identity' in text
    assert "--offline" in text
    assert "verify_hash" in text
    assert "uv-x86_64-unknown-linux-gnu.tar.gz" in text
    assert "uv-aarch64-apple-darwin.tar.gz" in text
    assert "astral.sh/uv" not in text
    assert "assert_safe_tar_archive" in text
    assert "the running installer does not match" in text
    assert "refs/tags/$council_tag" in text
    assert "--require-hashes" in text
    assert "--no-deps" in text
    assert "--no-index" in text
    assert "tool run" not in text
    assert "sigstore_requirements_sha256" in text
    assert "hermes_requirements_asset" in text
    assert "hermes-environments" in text
    assert "--clear" not in text
    assert "hermes.new.$$" in text
    assert "existing_receipt_seen=0" in text
    assert "required.issubset(receipt)" in text
    receipt_fields_match = re.search(
        r"required = \{(.*?)\n\}",
        text,
        flags=re.DOTALL,
    )
    assert receipt_fields_match is not None
    assert set(re.findall(r'"([a-z0-9_]+)"', receipt_fields_match.group(1))) == (
        REQUIRED_RECEIPT_FIELDS
    )
    assert 're.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value)' in text
    assert 'type(receipt.get("occult_initialized")) is not bool' in text
    assert 'type(receipt.get("occult_enabled")) is not bool' in text
    assert "version_output_matches" in text
    assert "verify_environment_records" in text
    assert "installed != expected_inventory" in text
    assert 'for bootstrap_name in ("_virtualenv.pth", "_virtualenv.py")' in text
    assert '"$requirements_path"' in text
    assert '"$wheel_path"' in text
    assert "*.dist-info/RECORD" in text
    assert '[ "$(readlink "$user_bin/hermes")" = "$hermes_executable" ]' in text
    assert '[ "$existing_receipt_initialized" = "$initialized" ]' in text
    assert "Verified existing Occult release v$version; no application files changed" in text
    assert text.index("existing_receipt_seen=0") < text.index(
        'hermes_venv=$(mktemp -d "$hermes_environments/$version.XXXXXX")'
    )
    assert "Existing Occult initialization was preserved" in text
    assert '"occult_enabled": $enabled' in text
    assert "grep -Eq '^v[0-9]+\\.[0-9]+\\.[0-9]+$'" in text
    assert text.index("verify_sigstore") < text.index(
        'step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert text.index('if [ "$verify_only" -eq 1 ]') < text.index(
        'step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    reuse_initialize_block = text.index('if [ "$initialize_local" -eq 1 ]')
    reuse_initialize_call = text.index(
        '"$hermes_executable" occult init --model "$model"',
        reuse_initialize_block,
    )
    assert reuse_initialize_call > reuse_initialize_block
    staged_install = text.index(
        'hermes_venv=$(mktemp -d "$hermes_environments/$version.XXXXXX")'
    )
    initialize_block = text.index(
        'if [ "$initialize_local" -eq 1 ]', staged_install
    )
    initialize_call = text.index(
        '"$hermes_staged" occult init --model "$model"',
        initialize_block,
    )
    activation = text.index('step "Activating the fully staged local commands"')
    assert initialize_call < activation
    assert "NousResearch/hermes-agent" not in text
    assert "agents-council@latest" not in text


def test_quickstart_is_the_single_public_tarot_router_entrypoint():
    quickstart = _text(QUICKSTART)
    legacy_quickstart = _text(LEGACY_QUICKSTART)
    readme = _text(README)
    manifest = json.loads(_text(MANIFEST))
    release_version = manifest["occult_release_version"]

    assert "Tarot Router local public v1 quickstart" in quickstart
    assert f"releases/download/v{release_version}/install-occult.ps1" in quickstart
    assert f"releases/download/v{release_version}/install-occult.sh" in quickstart
    for topic in (
        "Initialize local Ollama explicitly",
        "First zero-cost Major Arcana invocation",
        "First Agents Council reading",
        "Disable Tarot Router",
        "Backup and restore",
        "Roll back to the previous checksummed releases",
    ):
        assert topic in quickstart
    assert "[Tarot Router local public v1 quickstart](docs/tarot-router/quickstart.md)" in readme
    assert "../tarot-router/quickstart.md" in legacy_quickstart
    assert "hermes tarot init" in quickstart
    assert "hermes occult init" not in quickstart
    assert "agents-council.com" not in quickstart
    assert "agents-council@latest" not in quickstart
    assert 'sh "${TMPDIR:-/tmp}/install-occult.sh" --initialize-local' in quickstart
    powershell_block = re.search(
        r"```powershell\n(?P<body>.*?install-occult\.ps1.*?)\n```",
        quickstart,
        flags=re.DOTALL,
    )
    assert powershell_block is not None
    assert _sha256(POWERSHELL) in powershell_block.group("body")
    posix_block = re.search(
        r"```bash\n(?P<body>.*?install-occult\.sh.*?)\n```",
        quickstart,
        flags=re.DOTALL,
    )
    assert posix_block is not None
    assert _sha256(SHELL) in posix_block.group("body")
    rollback = manifest["rollback"]
    assert f"Hermes Occult `{rollback['hermes_release_tag']}`" in quickstart
    assert f"Agents Council `{rollback['council_release_tag']}`" in quickstart


def test_launch_canary_is_redacted_and_covers_the_operator_flow():
    text = _text(CANARY)
    compile(text, str(CANARY), "exec")

    for check in (
        "disabled_before_initialization",
        "explicit_local_initialization",
        "zero_cost_major_arcana_invocation",
        "gateway_restart",
        "council_pause_restart_resume",
        "audit_redaction",
        "backup_restore",
        "rollback_previous_checksummed_releases",
        "temporary_secret_cleanup",
        "installer_source_contract",
        "installer_idempotent_rerun",
    ):
        assert check in text
    assert '"contains_secrets": False' in text
    assert "OCCULT_E2E_HERMES_TOKEN" in text
    assert "council_result.stdout + council_result.stderr" in text
    assert "--candidate-hermes-wheel" in text
    assert "--candidate-council-archive" in text
    assert "--install-manifest" in text
    assert "--sigstore-requirements-lock" in text
    assert "--public-installer-rerun" in text
    assert "def validate_public_installer_rerun" in text
    assert "first_receipt_mtime" in text
    assert text.count("installer_command,") >= 2
    assert "release_artifacts" in text
    assert "install_hermes_environment" in text
    assert "validate_council_repository" in text
    assert "assert_redaction_surfaces" in text
    assert 'root.glob("gateway-*.log")' in text
    assert 'primary_home / "occult"' in text
    assert 'primary_home / "logs"' in text
    assert 'restored_home / "logs"' in text
    assert '"ollama_model": model' in text
    assert "os.replace(staged, destination)" in text
    assert "previous-after-rollback" in text
    assert "candidate-restored" in text
    assert "TemporaryDirectory" in text
    assert "command output, prompts, tokens" in text.lower()


def test_launch_canary_evidence_is_redacted_and_includes_rollback():
    evidence = json.loads(_text(CANARY_EVIDENCE))
    serialized = json.dumps(evidence, sort_keys=True).lower()

    assert evidence["candidate_status"] == "passed"
    assert evidence["overall_status"] == "candidate_passed"
    assert evidence["promotion_eligible"] is False
    assert "installer_idempotent_rerun" not in evidence["checks"]
    assert evidence["contains_secrets"] is False
    assert evidence["checks"]["rollback_previous_checksummed_releases"] == "passed"
    assert evidence["checks"]["installer_source_contract"] == "passed"
    manifest = json.loads(_text(MANIFEST))
    assert evidence["release"]["hermes"] == (
        f"v{manifest['occult_release_version']}"
    )
    assert evidence["release"]["ollama_model"] == manifest["ollama_model"]
    assert evidence["release"]["council_commit"] == manifest["council"]["commit_sha"]
    assert set(evidence["release_artifacts"]) == {
        "install_manifest",
        "hermes_wheel",
        "hermes_requirements_lock",
        "sigstore_requirements_lock",
        "installer_powershell",
        "installer_posix",
        "council_windows_x64",
    }
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        for artifact in evidence["release_artifacts"].values()
    )
    for forbidden in (
        "authorization",
        "api_key",
        "occult_api_key",
        "signed_url",
        "bearer ",
    ):
        assert forbidden not in serialized

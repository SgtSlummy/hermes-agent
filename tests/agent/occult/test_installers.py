import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "scripts" / "occult-install-manifest.json"
POWERSHELL = ROOT / "scripts" / "install-occult.ps1"
SHELL = ROOT / "scripts" / "install-occult.sh"
CANARY = ROOT / "scripts" / "run-occult-launch-canary.py"
QUICKSTART = ROOT / "docs" / "occult" / "quickstart.md"
README = ROOT / "README.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_install_manifest_pins_the_reviewed_patch_release():
    manifest = json.loads(_text(MANIFEST))

    assert manifest["schema_version"] == "1.0.0"
    assert manifest["occult_release_version"] == "1.0.1"
    assert manifest["council"] == {
        "release_tag": "v0.5.2",
        "commit_sha": "453676402fb3b3183aca6eccf64067ac4e86a4de",
        "contract_version": "1.0.0",
        "state_schema": 3,
        "assets": {
            "linux-x64": "agents-council-linux-x64.tar.gz",
            "linux-arm64": "agents-council-linux-arm64.tar.gz",
            "macos-x64": "agents-council-darwin-x64.tar.gz",
            "macos-arm64": "agents-council-darwin-arm64.tar.gz",
            "windows-x64": "agents-council-windows-x64.tar.gz",
        },
    }
    assert manifest["ollama_model"] == "qwen2.5:3b"
    assert manifest["sigstore_python_version"] == "4.5.0"
    assert manifest["uv_version"] == "0.11.28"


def test_install_manifest_wheel_matches_python_package_metadata():
    manifest = json.loads(_text(MANIFEST))
    project = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]
    normalized_name = project["name"].replace("-", "_")

    assert manifest["hermes_cli_version"] == project["version"]
    assert manifest["hermes_wheel_asset"] == (
        f"{normalized_name}-{project['version']}-py3-none-any.whl"
    )


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
    assert '"sigstore", "verify", "identity"' in text
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
    assert text.index("Assert-SigstoreIdentity") < text.index(
        'Write-Step "Installing the verified Hermes wheel per-user"'
    )
    assert text.index("if ($VerifyOnly)") < text.index(
        'Write-Step "Installing the verified Hermes wheel per-user"'
    )
    initialize_block = text.index("if ($InitializeLocal)")
    assert initialize_block < text.index(
        '"occult", "init", "--model", $Model', initialize_block
    )
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
    assert "sigstore verify identity" in text
    assert "--offline" in text
    assert "verify_hash" in text
    assert "uv-x86_64-unknown-linux-gnu.tar.gz" in text
    assert "uv-aarch64-apple-darwin.tar.gz" in text
    assert "astral.sh/uv" not in text
    assert "assert_safe_tar_archive" in text
    assert "the running installer does not match" in text
    assert "refs/tags/$council_tag" in text
    assert text.index("verify_sigstore") < text.index(
        'step "Installing the verified Hermes wheel per-user"'
    )
    assert text.index('if [ "$verify_only" -eq 1 ]') < text.index(
        'step "Installing the verified Hermes wheel per-user"'
    )
    initialize_block = text.index('if [ "$initialize_local" -eq 1 ]')
    assert initialize_block < text.index(
        '"$hermes_executable" occult init --model "$model"', initialize_block
    )
    assert "NousResearch/hermes-agent" not in text
    assert "agents-council@latest" not in text


def test_quickstart_is_the_single_public_occult_entrypoint():
    quickstart = _text(QUICKSTART)
    readme = _text(README)

    assert "Occult local public v1 quickstart" in quickstart
    assert "releases/download/v1.0.1/install-occult.ps1" in quickstart
    assert "releases/download/v1.0.1/install-occult.sh" in quickstart
    for topic in (
        "Initialize local Ollama explicitly",
        "First zero-cost Major Arcana invocation",
        "First Agents Council reading",
        "Disable Occult",
        "Backup and restore",
        "Roll back to the previous checksummed releases",
    ):
        assert topic in quickstart
    assert "[Occult local public v1 quickstart](docs/occult/quickstart.md)" in readme
    assert "agents-council.com" not in quickstart
    assert "agents-council@latest" not in quickstart


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
        "temporary_secret_cleanup",
    ):
        assert check in text
    assert '"contains_secrets": False' in text
    assert "OCCULT_E2E_HERMES_TOKEN" in text
    assert "council_result.stdout + council_result.stderr" in text
    assert "TemporaryDirectory" in text
    assert "command output, prompts, tokens" in text.lower()

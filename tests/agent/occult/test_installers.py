import base64
import csv
import hashlib
import importlib.util
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from agent.occult.contracts import OCCULT_CONTRACT_VERSION


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "scripts" / "occult-install-manifest.json"
POWERSHELL = ROOT / "scripts" / "install-occult.ps1"
SHELL = ROOT / "scripts" / "install-occult.sh"
CANARY = ROOT / "scripts" / "run-occult-launch-canary.py"
QUICKSTART = ROOT / "docs" / "tarot-router" / "quickstart.md"
LEGACY_QUICKSTART = ROOT / "docs" / "occult" / "quickstart.md"
README = ROOT / "README.md"
SIGSTORE_INPUT = ROOT / "scripts" / "occult-sigstore-requirements.in"
SIGSTORE_LOCK = ROOT / "scripts" / "occult-sigstore-requirements.lock"
ENVIRONMENT_VERIFIER = ROOT / "scripts" / "verify-occult-environment.py"

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
    assert manifest["environment_verifier_asset"] == ENVIRONMENT_VERIFIER.name
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
    packaging_pins = re.findall(
        r"(?m)^packaging==([^\s\\]+)",
        _text(SIGSTORE_INPUT),
    )

    assert len(packaging_pins) == 1
    assert f"packaging=={packaging_pins[0]}" in _text(SIGSTORE_LOCK)
    assert manifest["sigstore_requirements_sha256"] == lock_hash
    assert lock_hash in _text(POWERSHELL)
    assert lock_hash in _text(SHELL)


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode()
    return "sha256=" + encoded.rstrip("=")


def _write_test_environment(root: Path) -> tuple[Path, Path, Path]:
    if os.name == "nt":
        site = root / "Lib" / "site-packages"
        launcher = root / "Scripts" / "hermes.exe"
        runtime = root / "Scripts" / "python.exe"
        external_launcher = root / "Scripts" / "demo.exe"
    else:
        site = root / "lib" / "python3.11" / "site-packages"
        launcher = root / "bin" / "hermes"
        runtime = root / "bin" / "python"
        external_launcher = root / "bin" / "demo"
    package = site / "demo"
    metadata = site / "demo-1.0.0.dist-info"
    package.mkdir(parents=True)
    metadata.mkdir()
    launcher.parent.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    metadata_file = metadata / "METADATA"
    metadata_file.write_text(
        "Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher.write_bytes(f"launcher:{root}\n".encode())
    launcher.chmod(0o755)
    runtime.write_bytes(f"python:{root}\n".encode())
    runtime.chmod(0o755)
    external_launcher.write_bytes(f"demo:{root}\n".encode())
    external_launcher.chmod(0o755)
    (root / "pyvenv.cfg").write_text(
        f"command = {root}\n",
        encoding="utf-8",
        newline="\n",
    )
    record = metadata / "RECORD"
    relative_launcher = os.path.relpath(launcher, site).replace(os.sep, "/")
    rows = [
        (
            source.relative_to(site).as_posix(),
            _record_digest(source.read_bytes()),
            str(source.stat().st_size),
        ),
        (
            metadata_file.relative_to(site).as_posix(),
            _record_digest(metadata_file.read_bytes()),
            str(metadata_file.stat().st_size),
        ),
        (
            relative_launcher,
            _record_digest(launcher.read_bytes()),
            str(launcher.stat().st_size),
        ),
        (
            os.path.relpath(external_launcher, site).replace(os.sep, "/"),
            _record_digest(external_launcher.read_bytes()),
            str(external_launcher.stat().st_size),
        ),
        (record.relative_to(site).as_posix(), "", ""),
    ]
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)
    py_compile.compile(str(source), doraise=True)
    return source, record, launcher


def _run_environment_verifier(existing: Path, reference: Path) -> int:
    completed = subprocess.run(
        [
            sys.executable,
            str(ENVIRONMENT_VERIFIER),
            "--existing",
            str(existing),
            "--reference",
            str(reference),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return completed.returncode


def _load_environment_verifier():
    spec = importlib.util.spec_from_file_location(
        "occult_environment_verifier", ENVIRONMENT_VERIFIER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_zip_executable(path: Path, environment: Path, overlay: bytes = b"") -> None:
    path.write_bytes(f"executable-prefix:{environment}\n".encode())
    info = ZipInfo("__main__.py", date_time=(2024, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.external_attr = 0o644 << 16
    with ZipFile(path, mode="a") as archive:
        archive.writestr(info, f"ENVIRONMENT = {environment!r}\n")
    if overlay:
        with path.open("ab") as stream:
            stream.write(overlay)


def test_environment_verifier_accepts_an_authenticated_equivalent(tmp_path: Path):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    _write_test_environment(existing)
    _write_test_environment(reference)

    assert _run_environment_verifier(existing, reference) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX separators are meaningful on Unix")
def test_environment_verifier_does_not_normalize_backslashes_on_posix(
    tmp_path: Path,
):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    _write_test_environment(existing)
    _write_test_environment(reference)
    launcher = existing / "bin" / "demo"
    launcher.write_bytes(launcher.read_bytes().replace(b"/", b"\\"))

    assert _run_environment_verifier(existing, reference) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX package modes are meaningful")
def test_environment_verifier_authenticates_package_file_modes(tmp_path: Path):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, _, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    source.chmod(0o777)

    assert _run_environment_verifier(existing, reference) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX generated-file modes matter")
@pytest.mark.parametrize("target", ["bytecode", "bytecode_directory", "record"])
def test_environment_verifier_authenticates_generated_file_modes(
    tmp_path: Path, target: str
):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, record, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    if target == "bytecode":
        generated = next(source.parent.glob("__pycache__/*.pyc"))
    elif target == "bytecode_directory":
        generated = source.parent / "__pycache__"
    else:
        generated = record
    generated.chmod(0o777)

    assert _run_environment_verifier(existing, reference) == 1


def test_environment_verifier_allows_reproducible_cache_inventory_difference(
    tmp_path: Path,
):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    _write_test_environment(existing)
    _write_test_environment(reference)
    for cache_root in reference.rglob("__pycache__"):
        shutil.rmtree(cache_root)

    assert _run_environment_verifier(existing, reference) == 0


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are unavailable")
def test_environment_verifier_rejects_hard_linked_files(tmp_path: Path):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, _, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    external = tmp_path / "external-source.py"
    source.replace(external)
    try:
        os.link(external, source)
    except OSError as error:
        pytest.skip(f"hard links are unavailable: {error}")

    assert _run_environment_verifier(existing, reference) == 1


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="directory symlinks are unavailable"
)
def test_environment_verifier_rejects_linked_environment_root(tmp_path: Path):
    external = tmp_path / "external"
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    _write_test_environment(external)
    _write_test_environment(reference)
    try:
        existing.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    assert _run_environment_verifier(existing, reference) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics required")
@pytest.mark.parametrize("location", ["package", "runtime"])
def test_environment_verifier_rejects_internal_windows_junctions(
    tmp_path: Path, location: str
):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, _, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    if location == "package":
        linked_directory = source.parent
    else:
        linked_directory = existing / "runtime-data"
        reference_directory = reference / "runtime-data"
        linked_directory.mkdir()
        reference_directory.mkdir()
        (linked_directory / "state.bin").write_bytes(b"authenticated runtime state")
        (reference_directory / "state.bin").write_bytes(
            b"authenticated runtime state"
        )
    external = tmp_path / f"external-{location}"
    shutil.copytree(linked_directory, external)
    shutil.rmtree(linked_directory)
    created = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(linked_directory),
            str(external),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("Windows directory junctions are unavailable")

    assert _run_environment_verifier(existing, reference) == 1


def test_direct_url_normalization_preserves_authenticated_origin(tmp_path: Path):
    verifier = _load_environment_verifier()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    remote = tmp_path / "remote.json"
    first.write_text(
        json.dumps({"url": "file:///temporary/one/hermes.whl"}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"url": "file:///temporary/two/hermes.whl"}),
        encoding="utf-8",
    )
    remote.write_text(
        json.dumps({"url": "https://attacker.invalid/hermes.whl"}),
        encoding="utf-8",
    )

    assert verifier._normalized_direct_url(first) == verifier._normalized_direct_url(
        second
    )
    assert verifier._normalized_direct_url(first) != verifier._normalized_direct_url(
        remote
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are meaningful")
@pytest.mark.parametrize("target", ["environment", "site_root", "package"])
def test_environment_verifier_authenticates_directory_modes(
    tmp_path: Path, target: str
):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, _, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    if target == "environment":
        directory = existing
    elif target == "site_root":
        directory = source.parents[1]
    else:
        directory = source.parent
    directory.chmod(0o777)

    assert _run_environment_verifier(existing, reference) == 1


def test_zip_executable_normalization_authenticates_framing_and_overlay(
    tmp_path: Path,
):
    verifier = _load_environment_verifier()
    existing_environment = tmp_path / "existing0"
    reference_environment = tmp_path / "reference"
    existing_environment.mkdir()
    reference_environment.mkdir()
    existing = existing_environment / "launcher.exe"
    reference = reference_environment / "launcher.exe"
    _write_zip_executable(existing, existing_environment)
    _write_zip_executable(reference, reference_environment)

    trusted = verifier._normalized_environment_file(existing, existing_environment)
    expected = verifier._normalized_environment_file(reference, reference_environment)
    assert trusted == expected

    framed = bytearray(existing.read_bytes())
    local_header = framed.index(b"PK\x03\x04")
    central_header = framed.index(b"PK\x01\x02")
    framed[local_header + 10] ^= 1
    framed[central_header + 12] ^= 1
    existing.write_bytes(framed)
    assert (
        verifier._normalized_environment_file(existing, existing_environment)
        != expected
    )

    _write_zip_executable(existing, existing_environment, overlay=b"untrusted-overlay")
    assert (
        verifier._normalized_environment_file(existing, existing_environment)
        != expected
    )


def test_zip_executable_normalization_rejects_length_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    verifier = _load_environment_verifier()
    environment = tmp_path / "environment"
    environment.mkdir()
    launcher = environment / "launcher.exe"
    _write_zip_executable(launcher, environment)

    monkeypatch.setattr(
        verifier,
        "_replace_environment_paths",
        lambda data, _environment: data[:-1],
    )
    with pytest.raises(
        verifier.IntegrityError,
        match="path normalization changed archive length",
    ):
        verifier._normalized_environment_file(launcher, environment)


@pytest.mark.parametrize(
    "tamper",
    [
        "record_and_source",
        "bytecode",
        "launcher",
        "interpreter",
        "external_launcher",
        "pyvenv",
        "extra",
    ],
)
def test_environment_verifier_rejects_tampered_reuse(tmp_path: Path, tamper: str):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, record, launcher = _write_test_environment(existing)
    _write_test_environment(reference)

    if tamper == "record_and_source":
        source.write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
        rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
        rows[0][1] = _record_digest(source.read_bytes())
        rows[0][2] = str(source.stat().st_size)
        with record.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
    elif tamper == "bytecode":
        cache = next(source.parent.glob("__pycache__/*.pyc"))
        data = bytearray(cache.read_bytes())
        data[-1] ^= 1
        cache.write_bytes(data)
    elif tamper == "launcher":
        launcher.write_bytes(launcher.read_bytes() + b"tampered")
    elif tamper == "interpreter":
        runtime = existing / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        runtime.write_bytes(runtime.read_bytes() + b"tampered")
    elif tamper == "external_launcher":
        tool = existing / ("Scripts/demo.exe" if os.name == "nt" else "bin/demo")
        tool.write_bytes(tool.read_bytes() + b"tampered")
    elif tamper == "pyvenv":
        config = existing / "pyvenv.cfg"
        config.write_text("command = tampered\n", encoding="utf-8", newline="\n")
    else:
        (source.parent / "untrusted.pth").write_text("payload\n", encoding="utf-8")

    assert _run_environment_verifier(existing, reference) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose os.mkfifo")
def test_environment_verifier_rejects_special_nodes(tmp_path: Path):
    existing = tmp_path / "existing0"
    reference = tmp_path / "reference"
    source, _, _ = _write_test_environment(existing)
    _write_test_environment(reference)
    os.mkfifo(source.parent / "evil.pth")

    assert _run_environment_verifier(existing, reference) == 1


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
    assert '$receiptPropertyNames -contains "council_environment"' in text
    assert "-not $councilEnvironmentPresent -or" in text
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
    assert text.count('"--link-mode", "copy"') >= 4
    assert text.count('"--cache-dir", $referenceCache') == 2
    assert '"tool", "run"' not in text
    assert "New-SigstoreVerifier" in text
    assert "$SigstoreRequirementsSha256" in text
    assert "hermes_requirements_asset" in text
    assert '"--clear"' not in text
    assert "environment_verifier_asset" in text
    assert '"--existing" $existingHermesVenv' in text
    assert '"--reference" $referenceHermesVenv' in text
    assert "$requiredReceiptProperties" in text
    receipt_fields_match = re.search(
        r"\$requiredReceiptProperties = @\((.*?)\n\s*\)",
        text,
        flags=re.DOTALL,
    )
    assert receipt_fields_match is not None
    assert set(re.findall(r'"([a-z0-9_]+)"', receipt_fields_match.group(1))) == (
        REQUIRED_RECEIPT_FIELDS
    )
    assert text.index("Assert-SigstoreIdentity") < text.index(
        'Write-Step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert text.index("if ($VerifyOnly)") < text.index(
        'Write-Step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert "$referencePackagedCouncil" in text
    assert "function Test-IndependentRegularFile" in text
    assert "function Test-IndependentDirectory" in text
    assert "function Get-PathNodeState" in text
    assert "-Path $hermesExecutable" in text
    assert "-Path $referencePackagedCouncil" in text
    assert "-Path $existingPackagedCouncil" in text
    assert "-Path $councilExecutable" in text
    assert "if ($metadataMatches -and $SkipCouncil)" in text
    assert 'Join-Path $binRoot "council.exe"' in text
    assert "Remove-Item -LiteralPath $councilExecutable -Force" in text
    assert "the stale managed Council command is not a file" in text
    assert "the stale managed Council command could not be removed" in text
    assert 'Get-PathNodeState `' in text
    assert "the managed Hermes command path is a directory" in text
    assert "the managed Council command path is a directory" in text
    assert "the managed Hermes command path is not an independent file" in text
    assert "the managed Council command path is not an independent file" in text
    assert "$receiptStateChanged = (" in text
    assert "$InitializeLocal -or $receiptStateChanged" in text
    assert "Mutable Occult state was refreshed" in text
    assert (
        "[bool]$existingReceipt.occult_initialized -eq [bool]$state.initialized"
        not in text
    )
    assert "NousResearch/hermes-agent" not in text
    assert "agents-council@latest" not in text


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_windows_path_state_probe_survives_powershell_native_argument_parsing(
    tmp_path: Path,
):
    text = _text(POWERSHELL)
    match = re.search(
        r"function Get-PathNodeState\s*\{.*?\$probe = @'\r?\n"
        r"(?P<probe>.*?)\r?\n'@",
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    encoded_probe = base64.b64encode(match.group("probe").encode("utf-8")).decode(
        "ascii"
    )
    command = (
        "$probe=[Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{encoded_probe}')); "
        "& $env:PROBE_PYTHON -c $probe $env:PROBE_TARGET"
    )

    def probe(target: Path) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update({
            "PROBE_PYTHON": sys.executable,
            "PROBE_TARGET": str(target),
        })
        return subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            env=env,
            text=True,
            timeout=30,
        )

    target = tmp_path / "managed command.exe"
    absent = probe(target)
    assert absent.returncode == 0, absent.stderr
    assert absent.stdout.strip() == "absent"

    target.write_bytes(b"independent command")
    present = probe(target)
    assert present.returncode == 0, present.stderr
    assert present.stdout.strip() == "present"


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
    assert text.count("--link-mode copy") >= 4
    assert text.count('--cache-dir "$reference_cache"') == 2
    assert "tool run" not in text
    assert "sigstore_requirements_sha256" in text
    assert "hermes_requirements_asset" in text
    assert "--clear" not in text
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
    assert "environment_verifier_asset" in text
    assert '--existing "$existing_hermes_root"' in text
    assert '--reference "$reference_hermes_root"' in text
    assert text.index("verify_sigstore") < text.index(
        'step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert text.index('if [ "$verify_only" -eq 1 ]') < text.index(
        'step "Installing the verified Hermes wheel and hash-locked dependencies per-user"'
    )
    assert 'reference_packaged_council="$reference_council_root/cli/council"' in text
    assert 'regular_file_profile "$reference_packaged_council"' in text
    assert 'regular_file_profile "$existing_packaged_council"' in text
    assert 'regular_file_profile "$council_executable"' in text
    assert 'real_directory_profile "$existing_council_root"' in text
    assert '[ -e "$bin_root/council" ] || [ -L "$bin_root/council" ]' in text
    assert 'rm -f -- "$bin_root/council"' in text
    assert 'rm -f -- "$user_bin/council"' in text
    assert "the stale managed Council command is not a file" in text
    user_path_guard = '[ -e "$user_bin/hermes" ] || [ -L "$user_bin/hermes" ]'
    assert user_path_guard in text
    assert '[ "$(readlink "$user_bin/hermes")" != "$hermes_executable" ]' in text
    assert '[ "$(readlink "$user_bin/council")" != "$bin_root/council" ]' in text
    assert text.index(user_path_guard) < text.index(
        'step "Activating the fully staged local commands"'
    )
    assert 'if [ -d "$hermes_executable" ]' in text
    assert 'if [ "$skip_council" -eq 0 ] && [ -d "$bin_root/council" ]' in text
    assert '[ -L "$hermes_executable" ]' in text
    assert '"$(readlink "$hermes_executable")" = "$existing_venv_hermes"' in text
    assert "the user Hermes command link could not be verified" in text
    assert "the user Council command link could not be verified" in text
    assert 'receipt_state_changed=1' in text
    assert (
        'if [ "$initialize_local" -eq 1 ] || [ "$receipt_state_changed" -eq 1 ]'
        in text
    )
    assert "Mutable Occult state was refreshed" in text
    assert '[ "$existing_receipt_initialized" = "$initialized" ] || reuse_ok=0' not in text
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
    powershell_blocks = re.findall(
        r"(?m)^```powershell[ \t]*\r?\n"
        r"(?P<body>(?:(?!^```)[^\r\n]*(?:\r?\n|$))*)"
        r"^```[ \t]*$",
        quickstart,
    )
    powershell_block = next(
        (body for body in powershell_blocks if "install-occult.ps1" in body),
        None,
    )
    assert powershell_block is not None
    assert _sha256(POWERSHELL) in powershell_block
    posix_blocks = re.findall(
        r"(?m)^```bash[ \t]*\r?\n"
        r"(?P<body>(?:(?!^```)[^\r\n]*(?:\r?\n|$))*)"
        r"^```[ \t]*$",
        quickstart,
    )
    posix_block = next(
        (body for body in posix_blocks if "install-occult.sh" in body),
        None,
    )
    assert posix_block is not None
    assert _sha256(SHELL) in posix_block
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
        "installer_interface",
        "installer_idempotent_rerun",
        "installer_tamper_repair",
    ):
        assert check in text
    assert '"contains_secrets": False' in text
    assert "OCCULT_E2E_HERMES_TOKEN" in text
    assert "council_result.stdout + council_result.stderr" in text
    assert "--candidate-hermes-wheel" in text
    assert "--candidate-council-archive" in text
    assert "--install-manifest" in text
    assert "--sigstore-requirements-lock" in text
    assert "--environment-verifier" in text
    assert "--public-installer-rerun" in text
    assert "def validate_public_installer_rerun" in text
    assert '"tarot",\n                "init"' in text
    assert "mutable_state_rerun" in text
    assert "mutable profile state rerun changed an active command" in text
    assert "preserve and record mutable profile state" in text
    assert "def call_packaged_council_tool" in text
    assert "def validate_packaged_council_mcp_flow" in text
    assert "first_receipt_mtime" in text
    assert 'skip_command = [*installer_command, "-SkipCouncil"]' in text
    assert "--skip-council left a stale Council command" in text
    assert "--skip-council rerun changed the install receipt" in text
    assert text.count("installer_command,") >= 2
    assert "release_artifacts" in text
    assert "install_hermes_environment" in text
    assert "validate_council_repository" in text
    assert "assert_redaction_surfaces" in text
    assert 'root.glob("gateway-*.log")' in text
    assert 'checks["packaged_council_mcp_flow"] = "passed"' in text
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
    manifest = json.loads(_text(MANIFEST))
    canary_evidence = (
        ROOT
        / "docs"
        / "occult"
        / "evidence"
        / f"launch-canary-v{manifest['occult_release_version']}.json"
    )
    evidence = json.loads(_text(canary_evidence))
    serialized = json.dumps(evidence, sort_keys=True).lower()

    assert evidence["candidate_status"] == "passed"
    assert evidence["overall_status"] == "candidate_passed"
    assert evidence["promotion_eligible"] is False
    assert evidence["scope"] == "pre-release Windows x64 candidate canary"
    assert evidence["platform"] == {"os": "Windows", "architecture": "x86_64"}
    assert "installer_idempotent_rerun" not in evidence["checks"]
    assert evidence["contains_secrets"] is False
    assert evidence["checks"]["rollback_previous_checksummed_releases"] == "passed"
    assert evidence["checks"]["installer_interface"] == "passed"
    assert evidence["checks"]["packaged_council_mcp_flow"] == "passed"
    assert evidence["release"]["hermes"] == (
        f"v{manifest['occult_release_version']}"
    )
    assert evidence["release"]["ollama_model"] == manifest["ollama_model"]
    assert evidence["release"]["council_commit"] == manifest["council"]["commit_sha"]
    assert evidence["release"]["runtime_contract"] == manifest["council"][
        "contract_version"
    ]
    assert evidence["release"]["council_state_schema"] == manifest["council"][
        "state_schema"
    ]
    assert set(evidence["release_artifacts"]) == {
        "install_manifest",
        "hermes_wheel",
        "hermes_requirements_lock",
        "sigstore_requirements_lock",
        "installer_powershell",
        "installer_posix",
        "environment_verifier",
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

#!/usr/bin/env python3
"""Run the redacted Windows Tarot Router launch canary.

The canary intentionally records only pass/fail facts and public version
metadata. Command output, prompts, tokens, local paths, and response text stay
inside a temporary directory that is removed before the report is written.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HERMES_CLI_VERSION = "0.14.0"
HERMES_RELEASE = "v1.0.6"
COUNCIL_VERSION = "0.5.5"
CONTRACT_VERSION = "1.0.0"
COUNCIL_STATE_SCHEMA = 3
STARTER_CARD_ID = "minor.pentacles.ace.ollama.local"
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SIGNED_URL = re.compile(r"https?://\S+(?:signature|sigstore|x-amz-|token=)", re.I)


class CanaryFailure(RuntimeError):
    """A safe, non-secret canary failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the redacted Tarot Router v1.0.6 Windows launch canary."
    )
    parser.add_argument("--council-repository", type=Path, required=True)
    parser.add_argument("--bun-executable", type=Path, required=True)
    parser.add_argument("--uv-executable", type=Path, required=True)
    parser.add_argument("--install-manifest", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--candidate-hermes-wheel", type=Path, required=True)
    parser.add_argument("--candidate-council-archive", type=Path, required=True)
    parser.add_argument("--installer-powershell", type=Path, required=True)
    parser.add_argument("--installer-posix", type=Path, required=True)
    parser.add_argument("--environment-verifier", type=Path, required=True)
    parser.add_argument("--sigstore-requirements-lock", type=Path, required=True)
    parser.add_argument("--previous-hermes-wheel", type=Path, required=True)
    parser.add_argument("--previous-council-archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--public-installer-rerun",
        action="store_true",
        help="install the public release twice and require byte-stable commands and receipt",
    )
    parser.add_argument(
        "--ollama-base-url",
        default="http://127.0.0.1:11434/v1",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser.parse_args()


def run(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: int = 180,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        creationflags=flags,
        check=False,
    )
    succeeded = completed.returncode == 0
    if succeeded != expect_success:
        operation = Path(command[0]).name
        if len(command) > 1 and re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", command[1]):
            operation += " " + command[1]
        raise CanaryFailure(
            f"command failed its expected outcome: {operation}"
        )
    return completed


def assert_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CanaryFailure(f"{label} is missing")
    return resolved


def validate_installer_interface(
    installer_powershell: Path,
    installer_posix: Path,
) -> None:
    try:
        powershell = installer_powershell.read_text(encoding="utf-8-sig")
        posix = installer_posix.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CanaryFailure(
            f"an installer script could not be read: {error.__class__.__name__}"
        ) from None
    required_powershell = (
        "[switch]$InitializeLocal",
        "[switch]$SkipCouncil",
        "[switch]$VerifyOnly",
        "environment_verifier_asset",
    )
    required_posix = (
        "--initialize-local",
        "--skip-council",
        "--verify-only",
        "environment_verifier_asset",
    )
    if any(marker not in powershell for marker in required_powershell):
        raise CanaryFailure("Windows installer interface is incomplete")
    if any(marker not in posix for marker in required_posix):
        raise CanaryFailure("POSIX installer interface is incomplete")


def validate_public_installer_rerun(
    *,
    installer_powershell: Path,
    root: Path,
    env: dict[str, str],
    model: str,
    ollama_base_url: str,
    timeout: int,
) -> list[str]:
    """Exercise exact rerun, mutable state, and tamper repair on public bytes."""
    import winreg

    powershell_name = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell_name:
        raise CanaryFailure("Windows PowerShell is required for the public installer canary")
    powershell = Path(powershell_name).resolve()
    install_root = root / "public-installer-rerun"
    profile_home = root / "public-installer-profile"
    profile_home.mkdir()
    installer_env = dict(env)
    installer_env["HERMES_HOME"] = str(profile_home)

    path_existed = False
    original_path = ""
    original_path_kind = winreg.REG_EXPAND_SZ
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                original_path, original_path_kind = winreg.QueryValueEx(key, "Path")
                path_existed = True
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    installer_command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer_powershell),
        "-Version",
        HERMES_RELEASE.removeprefix("v"),
        "-InstallRoot",
        str(install_root),
    ]
    outputs: list[str] = []

    def fingerprint(path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return sha256_file(path), stat.st_size, stat.st_mtime_ns

    def update_record(record: Path, target: Path, site_root: Path) -> None:
        relative = target.relative_to(site_root).as_posix()
        original = record.read_bytes()
        line_terminator = "\r\n" if b"\r\n" in original else "\n"
        rows = list(csv.reader(original.decode("utf-8").splitlines()))
        matched = False
        data = target.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode()
        for row in rows:
            if row and row[0] == relative:
                row[1] = "sha256=" + digest.rstrip("=")
                row[2] = str(len(data))
                matched = True
        if not matched:
            raise CanaryFailure("the Hermes RECORD omitted its authenticated module")
        with record.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream, lineterminator=line_terminator).writerows(rows)

    try:
        first = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(first.stdout + first.stderr)
        hermes = assert_file(install_root / "bin" / "hermes.exe", "installed Hermes")
        council = assert_file(
            install_root / "bin" / "council.exe",
            "installed Council",
        )
        receipt = assert_file(
            install_root / "occult-install-receipt.json",
            "install receipt",
        )
        first_commands = {
            "hermes": fingerprint(hermes),
            "council": fingerprint(council),
        }
        first_receipt = receipt.read_bytes()
        first_receipt_mtime = receipt.stat().st_mtime_ns
        try:
            receipt_data = json.loads(first_receipt.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise CanaryFailure("the public installer wrote an invalid receipt") from None
        if receipt_data.get("occult_release_version") != HERMES_RELEASE.removeprefix("v"):
            raise CanaryFailure("the public installer receipt has the wrong release")
        if receipt_data.get("council_release") != f"v{COUNCIL_VERSION}":
            raise CanaryFailure("the public installer receipt has the wrong Council release")
        if receipt_data.get("occult_initialized") is not False:
            raise CanaryFailure("the public installer initialized Occult implicitly")
        if receipt_data.get("occult_enabled") is not False:
            raise CanaryFailure("the public installer enabled Occult implicitly")

        second = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(second.stdout + second.stderr)
        second_commands = {
            "hermes": fingerprint(hermes),
            "council": fingerprint(council),
        }
        if second_commands != first_commands:
            raise CanaryFailure("an exact installer rerun changed an active command")
        if receipt.read_bytes() != first_receipt:
            raise CanaryFailure("an exact installer rerun changed the install receipt")
        if receipt.stat().st_mtime_ns != first_receipt_mtime:
            raise CanaryFailure("an exact installer rerun rewrote the install receipt")

        council_environment = receipt_data.get("council_environment")
        hermes_environment = receipt_data.get("hermes_environment")
        if not isinstance(council_environment, str) or not council_environment:
            raise CanaryFailure("the public installer receipt omitted Council state")
        if not isinstance(hermes_environment, str) or not hermes_environment:
            raise CanaryFailure("the public installer receipt omitted Hermes state")

        run(
            [
                str(hermes),
                "tarot",
                "init",
                "--base-url",
                ollama_base_url,
                "--model",
                model,
            ],
            env=installer_env,
            timeout=timeout,
        )
        initialized_status = load_json_output(
            run(
                [str(hermes), "tarot", "status"],
                env=installer_env,
                timeout=timeout,
            ).stdout,
            "public installer initialized status",
        )
        if (
            initialized_status.get("initialized") is not True
            or initialized_status.get("enabled") is not True
        ):
            raise CanaryFailure("the public installer profile state did not change")
        mutable_state_rerun = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(mutable_state_rerun.stdout + mutable_state_rerun.stderr)
        if {
            "hermes": fingerprint(hermes),
            "council": fingerprint(council),
        } != first_commands:
            raise CanaryFailure("a mutable profile state rerun changed an active command")
        refreshed_receipt = json.loads(receipt.read_text(encoding="utf-8-sig"))
        if (
            refreshed_receipt.get("hermes_environment") != hermes_environment
            or refreshed_receipt.get("council_environment") != council_environment
            or refreshed_receipt.get("occult_initialized") is not True
            or refreshed_receipt.get("occult_enabled") is not True
        ):
            raise CanaryFailure(
                "the public installer did not preserve and record mutable profile state"
            )
        packaged_council = assert_file(
            install_root
            / "council-environments"
            / council_environment
            / "cli"
            / "council.exe",
            "packaged Council",
        )
        trusted_council = council.read_bytes()
        council.write_bytes(trusted_council + b"occult-canary-tamper")
        packaged_council.write_bytes(
            packaged_council.read_bytes() + b"occult-canary-tamper"
        )
        council_repair = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(council_repair.stdout + council_repair.stderr)
        if council.read_bytes() != trusted_council:
            raise CanaryFailure("the installer did not repair a modified Council binary")

        repaired_receipt = json.loads(receipt.read_text(encoding="utf-8-sig"))
        repaired_hermes_environment = repaired_receipt.get("hermes_environment")
        if not isinstance(repaired_hermes_environment, str):
            raise CanaryFailure("the repaired receipt omitted Hermes state")
        site_root = (
            install_root
            / "hermes-environments"
            / repaired_hermes_environment
            / "Lib"
            / "site-packages"
        )
        source = assert_file(
            site_root / "hermes_cli" / "__init__.py",
            "installed Hermes module",
        )
        records = list(site_root.glob("hermes_agent-*.dist-info/RECORD"))
        if len(records) != 1:
            raise CanaryFailure("the installed Hermes RECORD is ambiguous")
        trusted_source = source.read_bytes()
        source.write_bytes(trusted_source + b"\n# occult canary tamper\n")
        update_record(records[0], source, site_root)
        caches = sorted((site_root / "hermes_cli" / "__pycache__").glob("*.pyc"))
        if caches:
            bytecode = bytearray(caches[0].read_bytes())
            bytecode[-1] ^= 1
            caches[0].write_bytes(bytecode)
        hermes_repair = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(hermes_repair.stdout + hermes_repair.stderr)
        final_receipt = receipt.read_bytes()
        final_receipt_mtime = receipt.stat().st_mtime_ns
        final_commands = {
            "hermes": fingerprint(hermes),
            "council": fingerprint(council),
        }
        final_receipt_data = json.loads(final_receipt.decode("utf-8-sig"))
        final_site_root = (
            install_root
            / "hermes-environments"
            / final_receipt_data["hermes_environment"]
            / "Lib"
            / "site-packages"
        )
        final_source = assert_file(
            final_site_root / "hermes_cli" / "__init__.py",
            "repaired Hermes module",
        )
        if final_source.read_bytes() != trusted_source:
            raise CanaryFailure("the installer did not repair modified Hermes code")

        post_repair_rerun = run(
            installer_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(post_repair_rerun.stdout + post_repair_rerun.stderr)
        if {
            "hermes": fingerprint(hermes),
            "council": fingerprint(council),
        } != final_commands:
            raise CanaryFailure("a post-repair rerun changed an active command")
        if receipt.read_bytes() != final_receipt:
            raise CanaryFailure("a post-repair rerun changed the install receipt")
        if receipt.stat().st_mtime_ns != final_receipt_mtime:
            raise CanaryFailure("a post-repair rerun rewrote the install receipt")

        hermes_version = run([str(hermes), "--version"], env=installer_env, timeout=timeout)
        council_version = run(
            [str(council), "--version"],
            env=installer_env,
            timeout=timeout,
        )
        safe_public_version(
            hermes_version.stdout,
            HERMES_CLI_VERSION,
            "public installer Hermes",
        )
        safe_public_version(
            council_version.stdout,
            COUNCIL_VERSION,
            "public installer Council",
        )
        outputs.extend(
            [
                hermes_version.stdout + hermes_version.stderr,
                council_version.stdout + council_version.stderr,
            ]
        )

        skip_command = [*installer_command, "-SkipCouncil"]
        skip_transition = run(
            skip_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(skip_transition.stdout + skip_transition.stderr)
        if council.exists():
            raise CanaryFailure("--skip-council left a stale Council command")
        skip_receipt = receipt.read_bytes()
        skip_receipt_mtime = receipt.stat().st_mtime_ns
        skip_hermes = fingerprint(hermes)
        skip_receipt_data = json.loads(skip_receipt.decode("utf-8-sig"))
        if any(
            skip_receipt_data.get(field) is not None
            for field in (
                "council_release",
                "council_archive_sha256",
                "council_environment",
            )
        ):
            raise CanaryFailure("--skip-council preserved Council receipt metadata")

        skip_rerun = run(
            skip_command,
            env=installer_env,
            timeout=timeout,
        )
        outputs.append(skip_rerun.stdout + skip_rerun.stderr)
        if council.exists():
            raise CanaryFailure("a --skip-council rerun restored a Council command")
        if fingerprint(hermes) != skip_hermes:
            raise CanaryFailure("a --skip-council rerun changed the Hermes command")
        if receipt.read_bytes() != skip_receipt:
            raise CanaryFailure("a --skip-council rerun changed the install receipt")
        if receipt.stat().st_mtime_ns != skip_receipt_mtime:
            raise CanaryFailure("a --skip-council rerun rewrote the install receipt")
        return outputs
    finally:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if path_existed:
                winreg.SetValueEx(
                    key,
                    "Path",
                    0,
                    original_path_kind,
                    original_path,
                )
            else:
                try:
                    winreg.DeleteValue(key, "Path")
                except FileNotFoundError:
                    pass
            winreg.FlushKey(key)


def assert_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise CanaryFailure(f"{label} is missing")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_sha256(path: Path, expected: str, label: str) -> None:
    if sha256_file(path) != expected:
        raise CanaryFailure(f"{label} checksum mismatch")


def extract_regular_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            normalized = member.name.replace("\\", "/").strip("/")
            if normalized in {"", "."}:
                continue
            parts = Path(normalized).parts
            if (
                Path(member.name).is_absolute()
                or ".." in parts
                or member.issym()
                or member.islnk()
            ):
                raise CanaryFailure("Council archive contains an unsafe path")
            target = destination.joinpath(*parts).resolve()
            if not target.is_relative_to(destination_root):
                raise CanaryFailure("Council archive escapes its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise CanaryFailure("Council archive contains a special file")
            source = archive.extractfile(member)
            if source is None:
                raise CanaryFailure("Council archive could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def install_hermes_environment(
    *,
    environment: Path,
    uv: Path,
    requirements_lock: Path,
    wheel: Path,
    env: dict[str, str],
    timeout: int,
) -> Path:
    run(
        [
            str(uv),
            "venv",
            "--no-config",
            "--python",
            "3.11",
            str(environment),
        ],
        env=env,
        timeout=timeout,
    )
    environment_python = environment / "Scripts" / "python.exe"
    run(
        [
            str(uv),
            "pip",
            "sync",
            "--no-config",
            "--python",
            str(environment_python),
            "--require-hashes",
            str(requirements_lock),
        ],
        env=env,
        timeout=timeout * 3,
    )
    run(
        [
            str(uv),
            "pip",
            "install",
            "--no-config",
            "--python",
            str(environment_python),
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        env=env,
        timeout=timeout,
    )
    executable = environment / "Scripts" / "hermes.exe"
    if not executable.is_file():
        raise CanaryFailure("candidate Hermes environment is incomplete")
    return executable


def load_install_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise CanaryFailure("install manifest is invalid") from None
    if not isinstance(manifest, dict):
        raise CanaryFailure("install manifest has the wrong shape")
    council = manifest.get("council")
    if not isinstance(council, dict):
        raise CanaryFailure("install manifest Council metadata is missing")
    if (
        manifest.get("occult_release_version") != HERMES_RELEASE.removeprefix("v")
        or manifest.get("hermes_cli_version") != HERMES_CLI_VERSION
        or council.get("release_tag") != f"v{COUNCIL_VERSION}"
        or council.get("contract_version") != CONTRACT_VERSION
        or council.get("state_schema") != COUNCIL_STATE_SCHEMA
    ):
        raise CanaryFailure("install manifest release metadata is incompatible")
    council_commit = council.get("commit_sha")
    if not isinstance(council_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}",
        council_commit,
    ):
        raise CanaryFailure("install manifest Council commit is invalid")
    model = manifest.get("ollama_model")
    if not isinstance(model, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",
        model,
    ):
        raise CanaryFailure("install manifest Ollama model is invalid")
    rollback = manifest.get("rollback")
    if not isinstance(rollback, dict):
        raise CanaryFailure("install manifest rollback metadata is missing")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", rollback.get("hermes_release_tag", "")):
        raise CanaryFailure("rollback Hermes release is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", rollback.get("hermes_cli_version", "")):
        raise CanaryFailure("rollback Hermes CLI version is invalid")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", rollback.get("council_release_tag", "")):
        raise CanaryFailure("rollback Council release is invalid")
    for field in ("hermes_wheel_sha256", "council_windows_x64_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", rollback.get(field, "")):
            raise CanaryFailure(f"rollback checksum is invalid: {field}")
    return manifest


def validate_council_repository(
    repository: Path,
    expected_commit: str,
    *,
    env: dict[str, str],
    timeout: int,
) -> None:
    git = shutil.which("git")
    if git is None:
        raise CanaryFailure("Git is required to verify the Council canary checkout")
    head = run(
        [git, "-C", str(repository), "rev-parse", "HEAD"],
        env=env,
        timeout=timeout,
    ).stdout.strip()
    if head != expected_commit:
        raise CanaryFailure("Council canary checkout is not at the reviewed commit")
    status = run(
        [
            git,
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        env=env,
        timeout=timeout,
    ).stdout
    if status.strip():
        raise CanaryFailure("Council canary checkout contains local modifications")
    if not (repository / "scripts" / "occultHermesE2E.ts").is_file():
        raise CanaryFailure("Council cross-repository E2E script is missing")


def call_packaged_council_tool(
    council: Path,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    env: dict[str, str],
    timeout: int,
) -> tuple[dict[str, Any], str]:
    """Call one MCP tool through the packaged Council executable."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "tarot-router-launch-canary",
                    "version": "1.0.0",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    ]
    input_text = "".join(
        json.dumps(message, separators=(",", ":")) + "\n"
        for message in messages
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        [str(council), "mcp", "--format", "json", "--agent-name", "e2e-operator"],
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        creationflags=flags,
        check=False,
    )
    if completed.returncode != 0:
        raise CanaryFailure("the packaged Council MCP process failed")
    response: dict[str, Any] | None = None
    for line in completed.stdout.splitlines():
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("id") == 2:
            response = payload
    if response is None:
        raise CanaryFailure("the packaged Council MCP tool returned no response")
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise CanaryFailure("the packaged Council MCP tool returned an error")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise CanaryFailure("the packaged Council MCP tool omitted structured output")
    return structured, completed.stdout + completed.stderr


def approve_persisted_council_reading(
    state_path: Path,
    reading_id: str,
    approval_id: str,
) -> None:
    """Record the canary operator approval between packaged process runs."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != COUNCIL_STATE_SCHEMA:
            raise CanaryFailure("the packaged Council state schema is invalid")
        readings = state.get("occultReadings")
        if not isinstance(readings, list):
            raise CanaryFailure("the packaged Council state omitted readings")
        reading = next(
            (
                item
                for item in readings
                if isinstance(item, dict) and item.get("id") == reading_id
            ),
            None,
        )
        if reading is None:
            raise CanaryFailure("the packaged Council state omitted the reading")
        approvals = reading.get("approvals")
        if not isinstance(approvals, list):
            raise CanaryFailure("the packaged Council state omitted approvals")
        approval = next(
            (
                item
                for item in approvals
                if isinstance(item, dict) and item.get("approvalId") == approval_id
            ),
            None,
        )
        if approval is None or approval.get("state") != "pending":
            raise CanaryFailure("the packaged Council approval is not pending")
        resolved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        approval.update({
            "state": "approved",
            "resolvedAt": resolved_at,
            "resolvedBy": "e2e-operator",
        })
        reading["updatedAt"] = resolved_at
        staged = state_path.with_name(
            state_path.name + ".approval-" + uuid.uuid4().hex + ".tmp"
        )
        staged.write_text(
            json.dumps(state, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(staged, state_path)
    except CanaryFailure:
        raise
    except (OSError, TypeError, ValueError):
        raise CanaryFailure(
            "the packaged Council approval could not be persisted"
        ) from None


def packaged_council_plan(session_id: str) -> dict[str, Any]:
    """Return the public v1 build-review-synthesis wire plan."""
    return {
        "session_id": session_id,
        "spread_id": "occult.spread.build-review-synthesis",
        "spread_version": "1.0.0",
        "idempotency_key": "packaged-council:build-review-synthesis",
        "routing": {
            "mode": "local_only",
            "free_only": True,
            "local_only": True,
            "maximum_fallbacks": 0,
            "maximum_cost_usd": 0,
        },
        "maximum_parallelism": 1,
        "nodes": [
            {
                "node_id": "build",
                "agent_id": "occult.major.magician",
                "message": "Build the packaged production artifact.",
            },
            {
                "node_id": "review",
                "agent_id": "occult.major.justice",
                "message": "Review the packaged production artifact.",
                "requires_approval": True,
            },
            {
                "node_id": "synthesis",
                "agent_id": "occult.major.temperance",
                "message": "Synthesize the approved packaged result.",
            },
        ],
        "dependencies": [
            {"source": "build", "target": "review"},
            {"source": "review", "target": "synthesis"},
        ],
    }


def validate_packaged_council_mcp_flow(
    council: Path,
    root: Path,
    *,
    env: dict[str, str],
    hermes_url: str,
    service_token: str,
    timeout: int,
) -> list[str]:
    """Exercise pause, process restart, approval, and resume through Council MCP."""
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "packaged-council-state.json"
    council_env = dict(env)
    council_env.update({
        "AGENTS_COUNCIL_STATE_PATH": str(state_path),
        "OCCULT_ENABLED": "true",
        "OCCULT_HERMES_URL": hermes_url,
        "OCCULT_HERMES_SERVICE_TOKEN": service_token,
    })
    outputs: list[str] = []
    started, output = call_packaged_council_tool(
        council,
        "start_council",
        {"request": "Build, review, and synthesize the packaged release."},
        env=council_env,
        timeout=timeout,
    )
    outputs.append(output)
    session_id = started.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise CanaryFailure("the packaged Council MCP flow omitted its session")
    plan = packaged_council_plan(session_id)
    paused, output = call_packaged_council_tool(
        council,
        "occult_create_reading_v1",
        {"contract_version": CONTRACT_VERSION, "plan": plan},
        env=council_env,
        timeout=timeout,
    )
    outputs.append(output)
    reading_id = paused.get("reading_id")
    if (
        not isinstance(reading_id, str)
        or not reading_id
        or paused.get("contract_version") != CONTRACT_VERSION
        or paused.get("state") != "running"
    ):
        raise CanaryFailure("the packaged Council reading did not pause")
    approvals = paused.get("approvals")
    if not isinstance(approvals, list):
        raise CanaryFailure("the packaged Council reading omitted approvals")
    approval = next(
        (
            item
            for item in approvals
            if isinstance(item, dict)
            and item.get("node_id") == "review"
            and item.get("state") == "pending"
        ),
        None,
    )
    if approval is None or not isinstance(approval.get("approval_id"), str):
        raise CanaryFailure("the packaged Council reading did not request approval")
    inspected, output = call_packaged_council_tool(
        council,
        "occult_get_reading_v1",
        {
            "contract_version": CONTRACT_VERSION,
            "session_id": session_id,
            "reading_id": reading_id,
        },
        env=council_env,
        timeout=timeout,
    )
    outputs.append(output)
    if inspected.get("reading_id") != reading_id or inspected.get("state") != "running":
        raise CanaryFailure("the packaged Council reading did not survive restart")
    approve_persisted_council_reading(
        state_path,
        reading_id,
        approval["approval_id"],
    )
    completed, output = call_packaged_council_tool(
        council,
        "occult_resume_reading_v1",
        {
            "contract_version": CONTRACT_VERSION,
            "reading_id": reading_id,
            "plan": plan,
        },
        env=council_env,
        timeout=timeout,
    )
    outputs.append(output)
    events = completed.get("events")
    terminal = (
        [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("event_type")
            in {"reading.cancelled", "reading.completed", "reading.failed"}
        ]
        if isinstance(events, list)
        else []
    )
    if (
        completed.get("reading_id") != reading_id
        or completed.get("state") != "completed"
        or len(terminal) != 1
        or terminal[0].get("event_type") != "reading.completed"
    ):
        raise CanaryFailure("the packaged Council reading did not resume cleanly")
    return outputs


def rehearse_rollback(
    *,
    root: Path,
    uv: Path,
    requirements_lock: Path,
    candidate_hermes_wheel: Path,
    candidate_council_archive: Path,
    previous_hermes_wheel: Path,
    previous_council_archive: Path,
    previous_hermes_cli_version: str,
    previous_hermes_wheel_sha256: str,
    previous_council_version: str,
    previous_council_archive_sha256: str,
    env: dict[str, str],
    timeout: int,
) -> list[str]:
    assert_sha256(
        previous_hermes_wheel,
        previous_hermes_wheel_sha256,
        "previous Hermes wheel",
    )
    assert_sha256(
        previous_council_archive,
        previous_council_archive_sha256,
        "previous Council archive",
    )
    rollback_root = root / "rollback-rehearsal"
    environments = rollback_root / "hermes-environments"
    bin_root = rollback_root / "bin"
    environments.mkdir(parents=True)
    bin_root.mkdir()

    def activate(source: Path, destination: Path) -> None:
        staged = destination.with_name(destination.name + ".new-" + uuid.uuid4().hex)
        shutil.copy2(source, staged)
        os.replace(staged, destination)

    candidate_hermes = install_hermes_environment(
        environment=environments / "candidate",
        uv=uv,
        requirements_lock=requirements_lock,
        wheel=candidate_hermes_wheel,
        env=env,
        timeout=timeout,
    )
    previous_hermes = install_hermes_environment(
        environment=environments / "previous",
        uv=uv,
        requirements_lock=requirements_lock,
        wheel=previous_hermes_wheel,
        env=env,
        timeout=timeout,
    )
    safe_public_version(
        run([str(previous_hermes), "--version"], env=env, timeout=timeout).stdout,
        previous_hermes_cli_version,
        "previous Hermes",
    )

    candidate_council_root = rollback_root / "council-environments" / "candidate"
    previous_council_root = rollback_root / "council-environments" / "previous"
    extract_regular_tar(candidate_council_archive, candidate_council_root)
    extract_regular_tar(previous_council_archive, previous_council_root)
    candidate_council = candidate_council_root / "cli" / "council.exe"
    previous_council = previous_council_root / "cli" / "council.exe"
    safe_public_version(
        run([str(candidate_council), "--version"], env=env, timeout=timeout).stdout,
        COUNCIL_VERSION,
        "candidate Council",
    )
    safe_public_version(
        run([str(previous_council), "--version"], env=env, timeout=timeout).stdout,
        previous_council_version,
        "previous Council",
    )

    active_hermes = bin_root / "hermes.exe"
    active_council = bin_root / "council.exe"
    observations: list[str] = []

    def verify_active(
        hermes_source: Path,
        council_source: Path,
        hermes_version: str,
        council_version: str,
        label: str,
    ) -> None:
        activate(hermes_source, active_hermes)
        activate(council_source, active_council)
        safe_public_version(
            run([str(active_hermes), "--version"], env=env, timeout=timeout).stdout,
            hermes_version,
            f"{label} Hermes",
        )
        safe_public_version(
            run([str(active_council), "--version"], env=env, timeout=timeout).stdout,
            council_version,
            f"{label} Council",
        )
        gateway, handle = start_gateway(
            active_hermes,
            env,
            root / f"gateway-{label}.log",
            timeout,
        )
        try:
            status_result = run(
                [str(active_hermes), "occult", "status"],
                env=env,
                timeout=timeout,
            )
            status = load_json_output(status_result.stdout, f"{label} Occult status")
            if not status.get("agents") or not status.get("routes"):
                raise CanaryFailure(
                    f"{label} binaries could not read the existing Occult state"
                )
            observations.append(status_result.stdout + status_result.stderr)
        finally:
            stop_gateway(gateway, handle)

    verify_active(
        candidate_hermes,
        candidate_council,
        HERMES_CLI_VERSION,
        COUNCIL_VERSION,
        "candidate-before-rollback",
    )
    verify_active(
        previous_hermes,
        previous_council,
        previous_hermes_cli_version,
        previous_council_version,
        "previous-after-rollback",
    )
    verify_active(
        candidate_hermes,
        candidate_council,
        HERMES_CLI_VERSION,
        COUNCIL_VERSION,
        "candidate-restored",
    )
    return observations


def safe_public_version(output: str, expected: str, label: str) -> str:
    matches = re.findall(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", output)
    if expected not in matches:
        raise CanaryFailure(f"{label} version mismatch")
    if not SAFE_VERSION.fullmatch(expected):
        raise CanaryFailure(f"{label} version is unsafe")
    return expected


def load_json_output(output: str, label: str) -> dict[str, Any]:
    text = output.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        opening = text.find("{")
        if opening < 0:
            raise CanaryFailure(f"{label} did not return JSON") from None
        try:
            value = json.loads(text[opening:])
        except json.JSONDecodeError:
            raise CanaryFailure(f"{label} did not return parseable JSON") from None
    if not isinstance(value, dict):
        raise CanaryFailure(f"{label} returned the wrong JSON shape")
    return value


def load_dotenv_value(path: Path, name: str) -> str:
    if not path.is_file():
        raise CanaryFailure("Occult secret store was not created")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"'")
    raise CanaryFailure(f"{name} was not created")


def assert_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            raise CanaryFailure(f"loopback port {port} is already in use") from None


def wait_for_health(url: str, process: subprocess.Popen[bytes], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CanaryFailure("Hermes gateway exited before becoming healthy")
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                if 200 <= response.status < 300:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise CanaryFailure("Hermes gateway did not become healthy")


def start_gateway(
    hermes: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: int,
) -> tuple[subprocess.Popen[bytes], Any]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [str(hermes), "gateway", "run", "--replace"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    try:
        wait_for_health("http://127.0.0.1:8642/health", process, timeout)
    except BaseException:
        stop_gateway(process, log_handle)
        raise
    return process, log_handle


def stop_gateway(process: subprocess.Popen[bytes], log_handle: Any) -> None:
    try:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    [
                        "taskkill.exe",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    finally:
        log_handle.close()


def validate_ollama(base_url: str, model: str, timeout: int) -> None:
    url = base_url.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=min(timeout, 15)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        raise CanaryFailure(
            "Ollama is unavailable; install or start Ollama and pull the approved model"
        ) from None
    model_ids = {
        str(item.get("id", ""))
        for item in payload.get("data", [])
        if isinstance(item, dict)
    }
    if model not in model_ids:
        raise CanaryFailure(f"approved Ollama model is not installed: {model}")


def assert_redacted(output: str, token: str) -> None:
    lowered = output.lower()
    if token in output:
        raise CanaryFailure("Council output exposed its service token")
    if "authorization" in lowered or "api_key" in lowered:
        raise CanaryFailure("Council output exposed a credential field")
    if SIGNED_URL.search(output):
        raise CanaryFailure("Council output exposed a signed URL")


def assert_redaction_surfaces(
    *,
    text_surfaces: list[str],
    file_surfaces: list[Path],
    token: str,
    prompt_marker: str,
) -> None:
    forbidden = (
        (token.encode("utf-8"), "service token"),
        (prompt_marker.encode("utf-8"), "prompt"),
    )

    def inspect(data: bytes, label: str) -> None:
        lowered = data.lower()
        for value, description in forbidden:
            if value in data:
                raise CanaryFailure(f"Hermes audit surface exposed a {description}")
        if b"authorization:" in lowered or b"bearer " in lowered:
            raise CanaryFailure("Hermes audit surface exposed authorization details")
        decoded = data.decode("utf-8", errors="ignore")
        if SIGNED_URL.search(decoded):
            raise CanaryFailure("Hermes audit surface exposed a signed URL")

    for index, output in enumerate(text_surfaces):
        inspect(output.encode("utf-8"), f"command output {index}")
    for surface in file_surfaces:
        if surface.is_file():
            inspect(surface.read_bytes(), surface.name)
            continue
        if not surface.is_dir():
            continue
        for path in sorted(surface.rglob("*")):
            if path.is_file():
                inspect(path.read_bytes(), path.name)


def main() -> int:
    args = parse_args()
    if platform.system() != "Windows":
        raise CanaryFailure("the v1 launch canary must run on Windows")
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise CanaryFailure("the v1 launch canary requires Windows x64")
    if not 30 <= args.timeout_seconds <= 900:
        raise CanaryFailure("timeout must be from 30 to 900 seconds")

    bun = assert_file(args.bun_executable, "Bun executable")
    uv = assert_file(args.uv_executable, "uv executable")
    install_manifest = assert_file(args.install_manifest, "install manifest")
    manifest = load_install_manifest(install_manifest)
    model = manifest["ollama_model"]
    council_commit = manifest["council"]["commit_sha"]
    rollback = manifest["rollback"]
    requirements_lock = assert_file(
        args.requirements_lock,
        "Hermes requirements lock",
    )
    candidate_hermes_wheel = assert_file(
        args.candidate_hermes_wheel,
        "candidate Hermes wheel",
    )
    candidate_council_archive = assert_file(
        args.candidate_council_archive,
        "candidate Council archive",
    )
    installer_powershell = assert_file(
        args.installer_powershell,
        "PowerShell installer",
    )
    installer_posix = assert_file(args.installer_posix, "POSIX installer")
    environment_verifier = assert_file(
        args.environment_verifier,
        "environment verifier",
    )
    sigstore_requirements_lock = assert_file(
        args.sigstore_requirements_lock,
        "Sigstore requirements lock",
    )
    previous_hermes_wheel = assert_file(
        args.previous_hermes_wheel,
        "previous Hermes wheel",
    )
    previous_council_archive = assert_file(
        args.previous_council_archive,
        "previous Council archive",
    )
    council_repository = assert_directory(args.council_repository, "Council repository")
    checks: dict[str, str] = {}
    validate_installer_interface(
        installer_powershell,
        installer_posix,
    )
    checks["installer_interface"] = "passed"
    assert_port_available("127.0.0.1", 8642)
    validate_ollama(args.ollama_base_url, model, args.timeout_seconds)

    observed_outputs: list[str] = []
    prompt_marker = "OCCULT_CANARY_PRIVATE_PROMPT_" + uuid.uuid4().hex
    gateway: subprocess.Popen[bytes] | None = None
    gateway_log: Any | None = None
    with tempfile.TemporaryDirectory(
        prefix="tarot-router-v106-canary-",
        ignore_cleanup_errors=True,
    ) as temporary:
        root = Path(temporary)
        primary_home = root / "hermes-primary"
        restored_home = root / "hermes-restored"
        backup_path = root / "occult-backup.zip"
        primary_home.mkdir()
        restored_home.mkdir()
        env = dict(os.environ)
        env.update({
            "HERMES_HOME": str(primary_home),
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        validate_council_repository(
            council_repository,
            council_commit,
            env=env,
            timeout=args.timeout_seconds,
        )
        checks["council_source_provenance"] = "passed"

        if args.public_installer_rerun:
            observed_outputs.extend(
                validate_public_installer_rerun(
                    installer_powershell=installer_powershell,
                    root=root,
                    env=env,
                    model=model,
                    ollama_base_url=args.ollama_base_url,
                    timeout=args.timeout_seconds,
                )
            )
            checks["installer_idempotent_rerun"] = "passed"
            checks["installer_tamper_repair"] = "passed"

        hermes = install_hermes_environment(
            environment=root / "candidate-main-hermes",
            uv=uv,
            requirements_lock=requirements_lock,
            wheel=candidate_hermes_wheel,
            env=env,
            timeout=args.timeout_seconds,
        )
        candidate_council_root = root / "candidate-main-council"
        extract_regular_tar(candidate_council_archive, candidate_council_root)
        council = assert_file(
            candidate_council_root / "cli" / "council.exe",
            "candidate Council executable",
        )
        checks["candidate_artifact_installation"] = "passed"

        hermes_version = safe_public_version(
            run([str(hermes), "--version"], env=env).stdout,
            HERMES_CLI_VERSION,
            "Hermes",
        )
        council_version = safe_public_version(
            run([str(council), "--version"], env=env).stdout,
            COUNCIL_VERSION,
            "Council",
        )
        checks["versions"] = "passed"

        disabled_status = run(
            [str(hermes), "tarot", "status"],
            env=env,
            timeout=args.timeout_seconds,
        )
        observed_outputs.append(disabled_status.stdout + disabled_status.stderr)
        disabled_state = load_json_output(
            disabled_status.stdout,
            "pre-initialization Tarot Router status",
        )
        if (
            disabled_state.get("initialized") is not False
            or disabled_state.get("enabled") is not False
        ):
            raise CanaryFailure(
                "Tarot Router did not report a safe pre-initialization state"
            )
        if (primary_home / "occult").exists():
            raise CanaryFailure("Occult activated before explicit initialization")
        checks["disabled_before_initialization"] = "passed"

        initialized = load_json_output(
            run(
                [
                    str(hermes),
                    "occult",
                    "init",
                    "--base-url",
                    args.ollama_base_url,
                    "--model",
                    model,
                ],
                env=env,
                timeout=args.timeout_seconds,
            ).stdout,
            "Occult initialization",
        )
        if initialized.get("enabled") is not True:
            raise CanaryFailure("Occult initialization did not enable the runtime")
        token = load_dotenv_value(primary_home / ".env", "OCCULT_API_KEY")
        if len(token) < 32:
            raise CanaryFailure("Occult service token was not generated")
        checks["explicit_local_initialization"] = "passed"
        checks["default_model_binding"] = "passed"

        try:
            gateway, gateway_log = start_gateway(
                hermes,
                env,
                root / "gateway-first.log",
                args.timeout_seconds,
            )
            status_result = run(
                [str(hermes), "occult", "status"],
                env=env,
                timeout=args.timeout_seconds,
            )
            observed_outputs.append(status_result.stdout + status_result.stderr)
            status = load_json_output(
                status_result.stdout,
                "Occult status",
            )
            routes = status.get("routes", [])
            if not any(
                isinstance(route, dict)
                and route.get("card_id") == STARTER_CARD_ID
                and route.get("local") is True
                and route.get("free") is True
                for route in routes
            ):
                raise CanaryFailure("zero-cost local route is unavailable")

            invocation_result = run(
                [
                    str(hermes),
                    "occult",
                    "invoke",
                    "--agent",
                    "occult.major.magician",
                    "--message",
                    (
                        "Respond with READY only. Do not repeat this marker: "
                        + prompt_marker
                    ),
                    "--mode",
                    "local_only",
                    "--maximum-fallbacks",
                    "0",
                ],
                env=env,
                timeout=args.timeout_seconds,
            )
            observed_outputs.append(invocation_result.stdout + invocation_result.stderr)
            invocation = load_json_output(
                invocation_result.stdout,
                "Occult invocation",
            )
            route = invocation.get("route_summary", {})
            if (
                invocation.get("status") != "completed"
                or not isinstance(route, dict)
                or route.get("selected_card_id") != STARTER_CARD_ID
                or route.get("provider_id") != "ollama-local"
            ):
                raise CanaryFailure("local-only Major Arcana invocation failed")
            checks["zero_cost_major_arcana_invocation"] = "passed"
        finally:
            if gateway is not None and gateway_log is not None:
                stop_gateway(gateway, gateway_log)
                gateway = None
                gateway_log = None

        try:
            gateway, gateway_log = start_gateway(
                hermes,
                env,
                root / "gateway-restarted.log",
                args.timeout_seconds,
            )
            council_env = dict(env)
            council_env.update({
                "OCCULT_E2E_HERMES_URL": "http://127.0.0.1:8642",
                "OCCULT_E2E_HERMES_TOKEN": token,
            })
            packaged_outputs = validate_packaged_council_mcp_flow(
                council,
                root / "packaged-council-mcp",
                env=env,
                hermes_url="http://127.0.0.1:8642",
                service_token=token,
                timeout=args.timeout_seconds * 3,
            )
            for output in packaged_outputs:
                assert_redacted(output, token)
            observed_outputs.extend(packaged_outputs)
            checks["packaged_council_mcp_flow"] = "passed"
            council_result = run(
                [str(bun), "run", "test:occult-hermes-e2e"],
                env=council_env,
                cwd=council_repository,
                timeout=args.timeout_seconds * 3,
            )
            assert_redacted(council_result.stdout + council_result.stderr, token)
            observed_outputs.append(council_result.stdout + council_result.stderr)
            council_evidence = load_json_output(council_result.stdout, "Council E2E")
            if (
                council_evidence.get("contract_version") != CONTRACT_VERSION
                or council_evidence.get("state") != "completed"
                or council_evidence.get("terminal_event") != "reading.completed"
            ):
                raise CanaryFailure("Council reading recovery canary failed")
            checks["gateway_restart"] = "passed"
            checks["council_pause_restart_resume"] = "passed"
        finally:
            if gateway is not None and gateway_log is not None:
                stop_gateway(gateway, gateway_log)
                gateway = None
                gateway_log = None

        run(
            [str(hermes), "backup", "--output", str(backup_path)],
            env=env,
            timeout=args.timeout_seconds,
        )
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise CanaryFailure("backup archive was not created")
        restore_env = dict(env)
        restore_env["HERMES_HOME"] = str(restored_home)
        run(
            [str(hermes), "import", str(backup_path), "--force"],
            env=restore_env,
            timeout=args.timeout_seconds,
        )
        if not (restored_home / "config.yaml").is_file():
            raise CanaryFailure("restored configuration is missing")
        if load_dotenv_value(restored_home / ".env", "OCCULT_API_KEY") != token:
            raise CanaryFailure("restored Occult token does not match locally")
        try:
            gateway, gateway_log = start_gateway(
                hermes,
                restore_env,
                root / "gateway-restored.log",
                args.timeout_seconds,
            )
            restored_status_result = run(
                [str(hermes), "occult", "status"],
                env=restore_env,
                timeout=args.timeout_seconds,
            )
            observed_outputs.append(
                restored_status_result.stdout + restored_status_result.stderr
            )
            restored_status = load_json_output(
                restored_status_result.stdout,
                "restored Occult status",
            )
            if not restored_status.get("agents") or not restored_status.get("routes"):
                raise CanaryFailure("restored Occult runtime is incomplete")
            checks["backup_restore"] = "passed"
        finally:
            if gateway is not None and gateway_log is not None:
                stop_gateway(gateway, gateway_log)

        observed_outputs.extend(
            rehearse_rollback(
                root=root,
                uv=uv,
                requirements_lock=requirements_lock,
                candidate_hermes_wheel=candidate_hermes_wheel,
                candidate_council_archive=candidate_council_archive,
                previous_hermes_wheel=previous_hermes_wheel,
                previous_council_archive=previous_council_archive,
                previous_hermes_cli_version=rollback["hermes_cli_version"],
                previous_hermes_wheel_sha256=rollback["hermes_wheel_sha256"],
                previous_council_version=rollback["council_release_tag"].removeprefix(
                    "v"
                ),
                previous_council_archive_sha256=rollback[
                    "council_windows_x64_sha256"
                ],
                env=env,
                timeout=args.timeout_seconds,
            )
        )
        checks["rollback_previous_checksummed_releases"] = "passed"

        assert_redaction_surfaces(
            text_surfaces=observed_outputs,
            file_surfaces=[
                primary_home / "occult",
                primary_home / "logs",
                restored_home / "occult",
                restored_home / "logs",
                *sorted(root.glob("gateway-*.log")),
            ],
            token=token,
            prompt_marker=prompt_marker,
        )
        checks["audit_redaction"] = "passed"

    for _attempt in range(10):
        if not root.exists():
            break
        shutil.rmtree(root, ignore_errors=True)
        time.sleep(0.5)
    if root.exists():
        raise CanaryFailure("temporary secret directory could not be removed")
    checks["temporary_secret_cleanup"] = "passed"

    public_rerun_complete = args.public_installer_rerun
    report = {
        "schema_version": "1.0.0",
        "scope": (
            "public Windows x64 launch canary"
            if public_rerun_complete
            else "pre-release Windows x64 candidate canary"
        ),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release": {
            "hermes": HERMES_RELEASE,
            "hermes_cli": hermes_version,
            "agents_council": council_version,
            "runtime_contract": CONTRACT_VERSION,
            "council_state_schema": COUNCIL_STATE_SCHEMA,
            "council_commit": council_commit,
            "ollama_model": model,
        },
        "platform": {"os": "Windows", "architecture": "x86_64"},
        "release_artifacts": {
            "install_manifest": {
                "name": install_manifest.name,
                "sha256": sha256_file(install_manifest),
            },
            "hermes_wheel": {
                "name": candidate_hermes_wheel.name,
                "sha256": sha256_file(candidate_hermes_wheel),
            },
            "hermes_requirements_lock": {
                "name": requirements_lock.name,
                "sha256": sha256_file(requirements_lock),
            },
            "sigstore_requirements_lock": {
                "name": sigstore_requirements_lock.name,
                "sha256": sha256_file(sigstore_requirements_lock),
            },
            "installer_powershell": {
                "name": installer_powershell.name,
                "sha256": sha256_file(installer_powershell),
            },
            "installer_posix": {
                "name": installer_posix.name,
                "sha256": sha256_file(installer_posix),
            },
            "environment_verifier": {
                "name": environment_verifier.name,
                "sha256": sha256_file(environment_verifier),
            },
            "council_windows_x64": {
                "name": candidate_council_archive.name,
                "sha256": sha256_file(candidate_council_archive),
            },
        },
        "checks": checks,
        "candidate_status": "passed",
        "promotion_eligible": public_rerun_complete,
        "overall_status": "passed" if public_rerun_complete else "candidate_passed",
        "contains_secrets": False,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if SIGNED_URL.search(encoded):
        raise CanaryFailure("report contains a signed URL")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(encoded, encoding="utf-8", newline="\n")
    status = "public launch" if public_rerun_complete else "release candidate"
    print(f"Tarot Router {status} canary passed; redacted report: {args.report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryFailure as exc:
        print(f"Tarot Router launch canary failed safely: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

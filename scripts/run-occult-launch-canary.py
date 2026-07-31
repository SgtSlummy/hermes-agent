#!/usr/bin/env python3
"""Run the redacted Windows Occult launch canary.

The canary intentionally records only pass/fail facts and public version
metadata. Command output, prompts, tokens, local paths, and response text stay
inside a temporary directory that is removed before the report is written.
"""

from __future__ import annotations

import argparse
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HERMES_CLI_VERSION = "0.14.0"
HERMES_RELEASE = "v1.0.1"
COUNCIL_VERSION = "0.5.2"
CONTRACT_VERSION = "1.0.0"
COUNCIL_STATE_SCHEMA = 3
PREVIOUS_HERMES_CLI_VERSION = "0.14.0"
PREVIOUS_HERMES_WHEEL_SHA256 = (
    "9c5621f70171ffb965f64e19d789813fe024b6b32c6f03e3269a530a2a8b3a71"
)
PREVIOUS_COUNCIL_VERSION = "0.5.1"
PREVIOUS_COUNCIL_ARCHIVE_SHA256 = (
    "22a7c585a65680c7e99d8236bb4038f8c70e9ec7922c0759b42620546aaf9eec"
)
STARTER_CARD_ID = "minor.pentacles.ace.ollama.local"
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SIGNED_URL = re.compile(r"https?://\S+(?:signature|sigstore|x-amz-|token=)", re.I)


class CanaryFailure(RuntimeError):
    """A safe, non-secret canary failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the redacted Occult v1.0.1 Windows launch canary."
    )
    parser.add_argument("--hermes-executable", type=Path, required=True)
    parser.add_argument("--council-executable", type=Path, required=True)
    parser.add_argument("--council-repository", type=Path, required=True)
    parser.add_argument("--bun-executable", type=Path, required=True)
    parser.add_argument("--uv-executable", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--previous-hermes-wheel", type=Path, required=True)
    parser.add_argument("--previous-council-archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--ollama-base-url",
        default="http://127.0.0.1:11434/v1",
    )
    parser.add_argument("--model", default="qwen2.5:3b")
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
        raise CanaryFailure(
            f"command failed its expected outcome: {Path(command[0]).name}"
        )
    return completed


def assert_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CanaryFailure(f"{label} is missing")
    return resolved


def assert_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise CanaryFailure(f"{label} is missing")
    return resolved


def assert_sha256(path: Path, expected: str, label: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
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
                raise CanaryFailure("previous Council archive contains an unsafe path")
            target = destination.joinpath(*parts).resolve()
            if not target.is_relative_to(destination_root):
                raise CanaryFailure("previous Council archive escapes its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise CanaryFailure("previous Council archive contains a special file")
            source = archive.extractfile(member)
            if source is None:
                raise CanaryFailure("previous Council archive could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def rehearse_rollback(
    *,
    root: Path,
    uv: Path,
    requirements_lock: Path,
    previous_hermes_wheel: Path,
    previous_council_archive: Path,
    candidate_hermes: Path,
    candidate_council: Path,
    env: dict[str, str],
    timeout: int,
) -> None:
    assert_sha256(
        previous_hermes_wheel,
        PREVIOUS_HERMES_WHEEL_SHA256,
        "previous Hermes wheel",
    )
    assert_sha256(
        previous_council_archive,
        PREVIOUS_COUNCIL_ARCHIVE_SHA256,
        "previous Council archive",
    )
    rollback_root = root / "rollback-rehearsal"
    rollback_venv = rollback_root / "hermes-venv"
    run(
        [
            str(uv),
            "venv",
            "--no-config",
            "--clear",
            "--python",
            "3.11",
            str(rollback_venv),
        ],
        env=env,
        timeout=timeout,
    )
    rollback_python = rollback_venv / "Scripts" / "python.exe"
    run(
        [
            str(uv),
            "pip",
            "sync",
            "--no-config",
            "--python",
            str(rollback_python),
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
            str(rollback_python),
            "--no-deps",
            "--no-index",
            str(previous_hermes_wheel),
        ],
        env=env,
        timeout=timeout,
    )
    previous_hermes = rollback_venv / "Scripts" / "hermes.exe"
    safe_public_version(
        run([str(previous_hermes), "--version"], env=env, timeout=timeout).stdout,
        PREVIOUS_HERMES_CLI_VERSION,
        "previous Hermes",
    )

    previous_council_root = rollback_root / "council"
    extract_regular_tar(previous_council_archive, previous_council_root)
    previous_council = previous_council_root / "cli" / "council.exe"
    safe_public_version(
        run([str(previous_council), "--version"], env=env, timeout=timeout).stdout,
        PREVIOUS_COUNCIL_VERSION,
        "previous Council",
    )

    safe_public_version(
        run([str(candidate_hermes), "--version"], env=env, timeout=timeout).stdout,
        HERMES_CLI_VERSION,
        "restored Hermes",
    )
    safe_public_version(
        run([str(candidate_council), "--version"], env=env, timeout=timeout).stdout,
        COUNCIL_VERSION,
        "restored Council",
    )


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


def main() -> int:
    args = parse_args()
    if platform.system() != "Windows":
        raise CanaryFailure("the v1 launch canary must run on Windows")
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise CanaryFailure("the v1 launch canary requires Windows x64")
    if not 30 <= args.timeout_seconds <= 900:
        raise CanaryFailure("timeout must be from 30 to 900 seconds")

    hermes = assert_file(args.hermes_executable, "Hermes executable")
    council = assert_file(args.council_executable, "Council executable")
    bun = assert_file(args.bun_executable, "Bun executable")
    uv = assert_file(args.uv_executable, "uv executable")
    requirements_lock = assert_file(
        args.requirements_lock,
        "Hermes requirements lock",
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
    if not (council_repository / "scripts" / "occultHermesE2E.ts").is_file():
        raise CanaryFailure("Council cross-repository E2E script is missing")
    assert_port_available("127.0.0.1", 8642)
    validate_ollama(args.ollama_base_url, args.model, args.timeout_seconds)

    checks: dict[str, str] = {}
    gateway: subprocess.Popen[bytes] | None = None
    gateway_log: Any | None = None
    with tempfile.TemporaryDirectory(
        prefix="occult-v101-canary-",
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

        run(
            [str(hermes), "occult", "status"],
            env=env,
            timeout=args.timeout_seconds,
            expect_success=False,
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
                    args.model,
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

        try:
            gateway, gateway_log = start_gateway(
                hermes,
                env,
                root / "gateway-first.log",
                args.timeout_seconds,
            )
            status = load_json_output(
                run(
                    [str(hermes), "occult", "status"],
                    env=env,
                    timeout=args.timeout_seconds,
                ).stdout,
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

            invocation = load_json_output(
                run(
                    [
                        str(hermes),
                        "occult",
                        "invoke",
                        "--agent",
                        "occult.major.magician",
                        "--message",
                        "Return a concise local launch readiness confirmation.",
                        "--mode",
                        "local_only",
                        "--maximum-fallbacks",
                        "0",
                    ],
                    env=env,
                    timeout=args.timeout_seconds,
                ).stdout,
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
            council_result = run(
                [str(bun), "run", "test:occult-hermes-e2e"],
                env=council_env,
                cwd=council_repository,
                timeout=args.timeout_seconds * 3,
            )
            assert_redacted(council_result.stdout + council_result.stderr, token)
            council_evidence = load_json_output(council_result.stdout, "Council E2E")
            if (
                council_evidence.get("contract_version") != CONTRACT_VERSION
                or council_evidence.get("state") != "completed"
                or council_evidence.get("terminal_event") != "reading.completed"
            ):
                raise CanaryFailure("Council reading recovery canary failed")
            checks["gateway_restart"] = "passed"
            checks["council_pause_restart_resume"] = "passed"
            checks["audit_redaction"] = "passed"
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
            restored_status = load_json_output(
                run(
                    [str(hermes), "occult", "status"],
                    env=restore_env,
                    timeout=args.timeout_seconds,
                ).stdout,
                "restored Occult status",
            )
            if not restored_status.get("agents") or not restored_status.get("routes"):
                raise CanaryFailure("restored Occult runtime is incomplete")
            checks["backup_restore"] = "passed"
        finally:
            if gateway is not None and gateway_log is not None:
                stop_gateway(gateway, gateway_log)

        rehearse_rollback(
            root=root,
            uv=uv,
            requirements_lock=requirements_lock,
            previous_hermes_wheel=previous_hermes_wheel,
            previous_council_archive=previous_council_archive,
            candidate_hermes=hermes,
            candidate_council=council,
            env=env,
            timeout=args.timeout_seconds,
        )
        checks["rollback_previous_checksummed_releases"] = "passed"

    for _attempt in range(10):
        if not root.exists():
            break
        shutil.rmtree(root, ignore_errors=True)
        time.sleep(0.5)
    if root.exists():
        raise CanaryFailure("temporary secret directory could not be removed")
    checks["temporary_secret_cleanup"] = "passed"

    report = {
        "schema_version": "1.0.0",
        "scope": "pre-release Windows x64 candidate canary",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release": {
            "hermes": HERMES_RELEASE,
            "hermes_cli": hermes_version,
            "agents_council": council_version,
            "runtime_contract": CONTRACT_VERSION,
            "council_state_schema": COUNCIL_STATE_SCHEMA,
        },
        "platform": {"os": "Windows", "architecture": "x86_64"},
        "checks": checks,
        "overall_status": "passed",
        "contains_secrets": False,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if SIGNED_URL.search(encoded):
        raise CanaryFailure("report contains a signed URL")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(encoded, encoding="utf-8", newline="\n")
    print(f"Occult launch canary passed; redacted report: {args.report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryFailure as exc:
        print(f"Occult launch canary failed safely: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

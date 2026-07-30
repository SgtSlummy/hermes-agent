"""Deterministic Occult release assembly, verification, and promotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.occult.contracts import OCCULT_CONTRACT_VERSION

CHECKSUM_FILE = "SHA256SUMS.txt"
COMPATIBILITY_FILE = "occult-compatibility.json"
MANIFEST_FILE = "occult-release-manifest.json"
MIGRATIONS_FILE = "occult-migrations.json"
PROVENANCE_FILE = "occult-provenance.intoto.jsonl"
SBOM_FILE = "occult-sbom.cdx.json"
SIGNATURE_FILE = f"{CHECKSUM_FILE}.sigstore.json"

_TEXT_EXTENSIONS = frozenset({
    ".json",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".js",
    ".mjs",
    ".py",
})
_FORBIDDEN_NAMES = frozenset({".env", "credentials.json", "secrets.json"})
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"""["'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)["']"""
        r"""\s*[:=]\s*["'][^<${\s][^"']{11,}["']""",
        re.IGNORECASE,
    ),
)


class OccultReleaseError(ValueError):
    """A safe-to-surface release assembly or verification failure."""


def assemble_release(
    artifact_root: Path,
    output_root: Path,
    *,
    version: str,
    commit_sha: str,
    channel: str,
    source_date_epoch: int,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Copy staged artifacts once and add deterministic release metadata."""

    artifact_root = artifact_root.resolve()
    output_root = output_root.resolve()
    source_root = (source_root or Path(__file__).resolve().parents[2]).resolve()
    _validate_release_identity(version, commit_sha, channel, source_date_epoch)
    if not artifact_root.is_dir():
        raise OccultReleaseError("artifact root must be a directory")
    if output_root.exists() and any(output_root.iterdir()):
        raise OccultReleaseError("release output directory must be empty")
    if output_root == artifact_root or output_root.is_relative_to(artifact_root):
        raise OccultReleaseError("release output must be outside the artifact root")

    source_files = tuple(_release_files(artifact_root))
    if not source_files:
        raise OccultReleaseError("release requires at least one compiled artifact")
    output_root.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []
    for source in source_files:
        relative = source.relative_to(artifact_root).as_posix()
        _assert_safe_artifact(source, relative)
        destination = output_root / "artifacts" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.utime(destination, (source_date_epoch, source_date_epoch))
        copied.append(_file_descriptor(output_root, destination))

    generated_at = datetime.fromtimestamp(source_date_epoch, UTC).isoformat()
    compatibility = {
        "schema_version": "1.0.0",
        "release_version": version,
        "channel": channel,
        "occult_contract_versions": [OCCULT_CONTRACT_VERSION],
        "agents_council": {"minimum_version": "0.4.0"},
        "feature_gate": {"config": "occult.enabled", "default": False},
        "default_policy": {
            "local_first": True,
            "free_only": True,
            "paid_fallback": False,
            "maximum_cost_usd": 0,
        },
        "platforms": ["linux", "macos", "windows"],
    }
    migrations = {
        "schema_version": "1.0.0",
        "release_version": version,
        "data_loss_expected": False,
        "rollback_supported": True,
        "rollback_requires_backup_restore": ["readings-sqlite"],
        "state": [
            {"name": "mythos-state", "version": 1},
            {"name": "virtual-tokens-sqlite", "version": 1},
            {"name": "readings-sqlite", "version": 2},
            {"name": "invocation-results-sqlite", "version": 1},
            {"name": "decks-json", "version": 1},
        ],
        "backup_set": [
            "occult/mythos-state.json",
            "occult/virtual_tokens.db*",
            "occult/readings.db*",
            "occult/invocations.db*",
            "occult/decks.json",
        ],
    }
    components, materials = _dependency_inventory(source_root)
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:"
        + str(
            uuid.UUID(
                hex=hashlib.sha256(
                    f"{version}:{commit_sha}:{source_date_epoch}".encode()
                ).hexdigest()[:32]
            )
        ),
        "version": 1,
        "metadata": {
            "timestamp": generated_at,
            "component": {
                "type": "application",
                "name": "hermes-occult",
                "version": version,
            },
        },
        "components": components,
    }
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": item["path"], "digest": {"sha256": item["sha256"]}}
            for item in copied
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": (
                    "https://github.com/SgtSlummy/hermes-agent/occult-release/v1"
                ),
                "externalParameters": {
                    "version": version,
                    "channel": channel,
                    "source_date_epoch": source_date_epoch,
                },
                "resolvedDependencies": materials,
            },
            "runDetails": {
                "builder": {"id": "SgtSlummy/hermes-agent"},
                "metadata": {"invocationId": commit_sha},
            },
        },
    }
    manifest = {
        "schema_version": "1.0.0",
        "release_version": version,
        "channel": channel,
        "commit_sha": commit_sha,
        "generated_at": generated_at,
        "source_date_epoch": source_date_epoch,
        "artifacts": copied,
        "metadata": [
            COMPATIBILITY_FILE,
            MIGRATIONS_FILE,
            PROVENANCE_FILE,
            SBOM_FILE,
        ],
        "signing": {
            "scheme": "sigstore",
            "subject": CHECKSUM_FILE,
            "bundle": SIGNATURE_FILE,
            "required_for_stable": True,
        },
    }

    _write_json(output_root / COMPATIBILITY_FILE, compatibility, source_date_epoch)
    _write_json(output_root / MIGRATIONS_FILE, migrations, source_date_epoch)
    _write_json(output_root / SBOM_FILE, sbom, source_date_epoch)
    _write_jsonl(output_root / PROVENANCE_FILE, provenance, source_date_epoch)
    _write_json(output_root / MANIFEST_FILE, manifest, source_date_epoch)
    _write_checksums(output_root, source_date_epoch)
    return manifest


def verify_release(
    release_root: Path,
    *,
    require_signature: bool | None = None,
) -> dict[str, Any]:
    """Verify all staged bytes before signing or promotion."""

    release_root = release_root.resolve()
    manifest = _load_json_object(release_root / MANIFEST_FILE)
    channel = manifest.get("channel")
    signature_required = (
        channel == "stable" if require_signature is None else require_signature
    )
    expected = _load_checksums(release_root / CHECKSUM_FILE)
    actual_paths = {
        path.relative_to(release_root).as_posix()
        for path in _release_files(release_root)
        if path.name not in {CHECKSUM_FILE, SIGNATURE_FILE}
    }
    if set(expected) != actual_paths:
        raise OccultReleaseError("release checksum inventory does not match files")
    for relative, digest in expected.items():
        path = _safe_release_child(release_root, relative)
        if _sha256(path) != digest:
            raise OccultReleaseError(f"release checksum mismatch: {relative}")
    if signature_required:
        _validate_signature_bundle(release_root / SIGNATURE_FILE)
    return manifest


def promote_release(
    staged_root: Path,
    destination: Path,
    *,
    require_signature: bool = True,
) -> dict[str, Any]:
    """Promote the exact verified staged bytes without rebuilding."""

    staged_root = staged_root.resolve()
    destination = destination.resolve()
    manifest = verify_release(staged_root, require_signature=require_signature)
    if manifest.get("channel") == "stable" and not require_signature:
        raise OccultReleaseError("stable promotion requires a Sigstore bundle")
    if destination.exists():
        raise OccultReleaseError("promotion destination already exists")
    shutil.copytree(staged_root, destination, copy_function=shutil.copyfile)
    promoted = verify_release(destination, require_signature=require_signature)
    if _tree_digests(staged_root) != _tree_digests(destination):
        raise OccultReleaseError("promoted bytes differ from staged bytes")
    return promoted


def _dependency_inventory(
    source_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packages: set[tuple[str, str, str]] = set()
    materials: list[dict[str, Any]] = []
    uv_lock = source_root / "uv.lock"
    if uv_lock.is_file():
        payload = tomllib.loads(uv_lock.read_text(encoding="utf-8"))
        for package in payload.get("package", ()):
            name = package.get("name")
            version = package.get("version")
            if name and version:
                packages.add(("library", str(name), str(version)))
        materials.append(_material(source_root, uv_lock))
    for lock in (
        source_root / "web" / "package-lock.json",
        source_root / "ui-tui" / "package-lock.json",
        source_root / "website" / "package-lock.json",
    ):
        if not lock.is_file():
            continue
        payload = json.loads(lock.read_text(encoding="utf-8"))
        for key, package in payload.get("packages", {}).items():
            if not isinstance(package, dict):
                continue
            name = package.get("name") or str(key).rsplit("node_modules/", 1)[-1]
            version = package.get("version")
            if name and version:
                packages.add(("library", str(name), str(version)))
        materials.append(_material(source_root, lock))
    components = [
        {
            "type": kind,
            "name": name,
            "version": version,
            "bom-ref": f"pkg:{name}@{version}",
        }
        for kind, name, version in sorted(packages)
    ]
    return components, sorted(materials, key=lambda item: item["uri"])


def _material(root: Path, path: Path) -> dict[str, Any]:
    return {
        "uri": path.relative_to(root).as_posix(),
        "digest": {"sha256": _sha256(path)},
    }


def _validate_release_identity(
    version: str,
    commit_sha: str,
    channel: str,
    source_date_epoch: int,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version):
        raise OccultReleaseError("invalid release version")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise OccultReleaseError("commit SHA must contain 40 lowercase hex characters")
    if channel not in {"nightly", "preview", "stable"}:
        raise OccultReleaseError("invalid release channel")
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or source_date_epoch < 0
    ):
        raise OccultReleaseError("SOURCE_DATE_EPOCH must be non-negative")


def _assert_safe_artifact(path: Path, relative: str) -> None:
    lowered = path.name.lower()
    if (
        lowered in _FORBIDDEN_NAMES
        or lowered.endswith((".key", ".pem", ".p12", ".pfx"))
        or "credential" in lowered
        or "secret" in lowered
    ):
        raise OccultReleaseError(f"forbidden sensitive release file: {relative}")
    if path.suffix.lower() not in _TEXT_EXTENSIONS or path.stat().st_size > 5_000_000:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise OccultReleaseError(f"secret-shaped release content: {relative}")


def _write_json(path: Path, payload: Any, epoch: int) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.utime(path, (epoch, epoch))


def _write_jsonl(path: Path, payload: Any, epoch: int) -> None:
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.utime(path, (epoch, epoch))


def _write_checksums(root: Path, epoch: int) -> None:
    lines = []
    for path in _release_files(root):
        if path.name in {CHECKSUM_FILE, SIGNATURE_FILE}:
            continue
        lines.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}")
    checksum = root / CHECKSUM_FILE
    checksum.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.utime(checksum, (epoch, epoch))


def _load_checksums(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OccultReleaseError("release checksum file is missing")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if not match or match.group(2) in result:
            raise OccultReleaseError("invalid release checksum file")
        result[match.group(2)] = match.group(1)
    if not result:
        raise OccultReleaseError("release checksum file is empty")
    return result


def _validate_signature_bundle(path: Path) -> None:
    payload = _load_json_object(path)
    media_type = payload.get("mediaType")
    if not isinstance(media_type, str) or "sigstore.bundle" not in media_type:
        raise OccultReleaseError("stable release requires a Sigstore bundle")
    if not isinstance(payload.get("verificationMaterial"), dict):
        raise OccultReleaseError("Sigstore bundle lacks verification material")
    if not isinstance(payload.get("messageSignature"), dict):
        raise OccultReleaseError("Sigstore bundle lacks a message signature")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise OccultReleaseError(f"invalid release metadata: {path.name}") from None
    if not isinstance(payload, dict):
        raise OccultReleaseError(f"invalid release metadata: {path.name}")
    return payload


def _safe_release_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if (
        candidate == root
        or not candidate.is_relative_to(root)
        or not candidate.is_file()
    ):
        raise OccultReleaseError("invalid release checksum path")
    return candidate


def _release_files(root: Path):
    return sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _file_descriptor(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in _release_files(root)
    }


__all__ = [
    "CHECKSUM_FILE",
    "COMPATIBILITY_FILE",
    "MANIFEST_FILE",
    "MIGRATIONS_FILE",
    "OccultReleaseError",
    "PROVENANCE_FILE",
    "SBOM_FILE",
    "SIGNATURE_FILE",
    "assemble_release",
    "promote_release",
    "verify_release",
]

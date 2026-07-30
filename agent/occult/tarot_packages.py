"""Signed, profile-scoped Major Arcana package lifecycle.

The package manager is inert until explicitly constructed. It accepts only
data files, validates every archive member before extraction, verifies an
Ed25519 signature against caller-supplied trusted signers, and installs
immutable versions under the active Hermes profile.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hermes_constants import get_hermes_home

TAROT_FORMAT_VERSION = "1.0"
_SIGNATURE_DOMAIN = b"OCCULT-TAROT-SIGNATURE-v1\n"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_ALLOWED_SUFFIXES = frozenset({
    ".json",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
})
_REQUIRED_FILES = frozenset({
    "manifest.yaml",
    "system_prompt.md",
    "behavior.yaml",
    "routing.yaml",
    "memory.yaml",
    "tools.yaml",
    "signature.json",
})
_SENSITIVITY_ORDER = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


class TarotPackageError(ValueError):
    """Safe-to-surface package validation or lifecycle failure."""


class TemperamentAxis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default: float = Field(ge=0, le=1)
    minimum: float = Field(ge=0, le=1)
    maximum: float = Field(ge=0, le=1)

    def model_post_init(self, __context: Any) -> None:
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError("temperament default must be within its range")


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = Field(min_length=1, max_length=120)
    arcana_number: int = Field(ge=0, le=21)
    version: str
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("invalid agent id")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must use MAJOR.MINOR.PATCH")
        return value


class OrientationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upright: bool = True
    reversed: bool = False


class PermissionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_risk_level: int = Field(ge=0, le=3)


class EntrypointDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str
    behavior: str
    routing: str
    memory: str
    tools: str


class TarotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1.0"]
    agent: AgentDefinition
    orientation: OrientationDefinition
    capabilities: tuple[str, ...] = Field(min_length=1)
    temperament: dict[str, TemperamentAxis] = Field(min_length=1)
    permissions: PermissionDefinition
    entrypoints: EntrypointDefinition

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not _SAFE_ID.fullmatch(item) for item in value
        ):
            raise ValueError("capabilities must be unique safe identifiers")
        return value

    @field_validator("temperament")
    @classmethod
    def validate_temperament(
        cls, value: dict[str, TemperamentAxis]
    ) -> dict[str, TemperamentAxis]:
        if any(not _SAFE_ID.fullmatch(axis) for axis in value):
            raise ValueError("invalid temperament axis")
        return value


class BehaviorDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upright: str = Field(min_length=1, max_length=10000)
    reversed: str | None = Field(default=None, max_length=10000)


class RoutingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_capabilities: tuple[str, ...] = ("text",)
    allow_paid: bool = False
    allow_external: bool = False


class MemoryDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespaces: tuple[str, ...] = ()
    maximum_sensitivity: Literal["public", "internal", "confidential", "restricted"] = (
        "internal"
    )
    external_maximum_sensitivity: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "public"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: tuple[str, ...] = ()
    approval_required: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemPackagePolicy:
    """Global ceilings that a Major Arcana package cannot expand."""

    available_tools: frozenset[str] = frozenset()
    maximum_risk_level: int = 0
    allow_paid: bool = False
    allow_external: bool = False
    maximum_memory_sensitivity: str = "internal"
    external_maximum_sensitivity: str = "public"

    def __post_init__(self) -> None:
        if not 0 <= self.maximum_risk_level <= 3:
            raise ValueError("maximum_risk_level must be between 0 and 3")
        for value in (
            self.maximum_memory_sensitivity,
            self.external_maximum_sensitivity,
        ):
            if value not in _SENSITIVITY_ORDER:
                raise ValueError("invalid system memory sensitivity")


@dataclass(frozen=True, slots=True)
class ValidatedTarotPackage:
    manifest: TarotManifest
    behavior: BehaviorDefinition
    routing: RoutingDefinition
    memory: MemoryDefinition
    tools: ToolDefinition
    system_prompt: str
    signer_id: str
    files: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class InstalledTarotPackage:
    package: ValidatedTarotPackage
    path: Path


def _safe_yaml(data: bytes, name: str) -> Mapping[str, Any]:
    try:
        parsed = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise TarotPackageError(f"invalid {name}") from None
    if not isinstance(parsed, Mapping):
        raise TarotPackageError(f"{name} must contain an object")
    return parsed


def _model(model_type: type[BaseModel], data: bytes, name: str):
    try:
        return model_type.model_validate(_safe_yaml(data, name))
    except ValidationError as exc:
        fields = sorted({
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        })
        locations = ", ".join(fields) or "payload"
        raise TarotPackageError(f"invalid {name} fields: {locations}") from None


def signature_payload(
    signer_id: str,
    file_hashes: Mapping[str, str],
) -> bytes:
    """Return the deterministic bytes signed by an Ed25519 package signer."""

    if not _SAFE_ID.fullmatch(signer_id):
        raise TarotPackageError("invalid signer id")
    payload = {
        "algorithm": "ed25519",
        "format_version": TAROT_FORMAT_VERSION,
        "signer_id": signer_id,
        "files": dict(sorted(file_hashes.items())),
    }
    return _SIGNATURE_DOMAIN + json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_archive_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or ":" in name:
        raise TarotPackageError("unsafe archive member")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise TarotPackageError("unsafe archive member")
    if len(path.parts) > 8 or len(name) > 240:
        raise TarotPackageError("archive member path is too deep or long")
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise TarotPackageError("executable or unsupported package file")
    return path.as_posix()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


class TarotPackageManager:
    """Validate and manage immutable Major Arcana package versions."""

    def __init__(
        self,
        *,
        trusted_signers: Mapping[str, bytes],
        system_policy: SystemPackagePolicy,
        root: Path | None = None,
        maximum_archive_bytes: int = 5 * 1024 * 1024,
        maximum_uncompressed_bytes: int = 20 * 1024 * 1024,
        maximum_entries: int = 100,
        maximum_compression_ratio: float = 100.0,
    ) -> None:
        self.root = root or (get_hermes_home() / "occult" / "major_arcana")
        self.trusted_signers = dict(trusted_signers)
        self.system_policy = system_policy
        self.maximum_archive_bytes = maximum_archive_bytes
        self.maximum_uncompressed_bytes = maximum_uncompressed_bytes
        self.maximum_entries = maximum_entries
        self.maximum_compression_ratio = maximum_compression_ratio
        self.packages_root = self.root / "packages"
        self.quarantine_root = self.root / "quarantine"
        self.registry_path = self.root / "registry.json"

    def validate(self, archive_path: Path) -> ValidatedTarotPackage:
        try:
            archive_size = archive_path.stat().st_size
        except OSError:
            raise TarotPackageError("package cannot be read") from None
        if archive_path.suffix.lower() != ".tarot":
            raise TarotPackageError("package must use the .tarot extension")
        if archive_size <= 0 or archive_size > self.maximum_archive_bytes:
            raise TarotPackageError("package archive exceeds size limit")

        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > self.maximum_entries:
                    raise TarotPackageError("package archive has too many entries")
                files: dict[str, bytes] = {}
                total_size = 0
                for info in infos:
                    if info.is_dir():
                        continue
                    name = _safe_archive_name(info.filename)
                    if name in files or _is_symlink(info):
                        raise TarotPackageError("duplicate or linked archive member")
                    total_size += info.file_size
                    if total_size > self.maximum_uncompressed_bytes:
                        raise TarotPackageError("package expands beyond size limit")
                    ratio = info.file_size / max(info.compress_size, 1)
                    if (
                        info.file_size > 1024 * 1024
                        and ratio > self.maximum_compression_ratio
                    ):
                        raise TarotPackageError("package compression ratio is unsafe")
                    files[name] = archive.read(info)
        except (OSError, zipfile.BadZipFile, RuntimeError):
            raise TarotPackageError("invalid package archive") from None

        missing = sorted(_REQUIRED_FILES - files.keys())
        if missing:
            raise TarotPackageError(
                f"package is missing required files: {', '.join(missing)}"
            )
        signature = self._verify_signature(files)
        manifest = _model(TarotManifest, files["manifest.yaml"], "manifest")
        behavior = _model(BehaviorDefinition, files["behavior.yaml"], "behavior")
        routing = _model(RoutingDefinition, files["routing.yaml"], "routing")
        memory = _model(MemoryDefinition, files["memory.yaml"], "memory")
        tools = _model(ToolDefinition, files["tools.yaml"], "tools")
        try:
            system_prompt = files["system_prompt.md"].decode("utf-8").strip()
        except UnicodeDecodeError:
            raise TarotPackageError("invalid system prompt") from None
        if not system_prompt or len(system_prompt) > 50000:
            raise TarotPackageError("invalid system prompt")

        self._validate_entrypoints(manifest)
        self._validate_policy(manifest, behavior, routing, memory, tools)
        return ValidatedTarotPackage(
            manifest=manifest,
            behavior=behavior,
            routing=routing,
            memory=memory,
            tools=tools,
            system_prompt=system_prompt,
            signer_id=signature["signer_id"],
            files=files,
        )

    def install(self, archive_path: Path) -> InstalledTarotPackage:
        package = self.validate(archive_path)
        agent_id = package.manifest.agent.id
        version = package.manifest.agent.version
        destination = self.packages_root / agent_id / version
        if destination.exists():
            raise TarotPackageError("package version is already installed")

        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="tarot-", dir=self.quarantine_root))
        try:
            for name, data in package.files.items():
                target = staging.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return InstalledTarotPackage(package=package, path=destination)

    def activate(self, agent_id: str, version: str) -> InstalledTarotPackage:
        installed = self.load(agent_id, version)
        registry = self._registry()
        registry.setdefault("active", {})[agent_id] = version
        registry["generation"] = int(registry.get("generation", 0)) + 1
        self._write_registry(registry)
        return installed

    def deactivate(self, agent_id: str) -> None:
        safe_id = self._safe_component(agent_id, "agent id")
        registry = self._registry()
        registry.setdefault("active", {}).pop(safe_id, None)
        registry["generation"] = int(registry.get("generation", 0)) + 1
        self._write_registry(registry)

    def rollback(self, agent_id: str) -> InstalledTarotPackage:
        safe_id = self._safe_component(agent_id, "agent id")
        registry = self._registry()
        current = registry.get("active", {}).get(safe_id)
        if current is None:
            raise TarotPackageError("agent is not active")
        versions = sorted(
            (
                path.name
                for path in (self.packages_root / safe_id).glob("*")
                if path.is_dir() and _SEMVER.fullmatch(path.name)
            ),
            key=self._version_tuple,
        )
        previous = [
            version
            for version in versions
            if self._version_tuple(version) < self._version_tuple(current)
        ]
        if not previous:
            raise TarotPackageError("no previous package version is installed")
        return self.activate(safe_id, previous[-1])

    def active(self, agent_id: str) -> InstalledTarotPackage | None:
        safe_id = self._safe_component(agent_id, "agent id")
        version = self._registry().get("active", {}).get(safe_id)
        return self.load(safe_id, version) if version else None

    def load(self, agent_id: str, version: str) -> InstalledTarotPackage:
        safe_id = self._safe_component(agent_id, "agent id")
        safe_version = self._safe_component(version, "version")
        if not _SEMVER.fullmatch(safe_version):
            raise TarotPackageError("invalid version")
        directory = self.packages_root / safe_id / safe_version
        if not directory.is_dir():
            raise TarotPackageError("package version is not installed")
        files = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }
        package = self._validate_files(files)
        if (
            package.manifest.agent.id != safe_id
            or package.manifest.agent.version != safe_version
        ):
            raise TarotPackageError("installed package identity mismatch")
        return InstalledTarotPackage(package=package, path=directory)

    def generation(self) -> int:
        return int(self._registry().get("generation", 0))

    def active_packages(self) -> tuple[InstalledTarotPackage, ...]:
        """Return all active packages in stable agent-id order."""

        active = self._registry().get("active", {})
        return tuple(
            self.load(agent_id, version) for agent_id, version in sorted(active.items())
        )

    def _validate_files(self, files: Mapping[str, bytes]) -> ValidatedTarotPackage:
        with tempfile.TemporaryDirectory(prefix="tarot-load-") as temp:
            archive_path = Path(temp) / "package.tarot"
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            return self.validate(archive_path)

    def _verify_signature(self, files: Mapping[str, bytes]) -> Mapping[str, Any]:
        try:
            signature = json.loads(files["signature.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TarotPackageError("invalid package signature") from None
        if not isinstance(signature, Mapping):
            raise TarotPackageError("invalid package signature")
        if set(signature) != {
            "algorithm",
            "format_version",
            "signer_id",
            "files",
            "signature",
        }:
            raise TarotPackageError("invalid package signature fields")
        if (
            signature["algorithm"] != "ed25519"
            or signature["format_version"] != TAROT_FORMAT_VERSION
            or not isinstance(signature["files"], Mapping)
        ):
            raise TarotPackageError("unsupported package signature")
        signer_id = str(signature["signer_id"])
        public_key = self.trusted_signers.get(signer_id)
        if public_key is None:
            raise TarotPackageError("package signer is not trusted")

        expected_hashes = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in files.items()
            if name != "signature.json"
        }
        declared_hashes = dict(signature["files"])
        if declared_hashes != expected_hashes:
            raise TarotPackageError("package file hashes do not match signature")
        try:
            signature_bytes = base64.b64decode(signature["signature"], validate=True)
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature_bytes,
                signature_payload(signer_id, declared_hashes),
            )
        except (ValueError, TypeError, InvalidSignature):
            raise TarotPackageError("invalid package signature") from None
        return signature

    def _validate_entrypoints(self, manifest: TarotManifest) -> None:
        expected = {
            "system_prompt": "system_prompt.md",
            "behavior": "behavior.yaml",
            "routing": "routing.yaml",
            "memory": "memory.yaml",
            "tools": "tools.yaml",
        }
        if manifest.entrypoints.model_dump() != expected:
            raise TarotPackageError("manifest entrypoints must use canonical files")

    def _validate_policy(
        self,
        manifest: TarotManifest,
        behavior: BehaviorDefinition,
        routing: RoutingDefinition,
        memory: MemoryDefinition,
        tools: ToolDefinition,
    ) -> None:
        if manifest.orientation.reversed and behavior.reversed is None:
            raise TarotPackageError("reversed orientation requires reversed behavior")
        if (
            manifest.permissions.maximum_risk_level
            > self.system_policy.maximum_risk_level
        ):
            raise TarotPackageError("package requests excessive risk permission")
        if routing.allow_paid and not self.system_policy.allow_paid:
            raise TarotPackageError("package cannot enable paid routes")
        if routing.allow_external and not self.system_policy.allow_external:
            raise TarotPackageError("package cannot enable external routes")
        if not set(routing.required_capabilities).issubset(manifest.capabilities):
            raise TarotPackageError("routing requires undeclared capability")
        if (
            _SENSITIVITY_ORDER[memory.maximum_sensitivity]
            > _SENSITIVITY_ORDER[self.system_policy.maximum_memory_sensitivity]
            or _SENSITIVITY_ORDER[memory.external_maximum_sensitivity]
            > _SENSITIVITY_ORDER[self.system_policy.external_maximum_sensitivity]
        ):
            raise TarotPackageError("package requests excessive memory sensitivity")
        allowed = set(tools.allowed)
        approval_required = set(tools.approval_required)
        if not approval_required.issubset(allowed):
            raise TarotPackageError("approval-required tools must also be allowed")
        unknown = sorted(allowed - self.system_policy.available_tools)
        if unknown:
            raise TarotPackageError(
                f"package references unknown tools: {', '.join(unknown)}"
            )

    def _registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"version": 1, "generation": 0, "active": {}}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise TarotPackageError("invalid package registry") from None
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("active"), dict)
        ):
            raise TarotPackageError("invalid package registry")
        return data

    def _write_registry(self, registry: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".registry.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(registry, handle, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, self.registry_path)

    @staticmethod
    def _safe_component(value: str, field: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_ID.fullmatch(normalized):
            raise TarotPackageError(f"invalid {field}")
        return normalized

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, int, int]:
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise TarotPackageError("invalid version")
        major, minor, patch = match.groups()
        return int(major), int(minor), int(patch)


__all__ = [
    "AgentDefinition",
    "BehaviorDefinition",
    "InstalledTarotPackage",
    "MemoryDefinition",
    "PermissionDefinition",
    "RoutingDefinition",
    "SystemPackagePolicy",
    "TAROT_FORMAT_VERSION",
    "TarotManifest",
    "TarotPackageError",
    "TarotPackageManager",
    "TemperamentAxis",
    "ToolDefinition",
    "ValidatedTarotPackage",
    "signature_payload",
]

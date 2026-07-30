import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.occult.mythos import MinorArcanaDescriptor, PrivacyClass
from agent.occult.pairing import (
    MemoryRecord,
    PairingError,
    PairingSession,
    RuntimePolicy,
    ToolAuthorization,
)
from agent.occult.tarot_packages import (
    SystemPackagePolicy,
    TarotPackageError,
    TarotPackageManager,
    signature_payload,
)


def _package_files(version: str = "1.0.0") -> dict[str, bytes]:
    manifest = {
        "format_version": "1.0",
        "agent": {
            "id": "occult.major.magician",
            "name": "The Magician",
            "arcana_number": 1,
            "version": version,
            "description": "Builds deliberate systems.",
        },
        "orientation": {"upright": True, "reversed": True},
        "capabilities": ["text", "tool_calling"],
        "temperament": {
            "creativity": {"default": 0.6, "minimum": 0.2, "maximum": 0.8},
            "precision": {"default": 0.8, "minimum": 0.5, "maximum": 1.0},
        },
        "permissions": {"maximum_risk_level": 1},
        "entrypoints": {
            "system_prompt": "system_prompt.md",
            "behavior": "behavior.yaml",
            "routing": "routing.yaml",
            "memory": "memory.yaml",
            "tools": "tools.yaml",
        },
    }
    return {
        "manifest.yaml": yaml.safe_dump(manifest).encode(),
        "system_prompt.md": b"Build carefully and preserve user intent.",
        "behavior.yaml": yaml.safe_dump({
            "upright": "Create an implementation.",
            "reversed": "Test whether the implementation is feasible.",
        }).encode(),
        "routing.yaml": yaml.safe_dump({
            "required_capabilities": ["text"],
            "allow_paid": False,
            "allow_external": True,
        }).encode(),
        "memory.yaml": yaml.safe_dump({
            "namespaces": ["project", "agent"],
            "maximum_sensitivity": "confidential",
            "external_maximum_sensitivity": "internal",
        }).encode(),
        "tools.yaml": yaml.safe_dump({
            "allowed": ["read_file", "write_file"],
            "approval_required": ["write_file"],
        }).encode(),
    }


def _write_package(
    path: Path,
    private_key: Ed25519PrivateKey,
    *,
    files: dict[str, bytes] | None = None,
    signer_id: str = "test-signer",
) -> Path:
    content = files or _package_files()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    signature = private_key.sign(signature_payload(signer_id, hashes))
    signed = {
        "algorithm": "ed25519",
        "format_version": "1.0",
        "signer_id": signer_id,
        "files": hashes,
        "signature": base64.b64encode(signature).decode(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr(name, data)
        archive.writestr("signature.json", json.dumps(signed))
    return path


@pytest.fixture
def package_environment(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manager = TarotPackageManager(
        trusted_signers={"test-signer": public_key},
        system_policy=SystemPackagePolicy(
            available_tools=frozenset({"read_file", "write_file"}),
            maximum_risk_level=2,
            allow_external=True,
            maximum_memory_sensitivity="confidential",
            external_maximum_sensitivity="internal",
        ),
        root=tmp_path / "profile" / "occult" / "major_arcana",
    )
    return private_key, manager


def _route(
    privacy: PrivacyClass = PrivacyClass.LOCAL,
    *,
    card_id: str = "minor.pentacles.ace.local.test",
) -> MinorArcanaDescriptor:
    return MinorArcanaDescriptor(
        card_id=card_id,
        provider_id="test-provider",
        model_id="test-model",
        adapter_id="test-adapter",
        capabilities=frozenset({"text"}),
        local=privacy is PrivacyClass.LOCAL,
        free=True,
        privacy=privacy,
        quota_pool_id="test-pool",
    )


def _runtime_policy() -> RuntimePolicy:
    return RuntimePolicy(
        allowed_memory_namespaces=frozenset({"project", "agent"}),
        maximum_memory_sensitivity="confidential",
        external_maximum_sensitivity="public",
        allowed_tools=frozenset({"read_file", "write_file"}),
        approval_required_tools=frozenset({"write_file"}),
        tool_risk_levels={"read_file": 0, "write_file": 1},
        maximum_risk_level=1,
    )


def test_install_activate_invoke_and_rollback_are_session_bounded(
    package_environment, tmp_path: Path
):
    private_key, manager = package_environment
    first = _write_package(tmp_path / "magician-1.tarot", private_key)
    second = _write_package(
        tmp_path / "magician-2.tarot",
        private_key,
        files=_package_files("1.1.0"),
    )

    manager.install(first)
    manager.activate("occult.major.magician", "1.0.0")
    old_session = PairingSession.start(
        manager, "occult.major.magician", _runtime_policy()
    )

    manager.install(second)
    manager.activate("occult.major.magician", "1.1.0")
    new_session = PairingSession.start(
        manager, "occult.major.magician", _runtime_policy()
    )

    assert old_session.agent_version == "1.0.0"
    assert new_session.agent_version == "1.1.0"
    assert old_session.generation < new_session.generation
    assert old_session.pair(_route()).agent_id == "occult.major.magician"

    rolled_back = manager.rollback("occult.major.magician")
    assert rolled_back.package.manifest.agent.version == "1.0.0"
    assert old_session.agent_version == "1.0.0"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda files: files.update({"../escape.md": b"escape"}),
            "unsafe archive member",
        ),
        (
            lambda files: files.__setitem__("manifest.yaml", b"format_version: ["),
            "invalid manifest",
        ),
        (
            lambda files: files.pop("tools.yaml"),
            "missing required files",
        ),
    ],
)
def test_rejects_malformed_or_unsafe_packages(
    package_environment, tmp_path: Path, mutate, match: str
):
    private_key, manager = package_environment
    files = _package_files()
    mutate(files)
    package = _write_package(tmp_path / "bad.tarot", private_key, files=files)
    with pytest.raises(TarotPackageError, match=match):
        manager.validate(package)


def test_rejects_signature_tampering(package_environment, tmp_path: Path):
    private_key, manager = package_environment
    package = _write_package(tmp_path / "signed.tarot", private_key)
    with zipfile.ZipFile(package) as archive:
        entries = {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if not info.is_dir()
        }
    entries["system_prompt.md"] = b"tampered"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with pytest.raises(TarotPackageError, match="hashes do not match"):
        manager.validate(package)


def test_rejects_linked_members_and_compression_bombs(
    package_environment, tmp_path: Path
):
    _, manager = package_environment
    linked = tmp_path / "linked.tarot"
    with zipfile.ZipFile(linked, "w") as archive:
        info = zipfile.ZipInfo("linked.md")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    with pytest.raises(TarotPackageError, match="linked archive member"):
        manager.validate(linked)

    bomb = tmp_path / "bomb.tarot"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("knowledge/repeated.txt", b"A" * (1024 * 1024 + 1))
    with pytest.raises(TarotPackageError, match="compression ratio"):
        manager.validate(bomb)


def test_rejects_unknown_tools_and_policy_escalation(
    package_environment, tmp_path: Path
):
    private_key, manager = package_environment
    files = _package_files()
    files["tools.yaml"] = yaml.safe_dump({
        "allowed": ["unknown_tool"],
        "approval_required": [],
    }).encode()
    package = _write_package(tmp_path / "unknown.tarot", private_key, files=files)
    with pytest.raises(TarotPackageError, match="unknown tools"):
        manager.validate(package)

    files = _package_files()
    manifest = yaml.safe_load(files["manifest.yaml"])
    manifest["permissions"]["maximum_risk_level"] = 3
    files["manifest.yaml"] = yaml.safe_dump(manifest).encode()
    package = _write_package(tmp_path / "risk.tarot", private_key, files=files)
    with pytest.raises(TarotPackageError, match="excessive risk"):
        manager.validate(package)

    files = _package_files()
    routing = yaml.safe_load(files["routing.yaml"])
    routing["allow_paid"] = True
    files["routing.yaml"] = yaml.safe_dump(routing).encode()
    package = _write_package(tmp_path / "paid.tarot", private_key, files=files)
    with pytest.raises(TarotPackageError, match="paid routes"):
        manager.validate(package)

    files = _package_files()
    memory = yaml.safe_load(files["memory.yaml"])
    memory["maximum_sensitivity"] = "restricted"
    files["memory.yaml"] = yaml.safe_dump(memory).encode()
    package = _write_package(tmp_path / "memory.tarot", private_key, files=files)
    with pytest.raises(TarotPackageError, match="memory sensitivity"):
        manager.validate(package)


def test_pairing_clamps_temperament_and_filters_memory_by_route(
    package_environment, tmp_path: Path
):
    private_key, manager = package_environment
    manager.install(_write_package(tmp_path / "agent.tarot", private_key))
    manager.activate("occult.major.magician", "1.0.0")
    session = PairingSession.start(manager, "occult.major.magician", _runtime_policy())
    memories = (
        MemoryRecord("1", "project", "public", "Public fact"),
        MemoryRecord("2", "project", "internal", "Internal fact"),
        MemoryRecord("3", "project", "confidential", "Confidential fact"),
        MemoryRecord("4", "user", "public", "Wrong namespace"),
    )

    local = session.pair(
        _route(),
        temperament_modifiers={"creativity": 1.0, "precision": -1.0},
        memories=memories,
    )
    external = session.pair(
        _route(PrivacyClass.EXTERNAL, card_id="minor.swords.king.external.test"),
        memories=memories,
    )

    assert local.temperament == {"creativity": 0.8, "precision": 0.5}
    assert [item.record_id for item in local.memories] == ["1", "2", "3"]
    assert [item.record_id for item in external.memories] == ["1"]
    assert "credential" not in external.render_system_prompt().lower()


def test_tool_authorization_cannot_be_weakened_by_package_or_model(
    package_environment, tmp_path: Path
):
    private_key, manager = package_environment
    manager.install(_write_package(tmp_path / "agent.tarot", private_key))
    manager.activate("occult.major.magician", "1.0.0")
    session = PairingSession.start(manager, "occult.major.magician", _runtime_policy())

    assert (
        session.authorize_tool("read_file").authorization is ToolAuthorization.ALLOWED
    )
    assert (
        session.authorize_tool("write_file").authorization
        is ToolAuthorization.APPROVAL_REQUIRED
    )
    assert session.authorize_tool("terminal").authorization is ToolAuthorization.DENIED


def test_pairing_rejects_unsupported_orientation_and_capability(
    package_environment, tmp_path: Path
):
    private_key, manager = package_environment
    manager.install(_write_package(tmp_path / "agent.tarot", private_key))
    manager.activate("occult.major.magician", "1.0.0")
    session = PairingSession.start(manager, "occult.major.magician", _runtime_policy())

    with pytest.raises(PairingError, match="invalid orientation"):
        session.pair(_route(), orientation="sideways")
    with pytest.raises(PairingError, match="lacks required"):
        session.pair(
            MinorArcanaDescriptor(
                card_id="minor.cups.page.empty",
                provider_id="test-provider",
                model_id="test-model",
                adapter_id="test-adapter",
                capabilities=frozenset({"vision"}),
                local=True,
                free=True,
                privacy=PrivacyClass.LOCAL,
                quota_pool_id="test-pool",
            )
        )

import hashlib
import json
from pathlib import Path

import pytest

from agent.occult.release import (
    CHECKSUM_FILE,
    COMPATIBILITY_FILE,
    INSTALL_MANIFEST_FILE,
    MANIFEST_FILE,
    MIGRATIONS_FILE,
    PROVENANCE_FILE,
    SBOM_FILE,
    SIGNATURE_FILE,
    OccultReleaseError,
    assemble_release,
    promote_release,
    verify_release,
)

COMMIT_SHA = "a" * 40
SOURCE_DATE_EPOCH = 1_700_000_000
ROOT = Path(__file__).resolve().parents[3]


def _write_source_locks(root: Path) -> None:
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "aiohttp"\nversion = "3.12.0"\n',
        encoding="utf-8",
    )
    for folder, name in (
        ("web", "@occult/web"),
        ("ui-tui", "@occult/tui"),
        ("website", "@occult/docs"),
    ):
        destination = root / folder
        destination.mkdir()
        (destination / "package-lock.json").write_text(
            json.dumps({
                "lockfileVersion": 3,
                "packages": {"": {"name": name, "version": "1.0.0"}},
            }),
            encoding="utf-8",
        )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / INSTALL_MANIFEST_FILE).write_text(
        (ROOT / "scripts" / INSTALL_MANIFEST_FILE).read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _assembly_roots(tmp_path: Path) -> tuple[Path, Path]:
    artifacts = tmp_path / "compiled"
    artifacts.mkdir()
    (artifacts / "hermes_occult-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
    dashboard = artifacts / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text("<main>Occult</main>", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    _write_source_locks(source)
    return artifacts, source


def _assemble(
    artifacts: Path,
    source: Path,
    output: Path,
    *,
    channel: str = "preview",
) -> dict:
    return assemble_release(
        artifacts,
        output,
        version="1.0.0",
        commit_sha=COMMIT_SHA,
        channel=channel,
        source_date_epoch=SOURCE_DATE_EPOCH,
        source_root=source,
    )


def _tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_structural_sigstore_bundle(root: Path) -> None:
    (root / SIGNATURE_FILE).write_text(
        json.dumps({
            "mediaType": ("application/vnd.dev.sigstore.bundle+json;version=0.3"),
            "verificationMaterial": {},
            "messageSignature": {},
        }),
        encoding="utf-8",
    )


def test_release_assembly_is_deterministic_and_policy_safe(tmp_path: Path):
    artifacts, source = _assembly_roots(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    manifest = _assemble(artifacts, source, first)
    _assemble(artifacts, source, second)

    assert _tree_digests(first) == _tree_digests(second)
    assert verify_release(first, require_signature=False) == manifest
    compatibility = json.loads((first / COMPATIBILITY_FILE).read_text())
    assert compatibility["feature_gate"] == {
        "config": "occult.enabled",
        "default": False,
    }
    assert compatibility["default_policy"] == {
        "local_first": True,
        "free_only": True,
        "paid_fallback": False,
        "maximum_cost_usd": 0,
    }
    assert compatibility["platforms"] == ["linux", "macos", "windows"]
    install_manifest = json.loads(
        (source / "scripts" / INSTALL_MANIFEST_FILE).read_text(encoding="utf-8")
    )
    council = install_manifest["council"]
    assert compatibility["agents_council"] == {
        "minimum_version": council["release_tag"].removeprefix("v"),
        "release_tag": council["release_tag"],
        "commit_sha": council["commit_sha"],
    }
    migration = json.loads((first / MIGRATIONS_FILE).read_text())
    assert migration["rollback_supported"] is True
    assert migration["rollback_requires_backup_restore"] == ["readings-sqlite"]
    assert "occult/readings.db*" in migration["backup_set"]
    assert "occult/invocations.db*" in migration["backup_set"]
    assert {"name": "invocation-results-sqlite", "version": 1} in migration["state"]
    assert {"name": "readings-sqlite", "version": 3} in migration["state"]
    sbom = json.loads((first / SBOM_FILE).read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["serialNumber"].startswith("urn:uuid:")
    assert {component["name"] for component in sbom["components"]} == {
        "@occult/docs",
        "@occult/tui",
        "@occult/web",
        "aiohttp",
    }
    provenance = json.loads((first / PROVENANCE_FILE).read_text())
    assert {subject["name"] for subject in provenance["subject"]} == {
        item["path"] for item in manifest["artifacts"]
    }
    assert (first / CHECKSUM_FILE).is_file()
    assert (first / MANIFEST_FILE).is_file()


def test_verification_detects_tampering(tmp_path: Path):
    artifacts, source = _assembly_roots(tmp_path)
    release = tmp_path / "release"
    _assemble(artifacts, source, release)
    (release / "artifacts" / "dashboard" / "index.html").write_text("tampered")

    with pytest.raises(OccultReleaseError, match="checksum mismatch"):
        verify_release(release, require_signature=False)


@pytest.mark.parametrize(
    ("name", "content", "match"),
    [
        ("credentials.json", "{}", "forbidden sensitive"),
        (
            "client.txt",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "secret-shaped",
        ),
        ("private.pem", "not-a-real-key", "forbidden sensitive"),
    ],
)
def test_assembly_rejects_sensitive_artifacts(
    tmp_path: Path,
    name: str,
    content: str,
    match: str,
):
    artifacts, source = _assembly_roots(tmp_path)
    (artifacts / name).write_text(content, encoding="utf-8")

    with pytest.raises(OccultReleaseError, match=match):
        _assemble(artifacts, source, tmp_path / "release")


def test_stable_release_requires_sigstore_bundle_structure(tmp_path: Path):
    artifacts, source = _assembly_roots(tmp_path)
    release = tmp_path / "stable"
    _assemble(artifacts, source, release, channel="stable")

    with pytest.raises(OccultReleaseError, match="invalid release metadata"):
        verify_release(release)

    _write_structural_sigstore_bundle(release)
    assert verify_release(release)["channel"] == "stable"

    with pytest.raises(OccultReleaseError, match="stable promotion requires"):
        promote_release(
            release,
            tmp_path / "unsafe-promotion",
            require_signature=False,
        )


def test_promotion_copies_exact_staged_bytes_without_rebuild(tmp_path: Path):
    artifacts, source = _assembly_roots(tmp_path)
    staged = tmp_path / "staged"
    _assemble(artifacts, source, staged)
    destination = tmp_path / "promoted"

    manifest = promote_release(staged, destination, require_signature=False)

    assert manifest["commit_sha"] == COMMIT_SHA
    assert _tree_digests(staged) == _tree_digests(destination)
    with pytest.raises(OccultReleaseError, match="already exists"):
        promote_release(staged, destination, require_signature=False)


def test_invalid_release_identity_and_output_layout_fail_closed(tmp_path: Path):
    artifacts, source = _assembly_roots(tmp_path)
    with pytest.raises(OccultReleaseError, match="40 lowercase"):
        assemble_release(
            artifacts,
            tmp_path / "release",
            version="1.0.0",
            commit_sha="HEAD",
            channel="preview",
            source_date_epoch=SOURCE_DATE_EPOCH,
            source_root=source,
        )
    with pytest.raises(OccultReleaseError, match="outside the artifact root"):
        _assemble(artifacts, source, artifacts / "release")

import base64
import json
import zipfile
from pathlib import Path

from agent.occult.runtime import MAJOR_ARCANA_AGENT_IDS
from agent.occult.tarot_packages import SystemPackagePolicy, TarotPackageManager


def _signers() -> dict[str, bytes]:
    payload = json.loads(
        (Path(__file__).parents[3] / "agent" / "occult" / "starters" / "starter_signers.json")
        .read_text(encoding="utf-8")
    )
    return {
        signer: base64.b64decode(key, validate=True)
        for signer, key in payload["signers"].items()
    }


def test_bundled_full_major_arcana_is_signed_and_complete(tmp_path: Path):
    root = Path(__file__).parents[3] / "agent" / "occult" / "starters"
    archives = sorted(root.glob("*.tarot"))
    assert len(archives) == 22

    manager = TarotPackageManager(
        trusted_signers=_signers(),
        system_policy=SystemPackagePolicy(),
        root=tmp_path / "major_arcana",
    )
    installed = []
    for archive in archives:
        package = manager.validate(archive)
        assert package.manifest.agent.version == "1.1.0"
        installed.append(package.manifest.agent.id)
        manager.install(archive)
    assert tuple(sorted(installed)) == tuple(sorted(MAJOR_ARCANA_AGENT_IDS))

    for archive in archives:
        with zipfile.ZipFile(archive) as handle:
            assert "signature.json" in handle.namelist()

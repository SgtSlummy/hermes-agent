import base64
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_occult_starters import main


def test_starter_builder_reads_signing_key_from_environment(
    tmp_path: Path,
    monkeypatch,
):
    raw_key = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv(
        "OCCULT_STARTER_SIGNING_KEY",
        base64.b64encode(raw_key).decode("ascii"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_occult_starters.py", "--output", str(tmp_path)],
    )

    assert main() == 0
    assert (tmp_path / "starter_signers.json").is_file()
    assert len(tuple(tmp_path.glob("*.tarot"))) > 0


@pytest.mark.parametrize(
    "encoded_key",
    ("not-valid-base64!", base64.b64encode(b"too-short").decode("ascii")),
)
def test_starter_builder_rejects_invalid_signing_key(
    encoded_key: str,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OCCULT_STARTER_SIGNING_KEY", encoded_key)
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_occult_starters.py", "--output", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert not tuple(tmp_path.glob("*.tarot"))

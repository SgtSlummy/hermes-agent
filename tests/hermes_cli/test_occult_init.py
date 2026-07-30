from pathlib import Path

import yaml

from hermes_cli.occult import initialize_occult


def test_occult_init_creates_local_profile_without_returning_secrets(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )

    result = initialize_occult(model="qwen2.5:3b")

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["occult"]["enabled"] is True
    assert config["occult"]["local_model"] == "qwen2.5:3b"
    assert config["platforms"]["api_server"]["enabled"] is True
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "OCCULT_ADMIN_KEY=occult_admin_" in env_text
    assert "OCCULT_API_KEY=occult_" in env_text
    assert "occult_admin_" not in str(result)
    assert not any(
        isinstance(value, str) and value.startswith(("occult_", "occult_admin_"))
        for value in result.values()
    )
    assert result["token_created"] is True
    assert result["deck_id"] == "occult.deck.starter"


def test_occult_init_reuses_existing_virtual_token(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )

    first = initialize_occult(model="qwen2.5:3b")
    second = initialize_occult(model="qwen2.5:3b")

    assert first["token_created"] is True
    assert second["token_created"] is False

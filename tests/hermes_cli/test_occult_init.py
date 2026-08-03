from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest

from agent.occult.runtime import OccultRuntimeError
from hermes_cli.occult import OccultCLIError, initialize_occult


@pytest.fixture(autouse=True)
def _isolate_path_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_occult_init_creates_local_profile_without_returning_secrets(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    result = initialize_occult(model="qwen2.5:3b")

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["occult"]["enabled"] is True
    assert config["occult"]["local_model"] == "qwen2.5:3b"
    assert config["platforms"]["api_server"]["enabled"] is True
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "API_SERVER_KEY=hermes_api_" in env_text
    assert "OCCULT_ADMIN_KEY=occult_admin_" in env_text
    assert "OCCULT_API_KEY=occult_" in env_text
    assert "OCCULT_API_URL=" not in env_text
    assert "occult_admin_" not in str(result)
    assert not any(
        isinstance(value, str) and value.startswith(("occult_", "occult_admin_"))
        for value in result.values()
    )
    assert result["token_created"] is True
    assert result["deck_id"] == "occult.deck.starter"


def test_occult_init_preserves_existing_provider_and_api_server_key(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    api_server_key = "existing-api-server-key-" + "x" * 32
    (home / ".env").write_text(
        f"API_SERVER_KEY={api_server_key}\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "occult:\n"
        "  local_base_url: http://127.0.0.1:22434/v1\n"
        "  local_model: configured-model\n",
        encoding="utf-8",
    )
    discovered_urls: list[str] = []

    def discover(url: str):
        discovered_urls.append(url)
        return ("another-model", "configured-model")

    monkeypatch.setattr("hermes_cli.occult.discover_ollama_models", discover)
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    result = initialize_occult()

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert discovered_urls == ["http://127.0.0.1:22434/v1"]
    assert result["model"] == "configured-model"
    assert config["occult"]["local_base_url"] == "http://127.0.0.1:22434/v1"
    assert config["occult"]["local_model"] == "configured-model"
    assert f"API_SERVER_KEY={api_server_key}" in env_text


def test_occult_init_can_enable_unattended_keyless_mesh(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    result = initialize_occult(model="qwen2.5:3b", enable_keyless_mesh=True)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))

    assert config["occult"]["provider_mesh"] == {
        "enabled": True,
        "auto_enroll_keyless": True,
        "allow_anonymous": True,
        "allow_external_routes": True,
    }
    assert result["keyless_mesh_enabled"] is True


def test_occult_init_can_enable_full_free_mesh(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    result = initialize_occult(model="qwen2.5:3b", enable_free_mesh=True)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))

    assert config["occult"]["provider_mesh"] == {
        "enabled": True,
        "auto_enroll_keyless": True,
        "auto_enroll_free": True,
        "allow_anonymous": True,
        "allow_external_routes": True,
    }
    assert result["free_mesh_enabled"] is True


def test_occult_init_merges_protected_env_for_unattended_free_mesh(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    (home / ".env").write_text(
        "GROQ_API_KEY=authorized-value-that-stays-in-memory\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )
    seen: dict[str, str] = {}

    class _Authority:
        @staticmethod
        def statuses():
            return []

        @staticmethod
        def issue(_policy):
            return "occult_token"

    def fake_build(config, *, environ):
        seen.update({key: value for key, value in environ.items() if key == "GROQ_API_KEY"})
        return SimpleNamespace(
            service=SimpleNamespace(
                router=SimpleNamespace(routes=lambda: ()),
                token_authority=_Authority(),
            ),
            close=lambda: None,
        )

    monkeypatch.setattr("hermes_cli.occult.build_occult_http", fake_build)

    initialize_occult(model="qwen2.5:3b", enable_free_mesh=True)

    assert seen == {"GROQ_API_KEY": "authorized-value-that-stays-in-memory"}


def test_occult_init_reuses_existing_virtual_token(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("OCCULT_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    first = initialize_occult(model="qwen2.5:3b")
    second = initialize_occult(model="qwen2.5:3b")

    assert first["token_created"] is True
    assert second["token_created"] is False


def test_occult_init_replaces_token_without_full_starter_scope(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OCCULT_ADMIN_KEY", "a" * 32)
    monkeypatch.setenv("OCCULT_API_KEY", "occult_narrow")
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )
    issued = []

    class _Authority:
        @staticmethod
        def policy(_token):
            return SimpleNamespace(
                allowed_agent_ids=frozenset({"occult.major.magician"}),
                allowed_card_ids=frozenset(),
            )

        @staticmethod
        def statuses():
            return []

        @staticmethod
        def issue(policy):
            issued.append(policy)
            return "occult_replacement"

    monkeypatch.setattr(
        "hermes_cli.occult.build_occult_http",
        lambda _config, *, environ: SimpleNamespace(
            service=SimpleNamespace(token_authority=_Authority()),
            close=lambda: None,
        ),
    )

    result = initialize_occult(model="qwen2.5:3b")

    assert result["token_created"] is True
    assert issued[0].allowed_agent_ids == frozenset(result["agents"])
    assert issued[0].allowed_card_ids == frozenset({result["card_id"]})
    assert "OCCULT_API_KEY=occult_replacement" in (home / ".env").read_text(
        encoding="utf-8"
    )


def test_occult_init_does_not_enable_config_when_credentials_fail(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OCCULT_ADMIN_KEY", "a" * 32)
    monkeypatch.delenv("OCCULT_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )
    discarded = []
    closed = []

    class _Authority:
        @staticmethod
        def statuses():
            return []

        @staticmethod
        def issue(_policy):
            return "occult_new"

        @staticmethod
        def discard(token_id):
            discarded.append(token_id)

    monkeypatch.setattr(
        "hermes_cli.occult.build_occult_http",
        lambda _config, *, environ: SimpleNamespace(
            service=SimpleNamespace(token_authority=_Authority()),
            close=lambda: closed.append(True),
        ),
    )
    saved_configs = []
    monkeypatch.setattr(
        "hermes_cli.occult.cli_config.save_config",
        lambda config: saved_configs.append(config),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.cli_config.save_env_values",
        lambda _values: (_ for _ in ()).throw(OSError("read-only secrets mount")),
    )

    with pytest.raises(OSError, match="read-only"):
        initialize_occult(model="qwen2.5:3b")

    assert saved_configs == []
    assert discarded == ["local-default"]
    assert closed == [True]


def test_occult_init_rejects_short_admin_key(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OCCULT_ADMIN_KEY", "too-short")
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    with pytest.raises(OccultCLIError, match="at least 32 characters"):
        initialize_occult(model="qwen2.5:3b")

    assert not (home / "config.yaml").exists()


def test_occult_init_rejects_non_ascii_admin_key(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OCCULT_ADMIN_KEY", "é" * 32)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("qwen2.5:3b",),
    )
    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        lambda _url, _model, **_kwargs: None,
    )

    with pytest.raises(OccultCLIError, match="ASCII"):
        initialize_occult(model="qwen2.5:3b")

    assert not (home / "config.yaml").exists()


def test_occult_init_rejects_non_chat_model_before_writing(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("embedding-only",),
    )

    def reject_chat(_url, _model, **_kwargs):
        raise OccultRuntimeError(
            "Ollama model does not support chat completions: embedding-only"
        )

    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        reject_chat,
    )

    with pytest.raises(OccultCLIError, match="does not support chat"):
        initialize_occult(model="embedding-only")

    assert not (home / "config.yaml").exists()


def test_occult_init_uses_configured_timeout_for_model_probe(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "occult:\n  provider_timeout_seconds: 420\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: ("slow-model",),
    )
    seen = {}

    def stop_after_probe(_url, _model, *, timeout_seconds):
        seen["timeout_seconds"] = timeout_seconds
        raise OccultRuntimeError("probe complete")

    monkeypatch.setattr(
        "hermes_cli.occult.validate_ollama_chat_model",
        stop_after_probe,
    )

    with pytest.raises(OccultCLIError, match="probe complete"):
        initialize_occult(model="slow-model")

    assert seen == {"timeout_seconds": 420}


def test_occult_init_preserves_malformed_existing_config(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_path = home / "config.yaml"
    malformed = b"platforms: [\n"
    config_path.write_bytes(malformed)
    monkeypatch.setattr(
        "hermes_cli.occult.discover_ollama_models",
        lambda _url: pytest.fail("provider discovery must not run"),
    )

    with pytest.raises(OccultCLIError, match="refused to overwrite"):
        initialize_occult(model="qwen2.5:3b")

    assert config_path.read_bytes() == malformed


def test_occult_init_preserves_non_mapping_existing_config(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_path = home / "config.yaml"
    original = b"[]\n"
    config_path.write_bytes(original)

    with pytest.raises(OccultCLIError, match="must contain a YAML object"):
        initialize_occult(model="qwen2.5:3b")

    assert config_path.read_bytes() == original

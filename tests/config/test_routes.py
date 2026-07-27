from pathlib import Path

import pytest

from scriptorium.config import ConfigurationError, load_local_config


@pytest.mark.parametrize(
    ("runtime", "provider"),
    [
        ("codex", "openai"),
        ("claude_code", "anthropic"),
        ("antigravity", "gemini"),
    ],
)
def test_local_config_accepts_supported_runtimes(tmp_path: Path, runtime: str, provider: str) -> None:
    state_dir = tmp_path / ".scriptorium"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(
        (
            "[roles]\n"
            'revision = "primary"\n\n'
            "[routes.primary]\n"
            f'runtime = "{runtime}"\n'
            f'model_provider = "{provider}"\n'
            'model = "configured-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )

    route = load_local_config(tmp_path).routes["primary"]

    assert route.runtime == runtime
    assert route.model_provider == provider


def test_local_config_rejects_unknown_runtime(tmp_path: Path) -> None:
    state_dir = tmp_path / ".scriptorium"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(
        ("[routes.primary]\n" 'runtime = "unknown"\n' 'model_provider = "provider"\n' 'model = "model"\n'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unsupported runtime"):
        load_local_config(tmp_path)


def test_local_config_rejects_user_supplied_runtime_version(tmp_path: Path) -> None:
    state_dir = tmp_path / ".scriptorium"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(
        (
            "[routes.primary]\n"
            'runtime = "codex"\n'
            'runtime_version = "untrusted"\n'
            'model_provider = "openai"\n'
            'model = "model"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="cannot configure runtime_version"):
        load_local_config(tmp_path)


@pytest.mark.parametrize(
    ("runtime", "provider", "expected"),
    [
        ("claude_code", "openai", "requires model_provider 'anthropic'"),
        ("antigravity", "vertex", "requires model_provider 'gemini'"),
    ],
)
def test_native_runtime_requires_its_provider(
    tmp_path: Path,
    runtime: str,
    provider: str,
    expected: str,
) -> None:
    state_dir = tmp_path / ".scriptorium"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(
        ("[routes.primary]\n" f'runtime = "{runtime}"\n' f'model_provider = "{provider}"\n' 'model = "model"\n'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=expected):
        load_local_config(tmp_path)

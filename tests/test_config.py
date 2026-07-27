from pathlib import Path

import pytest

from scriptorium.config import (
    MODEL_PLACEHOLDER,
    ConfigurationError,
    RouteConfig,
    initialize_project,
    load_local_config,
    load_project_config,
    validate_ready,
)


def test_initialize_and_load_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "main.tex").write_text("paper", encoding="utf-8")

    initialize_project(tmp_path, "main.tex", "pdflatex")

    project = load_project_config(tmp_path)
    local = load_local_config(tmp_path)
    assert project.manuscript.main == "main.tex"
    assert project.profiles["full"][-1] == "figure_review"
    assert local.max_concurrency == 2
    assert local.routes["primary"].model == MODEL_PLACEHOLDER
    assert local.routes["primary"].runtime == "codex"
    assert local.routes["primary"].runtime_version is None
    assert ".scriptorium/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_placeholder_model_is_not_ready(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    initialize_project(tmp_path, "main.tex", "pdflatex")

    with pytest.raises(ConfigurationError, match=MODEL_PLACEHOLDER):
        validate_ready(load_project_config(tmp_path), load_local_config(tmp_path), "quick", None)


def test_route_cost_does_not_double_count_cached_or_reasoning_subtotals() -> None:
    route = RouteConfig(
        name="primary",
        model_provider="openai",
        model="model",
        input_usd_per_million=2,
        output_usd_per_million=8,
    )

    assert route.estimate_cost(1_000_000, 250_000, 100_000, 50_000) == pytest.approx(2.8)


def test_budget_requires_explicit_route_prices(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    initialize_project(tmp_path, "main.tex", "pdflatex")
    local_path = tmp_path / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        .replace(MODEL_PLACEHOLDER, "configured-model")
        .replace("output_usd_per_million = 0\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="needs input_usd_per_million"):
        validate_ready(load_project_config(tmp_path), load_local_config(tmp_path), "quick", 1)


def test_budget_accepts_explicit_zero_prices_for_gateway_routes(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    initialize_project(tmp_path, "main.tex", "pdflatex")
    local_path = tmp_path / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8").replace(MODEL_PLACEHOLDER, "local-model"),
        encoding="utf-8",
    )

    validate_ready(load_project_config(tmp_path), load_local_config(tmp_path), "quick", 1)


def test_project_profiles_only_accept_review_roles(tmp_path: Path) -> None:
    (tmp_path / "scriptorium.toml").write_text(
        (
            "[manuscript]\n"
            'main = "main.tex"\n'
            'engine = "pdflatex"\n\n'
            "[profiles.invalid]\n"
            'roles = ["workflow"]\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unsupported roles"):
        load_project_config(tmp_path)


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

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


def test_unused_visual_transcription_route_is_not_required(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    initialize_project(tmp_path, "main.tex", "pdflatex")
    local_path = tmp_path / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        .replace(MODEL_PLACEHOLDER, "configured-model")
        .replace("[routes.primary]", 'visual_transcription = "missing"\n\n[routes.primary]'),
        encoding="utf-8",
    )

    validate_ready(load_project_config(tmp_path), load_local_config(tmp_path), "quick", None)

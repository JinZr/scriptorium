from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from .errors import ConfigurationError

SUPPORTED_ENGINES = {"pdflatex", "xelatex", "lualatex"}
DEFAULT_PROFILES = {
    "quick": ("substantive_review", "copyedit"),
    "full": ("substantive_review", "copyedit", "consistency", "figure_review"),
}
REVIEW_ROLES = {"substantive_review", "copyedit", "consistency", "figure_review"}
MODEL_PLACEHOLDER = "USER_CONFIGURED_MODEL"


@dataclass(frozen=True)
class ManuscriptConfig:
    main: str
    engine: str


@dataclass(frozen=True)
class ProjectConfig:
    manuscript: ManuscriptConfig
    profiles: dict[str, tuple[str, ...]]

    def frozen_dict(self) -> dict[str, Any]:
        return {
            "manuscript": asdict(self.manuscript),
            "profiles": {name: list(roles) for name, roles in sorted(self.profiles.items())},
        }


@dataclass(frozen=True)
class RouteConfig:
    name: str
    model_provider: str
    model: str
    input_usd_per_million: float
    output_usd_per_million: float
    reasoning_effort: str = "high"
    pricing_configured: bool = True

    def estimate_cost(
        self,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int = 0,
    ) -> float:
        return (input_tokens * self.input_usd_per_million + output_tokens * self.output_usd_per_million) / 1_000_000


@dataclass(frozen=True)
class LocalConfig:
    max_concurrency: int
    roles: dict[str, str]
    routes: dict[str, RouteConfig]

    def route_for_role(self, role_key: str, override: str | None = None) -> RouteConfig:
        route_name = override or self.roles.get(role_key)
        if not route_name:
            raise ConfigurationError(f"No model route configured for role {role_key!r}")
        try:
            return self.routes[route_name]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown model route {route_name!r} for role {role_key!r}") from exc

    def frozen_dict(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "roles": dict(sorted(self.roles.items())),
            "routes": {name: asdict(route) for name, route in sorted(self.routes.items())},
        }


def find_repo(path: str | Path = ".") -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    current = candidate
    while current != current.parent:
        if (current / ".git").exists() or (current / "scriptorium.toml").exists():
            return current
        current = current.parent
    raise ConfigurationError(f"No Git manuscript repository found from {candidate}")


def load_project_config(repo: Path) -> ProjectConfig:
    path = repo / "scriptorium.toml"
    if not path.is_file():
        raise ConfigurationError(f"Missing project configuration: {path}")
    data = _read_toml(path)
    manuscript = data.get("manuscript", {})
    main = str(manuscript.get("main", "")).strip()
    engine = str(manuscript.get("engine", "")).strip()
    if not main or Path(main).is_absolute() or ".." in Path(main).parts:
        raise ConfigurationError("manuscript.main must be a relative path inside the repository")
    if Path(main).suffix.lower() != ".tex":
        raise ConfigurationError("manuscript.main must be a LaTeX .tex file")
    if engine not in SUPPORTED_ENGINES:
        raise ConfigurationError(f"manuscript.engine must be one of {sorted(SUPPORTED_ENGINES)}")
    profiles: dict[str, tuple[str, ...]] = {}
    for name, profile in data.get("profiles", {}).items():
        roles = tuple(str(role) for role in profile.get("roles", []))
        if not roles:
            raise ConfigurationError(f"Profile {name!r} must contain at least one role")
        unknown_roles = set(roles) - REVIEW_ROLES
        if unknown_roles:
            raise ConfigurationError(f"Profile {name!r} has unsupported roles: {sorted(unknown_roles)}")
        if len(set(roles)) != len(roles):
            raise ConfigurationError(f"Profile {name!r} contains duplicate roles")
        profiles[str(name)] = roles
    if not profiles:
        raise ConfigurationError("At least one review profile is required")
    return ProjectConfig(ManuscriptConfig(main, engine), profiles)


def load_local_config(repo: Path) -> LocalConfig:
    path = repo / ".scriptorium" / "config.toml"
    if not path.is_file():
        raise ConfigurationError(f"Missing local configuration: {path}")
    data = _read_toml(path)
    max_concurrency = int(data.get("max_concurrency", 2))
    if max_concurrency < 1:
        raise ConfigurationError("max_concurrency must be positive")
    roles = {str(key): str(value) for key, value in data.get("roles", {}).items()}
    routes: dict[str, RouteConfig] = {}
    for name, route in data.get("routes", {}).items():
        model = str(route.get("model", "")).strip()
        provider = str(route.get("model_provider", "")).strip()
        if not model or not provider:
            raise ConfigurationError(f"Route {name!r} requires model and model_provider")
        input_price = float(route.get("input_usd_per_million", 0))
        output_price = float(route.get("output_usd_per_million", 0))
        if not math.isfinite(input_price) or not math.isfinite(output_price):
            raise ConfigurationError(f"Route {name!r} prices must be finite")
        if input_price < 0 or output_price < 0:
            raise ConfigurationError(f"Route {name!r} prices cannot be negative")
        routes[str(name)] = RouteConfig(
            name=str(name),
            model_provider=provider,
            model=model,
            input_usd_per_million=input_price,
            output_usd_per_million=output_price,
            reasoning_effort=str(route.get("reasoning_effort", "high")),
            pricing_configured={
                "input_usd_per_million",
                "output_usd_per_million",
            }.issubset(route),
        )
    return LocalConfig(max_concurrency=max_concurrency, roles=roles, routes=routes)


def validate_ready(project: ProjectConfig, local: LocalConfig, profile: str, budget_usd: float | None) -> None:
    try:
        roles = project.profiles[profile]
    except KeyError as exc:
        raise ConfigurationError(f"Unknown review profile {profile!r}") from exc
    for role_key in (*roles, "revision", "verification"):
        route = local.route_for_role(role_key)
        if route.model == MODEL_PLACEHOLDER:
            raise ConfigurationError(f"Route {route.name!r} still uses {MODEL_PLACEHOLDER}")
        if budget_usd is not None and not route.pricing_configured:
            raise ConfigurationError(
                f"Route {route.name!r} needs input_usd_per_million and output_usd_per_million "
                "when --budget-usd is used"
            )


def initialize_project(repo: Path, main: str, engine: str) -> None:
    if engine not in SUPPORTED_ENGINES:
        raise ConfigurationError(f"Unsupported LaTeX engine {engine!r}")
    if Path(main).is_absolute() or ".." in Path(main).parts:
        raise ConfigurationError("--main must be a relative path inside the repository")
    if Path(main).suffix.lower() != ".tex":
        raise ConfigurationError("--main must be a LaTeX .tex file")
    if not (repo / ".git").exists():
        raise ConfigurationError(f"{repo} is not a Git repository")
    project_path = repo / "scriptorium.toml"
    if project_path.exists():
        raise ConfigurationError(f"{project_path} already exists")
    state_dir = repo / ".scriptorium"
    local_path = state_dir / "config.toml"
    if local_path.exists():
        raise ConfigurationError(f"{local_path} already exists")
    state_dir.mkdir(parents=True, exist_ok=True)
    project_text = (
        "[manuscript]\n"
        f"main = {json.dumps(main)}\n"
        f"engine = {json.dumps(engine)}\n\n"
        "[profiles.quick]\n"
        'roles = ["substantive_review", "copyedit"]\n\n'
        "[profiles.full]\n"
        'roles = ["substantive_review", "copyedit", "consistency", "figure_review"]\n'
    )
    project_path.write_text(project_text, encoding="utf-8")
    local_text = (
        "max_concurrency = 2\n\n"
        "[roles]\n"
        'substantive_review = "primary"\n'
        'copyedit = "primary"\n'
        'consistency = "primary"\n'
        'figure_review = "primary"\n'
        'revision = "primary"\n'
        'verification = "primary"\n\n'
        "[routes.primary]\n"
        'model_provider = "openai"\n'
        f'model = "{MODEL_PLACEHOLDER}"\n'
        "input_usd_per_million = 0\n"
        "output_usd_per_million = 0\n"
        'reasoning_effort = "high"\n'
    )
    local_path.write_text(local_text, encoding="utf-8")
    ignore_path = repo / ".gitignore"
    current = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    if ".scriptorium/" not in current.splitlines():
        separator = "" if not current or current.endswith("\n") else "\n"
        ignore_path.write_text(f"{current}{separator}.scriptorium/\n", encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {path}: {exc}") from exc

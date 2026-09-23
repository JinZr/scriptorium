from __future__ import annotations

from dataclasses import asdict, dataclass
import json
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


def reject_legacy_local_config(repo: Path) -> None:
    path = repo / ".scriptorium" / "config.toml"
    if path.is_file():
        raise ConfigurationError(
            f"{path} configures removed internal model routes; remove it before starting an external run"
        )


def validate_ready(project: ProjectConfig, profile: str) -> None:
    if profile not in project.profiles:
        raise ConfigurationError(f"Unknown review profile {profile!r}")


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

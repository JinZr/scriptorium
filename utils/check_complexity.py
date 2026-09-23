#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

from mccabe import PathGraphingAstVisitor

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@dataclass(frozen=True)
class Policy:
    max_complexity: int
    statement_warning: int
    paths: tuple[str, ...]
    advisory_paths: tuple[str, ...]
    debt: dict[tuple[str, str], int]


def positive_integer(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"Expected a positive integer, got {value!r}")
    return value


def relative_paths(values: list[str]) -> tuple[str, ...]:
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Scope must be a list of non-empty repository-relative paths")
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
            raise ValueError(f"Invalid repository-relative path: {value!r}")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate scope paths")
    return tuple(values)


def load_policy(content: str) -> Policy | None:
    data = tomllib.loads(content).get("tool", {}).get("scriptorium_complexity")
    if data is None:
        return None
    allowed = {"max_complexity", "statement_warning", "paths", "advisory_paths", "debt"}
    if set(data) - allowed:
        raise ValueError(f"Unknown complexity policy fields: {sorted(set(data) - allowed)}")
    maximum = positive_integer(data["max_complexity"])
    paths = relative_paths(data["paths"])
    if not paths:
        raise ValueError("The hard-check scope must not be empty")
    debt = {}
    for entry in data.get("debt", []):
        path = relative_paths([entry["path"]])[0]
        name = entry["function"]
        if not isinstance(name, str) or not name.strip() or not entry["reason"].strip():
            raise ValueError("Debt entries require a function name and reason")
        identity = (path, name)
        cap = positive_integer(entry["cap"])
        if identity in debt or cap <= maximum:
            raise ValueError(f"Duplicate or non-excess debt entry: {identity}")
        debt[identity] = cap
    return Policy(
        maximum,
        positive_integer(data["statement_warning"]),
        paths,
        relative_paths(data["advisory_paths"]),
        debt,
    )


def functions(node: ast.AST, prefix: str = ""):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        yield prefix + node.name, node
        prefix += node.name + "."
    elif isinstance(node, ast.ClassDef):
        prefix += node.name + "."
    for child in ast.iter_child_nodes(node):
        yield from functions(child, prefix)


def statement_count(node: ast.AST) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return 1
    return int(isinstance(node, ast.stmt)) + sum(statement_count(child) for child in ast.iter_child_nodes(node))


def measure_source(content: str) -> dict[str, tuple[int, int]]:
    measured = {}
    for name, node in functions(ast.parse(content)):
        visitor = PathGraphingAstVisitor()
        visitor.preorder(node, visitor)
        score = visitor.graphs[node.name].complexity()
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                body = body[1:]
        size = sum(statement_count(statement) for statement in body)
        previous_score, previous_size = measured.get(name, (0, 0))
        # Repeated definitions must not hide an earlier, more complex body.
        measured[name] = (max(score, previous_score), max(size, previous_size))
    return measured


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout


def measure_paths(root: Path, paths: tuple[str, ...], commit: str | None = None):
    if commit is not None:
        names = git(root, "ls-tree", "-r", "--name-only", "-z", commit, "--", *paths).split("\0")
        sources = {name: git(root, "show", f"{commit}:{name}") for name in names if name.endswith(".py")}
    else:
        sources = {}
        for relative in paths:
            path = root / relative
            files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
            if not files or not path.exists():
                raise ValueError(f"Missing or empty scope: {relative}")
            for file in files:
                sources[file.relative_to(root).as_posix()] = file.read_text(encoding="utf-8")
    return {
        (path, name): metrics for path, source in sources.items() for name, metrics in measure_source(source).items()
    }


def check_current(policy: Policy, measured: dict) -> list[str]:
    errors = []
    excessive = {identity: score for identity, (score, _) in measured.items() if score > policy.max_complexity}
    for identity, score in sorted(excessive.items()):
        cap = policy.debt.get(identity)
        if cap is None:
            errors.append(f"{identity}: McCabe {score} exceeds {policy.max_complexity}; no registered debt")
        elif score > cap:
            errors.append(f"{identity}: McCabe {score} exceeds debt cap {cap}")
        elif score < cap:
            errors.append(f"{identity}: lower debt cap from {cap} to {score}")
    for identity in sorted(policy.debt.keys() - excessive.keys()):
        errors.append(f"{identity}: stale debt; remove the entry")
    return errors


def check_base(root: Path, policy: Policy, reference: str) -> list[str]:
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}").strip()
    base = load_policy(git(root, "show", f"{commit}:pyproject.toml"))
    errors = []
    if base is None:
        measured = measure_paths(root, policy.paths, commit)
        caps = {identity: score for identity, (score, _) in measured.items() if score > policy.max_complexity}
    else:
        caps = base.debt
        if policy.max_complexity > base.max_complexity:
            errors.append("The hard complexity threshold must not increase")
        if not set(base.paths).issubset(policy.paths):
            errors.append("The hard-check scope must not narrow")
    for identity, cap in sorted(policy.debt.items()):
        if identity not in caps:
            errors.append(f"{identity}: new debt relative to base {commit}")
        elif cap > caps[identity]:
            errors.append(f"{identity}: debt cap {cap} exceeds base cap {caps[identity]}")
    return errors


def report(policy: Policy, measured: dict, advisory: dict) -> None:
    print(
        f"Core: {len(measured)} function identities; McCabe ceiling {policy.max_complexity}; "
        f"{len(policy.debt)} debt caps"
    )
    for label, entries in (("core", measured), ("research", advisory)):
        for (path, name), (score, size) in sorted(entries.items()):
            if size > policy.statement_warning:
                print(f"Advisory [{label}] {path}:{name}: {size} body statements (>{policy.statement_warning})")
            if label == "research" and score > policy.max_complexity:
                print(f"Advisory [research] {path}:{name}: McCabe {score}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check complexity debt without modifying source files.")
    parser.add_argument("--base", help="Explicit Git base commit for debt and policy comparison")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        policy = load_policy((root / "pyproject.toml").read_text(encoding="utf-8"))
        if policy is None:
            raise ValueError("Missing tool.scriptorium_complexity policy")
        measured = measure_paths(root, policy.paths)
        advisory = measure_paths(root, policy.advisory_paths) if policy.advisory_paths else {}
        errors = check_current(policy, measured)
        if args.base:
            errors.extend(check_base(root, policy, args.base))
        report(policy, measured, advisory)
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if not args.base:
            print("Current-tree check only; use --base COMMIT to enforce the historical debt ratchet.")
        return 1 if errors else 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        print(f"Complexity check could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

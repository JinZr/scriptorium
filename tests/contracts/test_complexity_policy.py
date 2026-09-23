from dataclasses import replace
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def checker():
    path = Path(__file__).resolve().parents[2] / "utils" / "check_complexity.py"
    spec = importlib.util.spec_from_file_location("scriptorium_complexity_check", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _function(score, name="work"):
    return f"def {name}():\n" + "    if True:\n        pass\n" * (score - 1) + "    return None\n"


def _policy_text(*, cap=None, maximum=20, paths='["src"]', name="work"):
    text = (
        "[tool.scriptorium_complexity]\n"
        f"max_complexity = {maximum}\n"
        "statement_warning = 80\n"
        f"paths = {paths}\n"
        "advisory_paths = []\n"
    )
    if cap is not None:
        text += (
            "[[tool.scriptorium_complexity.debt]]\n"
            'path = "src/code.py"\n'
            f'function = "{name}"\n'
            f"cap = {cap}\n"
            'reason = "Existing lifecycle coordinator"\n'
        )
    return text


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path, *, score=23, cap=None, initialized=False):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "code.py").write_text(_function(score), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        _policy_text(cap=cap) if initialized else "[project]\nname = 'fixture'\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Complexity Test",
        "-c",
        "user.email=complexity@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Baseline",
    )
    return _git(tmp_path, "rev-parse", "HEAD")


@pytest.mark.parametrize("score,passes", [(20, True), (21, False)])
def test_complexity_boundary(checker, score, passes):
    policy = checker.load_policy(_policy_text())
    metrics = checker.measure_source(_function(score))
    assert metrics["work"][0] == score
    errors = checker.check_current(policy, {("src/code.py", name): value for name, value in metrics.items()})
    assert (not errors) == passes


@pytest.mark.parametrize("score,message", [(24, "exceeds debt cap"), (22, "lower debt cap"), (20, "stale debt")])
def test_registered_debt_must_match_current_score(checker, score, message):
    policy = checker.load_policy(_policy_text(cap=23))
    errors = checker.check_current(policy, {("src/code.py", "work"): (score, 10)})
    assert any(message in error for error in errors)


def test_debt_removal_and_rename_are_not_hidden(checker):
    policy = checker.load_policy(_policy_text(cap=23))
    assert "stale debt" in checker.check_current(policy, {})[0]
    errors = checker.check_current(policy, {("src/code.py", "renamed"): (23, 10)})
    assert len(errors) == 2
    assert any("no registered debt" in error for error in errors)
    assert any("stale debt" in error for error in errors)


def test_duplicate_and_nested_definitions_are_measured(checker):
    source = _function(23) + _function(1)
    source += (
        "class Owner:\n    async def work(self):\n        def inner():\n            if True:\n                pass\n"
    )
    measured = checker.measure_source(source)
    assert measured["work"][0] == 23
    assert measured["Owner.work.inner"][0] == 2
    assert "Owner.work" in measured


def test_statement_count_excludes_only_leading_docstring_and_nested_bodies(checker):
    measured = checker.measure_source(
        'def work():\n    "docstring"\n    "ordinary string"\n'
        "    if True:\n        pass\n"
        "    def inner():\n        first = 1\n        second = 2\n"
        "    class Local:\n        value = 3\n"
        "    return None\n"
    )
    assert measured["work"][1] == 6
    assert measured["work.inner"][1] == 2


def test_size_and_research_complexity_are_advisory(checker, capsys):
    policy = checker.load_policy(_policy_text())
    measured = {("src/code.py", "flat"): (1, 81)}
    assert checker.check_current(policy, measured) == []
    checker.report(policy, measured, {("egs/code.py", "research"): (40, 90)})
    output = capsys.readouterr().out
    assert "Advisory [core]" in output
    assert "Advisory [research]" in output


def test_noqa_exclusions_and_untracked_files_cannot_hide_complexity(checker, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".flake8").write_text("[flake8]\nexclude = src\nignore = C901\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("src/\n", encoding="utf-8")
    source = _function(21).replace("def work():", "def work():  # noqa: C901")
    (tmp_path / "src" / "code.py").write_text(source, encoding="utf-8")
    measured = checker.measure_paths(tmp_path, ("src",))
    assert measured[("src/code.py", "work")][0] == 21
    assert checker.check_current(checker.load_policy(_policy_text()), measured)


@pytest.mark.parametrize("cap,passes", [(23, True), (22, True), (24, False)])
def test_bootstrap_caps_are_bounded_by_base_source(checker, tmp_path, cap, passes):
    base = _repository(tmp_path)
    # Changing current source must not change the historical bootstrap allowance.
    (tmp_path / "src" / "code.py").write_text(_function(30), encoding="utf-8")
    errors = checker.check_base(tmp_path, checker.load_policy(_policy_text(cap=cap)), base)
    assert (not errors) == passes


@pytest.mark.parametrize("score,name", [(20, "work"), (23, "new_function")])
def test_bootstrap_rejects_new_debt(checker, tmp_path, score, name):
    base = _repository(tmp_path, score=score)
    errors = checker.check_base(tmp_path, checker.load_policy(_policy_text(cap=23, name=name)), base)
    assert "new debt" in errors[0]


@pytest.mark.parametrize("cap,passes", [(23, True), (22, True), (24, False)])
def test_existing_caps_cannot_increase_against_base(checker, tmp_path, cap, passes):
    base = _repository(tmp_path, cap=23, initialized=True)
    errors = checker.check_base(tmp_path, checker.load_policy(_policy_text(cap=cap)), base)
    assert (not errors) == passes


def test_existing_policy_cannot_add_debt_even_if_base_source_is_complex(checker, tmp_path):
    base = _repository(tmp_path, initialized=True)
    errors = checker.check_base(tmp_path, checker.load_policy(_policy_text(cap=23)), base)
    assert "new debt" in errors[0]


def test_hard_policy_cannot_weaken(checker, tmp_path):
    base = _repository(tmp_path, cap=23, initialized=True)
    policy = checker.load_policy(_policy_text(cap=23))
    assert "threshold" in checker.check_base(tmp_path, replace(policy, max_complexity=21), base)[0]
    assert "scope" in checker.check_base(tmp_path, replace(policy, paths=("src/code.py",)), base)[0]


def test_missing_base_does_not_fall_back(checker, tmp_path):
    _repository(tmp_path)
    with pytest.raises(ValueError):
        checker.check_base(tmp_path, checker.load_policy(_policy_text()), "does-not-exist")


@pytest.mark.parametrize(
    "content", [_policy_text(paths="[]"), _policy_text(paths='["../src"]'), _policy_text(maximum=0)]
)
def test_invalid_policy_is_rejected(checker, content):
    with pytest.raises(ValueError):
        checker.load_policy(content)


def test_duplicate_debt_is_rejected(checker):
    content = _policy_text(cap=23)
    debt = "[[tool.scriptorium_complexity.debt]]" + content.split("[[tool.scriptorium_complexity.debt]]")[1]
    with pytest.raises(ValueError, match="Duplicate"):
        checker.load_policy(content + debt)


def test_cli_policy_removal_and_base_failure_are_errors(checker, tmp_path, monkeypatch, capsys):
    base = _repository(tmp_path, cap=23, initialized=True)
    monkeypatch.setattr(checker, "__file__", str(tmp_path / "utils" / "check_complexity.py"))
    assert checker.main(["--base", base]) == 0
    assert checker.main(["--base", "missing-base"]) == 2
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    assert checker.main(["--base", base]) == 2
    assert "Missing tool.scriptorium_complexity" in capsys.readouterr().err


def test_cli_without_base_describes_its_limit(checker, tmp_path, monkeypatch, capsys):
    _repository(tmp_path, score=20, initialized=True)
    monkeypatch.setattr(checker, "__file__", str(tmp_path / "utils" / "check_complexity.py"))
    assert checker.main([]) == 0
    assert "Current-tree check only" in capsys.readouterr().out

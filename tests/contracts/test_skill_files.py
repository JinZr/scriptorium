import argparse
from pathlib import Path
import re

import pytest

from scriptorium.cli import build_parser

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
SKILLS = ("scriptorium", "scriptorium-review")
WORD_BUDGET = 900
PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _skill(name: str) -> str:
    return (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")


def _front_matter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text[4:].split("\n---\n", 1)[0]
    return dict(line.split(": ", 1) for line in block.splitlines())


def _quick_reference(text: str) -> list[list[str]]:
    section = text.split("\n## Quick reference\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```text\n(.*?)\n```", section, re.DOTALL)
    assert len(blocks) == 1
    return [line.split("->", 1)[0].split() for line in blocks[0].splitlines() if line.startswith("scriptorium ")]


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    actions = [item for item in parser._actions if isinstance(item, argparse._SubParsersAction)]
    return actions[0].choices if actions else {}


def _commands(tokens: list[str]) -> tuple[list[argparse.ArgumentParser], int]:
    """Resolve `group command` or a top-level command, where `a|b` names alternative commands."""
    parsers, index = [build_parser()], 2
    while index < len(tokens) and _subcommands(parsers[0]):
        parsers = [_subcommands(parser)[name] for parser in parsers for name in tokens[index].split("|")]
        index += 1
    return parsers, index


def _options(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {option: action for action in parser._actions for option in action.option_strings}


@pytest.mark.parametrize("name", SKILLS)
def test_skill_has_front_matter_and_fits_its_word_budget(name: str) -> None:
    text = _skill(name)
    front = _front_matter(text)
    assert front["name"] == name
    assert front["description"].strip()
    assert len(text.split()) <= WORD_BUDGET


@pytest.mark.parametrize("name", SKILLS)
def test_skill_reference_pages_exist(name: str) -> None:
    for relative in re.findall(r"`(reference/[^`<]+\.md)`", _skill(name)):
        assert (SKILLS_ROOT / name / relative).is_file(), relative


@pytest.mark.parametrize("name", SKILLS)
def test_quick_reference_commands_match_the_parser(name: str) -> None:
    lines = _quick_reference(_skill(name))
    assert lines
    for tokens in lines:
        assert tokens[:2] == ["scriptorium", "--json"], tokens
        commands, start = _commands(tokens)
        options = {}
        for command in commands:
            options.update(_options(command))
        for index, token in enumerate(tokens[start:], start=start):
            token = token.strip("[]")
            if not token.startswith("--"):
                continue
            for option in token.split("|"):
                assert option in options, (tokens, option)
            action = options[token.split("|")[0]]
            value = tokens[index + 1].strip("[]") if index + 1 < len(tokens) else ""
            if action.choices and value and not PLACEHOLDER.match(value):
                assert set(value.split("|")) <= set(action.choices), (tokens, value)

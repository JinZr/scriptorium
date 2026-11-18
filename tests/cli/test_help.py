import argparse

from scriptorium import cli


def _parsers(parser, path=("scriptorium",)):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                assert helps.get(name), f"{' '.join((*path, name))} has no help"
                yield from _parsers(child, (*path, name))


def test_every_command_and_visible_option_has_help():
    missing = [
        f"{' '.join(path)} {action.option_strings or action.dest}"
        for path, parser in _parsers(cli.build_parser())
        for action in parser._actions
        if not isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))
        and action.help != argparse.SUPPRESS
        and not action.help
    ]
    assert missing == []

import random

import pytest

from scriptorium.errors import ConfigurationError
from scriptorium.tool_output import _fit_prefix, bound_read, fits_response, read_anchor, tool_command


def _linear_bound_read(response, attempt_id, max_lines, max_chars, anchor):
    # The original per-line scan, kept as an independent reference for the bisected implementation.
    def build(pieces, line, offset):
        arguments = ["read", attempt_id, f"--path={response['path']}", "--start-line", line, "--offset", offset]
        arguments += ["--max-lines", max_lines, "--max-chars", max_chars] + (["--anchor"] if anchor else [])
        result = {
            **response,
            "lines": pieces,
            "next_line": line,
            "next_offset": offset,
            "next_command": tool_command(*arguments) if line is not None else None,
        }
        if anchor:
            result["anchor"] = read_anchor(response, pieces, line)
        return result

    pieces = response["lines"]
    full = build(pieces, response["next_line"], response["next_offset"])
    if fits_response(full):
        return full
    returned = []
    for index, piece in enumerate(pieces):
        following = pieces[index + 1] if index + 1 < len(pieces) else None
        line = following["line"] if following else response["next_line"]
        offset = following["offset"] if following else response["next_offset"]
        if fits_response(build([*returned, piece], line, offset)):
            returned.append(piece)
            continue

        def fragment(length):
            prefix = [*returned, {**piece, "text": piece["text"][:length]}] if length else returned
            return build(prefix, piece["line"], piece["offset"] + length)

        return _fit_prefix(len(piece["text"]), fragment)[1]
    raise AssertionError("unreachable")


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("anchor", [False, True])
def test_bisected_read_bounding_matches_the_linear_scan(seed, anchor):
    generator = random.Random(seed)
    alphabet = 'ab "\\\t实验🧬'
    pieces = [
        {
            "line": 7 + index,
            "offset": 0,
            "text": "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 900))),
        }
        for index in range(generator.randint(1, 60))
    ]
    response = {
        "path": "sources/main.tex",
        "source_path": "main.tex",
        "source_digest": "a" * 64,
        "lines": pieces,
        "next_line": pieces[-1]["line"] + 1,
        "next_offset": 0,
    }
    expected = _linear_bound_read(response, "attempt_1", 100, 8000, anchor)
    actual = bound_read(response, "attempt_1", 100, 8000, anchor=anchor)
    assert actual == expected
    assert fits_response(expected)


def test_metadata_without_text_fails_explicitly():
    source_path = "d" * 4000
    response = {
        "path": f"sources/{source_path}",
        "source_path": source_path,
        "source_digest": "a" * 64,
        "lines": [],
        "next_line": None,
        "next_offset": None,
    }
    with pytest.raises(ConfigurationError, match="exceeds the 7000-byte tool response limit"):
        bound_read(response, "attempt_1", 100, 8000)

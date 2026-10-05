import io

import pytest

from scriptorium.manuscript import source_lines

CASES = [
    "",
    "one",
    "one\n",
    "one\ntwo",
    "one\r\ntwo\rthree\n",
    "alpha\fbeta\vgamma\x1c\x1d\x1e\x85  end\n",
    "\n\n",
]


@pytest.mark.parametrize("text", CASES)
def test_source_lines_match_universal_newline_retrieval(text):
    with io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8") as stream:
        retrieved = [line.removesuffix("\n") for line in stream]
    assert source_lines(text) == retrieved
    assert "".join(source_lines(text, keepends=True)) == text


def test_source_lines_keep_form_feeds_inside_a_line():
    assert source_lines("a\fb\nc") == ["a\fb", "c"]
    assert source_lines("a\fb\r\nc", keepends=True) == ["a\fb\r\n", "c"]

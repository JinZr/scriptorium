import tracemalloc

import pytest

from scriptorium.errors import ConfigurationError
from scriptorium.service import _read_text_window


def test_streamed_read_preserves_line_and_offset_continuations(tmp_path):
    path = tmp_path / "source.tex"
    path.write_text("a\nbb\nccc\n", encoding="utf-8")

    assert _read_text_window(path, 1, 2, 0, 2) == (
        [{"line": 1, "offset": 0, "text": "a"}, {"line": 2, "offset": 0, "text": "b"}],
        2,
        1,
    )
    assert _read_text_window(path, 2, 2, 2, 2) == (
        [{"line": 2, "offset": 2, "text": ""}, {"line": 3, "offset": 0, "text": "cc"}],
        3,
        2,
    )
    assert _read_text_window(path, 3, 1, 0, 3) == ([{"line": 3, "offset": 0, "text": "ccc"}], None, None)
    with pytest.raises(ConfigurationError, match="start line"):
        _read_text_window(path, 4, 1, 0, 10)
    with pytest.raises(ConfigurationError, match="offset"):
        _read_text_window(path, 2, 1, 3, 10)


def test_streamed_read_does_not_materialize_a_large_line(tmp_path):
    path = tmp_path / "long.tex"
    path.write_text("x" * 5_000_000 + "\nnext\n", encoding="utf-8")

    tracemalloc.start()
    try:
        pieces, next_line, next_offset = _read_text_window(path, 1, 1, 4_999_000, 100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert pieces == [{"line": 1, "offset": 4_999_000, "text": "x" * 100}]
    assert (next_line, next_offset) == (1, 4_999_100)
    assert peak < 1_000_000

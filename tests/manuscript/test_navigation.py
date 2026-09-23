from hashlib import sha256
import json
from pathlib import Path

import pytest

from scriptorium.manuscript import ManuscriptManager


def test_navigation_is_deterministic_and_locations_match_literal_sources(tmp_path):
    text = (
        "% \\section{ignored}\n"
        "\\begin{verbatim}\n\\label{ignored}\n\\end{verbatim}\n"
        "\\verb|\\cite{ignored}|\n"
        "\\section*[Short]{Results with \\textbf{detail}}\n"
        "\\label{sec:result}\n"
        "See \\cref{fig:x,tab:y} and \\citep[see][p. 2]{a,b}.\n"
        "\\caption{First line\nsecond line}\n"
        "\\includegraphics*[width=2cm]{plot}\n"
        "\\input{appendix}\n"
    )
    (tmp_path / "main.tex").write_text(text)
    (tmp_path / "appendix.tex").write_text("\\subsection{Supplement}\\label{tab:y}\n")
    (tmp_path / "plot.png").write_bytes(b"frozen image")
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, "main.tex")
    content = manager.create_navigation(tmp_path, sources)
    assert manager.create_navigation(tmp_path, tuple(reversed(sources))) == content
    navigation = json.loads(content)
    assert "ignored" not in content
    for entry in navigation["entries"]:
        lines = (tmp_path / entry["source_path"]).read_text().splitlines()
        excerpt = "\n".join(lines[entry["start_line"] - 1 : entry["end_line"]])
        assert "\\" + entry["command"] in excerpt
        assert entry["value"] in excerpt
    entries = {entry["command"]: entry for entry in navigation["entries"] if entry["source_path"] == "main.tex"}
    assert entries["section"]["start_line"] == 6
    assert entries["section"]["value"] == r"Results with \textbf{detail}"
    assert entries["caption"]["start_line"] == 9
    assert entries["caption"]["end_line"] == 10
    assert entries["cref"]["value"] == "fig:x,tab:y"
    assert entries["includegraphics"]["target_path"] == "plot.png"
    assert all("page" not in entry for entry in navigation["entries"])
    assert navigation["sources"][0]["digest"] == sha256((tmp_path / sources[0].path).read_bytes()).hexdigest()


def test_navigation_does_not_guess_ambiguous_graphics_or_execute_macros(tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\includegraphics{figures/plot.png}\n\\includegraphics{other/plot.png}\n" "\\input{extra}\n"
    )
    (tmp_path / "extra.tex").write_text("Text\n")
    for folder in ("figures", "other"):
        (tmp_path / folder / "plot.png").write_bytes(b"image")
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, "main.tex")
    assert manager._navigation_graphics("plot", sources) == ["figures/plot.png", "other/plot.png"]
    assert manager._navigation_graphics(r"\dynamic", sources) == []
    assert manager._navigation_graphics("../outside", sources) == []
    masked = manager._dependency_text(r"\\section{escaped} \section{unclosed", preserve_positions=True)
    assert list(manager._navigation_commands(masked)) == []


def test_navigation_preserves_escaped_argument_text_and_line_positions(tmp_path):
    text = "\\section{A \\{group\\} at 50\\%}\n% comment\n\\caption{one% ignored\ntwo}\n"
    (tmp_path / "main.tex").write_text(text)
    manager = ManuscriptManager(tmp_path)
    masked = manager._dependency_text(text, preserve_positions=True)
    assert len(masked) == len(text)
    assert [i for i, char in enumerate(masked) if char == "\n"] == [i for i, char in enumerate(text) if char == "\n"]
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    assert navigation["entries"][0]["value"] == r"A \{group\} at 50\%"
    assert navigation["entries"][1]["value"] == "one% ignored\ntwo"
    assert navigation["entries"][1]["start_line"] == 3
    assert navigation["entries"][1]["end_line"] == 4


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/retrieval_cases.json").read_text()),
    ids=lambda case: case["name"],
)
def test_retrieval_comparison_cases_keep_supplementary_context_reachable(tmp_path, case):
    for name, content in case["files"].items():
        (tmp_path / name).write_text(content)
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, "main.tex")
    navigation = json.loads(manager.create_navigation(tmp_path, sources))
    assert {source["path"] for source in navigation["sources"]} == set(case["files"])
    assert any(entry["command"] == "label" and entry["value"] == "sec:supp" for entry in navigation["entries"])
    for location in case["relevant_locations"]:
        assert (tmp_path / location["source_path"]).read_text().splitlines()[location["start_line"] - 1]

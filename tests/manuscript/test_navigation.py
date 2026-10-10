from hashlib import sha256
import json
from pathlib import Path

import pytest

from scriptorium.config import ManuscriptConfig
from scriptorium.manuscript import QUANTITY_COMMANDS, ManuscriptManager
from scriptorium.schemas import DEFAULT_EVIDENCE_ANCHOR_CONTRACT


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
    assert "quantity" not in {entry["command"] for entry in navigation["entries"]}
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
    section, percentage, caption = navigation["entries"]
    assert section["value"] == r"A \{group\} at 50\%"
    assert (percentage["command"], percentage["value"]) == ("quantity", r"50\%")
    assert caption["value"] == "one% ignored\ntwo"
    assert caption["start_line"] == 3
    assert caption["end_line"] == 4


def test_navigation_indexes_tables_equations_and_reported_quantities(tmp_path):
    preamble = "\\documentclass{article}\n\\usepackage[margin=1.5in]{geometry}\n\\setlength{\\parskip}{0.5em}\n"
    body = (
        "\\section{Results 2}\\label{sec:3.2}\n"
        "The energy is $-7.49$~eV/atom, within 0.1 eV/atom (code version 3.10.2, year 2017, Fig.~2a).\n"
        "Errors stay below 1\\% and $E = -0.572922(1)$ with $\\alpha = 1.2 \\pm 0.3$ and\n"
        "$\\tau = 2.5\\times 10^{-3}$.\n"
        "Use \\SI{300}{\\kelvin}, \\num{1.5e-3}, and \\numrange{3}{4}{5.5} widths.\n"
        "\\includegraphics[width=0.48\\linewidth]{plot}\\vspace{0.3cm} see \\cite{a1.2}\n"
        "% a commented 0.75 eV\n"
        "\\begin{table}[h]\n\\begin{tabular}{p{0.3\\linewidth}c}\nH10 & \\multicolumn{2}{c}{0.57}\\\\\n"
        "\\end{tabular}\n\\end{table}\n"
        "\\begin{equation}\nE_c = 0.113 \\label{eq:1}\n\\end{equation}\n"
    )
    text = preamble + "\\begin{document}\n" + body + "\\end{document}\n"
    (tmp_path / "main.tex").write_text(text)
    (tmp_path / "plot.png").write_bytes(b"image")
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    assert {"table", "equation", "quantity", "SI", "numrange"} <= set(navigation["commands"])
    entries = [(entry["command"], entry["start_line"], entry["end_line"]) for entry in navigation["entries"]]
    assert ("table", 12, 16) in entries and ("tabular", 13, 15) in entries and ("equation", 17, 19) in entries
    lines = text.splitlines()
    quantities = []
    for entry in navigation["entries"]:
        excerpt = "\n".join(lines[entry["start_line"] - 1 : entry["end_line"]])
        assert entry["value"] in excerpt
        if entry["command"] in {"quantity", "SI", "num", "numrange"}:
            quantities.append((entry["command"], entry["value"]))
    assert quantities == [
        ("quantity", "-7.49$~eV/atom"),
        ("quantity", "0.1 eV/atom"),
        ("quantity", r"1\%"),
        ("quantity", "-0.572922(1)"),
        ("quantity", r"1.2 \pm 0.3"),
        ("quantity", r"2.5\times 10^{-3}"),
        ("SI", r"{300}{\kelvin}"),
        ("num", "{1.5e-3}"),
        ("numrange", "{3}{4}"),
        ("quantity", "5.5"),
        ("quantity", "0.57"),
        ("quantity", "0.113"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "\\section{One}\r% note\r\\section{Two}\r\\caption{a\rb}\r",
        "\\section{One}\r\n% note\r\n\\section{Two}\r\n\\caption{a\r\nb}\r\n",
        "\\section{One}\n\x0c\\section{Two}\r\n\u2028x\r\\caption{a\rb}\n\x85\\label{z}\n",
    ],
    ids=["cr", "crlf", "mixed"],
)
def test_navigation_lines_follow_the_frozen_line_rule(tmp_path, body):
    # Task read numbers lines by the run's frozen rule; navigation must point at the same lines for any ending.
    (tmp_path / "main.tex").write_text("\\input{part}\n", encoding="utf-8")
    (tmp_path / "part.tex").write_bytes(body.encode("utf-8"))
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    lines = DEFAULT_EVIDENCE_ANCHOR_CONTRACT.split_lines(body)
    entries = [entry for entry in navigation["entries"] if entry["source_path"] == "part.tex"]
    assert [entry["command"] for entry in entries][:3] == ["section", "section", "caption"]
    for entry in entries:
        span = "\n".join(lines[entry["start_line"] - 1 : entry["end_line"]])
        assert entry["command"] == "quantity" or "\\" + entry["command"] in span
        assert all(piece in span for piece in entry["value"].split("\n"))


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


def _quantities(tmp_path, files):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    return [
        (entry["source_path"], entry["command"], entry["value"])
        for entry in navigation["entries"]
        if entry["command"] in {"quantity", "alignat", "alignat*", *QUANTITY_COMMANDS}
    ]


def test_wrapper_content_and_control_word_neighbours_keep_their_quantities(tmp_path):
    body = (
        "\\resizebox{0.9\\textwidth}{!}{Gap 1.5 eV}\n"
        "\\scalebox{0.8}[1.2]{Loss 0.25}\n"
        "\\href{https://example.org/v2.1}{Rate 3.5\\%}\n"
        "$x\\approx0.5$ and $y\\approx-0.7$ but $\\pm0.3$ stays attached in $2.0\\pm0.1$\n"
        "The current was 1.5 A and 2.5 a day.\n"
        "\\begin{alignat}{2}\nE &= 0.113\n\\end{alignat}\n"
    )
    quantities = _quantities(tmp_path, {"main.tex": "\\begin{document}\n" + body + "\\end{document}\n"})
    assert [(command, value) for _, command, value in quantities] == [
        ("quantity", "1.5 eV"),
        ("quantity", "0.25"),
        ("quantity", r"3.5\%"),
        ("quantity", "0.5"),
        ("quantity", "-0.7"),
        ("quantity", r"2.0\pm0.1"),
        ("quantity", "1.5 A"),
        ("quantity", "2.5"),
        ("alignat", "E &= 0.113"),
        ("quantity", "0.113"),
    ]


def test_sources_input_before_the_document_report_no_quantities(tmp_path):
    files = {
        "main.tex": "\\documentclass{article}\n\\input{preamble}\n\\begin{document}\n\\input{body}\n\\end{document}\n",
        "preamble.tex": "\\pgfplotsset{compat=1.18}\n\\input{macros}\n",
        "macros.tex": "\\def\\ratio{0.75}\n",
        "body.tex": "The gap is 1.25 eV.\n\\input{shared}\n",
        "shared.tex": "A loss of 0.5\\%.\n",
    }
    assert _quantities(tmp_path, files) == [
        ("body.tex", "quantity", "1.25 eV"),
        ("shared.tex", "quantity", r"0.5\%"),
    ]


def test_environment_bodies_grouped_digits_and_the_document_end_bound_quantities(tmp_path):
    body = (
        "\\begin{equation}{E=0.5}\\end{equation}\n"
        "\\begin{table}[h]{\\small Loss 0.25}\\end{table}\n"
        "\\begin{tabular*}{0.9\\linewidth}{@{}cc@{}}\n1,234.5 & 2{,}000.25\\\\\n\\end{tabular*}\n"
        "\\begin{minipage}[t]{0.45\\linewidth}Gap 1.5 eV\\end{minipage}\n"
    )
    text = "\\begin{document}\n" + body + "\\end{document}\nExpected 2.5 after the end.\n"
    assert [value for _, command, value in _quantities(tmp_path, {"main.tex": text}) if command == "quantity"] == [
        "0.5",
        "0.25",
        "1,234.5",
        "2{,}000.25",
        "1.5 eV",
    ]


def test_versions_references_postscript_inputs_and_powered_units(tmp_path):
    files = {
        "main.tex": (
            "\\begin{document}\n"
            "Compiled with Python 3.10 and CUDA~11.8, as in Table~2.1 and Eq.~(3.4).\n"
            "The flux is 1.5 m^{-2} over 2.5 \\mathrm{m^{2}} at \\SI{3.5}{K}.\n"
            "\\end{document}\n"
            "\\SI{9.5}{K} and \\input{notes}\n"
        ),
        "notes.tex": "Untypeset 4.5 eV.\n",
    }
    assert _quantities(tmp_path, files) == [
        ("main.tex", "quantity", "1.5 m^{-2}"),
        ("main.tex", "quantity", "2.5 \\mathrm{m^{2}}"),
        ("main.tex", "SI", "{3.5}{K}"),
    ]


def test_attached_exponent_units_and_arguments_on_the_next_line(tmp_path):
    files = {
        "main.tex": (
            "\\begin{document}\n"
            "Gaps of 1.2e-3eV and 1.2E5Pa.\n"
            "\\ref\n{fig-1.5} \\cite\n  {study-2.0}\n"
            "\\label{x}\n{7.5 K}\n"
            "\\end{document}\n"
        ),
    }
    assert _quantities(tmp_path, files) == [
        ("main.tex", "quantity", "1.2e-3eV"),
        ("main.tex", "quantity", "1.2E5Pa"),
        ("main.tex", "quantity", "7.5 K"),
    ]


def test_row_spacing_split_settings_and_environment_arguments_stay_out_of_values(tmp_path):
    files = {
        "main.tex": (
            "\\begin{document}\n"
            "\\begin{table}[h]\n\\begin{tabular}[t]{cc}\n0.5 & 1.5 m \\\\[1.5mm]\n\\end{tabular}\n\\end{table}\n"
            "\\newcommand{\\score}\n{2.5}\n"
            "\\end{document}\n"
        ),
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    values = [(entry["command"], entry["value"]) for entry in navigation["entries"]]
    assert ("quantity", "0.5") in values and ("quantity", "1.5 m") in values
    assert [value for command, value in values if command == "quantity"] == ["0.5", "1.5 m"]
    assert dict(values)["tabular"].startswith("0.5 & 1.5 m")
    assert dict(values)["table"].startswith("\\begin{tabular}")


def test_range_bounds_siunitx_lists_and_units_stop_at_prose_after_math(tmp_path):
    body = (
        "Gaps span 1.2--1.5 eV, \\numlist{0.1;0.2}, \\qtylist{1;2}{\\metre}, and \\SIproduct{2 x 3}{\\metre}.\n"
        "A $p=0.05$ threshold, $0.25$~eV, and $0.5$ \\si{K}.\n"
        "Loss 0.75\nwas low.\n"
    )
    quantities = _quantities(tmp_path, {"main.tex": "\\begin{document}\n" + body + "\\end{document}\n"})
    assert [(command, value) for _, command, value in quantities] == [
        ("quantity", "1.2"),
        ("quantity", "1.5 eV"),
        ("numlist", "{0.1;0.2}"),
        ("qtylist", "{1;2}{\\metre}"),
        ("SIproduct", "{2 x 3}{\\metre}"),
        ("quantity", "0.05"),
        ("quantity", "0.25$~eV"),
        ("quantity", "0.5$ \\si{K}"),
        ("quantity", "0.75"),
    ]


def test_document_state_follows_inputs_in_processing_order(tmp_path):
    files = {
        "main.tex": "\\documentclass{article}\n\\input{settings}\n\\input{paper}\n\\input{notes}\n",
        "settings.tex": "\\def\\ratio{0.75}\n",
        "paper.tex": "\\begin{document}\nThe gap is 1.25 eV.\n\\input{body}\n\\end{document}\n",
        "body.tex": "A loss of 0.5\\%.\n",
        "notes.tex": "Untypeset 4.5 eV.\n",
    }
    assert _quantities(tmp_path, files) == [
        ("body.tex", "quantity", r"0.5\%"),
        ("paper.tex", "quantity", "1.25 eV"),
    ]


def test_primitive_skips_definitions_and_tags_are_not_quantities_and_siunitx_angles_are(tmp_path):
    body = (
        "\\hskip 1.5cm\\vskip 2.5mm plus 1fil\\kern-0.5em\n"
        "\\def\\arraystretch{1.5}\n"
        "\\begin{equation}E=mc^2\\tag{2.1}\\end{equation}\n"
        "\\ang{30} and \\complexqty{1+2i}{\\ohm} with \\complexnum{3-4i}.\n"
        "\\(1.5\\)~eV and \\[2.5\\]~eV but \\(3.5\\) eV prose.\n"
        "\\begin{tabulary}{\\linewidth}{LC}\nGap & 0.5 \\\\\n\\end{tabulary}\n"
    )
    quantities = _quantities(tmp_path, {"main.tex": "\\begin{document}\n" + body + "\\end{document}\n"})
    assert [(command, value) for _, command, value in quantities] == [
        ("ang", "{30}"),
        ("complexqty", "{1+2i}{\\ohm}"),
        ("complexnum", "{3-4i}"),
        ("quantity", "1.5\\)~eV"),
        ("quantity", "2.5\\]~eV"),
        ("quantity", "3.5"),
        ("quantity", "0.5"),
    ]
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    assert "tabulary" in {entry["command"] for entry in navigation["entries"]}


def test_configured_entrypoints_stay_document_roots_when_another_source_inputs_them(tmp_path):
    (tmp_path / "main.tex").write_text("\\begin{document}\nGap 1.25 eV.\n\\end{document}\n\\input{supplement}\n")
    (tmp_path / "supplement.tex").write_text("\\begin{document}\nLoss 0.5\\%.\n\\end{document}\n")
    manager = ManuscriptManager(tmp_path)
    config = ManuscriptConfig(main="main.tex", engine="pdflatex", supplements=("./supplement.tex",))
    sources = manager.scan_project_sources(tmp_path, config)
    navigation = json.loads(manager.create_navigation(tmp_path, sources, config.entrypoints))
    assert [
        (entry["source_path"], entry["value"]) for entry in navigation["entries"] if entry["command"] == "quantity"
    ] == [
        ("main.tex", "1.25 eV"),
        ("supplement.tex", "0.5\\%"),
    ]
    unconfigured = json.loads(manager.create_navigation(tmp_path, sources))
    assert [entry["source_path"] for entry in unconfigured["entries"] if entry["command"] == "quantity"] == ["main.tex"]


def test_definitions_hide_siunitx_calls_and_display_math_keeps_a_leading_bracket(tmp_path):
    body = (
        "\\newcommand{\\defaulttemp}{\\SI{300}{K}}\n"
        "\\begin{equation}[0.5, 1.0]\\end{equation}\n"
        "\\begin{table}[0.5]\n\\end{table}\n"
        "\\scalebox{0.8}[1.2]{Loss 0.25}\n"
    )
    quantities = _quantities(tmp_path, {"main.tex": "\\begin{document}\n" + body + "\\end{document}\n"})
    assert [(command, value) for _, command, value in quantities] == [
        ("quantity", "0.5"),
        ("quantity", "1.0"),
        ("quantity", "0.25"),
    ]


def test_long_environment_bodies_are_stored_as_a_bounded_prefix(tmp_path):
    rows = "".join(f"{index} & {index}.5 \\\\\n" for index in range(400))
    (tmp_path / "main.tex").write_text("\\begin{tabular}{cc}\n" + rows + "\\end{tabular}\n")
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    (entry,) = [entry for entry in navigation["entries"] if entry["command"] == "tabular"]
    assert len(entry["value"]) == 2000 and rows.startswith(entry["value"])
    assert entry["value_truncated"] is True
    assert (entry["start_line"], entry["end_line"]) == (1, 402)


def test_display_brackets_powers_of_ten_and_post_document_environments(tmp_path):
    main = (
        "\\begin{document}\n"
        "A rate of $10^{-3}$ and $10^{-3.5}$, row \\\\[2mm] break, % \\[ ignored \\]\n"
        "\\[\nE = 0.5\n\\]\n"
        "\\end{document}\n"
        "\\begin{table}\nCleanup 0.5\n\\end{table}\n\\input{late}\n"
    )
    (tmp_path / "main.tex").write_text(main)
    (tmp_path / "late.tex").write_text("\\begin{equation}x=1\\end{equation}\n")
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    entries = [
        (entry["command"], entry["value"], entry["start_line"], entry["end_line"]) for entry in navigation["entries"]
    ]
    assert entries == [
        ("quantity", "10^{-3}", 2, 2),
        ("quantity", "10^{-3.5}", 2, 2),
        ("displaymath", "E = 0.5", 3, 5),
        ("quantity", "0.5", 4, 4),
    ]


def test_fixed_arity_settings_definition_displays_and_bracketed_table_bodies(tmp_path):
    body = (
        "\\label{result} {0.5 eV} and \\citep[p.~2]{key} {1.5 K}\n"
        "\\newcommand{\\foo}{\\begin{equation}x=2.5\\end{equation}\\[y\\]}\n"
        "\\begin{tabular}{c}[0.25, 0.75] \\\\\n\\end{tabular}\n"
        "\\begin{table}[h][3.5]\n\\end{table}\n"
    )
    (tmp_path / "main.tex").write_text("\\begin{document}\n" + body + "\\end{document}\n")
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    entries = [
        (entry["command"], entry["value"])
        for entry in navigation["entries"]
        if entry["command"] not in {"label", "citep"}
    ]
    assert entries == [
        ("quantity", "0.5 eV"),
        ("quantity", "1.5 K"),
        ("tabular", "[0.25, 0.75] \\\\"),
        ("quantity", "0.25"),
        ("quantity", "0.75"),
        ("table", "[3.5]"),
        ("quantity", "3.5"),
    ]


def test_prose_words_are_not_units_and_crlf_separates_arguments(tmp_path):
    body = (
        "The score was 0.5 higher, 2.5 ms, 3.5 GB, and 1.5 as before.\r\n"
        "\\setcounter{score}\r\n{2.5}\r\n"
        "\\begin{tabular}\r\n{c}\r\n0.75\r\n\\end{tabular}\r\n"
    )
    (tmp_path / "main.tex").write_bytes(("\\begin{document}\r\n" + body + "\\end{document}\r\n").encode())
    manager = ManuscriptManager(tmp_path)
    navigation = json.loads(manager.create_navigation(tmp_path, manager.scan_sources(tmp_path, "main.tex")))
    assert [(entry["command"], entry["value"]) for entry in navigation["entries"]] == [
        ("quantity", "0.5"),
        ("quantity", "2.5 ms"),
        ("quantity", "3.5 GB"),
        ("quantity", "1.5"),
        ("tabular", "0.75"),
        ("quantity", "0.75"),
    ]


def test_navigation_does_not_treat_a_verbatim_tex_target_as_typeset_text(tmp_path):
    (tmp_path / "main.tex").write_text("\\begin{document}\\verbatiminput{example.tex}\\end{document}\n")
    (tmp_path / "example.tex").write_text("\\section{Shown}\\SI{3}{ms}\n")
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, "main.tex")
    entries = json.loads(manager.create_navigation(tmp_path, sources, ("main.tex",)))["entries"]
    assert not [entry for entry in entries if entry["source_path"] == "example.tex" and entry["command"] != "section"]

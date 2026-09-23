"""Compare the static dependency closure with real TeX inputs for supported syntax."""

from pathlib import Path
import shutil
import subprocess

import fitz
import pytest

from scriptorium.manuscript import ManuscriptManager


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex is required; exercised in the LaTeX CI job")
@pytest.mark.parametrize(
    ("body", "files", "expected"),
    [
        (
            r"\input{sections/body}",
            {
                "sections/body.tex": r"\input{methods}",
                "methods.tex": "Root methods.",
                "sections/methods.tex": "Wrong methods.",
            },
            {"main.tex", "sections/body.tex", "methods.tex"},
        ),
        ("\\input supplement.tex\n", {"supplement.tex": "Supplement."}, {"main.tex", "supplement.tex"}),
        (r"\includegraphics*[width=1cm]{figure.pdf}", {"figure.pdf": ""}, {"main.tex", "figure.pdf"}),
        (
            r"\input{preamble}\input{sections/body}\graphicspath{{second/}}\includegraphics{figure}",
            {
                "preamble.tex": r"\graphicspath{{missing/}{figures/}}",
                "sections/body.tex": r"\includegraphics{figure}",
                "figures/figure.pdf": "",
                "second/figure.pdf": "",
                "sections/figure.pdf": "",
            },
            {"main.tex", "preamble.tex", "sections/body.tex", "figures/figure.pdf", "second/figure.pdf"},
        ),
        (
            r"\graphicspath{{figures/}}\includegraphics{figure}",
            {"figure.pdf": "", "figures/figure.pdf": ""},
            {"main.tex", "figure.pdf"},
        ),
        (
            r"\graphicspath{{figures/}}\includegraphics{figure}",
            {"figure.png": "", "figures/figure.pdf": ""},
            {"main.tex", "figures/figure.pdf"},
        ),
        (r"row\\input data", {}, {"main.tex"}),
        ("row\\\\\\input kept\n", {"kept.tex": "Included."}, {"main.tex", "kept.tex"}),
        (
            "\\begin{verbatim}\n\\input missing\n% \\end{verbatim}\n\\input kept\n",
            {"kept.tex": "Included.", "missing.tex": "Unrelated namesake."},
            {"main.tex", "kept.tex"},
        ),
        (
            "\\begin{verbatim*}\n\\input missing\n\\end{verbatim*}\n\\input kept\n",
            {"kept.tex": "Included."},
            {"main.tex", "kept.tex"},
        ),
        (
            r"\verb|\input missing| \verb*+\input missing+ \verb%\input missing% \verb|% \input missing|"
            "\n\\input kept\n",
            {"kept.tex": "Included."},
            {"main.tex", "kept.tex"},
        ),
    ],
    ids=[
        "root-precedence",
        "unbraced-input",
        "starred-graphics",
        "graphicspath-order",
        "graphics-root",
        "graphics-extension",
        "escaped-input",
        "input-after-linebreak",
        "verbatim",
        "verbatim-star",
        "inline-verb",
    ],
)
def test_scanned_sources_match_compiler_inputs(
    tmp_path: Path, body: str, files: dict[str, str], expected: set[str]
) -> None:
    files = {
        **files,
        "main.tex": "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
        + body
        + "\n\\end{document}\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in {".pdf", ".png"}:
            with fitz.open() as document:
                page = document.new_page(width=30, height=30)
                page.insert_text((5, 15), "X", fontsize=10)
                if path.suffix == ".pdf":
                    document.save(path)
                else:
                    page.get_pixmap().save(path)
        else:
            path.write_text(content, encoding="utf-8")
    sources = ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-recorder", "main.tex"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = {
        (tmp_path / line.removeprefix("INPUT ")).resolve()
        for line in (tmp_path / "main.fls").read_text().splitlines()
        if line.startswith("INPUT ")
    }
    actual = {name for name in files if (tmp_path / name).resolve() in recorded}
    assert actual == expected
    assert {source.path for source in sources} == actual

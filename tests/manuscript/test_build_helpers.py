from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scriptorium.config import ManuscriptConfig
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import ManuscriptManager


@pytest.mark.parametrize("name", ["main.aux", "main.log", "main.bbl", "subdir", "dangling.log"])
def test_escaping_links_are_rejected_before_any_tool_runs(tmp_path, monkeypatch, name):
    workspace = tmp_path / "build"
    workspace.mkdir()
    (workspace / "main.tex").write_text("Frozen main.")
    target = tmp_path / "outside"
    if name == "subdir":
        target.mkdir()
        (target / "private.tex").write_text("Uncommitted text.")
    elif name != "dangling.log":
        target.write_text("Must not be overwritten.")
    (workspace / name).symlink_to(target, target_is_directory=name == "subdir")
    monkeypatch.setattr(
        "scriptorium.manuscript.subprocess.run", lambda *a, **kw: pytest.fail("tool ran before link check")
    )
    with pytest.raises(InfrastructureError, match="leaves the snapshot"):
        ManuscriptManager(tmp_path).build(workspace, ManuscriptConfig("main.tex", "pdflatex"))
    if target.is_file():
        assert target.read_text() == "Must not be overwritten."
    elif target.is_dir():
        assert (target / "private.tex").read_text() == "Uncommitted text."
    else:
        assert not target.exists()


@pytest.mark.parametrize("helper", ["bibtex", "biber", "eps", "eps-long"])
@pytest.mark.parametrize("main", ["main.tex", "paper dir/main.tex"])
def test_real_helper_inputs_and_derivations(tmp_path, helper, main):
    required = ["latexmk", "pdflatex", "kpsewhich", "biber" if helper == "biber" else "bibtex"]
    if helper.startswith("eps"):
        required += ["repstopdf", "gs"]
    if any(shutil.which(tool) is None for tool in required):
        pytest.skip("native helper tools required; exercised in LaTeX CI")
    (tmp_path / main).parent.mkdir(parents=True, exist_ok=True)
    figure = "figure-" + "long" * 25 if helper == "eps-long" else "figure"
    preamble = r"\documentclass{article}"
    if helper.startswith("eps"):
        preamble += r"\usepackage{graphicx}"
        body = rf"\includegraphics{{{figure}.eps}}"
        (tmp_path / f"{figure}.eps").write_text(
            "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 20 20\n"
            "newpath 0 0 moveto 20 20 lineto stroke\nshowpage\n%%EOF\n"
        )
    else:
        (tmp_path / "refs.bib").write_text(
            "@article{example, author={A}, title={Title}, journal={Journal}, year={2026}}"
        )
        if helper == "bibtex":
            (tmp_path / Path(main).with_suffix(".bbl")).write_text("Tracked old bibliography.\n")
            style = subprocess.check_output(["kpsewhich", "plain.bst"], text=True).strip()
            shutil.copyfile(style, tmp_path / "local.bst")
            body = r"\cite{example}\bibliographystyle{local}\bibliography{refs}"
        else:
            preamble += r"\usepackage[backend=biber,bibstyle=local]{biblatex}\addbibresource{refs.bib}"
            (tmp_path / "local.bbx").write_text(r"\ProvidesFile{local.bbx}\RequireBibliographyStyle{numeric}")
            body = r"\cite{example}\printbibliography"
    (tmp_path / main).write_text(preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n")
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, main)
    build = manager.build(tmp_path, ManuscriptConfig(main, "pdflatex"))
    manager.validate_build_sources(build, sources)
    evidence = json.loads(build.log.split("\nCompiler input evidence: ")[1])
    derived = evidence["derived_outputs"]
    if helper.startswith("eps"):
        assert derived == [
            {
                "path": f"{figure}-eps-converted-to.pdf",
                "tool": "epstopdf",
                "inputs": [f"{figure}.eps"],
                "digest": sha256((tmp_path / f"{figure}-eps-converted-to.pdf").read_bytes()).hexdigest(),
            }
        ]
        required_source = f"{figure}.eps"
    else:
        style_name = "local.bst" if helper == "bibtex" else "local.bbx"
        record = next(item for item in build.compiler_inputs if item.path == style_name)
        assert record.kind == "build"
        assert record.digest == sha256((tmp_path / style_name).read_bytes()).hexdigest()
        bbl = next(item for item in derived if item["path"] == Path(main).with_suffix(".bbl").as_posix())
        assert bbl["tool"] == f"{helper} {Path(main).with_suffix('').as_posix()}"
        assert "refs.bib" in bbl["inputs"]
        assert bbl["digest"] == sha256((tmp_path / Path(main).with_suffix(".bbl")).read_bytes()).hexdigest()
        required_source = "refs.bib"
    with pytest.raises(InfrastructureError, match=required_source):
        manager.validate_build_sources(build, tuple(source for source in sources if source.path != required_source))


def test_unknown_pdf_is_not_accepted_as_converted_graphic(tmp_path):
    (tmp_path / "figure.eps").write_text("EPS")
    (tmp_path / "figure-eps-converted-to.pdf").write_bytes(b"PDF")
    (tmp_path / "main.fdb_latexmk").write_text('# Fdb version 4\n["pdflatex"] 1 "main.tex" "main.pdf" "main" 1 0\n')
    (tmp_path / "main.log").write_text("No conversion evidence.")
    inputs = {Path("figure-eps-converted-to.pdf")}
    originals = {"figure.eps": sha256(b"EPS").hexdigest()}
    _, derivations = ManuscriptManager._helper_evidence(tmp_path, Path("main.tex"), (), inputs, originals)
    assert derivations == ()
    with pytest.raises(InfrastructureError, match="no snapshot source"):
        ManuscriptManager._compiler_inputs(tmp_path, originals, inputs, set())


@pytest.mark.parametrize("problem", ["old-output", "missing-source", "wrong-command", "outside", "missing-command"])
def test_eps_derivation_requires_fresh_output_and_consistent_provenance(tmp_path, problem):
    (tmp_path / "figure.eps").write_text("EPS")
    (tmp_path / "converted.pdf").write_bytes(b"PDF")
    (tmp_path / "main.fdb_latexmk").write_text('# Fdb version 4\n["pdflatex"] 1 "main.tex" "main.pdf" "main" 1 0\n')
    source = "../outside.eps" if problem == "outside" else "figure.eps"
    command_source = "wrong.eps" if problem == "wrong-command" else source
    log = f"Package epstopdf Info: Source file: <{source}>\n(epstopdf) Output file: <converted.pdf>\n"
    if problem != "missing-command":
        log += f"(epstopdf) Command: <repstopdf --outfile=converted.pdf {command_source}>\n"
    (tmp_path / "main.log").write_text(log)
    originals = {"figure.eps": sha256(b"EPS").hexdigest()}
    if problem == "old-output":
        originals["converted.pdf"] = "stale"
    elif problem == "missing-source":
        originals = {}
    with pytest.raises(InfrastructureError):
        ManuscriptManager._helper_evidence(tmp_path, Path("main.tex"), (), {Path("converted.pdf")}, originals)


@pytest.mark.parametrize(
    "contents", ["", "# Fdb version 3\n", "# Fdb version 4\n", '# Fdb version 4\n["biber main"] broken\n']
)
def test_malformed_helper_database_fails_closed(tmp_path, contents):
    (tmp_path / "main.fdb_latexmk").write_text(contents)
    with pytest.raises(InfrastructureError):
        list(ManuscriptManager._latexmk_rules(tmp_path, Path("main.tex")))


def test_successful_latexmk_eps_rule_records_derivation(tmp_path):
    (tmp_path / "figure.eps").write_bytes(b"EPS")
    (tmp_path / "figure.pdf").write_bytes(b"PDF")
    (tmp_path / "main.log").write_text("Native conversion rule; no epstopdf package.")
    (tmp_path / "main.fdb_latexmk").write_text(
        '# Fdb version 4\n["cusdep eps pdf figure"] 1 "figure.eps" "figure.pdf" "figure" 1 0\n'
        '  "figure.eps" 1 3 hash ""\n  (generated)\n  "figure.pdf"\n'
        '["pdflatex"] 1 "main.tex" "main.pdf" "main" 1 0\n'
    )
    originals = {"figure.eps": sha256(b"EPS").hexdigest()}
    sources, derived = ManuscriptManager._helper_evidence(
        tmp_path, Path("main.tex"), (), {Path("figure.pdf")}, originals
    )
    assert sources == {Path("figure.eps")}
    assert derived[0].inputs == ("figure.eps",)
    assert derived[0].digest == sha256(b"PDF").hexdigest()


@pytest.mark.parametrize("status,generated", [("1", True), ("0", False)])
def test_failed_or_incomplete_helper_rule_is_not_evidence(tmp_path, status, generated):
    text = (
        f'# Fdb version 4\n["bibtex main"] 1 "main.aux" "main.bbl" "main" 1 {status}\n'
        '  "local.bst" 1 3 hash ""\n  (generated)\n'
    )
    if generated:
        text += '  "main.bbl"\n'
    text += '["pdflatex"] 1 "main.tex" "main.pdf" "main" 1 0\n'
    (tmp_path / "main.fdb_latexmk").write_text(text)
    with pytest.raises(InfrastructureError):
        list(ManuscriptManager._latexmk_rules(tmp_path, Path("main.tex")))

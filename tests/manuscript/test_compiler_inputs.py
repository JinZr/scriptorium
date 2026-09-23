from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess

import pytest

from scriptorium.config import ManuscriptConfig
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import BuildResult, CompilerInput, ManuscriptManager, SourceFile


def test_compiler_coverage_compares_exact_frozen_paths_and_digests(tmp_path):
    source = SourceFile("main.tex", "digest", 1)
    result = BuildResult(tmp_path / "main.pdf", "build", (CompilerInput("main.tex", "digest", "review"),))
    ManuscriptManager.validate_build_sources(result, (source,))
    for sources in ((), (replace(source, digest="stale"),), (replace(source, path="other.tex"),)):
        with pytest.raises(InfrastructureError, match="frozen review sources"):
            ManuscriptManager.validate_build_sources(result, sources)
    ManuscriptManager.validate_build_sources(BuildResult(tmp_path / "synthetic.pdf", "synthetic"), ())


@pytest.mark.parametrize(
    "record", ["", "INPUT main.tex\n", "PWD relative\n", "garbage\n", "PWD /elsewhere\n", "PWD {root}\nINPUT \n"]
)
def test_invalid_recorder_is_rejected(tmp_path, record):
    (tmp_path / "main.fls").write_text(record.format(root=tmp_path))
    if not record:
        # An empty recorder yields no proof; the build requires main input/PDF output.
        assert ManuscriptManager._read_recorder(tmp_path, Path("main.tex")) == (set(), set())
    else:
        with pytest.raises(InfrastructureError, match="recorder"):
            ManuscriptManager._read_recorder(tmp_path, Path("main.tex"))


@pytest.mark.parametrize("path", ["../outside.tex", ".codex/config.toml", "AGENTS.md"])
def test_recorder_never_overrides_bundle_path_restrictions(tmp_path, path):
    (tmp_path / "main.fls").write_text(f"PWD {tmp_path}\nINPUT {path}\n")
    with pytest.raises(InfrastructureError, match="snapshot|not allowed"):
        ManuscriptManager._read_recorder(tmp_path, Path("main.tex"))


def test_recorder_normalizes_paths_and_ignores_external_tex_inputs(tmp_path):
    (tmp_path / "main.fls").write_text(
        f"PWD {tmp_path}\nINPUT ./main.tex\nINPUT {tmp_path}/main.tex\n"
        "INPUT /usr/share/texmf/article.cls\nOUTPUT main.pdf\n"
    )
    assert ManuscriptManager._read_recorder(tmp_path, Path("main.tex"), (Path("/usr/share/texmf"),)) == (
        {Path("main.tex")},
        {Path("main.pdf")},
    )


def test_native_build_cannot_use_copied_recorder_or_pdf(tmp_path, monkeypatch):
    for suffix in (".tex", ".fls", ".fdb_latexmk", ".pdf"):
        (tmp_path / ("main" + suffix)).write_text("stale")

    def no_op(command, **kwargs):
        assert "-g" in command and "-recorder" in command
        assert not (tmp_path / "main.fls").exists()
        assert not (tmp_path / "main.fdb_latexmk").exists()
        assert not (tmp_path / "main.pdf").exists()
        (tmp_path / "main.pdf").write_bytes(b"fake fresh PDF")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scriptorium.manuscript.subprocess.run", no_op)
    monkeypatch.setattr(ManuscriptManager, "_texmf_roots", staticmethod(lambda: ()))
    with pytest.raises(InfrastructureError, match="fresh LaTeX recorder"):
        ManuscriptManager(tmp_path).build(tmp_path, ManuscriptConfig(main="main.tex", engine="pdflatex"))


@pytest.mark.parametrize(
    "name,kind", [("local.cls", "build"), ("font.otf", "build"), ("data.csv", "review"), ("main.bbl", "review")]
)
def test_original_input_classification_and_identity(tmp_path, name, kind):
    path = tmp_path / name
    path.write_bytes(b"content")
    digest = sha256(b"content").hexdigest()
    evidence = ManuscriptManager._compiler_inputs(tmp_path, {name: digest}, {Path(name)}, set())
    assert evidence == (CompilerInput(name, digest, kind),)
    path.write_bytes(b"modified")
    with pytest.raises(InfrastructureError, match="modified a snapshot input"):
        ManuscriptManager._compiler_inputs(tmp_path, {name: digest}, {Path(name)}, set())


@pytest.mark.parametrize(
    "name,original,error", [("data.unknown", True, "Unclassified"), ("generated.tex", False, "no snapshot source")]
)
def test_unknown_and_generated_content_inputs_block(tmp_path, name, original, error):
    (tmp_path / name).write_bytes(b"content")
    originals = {name: sha256(b"content").hexdigest()} if original else {}
    with pytest.raises(InfrastructureError, match=error):
        ManuscriptManager._compiler_inputs(tmp_path, originals, {Path(name)}, set())


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="latexmk required; exercised in LaTeX CI")
@pytest.mark.parametrize(
    "case", ["omitted", "template", "generated", "bbl", "bibtex", "nested", "spaces", "symlink", "stale"]
)
def test_real_build_recorder_coverage(tmp_path, case):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    main = "paper/main.tex" if case == "nested" else "main.tex"
    if case == "spaces":
        main = "paper space/main.tex"
    path = snapshot / main
    path.parent.mkdir(exist_ok=True)
    preamble = r"\documentclass{article}"
    body = "Text."
    if case == "omitted":
        body = r"\InputIfFileExists{hidden.tex}{}{}"
        (snapshot / "hidden.tex").write_text("Hidden scientific content.")
    elif case == "template":
        preamble += r"\usepackage{local}"
        (snapshot / "local.sty").write_text(r"\ProvidesPackage{local}")
    elif case == "generated":
        body += r"\tableofcontents\section{Results}"
    elif case == "bibtex":
        body += r"\cite{example}\bibliographystyle{plain}\bibliography{refs}"
        (snapshot / "refs.bib").write_text(
            "@article{example, author={A}, title={Title}, journal={Journal}, year={2026}}"
        )
    elif case == "bbl":
        body += r"\bibliography{missing}"
        (snapshot / "main.bbl").write_text("Tracked bibliography.")
    path.write_text(preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n")
    if case == "symlink":
        path.rename(snapshot / "document.tex")
        path.symlink_to("document.tex")
    if case == "stale":
        for suffix in (".fls", ".fdb_latexmk", ".pdf", ".aux", ".toc"):
            (snapshot / Path(main).with_suffix(suffix)).write_text("committed stale output")
    before = {p.relative_to(snapshot): p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    workspace = tmp_path / "build"
    shutil.copytree(snapshot, workspace, symlinks=True)
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(snapshot, main)
    build = manager.build(workspace, ManuscriptConfig(main=main, engine="pdflatex"))
    assert build.compiler_inputs is not None
    assert "Compiler input evidence" in build.log
    if case == "omitted":
        with pytest.raises(InfrastructureError, match="hidden.tex"):
            manager.validate_build_sources(build, sources)
    else:
        manager.validate_build_sources(build, sources)
    if case == "template":
        assert next(item for item in build.compiler_inputs if item.path == "local.sty").kind == "build"
    assert all(not item.path.endswith((".aux", ".toc")) for item in build.compiler_inputs)
    assert before == {p.relative_to(snapshot): p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}


def test_recorder_rejects_absolute_external_content_and_symlinks(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside.tex"
    outside.write_text("Not frozen.")
    (root / "linked.tex").symlink_to(outside)
    for name in (str(outside), "linked.tex"):
        (root / "main.fls").write_text(f"PWD {root}\nINPUT {name}\n")
        with pytest.raises(InfrastructureError, match="outside the snapshot|leaves the snapshot"):
            ManuscriptManager._read_recorder(root, Path("main.tex"))


def test_generated_auxiliary_must_have_output_evidence(tmp_path):
    with pytest.raises(InfrastructureError, match="no snapshot source"):
        ManuscriptManager._compiler_inputs(tmp_path, {}, {Path("hidden.aux")}, set())
    assert ManuscriptManager._compiler_inputs(tmp_path, {}, {Path("main.aux")}, {Path("main.aux")}) == ()
    assert ManuscriptManager._compiler_inputs(tmp_path, {}, {Path("main.bbl")}, set()) == ()


@pytest.mark.parametrize(
    "recorder",
    ["", "INPUT main.tex\n", "PWD {root}\nINPUT main.tex\n", "PWD {root}\nINPUT main.tex\nOUTPUT main.pdf\nBAD line\n"],
)
def test_native_build_rejects_incomplete_or_malformed_fresh_recorder(tmp_path, monkeypatch, recorder):
    (tmp_path / "main.tex").write_text("Text.")

    def fake_build(command, **kwargs):
        (tmp_path / "main.pdf").write_bytes(b"PDF")
        (tmp_path / "main.fls").write_text(recorder.format(root=tmp_path))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scriptorium.manuscript.subprocess.run", fake_build)
    monkeypatch.setattr(ManuscriptManager, "_texmf_roots", staticmethod(lambda: ()))
    with pytest.raises(InfrastructureError, match="recorder"):
        ManuscriptManager(tmp_path).build(tmp_path, ManuscriptConfig(main="main.tex", engine="pdflatex"))


@pytest.mark.parametrize("engine", ["pdflatex", "xelatex", "lualatex"])
def test_native_recorder_supports_each_configured_engine(tmp_path, monkeypatch, engine):
    if shutil.which("latexmk") is None or shutil.which(engine) is None:
        pytest.skip(f"{engine} and latexmk required; exercised in LaTeX CI")
    cache = tmp_path.parent / "texmf-cache"
    cache.mkdir(exist_ok=True)
    monkeypatch.setenv("TEXMFVAR", str(cache))
    monkeypatch.setenv("TEXMFCACHE", str(cache))
    (tmp_path / "main.tex").write_text(r"\documentclass{article}\begin{document}Text.\end{document}")
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_sources(tmp_path, "main.tex")
    build = manager.build(tmp_path, ManuscriptConfig(main="main.tex", engine=engine))
    manager.validate_build_sources(build, sources)

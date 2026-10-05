import asyncio
import json
import shlex
import subprocess

import pytest

from scriptorium import cli
from scriptorium.errors import ConfigurationError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import PdfBuildingManuscriptManager, claim, make_repository

MAIN = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\section{Introduction}\\label{sec:intro}\n"
    "We extend prior work \\cite{smith}. See Section~\\ref{sec:results}.\n"
    "\\input{results}\n"
    "\\end{document}\n"
)
RESULTS = (
    "\\section{Results}\\label{sec:results}\n"
    "\\subsection{Accuracy}\n"
    "Accuracy improves \\citep{jones}, as Figure~\\ref{fig:curve} shows.\n"
    "\\caption{" + "Long caption " * 60 + "}\\label{fig:curve}\n"
)


@pytest.fixture
def navigator(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(MAIN, encoding="utf-8")
    (repo / "results.tex").write_text(RESULTS, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Navigation"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt_id = claim(service, run.id, "copyedit")["attempt"].id
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def follow(arguments):
            responses = []
            while arguments:
                assert cli.main(arguments) == 0
                output = capsys.readouterr().out
                assert len(output.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
                responses.append(json.loads(output)["data"])
                command = responses[-1]["next_command"]
                arguments = shlex.split(command)[1:] if command else None
            return responses

        yield service, run, attempt_id, follow


def test_heading_outline_has_exact_source_lines(navigator):
    _, _, attempt_id, follow = navigator
    (response,) = follow(["--json", "task", "nav", attempt_id, "--command", "heading"])
    outline = [(e["command"], e["value"], e["source_path"], e["start_line"]) for e in response["entries"]]
    assert outline == [
        ("section", "Introduction", "main.tex", 3),
        ("section", "Results", "results.tex", 1),
        ("subsection", "Accuracy", "results.tex", 2),
    ]
    assert response["total_entries"] == 3
    assert response["command_counts"] == {"section": 2, "subsection": 1}


def test_filters_combine_and_continuations_keep_them(navigator):
    service, run, attempt_id, follow = navigator
    pages = follow(
        ["--json", "task", "nav", attempt_id, "--command=label", "--command", "reference", "--query", "RESULTS"]
        + ["--limit", "1"]
    )
    entries = [entry for page in pages for entry in page["entries"]]
    assert [(e["command"], e["source_path"]) for e in entries] == [("ref", "main.tex"), ("label", "results.tex")]
    assert all("--command=label --command=reference --query=RESULTS" in p["next_command"] for p in pages[:-1])
    (by_path,) = follow(["--json", "task", "nav", attempt_id, "--path", "sources/results.tex", "--command", "citation"])
    assert [e["value"] for e in by_path["entries"]] == ["jones"]
    events = [e.payload for e in service.database.list_events(run.id) if e.event_type == "tool.nav"]
    assert events[0]["entries"] == [{"command": "ref", "source_path": "main.tex", "start_line": 4, "end_line": 4}]
    assert events[-1]["path"] == "sources/results.tex"
    report = service.render_report(run.id, "json")
    assert report["review_tool_access"][0]["returns"]["nav"] == len(events)


def test_long_values_are_truncated_and_bad_filters_rejected(navigator):
    service, _, attempt_id, _ = navigator
    (caption,) = service.nav_task(attempt_id, ["caption"])["entries"]
    assert caption["value_truncated"] is True and len(caption["value"]) == 500
    with pytest.raises(ConfigurationError, match="unknown navigation command 'figure'"):
        service.nav_task(attempt_id, ["figure"])
    with pytest.raises(ConfigurationError, match="closest text read paths"):
        service.nav_task(attempt_id, path="result.tex")


def _nav_run(tmp_path, files, main_body):
    repo = make_repository(tmp_path, roles=("copyedit",))
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (repo / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n" + main_body + "\\end{document}\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Navigation sources"], check=True)
    service = ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    return service, claim(service, run.id, "copyedit")["attempt"].id


def test_read_paths_take_priority_over_colliding_source_paths(tmp_path):
    files = {"z.tex": "\\section{Root file}\n", "sources/z.tex": "\\section{Nested file}\n"}
    service, attempt_id = _nav_run(tmp_path, files, "\\input{z}\n\\input{sources/z}\n")
    with service:

        def headings(path):
            return [entry["value"] for entry in service.nav_task(attempt_id, ["heading"], path=path)["entries"]]

        # "sources/z.tex" is the root file's read path and the nested file's source path, as in task read.
        assert headings("sources/z.tex") == ["Root file"]
        assert headings("sources/sources/z.tex") == ["Nested file"]
        assert headings("z.tex") == ["Root file"]


def test_ambiguous_graphics_candidates_are_capped_so_every_entry_fits(tmp_path, monkeypatch, capsys):
    panels = [f"figures/panel-{index:03d}/plot.png" for index in range(250)]
    files = {path: "png" for path in ["plot.png", *panels]}
    body = "\\includegraphics{plot}\n" + "".join(f"\\includegraphics{{{path[:-4]}}}\n" for path in panels)
    service, attempt_id = _nav_run(tmp_path, files, body)
    with service:
        monkeypatch.chdir(service.repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()
        arguments = ["--json", "task", "nav", attempt_id, "--command", "graphics"]
        entries = []
        while arguments:
            assert cli.main(arguments) == 0
            output = capsys.readouterr().out
            assert len(output.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
            response = json.loads(output)["data"]
            entries.extend(response["entries"])
            arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
        assert len(entries) == response["total_entries"] == 251
        ambiguous = next(entry for entry in entries if entry["value"] == "plot")
        assert ambiguous["candidate_count"] == 251 and ambiguous["candidate_paths_truncated"] is True
        assert len(ambiguous["candidate_paths"]) == 10 and ambiguous["target_path"] is None
        assert all("candidate_count" not in entry for entry in entries if entry is not ambiguous)


def test_long_graphics_candidates_are_cut_to_the_response_bound(tmp_path, monkeypatch, capsys):
    deep = [f"figures/{index}-{'a' * 220}/{'b' * 220}/{'c' * 220}/plot.png" for index in range(10)]
    files = {path: "png" for path in ["plot.png", *deep]}
    body = "\\includegraphics{plot}\n" + "".join(f"\\includegraphics{{{path[:-4]}}}\n" for path in deep)
    service, attempt_id = _nav_run(tmp_path, files, body)
    with service:
        monkeypatch.chdir(service.repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()
        arguments = ["--json", "task", "nav", attempt_id, "--command", "graphics"]
        entries = []
        while arguments:
            assert cli.main(arguments) == 0
            output = capsys.readouterr().out
            assert len(output.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
            response = json.loads(output)["data"]
            entries.extend(response["entries"])
            arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
        assert len(entries) == response["total_entries"] == 11
        ambiguous = entries[0]
        kept = ambiguous["candidate_paths"]
        assert ambiguous["value"] == "plot" and ambiguous["target_path"] is None
        assert ambiguous["candidate_count"] == 11 and ambiguous["candidate_paths_truncated"] is True
        assert 0 < len(kept) < 10 and kept == sorted(deep)[: len(kept)]
        assert [entry["target_path"] for entry in entries[1:]] == deep

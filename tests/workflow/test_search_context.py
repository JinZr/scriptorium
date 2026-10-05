import asyncio
import json
import shlex
import subprocess

import pytest

from scriptorium import cli
from scriptorium.errors import ConfigurationError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository

LINES = ["\\documentclass{article}", "\\begin{document}", "needle one", "plain", "needle two", "\\label{sec:needle}"]
SOURCE = "\n".join([*LINES, "\\end{document}"]) + "\n"


@pytest.fixture
def searcher(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(SOURCE, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Needles"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def follow(arguments):
            responses = []
            while arguments:
                assert cli.main(arguments) == 0
                responses.append(json.loads(capsys.readouterr().out)["data"])
                command = responses[-1]["next_command"]
                arguments = shlex.split(command)[1:] if command else None
            return responses

        yield service, run, context["attempt"].id, follow


def test_whole_bundle_search_omits_metadata_unless_requested(searcher):
    service, run, attempt_id, follow = searcher
    plain = follow(["--json", "task", "search", attempt_id, "--query", "sec:needle"])
    assert [(m["path"], m["line"]) for r in plain for m in r["matches"]] == [("main.tex", 6)]
    pages = follow(
        ["--json", "task", "search", attempt_id, "--query", "sec:needle", "--limit", "1", "--include-metadata"]
    )
    assert len(pages) > 1
    assert all("--include-metadata" in item["next_command"] for item in pages[:-1])
    assert {m["path"] for r in pages for m in r["matches"]} == {"main.tex", "navigation.json"}
    explicit = service.search_task(attempt_id, "sec:needle", "navigation.json", 0, 20)
    assert {m["path"] for m in explicit["matches"]} == {"navigation.json"}
    events = [e.payload for e in service.database.list_events(run.id) if e.event_type == "tool.search"]
    assert [e["include_metadata"] for e in events] == [False] + [True] * len(pages) + [False]


def test_context_lines_surround_each_returned_match(searcher):
    service, run, attempt_id, follow = searcher
    pages = follow(["--json", "task", "search", attempt_id, "--query", "needle", "--context", "2", "--limit", "1"])
    assert all("--context 2" in item["next_command"] for item in pages[:-1])
    matches = [m for r in pages for m in r["matches"]]
    assert [m["line"] for m in matches] == [3, 5, 6]
    first, second, last = matches
    assert first["before"] == [{"line": 1, "text": LINES[0]}, {"line": 2, "text": LINES[1]}]
    assert first["after"] == [{"line": 4, "text": "plain"}, {"line": 5, "text": "needle two"}]
    assert [item["line"] for item in second["before"]] == [3, 4]
    assert [item["line"] for item in last["after"]] == [7]
    events = [e.payload for e in service.database.list_events(run.id) if e.event_type == "tool.search"]
    assert [m["context"] for e in events for m in e["matches"]] == [
        {"start_line": 1, "end_line": 5},
        {"start_line": 3, "end_line": 7},
        {"start_line": 4, "end_line": 7},
    ]
    plain = service.search_task(attempt_id, "needle", None, 0, 20)
    assert all("before" not in m and "after" not in m for m in plain["matches"])
    with pytest.raises(ConfigurationError, match="context"):
        service.search_task(attempt_id, "needle", None, 0, 20, 4)

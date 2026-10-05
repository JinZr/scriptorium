import asyncio
import json
import shlex
import subprocess

import pytest

from scriptorium import cli, workflow
from scriptorium.errors import ConfigurationError
from scriptorium.schemas import DEFAULT_EVIDENCE_ANCHOR_CONTRACT
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit

BODY = [f"Line {number} reports a value of {number}.{number}." for number in range(1, 41)]
SOURCE = "\\documentclass{article}\n\\begin{document}\n" + "\n".join(BODY) + "\n" + "x" * 50 + "\n\\end{document}\n"


@pytest.fixture
def reader(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(SOURCE, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Numbered lines"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def command(arguments):
            code = cli.main(arguments)
            captured = capsys.readouterr()
            assert len(captured.out.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
            value = json.loads(captured.out)
            assert code == 0, value
            return value["data"]

        yield service, context, command


def _follow(command, arguments):
    responses = []
    while arguments:
        response = command(arguments)
        responses.append(response)
        arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
    return responses


def test_end_line_bounds_the_whole_traversal(reader):
    _, context, command = reader
    attempt_id = context["attempt"].id
    responses = _follow(
        command,
        ["--json", "task", "read", attempt_id, "--path", "main.tex", "--start-line", "5", "--end-line", "17"]
        + ["--max-lines", "4"],
    )
    assert len(responses) == 4
    assert all("--end-line 17" in item["next_command"] for item in responses[:-1])
    lines = [piece for item in responses for piece in item["lines"]]
    assert [piece["line"] for piece in lines] == list(range(5, 18))
    assert [piece["text"] for piece in lines] == SOURCE.splitlines()[4:17]
    with pytest.raises(ConfigurationError, match="end line"):
        reader[0].read_task(attempt_id, "main.tex", 5, 10, 0, 8000, end_line=4)


def test_anchor_covers_complete_lines_and_validates(reader):
    service, context, command = reader
    attempt_id = context["attempt"].id
    single = command(
        ["--json", "task", "read", attempt_id, "--path", "main.tex", "--start-line", "5"]
        + ["--end-line", "5", "--anchor"]
    )
    multi = command(
        ["--json", "task", "read", attempt_id, "--path", "sources/main.tex", "--start-line", "3"]
        + ["--end-line", "6", "--anchor"]
    )
    source = next(item for item in context["source_map"]["sources"] if item["source_path"] == "main.tex")
    assert single["anchor"] == {
        "source_path": "main.tex",
        "start_line": 5,
        "end_line": 5,
        "source_digest": source["source_digest"],
        "quoted_text": BODY[2],
    }
    assert multi["anchor"]["source_path"] == "main.tex"
    assert (multi["anchor"]["start_line"], multi["anchor"]["end_line"]) == (3, 6)
    assert multi["anchor"]["quoted_text"] == "\n".join(BODY[:4])
    finding = {
        "category": "clarity",
        "severity": "minor",
        "title": "Anchored values",
        "claim": "The values are unexplained.",
        "evidence": [single["anchor"], multi["anchor"]],
        "explanation": "No unit is given.",
        "suggested_action": "State the unit.",
        "confidence": 0.5,
    }
    receipt = submit(service, context, {"summary": "Checked.", "findings": [finding]})
    assert receipt["validation_report"] is None


def test_anchor_excludes_partial_lines_and_metadata(reader):
    _, context, command = reader
    attempt_id = context["attempt"].id
    long_line = len(BODY) + 3
    responses = _follow(
        command,
        ["--json", "task", "read", attempt_id, "--path", "main.tex", "--start-line", str(long_line)]
        + ["--end-line", str(long_line), "--max-chars", "20", "--anchor"],
    )
    assert len(responses) == 3
    assert all(item["anchor"] is None for item in responses)
    assert all("--anchor" in item["next_command"] for item in responses[:-1])
    mixed = command(
        ["--json", "task", "read", attempt_id, "--path", "main.tex", "--start-line", "41"]
        + ["--max-chars", "80", "--anchor"]
    )
    assert mixed["lines"][-1]["line"] == mixed["next_line"] == long_line
    assert (mixed["anchor"]["start_line"], mixed["anchor"]["end_line"]) == (41, 42)
    metadata = command(["--json", "task", "read", attempt_id, "--path", "manifest.json", "--anchor"])
    assert metadata["source_path"] is None and metadata["anchor"] is None


@pytest.mark.parametrize("operation", ["read", "search"])
def test_unknown_path_lists_closest_read_paths(reader, operation):
    service, context, _ = reader
    with pytest.raises(ConfigurationError) as error:
        if operation == "read":
            service.read_task(context["attempt"].id, "main.txt", 1, 10, 0, 8000)
        else:
            service.search_task(context["attempt"].id, "Line", "sources/main.txt", 0, 10)
    message = str(error.value)
    assert message.startswith("path is not a text source in the frozen bundle")
    assert message.split("closest text read paths: ")[1].split(", ")[0] == "sources/main.tex"


FORM_FEED_SOURCE = "\\documentclass{article}\n\\begin{document}\nalpha\fbeta\ngamma delta\n\\end{document}\n"
PLAIN_SOURCE = "\\documentclass{article}\n\\begin{document}\nalpha beta\ngamma delta\n\\end{document}\n"


@pytest.mark.parametrize(
    ("legacy", "text", "anchored"),
    [(False, FORM_FEED_SOURCE, True), (True, PLAIN_SOURCE, True), (True, FORM_FEED_SOURCE, False)],
    ids=["current-form-feed", "earlier-plain", "earlier-form-feed"],
)
def test_anchor_follows_the_runs_frozen_line_rule(tmp_path, monkeypatch, legacy, text, anchored):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Line rule"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        if legacy:
            # Freeze the run as an earlier version did: its contract names no line terminators.
            monkeypatch.setattr(
                workflow,
                "DEFAULT_EVIDENCE_ANCHOR_CONTRACT",
                DEFAULT_EVIDENCE_ANCHOR_CONTRACT.model_copy(update={"line_terminators": None}),
            )
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        monkeypatch.undo()
        context = claim(service, run.id, "copyedit")
        response = service.read_task(context["attempt"].id, "main.tex", 3, 2, 0, 8000, anchor=True)
        if not anchored:
            # The earlier rule splits "alpha\fbeta" in two, so retrieval's line 4 is not the frozen line 4.
            assert response["anchor"] is None
            return
        anchor = response["anchor"]
        assert (anchor["start_line"], anchor["end_line"]) == (3, 4)
        finding = {
            "category": "clarity",
            "severity": "minor",
            "title": "Anchored lines",
            "claim": "The lines are unclear.",
            "evidence": [anchor],
            "explanation": "They are hard to read.",
            "suggested_action": "Clarify them.",
            "confidence": 0.5,
        }
        receipt = submit(service, context, {"summary": "Checked.", "findings": [finding]})
        assert receipt["validation_report"] is None

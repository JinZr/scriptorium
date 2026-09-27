import asyncio
from hashlib import sha256
import json
import shlex
import subprocess

import pytest

from scriptorium import cli
from scriptorium.errors import ConfigurationError, StateError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES, bound_read, bound_search

from ._support import PdfBuildingManuscriptManager, make_repository


@pytest.fixture
def review_cli(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    text = (
        "\\documentclass{article}\n\\begin{document}\n"
        + "% "
        + "实验'needle \"\\value\t🧬 " * 300
        + "counterevidence at the tail\n"
        + "\n" * 120
        + ('% "\\value 🧬\t实验\n' * 90)
        + "\\end{document}\n"
    )
    (repo / "main.tex").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "main.tex"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Long Unicode source"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        task = service.database.list_tasks(run.id)[0]
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def command(arguments):
            assert cli.main(arguments) == 0
            captured = capsys.readouterr()
            assert not captured.err
            assert len(captured.out.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
            return json.loads(captured.out)["data"]

        yield service, task, command, text


@pytest.mark.parametrize("client", ["codex", "claude_code", "antigravity"])
def test_cli_input_fragments_reconstruct_frozen_artifacts(review_cli, client):
    service, task, command, _ = review_cli
    arguments = [
        "--json",
        "task",
        "claim",
        task.id,
        "--client",
        client,
        "--model",
        "selected",
        "--effort",
        "high",
        "--session-id",
        "session",
        "--session-source",
        "host",
    ]
    overview = command(arguments)
    assert command(arguments)["attempt"]["id"] == overview["attempt"]["id"]
    assert "prompt" not in overview and "schema" not in overview and "source_map" not in overview
    assert overview["attempt"]["external_client"] == client
    for part in ("prompt", "schema"):
        next_command = overview["inputs"][part]["command"]
        text = ""
        chunks = []
        while next_command:
            response = command(shlex.split(next_command)[1:])
            assert response["offset"] == len(text)
            assert response["input_digest"] == overview["input_digest"]
            assert response["part"] == part
            text += response["text"]
            chunks.append(response)
            next_command = response["next_command"]
        assert len(text) == response["total_chars"]
        assert response["next_offset"] is None
        assert text.encode() == service.artifacts.get_bytes(overview["inputs"][part]["digest"])
        assert sha256(text.encode()).hexdigest() == response["digest"]
        if part == "schema":
            assert "claim_checks" in json.loads(text)["required"]
        events = [
            e.payload
            for e in service.database.list_events(task.run_id)
            if e.event_type == "tool.show" and e.payload["part"] == part
        ]
        assert len(events) == len(chunks)
        for event, chunk in zip(events, chunks):
            assert event["start_offset"] == chunk["offset"]
            assert event["end_offset"] == chunk["offset"] + len(chunk["text"])


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_terminal_attempt_keeps_bounded_source_map_inspection(review_cli, status):
    service, task, command, _ = review_cli
    context = service.claim_task(task.id, "codex", "model", "high", "session", "host")
    attempt_id = context["attempt"].id
    if status == "interrupted":
        service.cancel_run(task.run_id, "Stop review")
    else:
        output = {
            "summary": "Partial check.",
            "findings": [],
            "claim_checks": [],
            "scope": {"completion": "partial", "checked": [], "outstanding": [], "limitations": []},
        }
        asyncio.run(
            service.submit_task(
                attempt_id, context["input_digest"], json.dumps(output) if status == "completed" else "{}"
            )
        )
    before = (
        service.database.get_attempt(attempt_id),
        service.database.get_task(task.id),
        service.database.get_run(task.run_id),
    )
    assert before[0].status.value == status
    overview = command(["--json", "task", "show", attempt_id])
    next_command = overview["source_map_command"]
    text = ""
    while next_command:
        response = command(shlex.split(next_command)[1:])
        assert response["offset"] == len(text)
        assert response["digest"] == overview["inputs"]["source-map"]["digest"]
        text += response["text"]
        next_command = response["next_command"]
    assert text == context["source_map_text"]
    assert sha256(text.encode()).hexdigest() == context["source_map_digest"]
    assert json.loads(text) == context["source_map"]
    assert before == (
        service.database.get_attempt(attempt_id),
        service.database.get_task(task.id),
        service.database.get_run(task.run_id),
    )
    with pytest.raises(StateError, match="active attempt"):
        service.read_task(attempt_id, "source-map.json", 1, 40, 0, 6000)


def test_cli_read_continuations_preserve_every_character_and_logged_range(review_cli):
    service, task, command, text = review_cli
    context = service.claim_task(task.id, "antigravity", "model", "high", "session", "host")
    next_command = (
        f"scriptorium --json task read {context['attempt'].id} --path main.tex --max-lines 100 --max-chars 8000"
    )
    returned = {}
    chunks = []
    while next_command:
        response = command(shlex.split(next_command)[1:])
        for piece in response["lines"]:
            assert piece["offset"] == len(returned.setdefault(piece["line"], ""))
            returned[piece["line"]] += piece["text"]
        chunks.append(response)
        next_command = response["next_command"]
    assert list(returned.values()) == text.splitlines()
    assert any(chunk["next_offset"] for chunk in chunks)
    accesses = [e.payload for e in service.database.list_events(task.run_id) if e.event_type == "tool.read"]
    assert len(accesses) == len(chunks)
    for access, chunk in zip(accesses, chunks):
        assert access["ranges"] == [
            {"line": p["line"], "start_offset": p["offset"], "end_offset": p["offset"] + len(p["text"])}
            for p in chunk["lines"]
        ]
        assert access["next_line"] == chunk["next_line"]
        assert access["next_offset"] == chunk["next_offset"]


def test_cli_search_byte_pages_preserve_all_matches_and_log_only_returns(review_cli):
    service, task, command, text = review_cli
    context = service.claim_task(task.id, "antigravity", "model", "high", "session", "host")
    query = "实验'needle"
    arguments = [
        "--json",
        "task",
        "search",
        context["attempt"].id,
        "--query",
        query,
        "--path",
        "main.tex",
        "--limit",
        "50",
    ]
    matches = []
    chunks = []
    while arguments:
        response = command(arguments)
        assert response["total_matches"] == 300
        assert response["matches"]
        matches.extend(response["matches"])
        chunks.append(response)
        if response["next_cursor"] is not None:
            assert response["next_cursor"] == len(matches)
        arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
    line = text.splitlines()[2]
    assert [m["column"] for m in matches] == [i + 1 for i in range(len(line)) if line.startswith(query, i)]
    assert len(chunks[0]["matches"]) < 50
    accesses = [e.payload for e in service.database.list_events(task.run_id) if e.event_type == "tool.search"]
    assert len(accesses) == len(chunks)
    for access, chunk in zip(accesses, chunks):
        assert access["matches"] == [{k: m[k] for k in ("path", "line", "source_digest")} for m in chunk["matches"]]
        assert access["next_cursor"] == chunk["next_cursor"]


@pytest.mark.parametrize("part,offset", [(None, 1), ("prompt", -1), ("schema", 10_000_000)])
def test_invalid_input_offsets_do_not_record_access(review_cli, part, offset):
    service, task, _, _ = review_cli
    context = service.claim_task(task.id, "codex", "model", "high", "session", "host")
    before = service.database.list_events(task.run_id)
    with pytest.raises(ConfigurationError, match="offset"):
        service.task_view(context, part, offset)
    assert service.database.list_events(task.run_id) == before


def test_oversized_metadata_fails_instead_of_returning_a_nonprogressing_cursor():
    path = "长" * 2500
    with pytest.raises(ConfigurationError, match="metadata"):
        bound_read(
            {
                "path": path,
                "source_digest": "a" * 64,
                "lines": [{"line": 1, "offset": 0, "text": "x"}],
                "next_line": None,
                "next_offset": None,
            },
            "attempt_1",
            100,
            8000,
        )
    with pytest.raises(ConfigurationError, match="metadata"):
        bound_search([{"path": path}], 2, "attempt_1", "q", path, 0, 20)

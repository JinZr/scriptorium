import asyncio
import json
import shlex
import subprocess

import pytest

from scriptorium import cli
from scriptorium.domain import AgentRole
from scriptorium.errors import ConfigurationError, InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit


@pytest.mark.parametrize("path", ["main.tex", "sources/main.tex"])
def test_search_counts_many_matches_without_retaining_unreturned_pages(tmp_path, path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    (repo / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n" + "needle " * 5000 + "\n\\end{document}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "main.tex"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Add repeated evidence"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)["attempt"]
        first = service.search_task(attempt.id, "needle", path, 0, 20)
        middle = service.search_task(attempt.id, "needle", path, 2490, 20)
        last = service.search_task(attempt.id, "needle", path, 4990, 20)
        assert first["total_matches"] == middle["total_matches"] == last["total_matches"] == 5000
        assert 0 < len(first["matches"]) <= 20
        assert 0 < len(middle["matches"]) <= 20
        assert len(last["matches"]) == 10
        assert first["next_cursor"] == len(first["matches"])
        assert last["next_cursor"] is None
        assert "\n" not in last["matches"][-1]["excerpt"]
        assert middle["matches"][0]["column"] == 17431
        assert {match["path"] for match in first["matches"]} == {"main.tex"}
        assert "--path=main.tex" in first["next_command"]
        events = [event for event in service.database.list_events(run.id) if event.event_type == "tool.search"]
        assert all(event.payload["path"] == "main.tex" for event in events)


def test_read_paths_take_priority_over_colliding_source_paths(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    (repo / "sources").mkdir()
    (repo / "sources/main.tex").write_text("Nested evidence.\n", encoding="utf-8")
    (repo / "main.tex").write_text("Root evidence.\n\\input{sources/main.tex}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "main.tex", "sources/main.tex"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Nested source"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)["attempt"]
        for path, query, expected_path in [
            ("main.tex", "Root evidence", "main.tex"),
            ("sources/main.tex", "Root evidence", "main.tex"),
            ("sources/sources/main.tex", "Nested evidence", "sources/main.tex"),
        ]:
            read = service.read_task(attempt.id, path, 1, 10, 0, 1000)
            search = service.search_task(attempt.id, query, path, 0, 20)
            assert search["total_matches"] == 1
            assert search["matches"][0]["path"] == expected_path
            assert search["matches"][0]["source_digest"] == read["source_digest"]
        for path in ["../main.tex", "sources/../main.tex", str(repo / "main.tex")]:
            with pytest.raises(ConfigurationError, match="not a text source"):
                service.search_task(attempt.id, "evidence", path, 0, 20)
        bundle = service.repo / ".scriptorium/runs" / run.id / "bundle"
        (bundle / "sources/sources/main.tex").write_text("Tampered evidence.\n")
        with pytest.raises(InfrastructureError):
            service.search_task(attempt.id, "evidence", "sources/sources/main.tex", 0, 20)


@pytest.mark.parametrize("name", ["manifest.json", "navigation.json", "source-map.json"])
def test_search_alias_keeps_sources_separate_from_same_named_metadata(tmp_path, name):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    (repo / name).write_text(f"{name} {name} {name}\n", encoding="utf-8")
    (repo / "main.tex").write_text(f"Root evidence.\n\\input{{{name}}}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "main.tex", name], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Source named like metadata"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)["attempt"]
        alias = f"sources/{name}"
        source = service.read_task(attempt.id, alias, 1, 10, 0, 1000)
        metadata = service.read_task(attempt.id, name, 1, 10, 0, 1000)
        assert source["source_digest"] != metadata["source_digest"]
        cursor = 0
        matches = []
        while True:
            page = service.search_task(attempt.id, name, alias, cursor, 1)
            assert page["total_matches"] == 3
            matches.extend(page["matches"])
            if page["next_cursor"] is None:
                break
            assert f"--path={alias}" in page["next_command"]
            cursor = page["next_cursor"]
        assert len(matches) == 3
        assert {match["path"] for match in matches} == {name}
        assert {match["source_digest"] for match in matches} == {source["source_digest"]}
        events = [event for event in service.database.list_events(run.id) if event.event_type == "tool.search"]
        assert {event.payload["path"] for event in events} == {alias}
        metadata_search = service.search_task(attempt.id, name, name, 0, 50)
        metadata_text = (repo / ".scriptorium/runs" / run.id / "bundle" / name).read_text()
        assert metadata_search["total_matches"] == metadata_text.count(name) > 0
        assert {match["source_digest"] for match in metadata_search["matches"]} == {metadata["source_digest"]}


@pytest.mark.parametrize("name", ["manifest.json", "navigation.json", "source-map.json"])
@pytest.mark.parametrize("identical_content", [False, True])
def test_every_source_in_a_metadata_alias_chain_remains_retrievable(
    tmp_path, monkeypatch, capsys, name, identical_content
):
    repo = make_repository(tmp_path, roles=("copyedit",))
    paths = ["sources/" * depth + name for depth in range(3)]
    for depth, path in enumerate(paths):
        source = repo / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"File {0 if identical_content else depth}: needle needle needle\n", encoding="utf-8")
    (repo / "main.tex").write_text("\n".join(f"\\input{{{path}}}" for path in paths), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Colliding source chain"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, AgentRole.COPYEDIT)
        attempt_id = context["attempt"].id
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def command(arguments):
            assert cli.main(arguments) == 0
            return json.loads(capsys.readouterr().out)["data"]

        metadata = command(["--json", "task", "read", attempt_id, "--path", name])
        assert metadata["source_path"] is None
        for path in paths:
            source = next(item for item in context["source_map"]["sources"] if item["source_path"] == path)
            arguments = ["--json", "task", "read", attempt_id, "--path", source["read_path"], "--max-chars", "10"]
            text = ""
            while arguments:
                response = command(arguments)
                assert response["source_path"] == path
                assert response["source_digest"] == source["source_digest"]
                text += "".join(line["text"] for line in response["lines"])
                arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
            assert text == (repo / path).read_text().rstrip("\n")
            arguments = [
                "--json",
                "task",
                "search",
                attempt_id,
                "--path",
                source["read_path"],
                "--query",
                "needle",
                "--limit",
                "1",
            ]
            matches = []
            while arguments:
                response = command(arguments)
                assert response["total_matches"] == 3
                matches.extend(response["matches"])
                arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
            assert len(matches) == 3
            assert {match["path"] for match in matches} == {path}
            assert {match["source_digest"] for match in matches} == {source["source_digest"]}
        submit(
            service,
            context,
            {
                "summary": "Checked each source.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": path} for path in paths],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        audit = service.render_report(run.id, "json")["review_coverage_audit"][0]
        assert audit["read_lines"] == [
            {"source_path": path, "ranges": [{"start_line": 1, "end_line": 1}]} for path in paths
        ]
        assert audit["declared_without_task_read"] == []

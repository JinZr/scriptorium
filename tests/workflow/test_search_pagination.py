import asyncio
import subprocess

import pytest

from scriptorium.domain import AgentRole
from scriptorium.errors import ConfigurationError, InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository


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


def test_search_source_paths_take_priority_over_read_path_aliases(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    (repo / "sources").mkdir()
    (repo / "sources/main.tex").write_text("Nested evidence.\n", encoding="utf-8")
    (repo / "main.tex").write_text("Root evidence.\n\\input{sources/main.tex}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "main.tex", "sources/main.tex"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Nested source"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)["attempt"]
        for path in ["sources/main.tex", "sources/sources/main.tex"]:
            read = service.read_task(attempt.id, path, 1, 10, 0, 1000)
            search = service.search_task(attempt.id, "Nested evidence", path, 0, 20)
            assert search["total_matches"] == 1
            assert search["matches"][0]["path"] == "sources/main.tex"
            assert search["matches"][0]["source_digest"] == read["source_digest"]
        for path in ["../main.tex", "sources/../main.tex", str(repo / "main.tex")]:
            with pytest.raises(ConfigurationError, match="not a text source"):
                service.search_task(attempt.id, "evidence", path, 0, 20)
        bundle = service.repo / ".scriptorium/runs" / run.id / "bundle"
        (bundle / "sources/sources/main.tex").write_text("Tampered evidence.\n")
        with pytest.raises(InfrastructureError):
            service.search_task(attempt.id, "evidence", "sources/sources/main.tex", 0, 20)

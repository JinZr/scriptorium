import asyncio
import subprocess

from scriptorium.domain import AgentRole
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository


def test_search_counts_many_matches_without_retaining_unreturned_pages(tmp_path):
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
        first = service.search_task(attempt.id, "needle", "main.tex", 0, 20)
        middle = service.search_task(attempt.id, "needle", "main.tex", 2490, 20)
        last = service.search_task(attempt.id, "needle", "main.tex", 4990, 20)
        assert first["total_matches"] == middle["total_matches"] == last["total_matches"] == 5000
        assert len(first["matches"]) == len(middle["matches"]) == 20
        assert len(last["matches"]) == 10
        assert first["next_cursor"] == 20
        assert last["next_cursor"] is None
        assert "\n" not in last["matches"][-1]["excerpt"]
        assert middle["matches"][0]["column"] == 17431

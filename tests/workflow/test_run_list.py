import asyncio
import json

import pytest

from scriptorium import cli
from scriptorium.errors import ConfigurationError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import PdfBuildingManuscriptManager, make_repository

_BRIEF = {"venue_family": "ml_conference", "venue": "NeurIPS 2026", "stage": "presubmission"}


def _start(service, brief=None):
    text = None if brief is None else json.dumps(brief)
    return asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True, brief=text))["run"]


def test_run_list_reports_bounded_rows_with_brief_fields_and_truncation(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)
        briefed = _start(service, _BRIEF)
        service.cancel_run(plain.id, "superseded")

        listing = service.list_runs()
        assert (listing["total"], listing["limit"], listing["returned"], listing["truncated"]) == (2, 20, 2, False)
        by_id = {row["run_id"]: row for row in listing["runs"]}
        assert set(by_id) == {plain.id, briefed.id}
        assert by_id[briefed.id] == {
            "run_id": briefed.id,
            "status": "reviewing",
            "commit_sha": briefed.commit_sha,
            "profile": "quick",
            "created_at": briefed.created_at,
            "updated_at": by_id[briefed.id]["updated_at"],
            "task_counts": {"pending": 1},
            "venue_family": "ml_conference",
            "venue": "NeurIPS 2026",
            "stage": "presubmission",
        }
        assert "venue" not in by_id[plain.id] and by_id[plain.id]["status"] == "cancelled"
        assert listing["next_actions"] == [{"command": "run status", "run_id": listing["runs"][0]["run_id"]}]

        assert [row["run_id"] for row in service.list_runs("cancelled")["runs"]] == [plain.id]
        capped = service.list_runs(limit=1)
        assert (capped["total"], capped["returned"], capped["truncated"]) == (2, 1, True)


def test_run_list_output_stays_within_the_response_bound(tmp_path, monkeypatch, capsys) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    brief = {**_BRIEF, "venue": "V" * 200}
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        for _ in range(30):
            _start(service, brief)
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        for options in (["--json"], []):
            assert cli.main([*options, "run", "list", "--limit", "100"]) == 0
            raw = capsys.readouterr().out
            assert len(raw.encode()) <= MAX_TOOL_RESPONSE_BYTES
            data = json.loads(raw)["data"] if options else json.loads(raw)
            assert data["total"] == 30 and data["truncated"] and 0 < data["returned"] < 30
            assert data["returned"] == len(data["runs"])


@pytest.mark.parametrize("arguments", [["--status", "done"], ["--limit", "0"], ["--limit", "101"]])
def test_run_list_rejects_bad_filters_with_an_error_envelope(tmp_path, monkeypatch, capsys, arguments) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        assert cli.main(["--json", "run", "list", *arguments]) == 2
        error = json.loads(capsys.readouterr().out)
        assert error["ok"] is False and error["error"]["code"] == "configuration_error"
        with pytest.raises(ConfigurationError):
            service.list_runs(*(arguments[1:] if arguments[0] == "--status" else [None, int(arguments[1])]))


@pytest.mark.parametrize("failure", ["corrupt", "unreadable"])
def test_run_list_omits_the_brief_of_one_unreadable_run_and_lists_the_rest(tmp_path, monkeypatch, failure) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        broken = _start(service, _BRIEF)
        healthy = _start(service, {**_BRIEF, "venue": "ICML 2026"})
        if failure == "corrupt":
            service.artifacts.path_for(broken.brief_digest).write_text("corrupt")
        else:
            original = service.artifacts.get_bytes

            def get_bytes(digest):
                if digest == broken.brief_digest:
                    raise PermissionError("denied")
                return original(digest)

            monkeypatch.setattr(service.artifacts, "get_bytes", get_bytes)

        rows = {row["run_id"]: row for row in service.list_runs()["runs"]}
        assert set(rows) == {broken.id, healthy.id}
        assert "venue" not in rows[broken.id] and rows[healthy.id]["venue"] == "ICML 2026"

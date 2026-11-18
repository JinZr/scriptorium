import asyncio
import json
from pathlib import Path

import fitz
import pytest

from scriptorium import cli, manuscript
from scriptorium.artifacts import ArtifactStore
from scriptorium.errors import ConfigurationError, StateError
from scriptorium.manuscript import BuildResult
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit


@pytest.fixture
def pages(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        capsys.readouterr()

        def command(arguments):
            code = cli.main(arguments)
            value = json.loads(capsys.readouterr().out)
            assert code == 0, value
            return value["data"]

        yield service, run, context, command


def test_scaled_crop_is_rendered_from_the_frozen_pdf(pages):
    service, run, context, command = pages
    attempt_id = context["attempt"].id
    frozen = command(["--json", "task", "page", attempt_id, "--number", "1"])
    view = command(["--json", "task", "page", attempt_id, "--number", "1", "--scale", "3", "--crop", "0,0,0.5,0.25"])
    assert view["digest"] == frozen["digest"] and view["page"] == 1
    assert view["view"]["scale"] == 3.0 and view["view"]["crop"] == [0.0, 0.0, 0.5, 0.25]
    assert view["path"].startswith(str(service.repo / ".scriptorium/runs" / run.id / "page-views"))
    assert ArtifactStore.digest_file(view["path"]) == view["view"]["digest"]
    with fitz.open(context["bundle_path"] + "/manuscript.pdf") as document:
        bounds = document[0].rect
    image = fitz.Pixmap(view["path"])
    assert abs(image.width - bounds.width * 0.5 * 3) <= 1 and abs(image.height - bounds.height * 0.25 * 3) <= 1
    assert (
        command(["--json", "task", "page", attempt_id, "--number", "1", "--scale", "3", "--crop", "0,0,0.5,0.25"])[
            "view"
        ]
        == view["view"]
    )
    events = [e.payload for e in service.database.list_events(run.id) if e.event_type == "tool.page"]
    assert "view" not in events[0] and events[1]["view"] == {k: v for k, v in view["view"].items() if k != "path"}


def test_oversized_views_are_rejected_before_rendering(pages, monkeypatch):
    service, run, context, _ = pages
    attempt_id = context["attempt"].id
    # An A4 page at scale 4 is about eight megapixels; lower the cap instead of building a poster.
    monkeypatch.setattr(manuscript, "MAX_PAGE_VIEW_PIXELS", 1_000_000)
    with pytest.raises(ConfigurationError, match="exceed 1 megapixels; lower --scale or crop a smaller region"):
        service.page_task(attempt_id, 1, scale=4.0)
    assert not (service.repo / ".scriptorium/runs" / run.id / "page-views").exists()
    assert service.page_task(attempt_id, 1, scale=4.0, crop=(0, 0, 0.25, 0.25))["view"]["scale"] == 4.0


class RotatedPageManager(PdfBuildingManuscriptManager):
    def build(self, workspace, manuscript):
        # A portrait page marked at its unrotated top-left and displayed in landscape through /Rotate 90.
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(0, 0, 100, 100), color=(0, 0, 0), fill=(0, 0, 0))
            page.set_rotation(90)
            document.save(pdf_path)
        return BuildResult(pdf_path=pdf_path, log="fake rotated build succeeded")


def test_crops_select_the_displayed_region_of_a_rotated_page(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=RotatedPageManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        attempt_id = claim(service, run.id, "copyedit")["attempt"].id

        def dark_pixels(crop):
            image = fitz.Pixmap(service.page_task(attempt_id, 1, scale=1.0, crop=crop)["path"])
            assert (image.width, image.height) == (160, 120)
            return sum(image.pixel(x, y)[0] < 50 for x in range(image.width) for y in range(image.height))

        # The mark shows at the top right of the displayed landscape page, so crops follow what a reader sees.
        assert dark_pixels((0.8, 0, 1, 0.2)) == 100 * 100
        assert dark_pixels((0, 0, 0.2, 0.2)) == 0


def test_text_layer_is_written_to_a_file_and_is_never_evidence(pages):
    service, run, context, command = pages
    attempt_id = context["attempt"].id
    response = command(["--json", "task", "page", attempt_id, "--number", "1", "--text"])
    path = Path(response["path"])
    assert path.parent == service.repo / ".scriptorium/runs" / run.id / "page-views"
    assert path.name == f"{response['digest']}.txt" and ArtifactStore.digest_file(path) == response["digest"]
    assert "The result is clear." in path.read_text(encoding="utf-8")
    assert response["bytes"] == path.stat().st_size
    assert response["evidence"] is False and response["text_layer"] == "pdf" and "never evidence" in response["note"]
    assert (response["source_path"], response["page"]) == ("manuscript.pdf", 1)
    assert "text" not in response and "next_command" not in response
    assert command(["--json", "task", "page", attempt_id, "--number", "1", "--text"]) == response
    events = [event for event in service.database.list_events(run.id) if event.event_type.startswith("tool.")]
    assert [event.event_type for event in events] == ["tool.page", "tool.page"]
    assert events[0].payload["text"] == {"digest": response["digest"], "path": response["path"]}


def test_a_changed_text_layer_gets_a_new_file_and_the_old_one_keeps_its_bytes(pages, monkeypatch):
    service, _, context, _ = pages
    attempt_id = context["attempt"].id
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: "页面文字 " * 3000)
    first = service.page_task(attempt_id, 1, text=True)
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: "second extraction")
    second = service.page_task(attempt_id, 1, text=True)
    assert second["path"] != first["path"]
    assert Path(first["path"]).read_text(encoding="utf-8") == "页面文字 " * 3000
    assert Path(second["path"]).read_bytes() == b"second extraction"


def test_views_keep_their_bytes_and_a_run_keeps_a_bounded_amount(pages, monkeypatch):
    service, run, context, _ = pages
    attempt_id = context["attempt"].id
    first = service.page_task(attempt_id, 1, scale=2.0)
    assert first["path"].endswith(f"/{first['view']['digest']}.png")
    # A renderer that now draws different bytes for the same request gets a new path; the old one is untouched.
    real_render = manuscript.render_page_view

    def changed_render(pdf, page, scale, crop, destination):
        real_render(pdf, page, scale + 0.25, crop, destination)

    monkeypatch.setattr("scriptorium.service.render_page_view", changed_render)
    second = service.page_task(attempt_id, 1, scale=2.0)
    assert second["path"] != first["path"]
    assert ArtifactStore.digest_file(first["path"]) == first["view"]["digest"]
    text = service.page_task(attempt_id, 1, text=True)
    views = service.repo / ".scriptorium/runs" / run.id / "page-views"
    kept = sum(path.stat().st_size for path in views.iterdir() if not path.name.startswith("."))
    # Text layers count toward the same limit as rendered views.
    monkeypatch.setattr("scriptorium.service.MAX_RUN_PAGE_VIEW_BYTES", kept)
    with pytest.raises(StateError, match="reached the 0 MiB limit"):
        service.page_task(attempt_id, 1, scale=3.0)
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: "a different extraction")
    with pytest.raises(StateError, match="reached the 0 MiB limit"):
        service.page_task(attempt_id, 1, text=True)
    assert sum(path.stat().st_size for path in views.iterdir()) == kept
    assert service.page_task(attempt_id, 1, scale=2.0)["path"] == second["path"]
    assert Path(text["path"]).exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"scale": 5.0}, "scale"),
        ({"crop": (0.5, 0.0, 0.5, 1.0)}, "crop"),
        ({"text": True, "scale": 2.0}, "cannot be combined"),
    ],
)
def test_invalid_page_options_are_rejected_before_access(pages, options, message):
    service, run, context, _ = pages
    before = service.database.list_events(run.id)
    with pytest.raises(ConfigurationError, match=message):
        service.page_task(context["attempt"].id, 1, **options)
    assert service.database.list_events(run.id) == before


def test_cli_rejects_malformed_crop(pages, capsys):
    _, _, context, _ = pages
    assert cli.main(["--json", "task", "page", context["attempt"].id, "--number", "1", "--crop", "0,0,1"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"


def test_a_finished_attempt_cannot_render_pages(pages):
    service, _, context, _ = pages
    submit(service, context, {"summary": "Done.", "findings": []})
    with pytest.raises(StateError, match="active attempt"):
        service.page_task(context["attempt"].id, 1)
